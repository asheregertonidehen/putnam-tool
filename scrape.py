#!/usr/bin/env python3
"""Single-call scraper for the Kedlaya Putnam archive.

For each year it fetches three things:
  1. the problems TeX file      https://kskedlaya.org/putnam-archive/<year>.tex
  2. the solutions TeX file     https://kskedlaya.org/putnam-archive/<year>s.tex
  3. the scoring HTML page      https://kskedlaya.org/putnam-archive/putnam<year>stats.html

Everything raw goes into myputnam.db.

    python3 scrape.py                 # all years, 1985..current
    python3 scrape.py 2015 2024       # a range
    python3 scrape.py 2024            # a single year
"""

import html
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import rank

BASE = "https://kskedlaya.org/putnam-archive"
DB_PATH = Path(__file__).with_name("myputnam.db")
FIRST_YEAR = 1985
USER_AGENT = "putnam-tool/0.1 (personal study tool)"

SESSIONS = ("A", "B")
NUMBERS = (1, 2, 3, 4, 5, 6)

# matches \item[A1], \item[A--1], \item[B-6] ... but not \item[(i)]
ITEM_RE = re.compile(r"\\item\s*\[\s*([AB])\s*[-\u2013\u2014]*\s*([1-6])\s*\]")
# matches a stats-table column header: A1 / A-1 / A--1
LABEL_RE = re.compile(r"^([AB])\s*[-\u2013\u2014]*\s*([1-6])$")


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS problems (
    id                  INTEGER PRIMARY KEY,
    year                INTEGER NOT NULL,
    session             TEXT    NOT NULL,   -- 'A' or 'B'
    number              INTEGER NOT NULL,   -- 1..6
    page_image_path     TEXT,               -- reserved: rendered problem image
    solution_image_path TEXT,               -- reserved: rendered solution image
    difficulty          TEXT,               -- 'easy' | 'medium' | 'hard' (from rank.py)
    problem_tex         TEXT,
    solution_tex        TEXT,
    UNIQUE (year, session, number)
);

-- raw scraped payloads, exactly as downloaded
CREATE TABLE IF NOT EXISTS sources (
    url        TEXT PRIMARY KEY,
    year       INTEGER NOT NULL,
    kind       TEXT    NOT NULL,   -- 'problems' | 'solutions' | 'stats'
    content    TEXT,
    fetched_at TEXT
);

-- frequency distribution of problem scores, straight off the stats page
CREATE TABLE IF NOT EXISTS score_dist (
    year    INTEGER NOT NULL,
    session TEXT    NOT NULL,
    number  INTEGER NOT NULL,
    score   TEXT    NOT NULL,      -- '0'..'10', or 'blank'/'NA'
    count   INTEGER NOT NULL,
    PRIMARY KEY (year, session, number, score)
);
"""


def connect(path=DB_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch(url):
    """Return the text at url, or None if it isn't there."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except urllib.error.URLError as e:
        print(f"    ! {url}: {e.reason}")
        return None


def store_source(con, url, year, kind, content):
    con.execute(
        "INSERT INTO sources (url, year, kind, content, fetched_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET content=excluded.content, fetched_at=excluded.fetched_at",
        (url, year, kind, content, time.strftime("%Y-%m-%d %H:%M:%S")),
    )


# --------------------------------------------------------------------------
# TeX splitting
# --------------------------------------------------------------------------

def split_tex(tex):
    """Split a problems/solutions TeX file into {(session, number): body}."""
    if not tex:
        return {}
    end = tex.find(r"\end{document}")
    if end != -1:
        tex = tex[:end]

    marks = [(m.start(), m.end(), m.group(1), int(m.group(2))) for m in ITEM_RE.finditer(tex)]
    out = {}
    for i, (_, body_start, session, number) in enumerate(marks):
        body_end = marks[i + 1][0] if i + 1 < len(marks) else len(tex)
        body = tex[body_start:body_end]
        body = re.sub(r"\\end\{itemize\}\s*$", "", body.strip()).strip()
        out[(session, number)] = body
    return out


# --------------------------------------------------------------------------
# stats HTML parsing
# --------------------------------------------------------------------------

class TableParser(HTMLParser):
    """Collect every <table> as a list of rows of plain-text cells."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None

    # older pages in the archive leave <td> and <tr> unclosed, so every
    # boundary closes whatever is still open rather than trusting end tags.

    def _close_cell(self):
        if self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(html.unescape(text))
            self._cell = None

    def _close_row(self):
        self._close_cell()
        if self._row is not None:
            self._table.append(self._row)
            self._row = None

    def _close_table(self):
        self._close_row()
        if self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._close_table()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._close_row()
            self._row = []
        elif tag in ("td", "th") and self._table is not None:
            self._close_cell()
            if self._row is None:
                self._row = []
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()
        elif tag == "table":
            self._close_table()

    def close(self):
        super().close()
        self._close_table()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_stats(html_text):
    """Pull the per-problem score frequency distribution out of a stats page.

    Returns {(session, number): {score_label: count}}.
    """
    if not html_text:
        return {}

    parser = TableParser()
    parser.feed(html_text)
    parser.close()

    for table in parser.tables:
        # find the header row that names at least 12 problems
        header_idx, columns = None, {}
        for i, row in enumerate(table):
            found = {}
            for col, cell in enumerate(row):
                m = LABEL_RE.match(cell.strip())
                if m:
                    found[col] = (m.group(1), int(m.group(2)))
            if len(found) >= 12:
                header_idx, columns = i, found
                break
        if header_idx is None:
            continue

        dist = {key: {} for key in columns.values()}
        for row in table[header_idx + 1:]:
            if not row:
                continue
            label = row[0].strip()
            if re.fullmatch(r"\d{1,2}", label):
                score = str(int(label))
            elif label.lower() in ("blank", "na", "n/a", "-"):
                score = "blank"
            else:
                continue
            for col, key in columns.items():
                if col >= len(row):
                    continue
                value = row[col].strip().replace(",", "")
                if re.fullmatch(r"\d+", value):
                    dist[key][score] = int(value)
        if any(dist.values()):
            return dist
    return {}


# --------------------------------------------------------------------------
# one year
# --------------------------------------------------------------------------

def scrape_year(con, year):
    urls = {
        "problems": f"{BASE}/{year}.tex",
        "solutions": f"{BASE}/{year}s.tex",
        "stats": f"{BASE}/putnam{year}stats.html",
    }
    raw = {}
    for kind, url in urls.items():
        raw[kind] = fetch(url)
        if raw[kind] is not None:
            store_source(con, url, year, kind, raw[kind])

    if raw["problems"] is None:
        print(f"{year}: no problems file, skipping")
        return 0

    problems = split_tex(raw["problems"])
    solutions = split_tex(raw["solutions"])
    dist = parse_stats(raw["stats"])

    n = 0
    for session in SESSIONS:
        for number in NUMBERS:
            key = (session, number)
            if key not in problems:
                continue
            counts = dist.get(key, {})
            difficulty = rank.classify(counts) if counts else None

            con.execute(
                """INSERT INTO problems (year, session, number, problem_tex, solution_tex, difficulty)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(year, session, number) DO UPDATE SET
                       problem_tex  = excluded.problem_tex,
                       solution_tex = COALESCE(excluded.solution_tex, problems.solution_tex),
                       difficulty   = COALESCE(excluded.difficulty, problems.difficulty)""",
                (year, session, number, problems[key], solutions.get(key), difficulty),
            )
            for score, count in counts.items():
                con.execute(
                    "INSERT INTO score_dist (year, session, number, score, count) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(year, session, number, score) DO UPDATE SET count=excluded.count",
                    (year, session, number, score, count),
                )
            n += 1

    print(
        f"{year}: {n} problems"
        f", solutions {len(solutions)}"
        f", stats {'yes' if dist else 'no'}"
    )
    return n


def main(argv):
    last = date.today().year
    if len(argv) == 1:
        years = range(FIRST_YEAR, last + 1)
    elif len(argv) == 2:
        years = [int(argv[1])]
    else:
        years = range(int(argv[1]), int(argv[2]) + 1)

    con = connect()
    total = 0
    for year in years:
        try:
            total += scrape_year(con, year)
        except Exception as e:  # keep going; one bad year shouldn't kill the run
            print(f"{year}: error {e!r}")
        con.commit()
    con.close()
    print(f"\ndone: {total} problems in {DB_PATH}")


if __name__ == "__main__":
    main(sys.argv)
