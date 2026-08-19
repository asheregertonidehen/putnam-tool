#!/usr/bin/env python3
"""Two-pass topic classification of Putnam problems via OpenRouter.

    export OPENROUTER_API_KEY=sk-or-...
    python3 classify.py                 # every unclassified problem
    python3 classify.py --year 2019     # just one year
    python3 classify.py --smoke --dry-run   # 15-problem end-to-end check

Only problems that carry both a problem statement and a solution are eligible;
the solution is what makes the second pass more than a guess at the wording.

Pass 1 (represent)
    problem + solution  ->  topic + a short description of what the problem is
    really about and how it is solved.

Pass 2 (classify)
    problem + solution + topic + description  ->  domain, up to three
    sub-domains, a proof method (or none), and a confidence score.

The two passes are separate API calls with separate prompts, so the second one
reasons over the representation rather than re-deriving it.  Everything lands
in the problem_topics table; run again to fill in whatever failed.

Every run finishes by printing the summary table: A1, A2, B1 and B2 pooled
together and counted by domain, most common domain first, with the confidence
the model reported for those calls.  `--stats` prints just that table.

Stdlib only -- the HTTP call is urllib.
"""

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

from taxonomy import (
    MAX_SUBDOMAINS,
    PROOF_METHODS,
    TOPICS,
    domains_of,
    subdomains_of,
    topic_outline,
)

DB_PATH = Path(__file__).with_name("myputnam.db")
API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Gemini's cheapest current flash tier: strong on competition math, ~$0.375/M
# input tokens, which keeps a full 372-problem run well under a dollar.
MODEL = "google/gemini-3.7-flash"

MAX_RETRIES = 4
TIMEOUT = 180


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS problem_topics (
    problem_id   INTEGER PRIMARY KEY REFERENCES problems(id),
    topic        TEXT NOT NULL,
    description  TEXT NOT NULL,
    domain       TEXT NOT NULL,
    subdomains   TEXT NOT NULL,      -- JSON array, 1..3 entries
    proof_method TEXT,               -- NULL when the model said none
    confidence   REAL NOT NULL,      -- 0..1
    reasoning    TEXT,
    basis        TEXT NOT NULL DEFAULT 'archive',   -- 'archive' | 'model'
    attempt      TEXT,               -- the model's own solution, for 'model' rows
    model        TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# What the classification was read from.  The archive has solutions back to
# 1995 only, so the earlier years are filed from a solution the model worked
# out itself -- worth keeping apart, since it can be wrong in a way a
# published solution cannot.
ARCHIVE = "archive"
MODEL_SOLVED = "model"


def connect(db_path=DB_PATH):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(problem_topics)")}
    for column, spec in (("basis", "TEXT NOT NULL DEFAULT 'archive'"),
                         ("attempt", "TEXT")):
        if column not in have:                      # table predates the columns
            con.execute(f"ALTER TABLE problem_topics ADD COLUMN {column} {spec}")
    con.commit()
    return con


def eligible(con, year=None, redo=False, limit=None):
    """Problems with both a statement and a solution, not yet classified."""
    sql = ["SELECT id, year, session, number, problem_tex, solution_tex",
           "FROM problems",
           "WHERE COALESCE(TRIM(problem_tex), '') != ''",
           "  AND COALESCE(TRIM(solution_tex), '') != ''"]
    args = []
    if not redo:
        sql.append("AND id NOT IN (SELECT problem_id FROM problem_topics)")
    if year:
        sql.append("AND year = ?")
        args.append(year)
    sql.append("ORDER BY year DESC, session, number")
    if limit:
        sql.append("LIMIT ?")
        args.append(limit)
    return [dict(r) for r in con.execute("\n".join(sql), args)]


SMOKE_HEAD = 5
SMOKE_RANDOM = 10


def smoke_sample(con, seed=None):
    """The first few problems plus a random spread -- one cheap end-to-end check.

    The head of the list is 2025 A1-A5, which is where the LaTeX-in-JSON
    failures showed up, so a smoke run re-covers them every time.  The random
    tail is drawn from every other eligible problem, and drawn afresh unless a
    seed pins it, so repeated runs keep meeting new material.  Already
    classified problems stay in the pool: a smoke test should test the same
    thing twice.
    """
    pool = eligible(con, redo=True)
    head = pool[:SMOKE_HEAD]
    rest = pool[SMOKE_HEAD:]
    tail = random.Random(seed).sample(rest, min(SMOKE_RANDOM, len(rest)))
    return head + sorted(tail, key=lambda p: (-p["year"], p["session"], p["number"]))


def save(con, problem_id, record, model=MODEL):
    con.execute(
        "INSERT OR REPLACE INTO problem_topics "
        "(problem_id, topic, description, domain, subdomains, proof_method, "
        " confidence, reasoning, basis, attempt, model, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'))",
        (
            problem_id,
            record["topic"],
            record["description"],
            record["domain"],
            json.dumps(record["subdomains"]),
            record["proof_method"],
            record["confidence"],
            record.get("reasoning"),
            record.get("basis", ARCHIVE),
            record.get("attempt"),
            model,
        ),
    )
    con.commit()


# --------------------------------------------------------------------------
# openrouter
# --------------------------------------------------------------------------

def ask(system, user, api_key, model=MODEL):
    """One chat completion, parsed as JSON.  Retries on transient failures."""
    payload = json.dumps({
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode()

    last = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(
            API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "putnam-tool",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = json.load(resp)
            text = body["choices"][0]["message"]["content"]
            return loads_lenient(text)
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError) as e:
            last = e
            if isinstance(e, json.JSONDecodeError):
                last = json.JSONDecodeError(f"{e.msg} in: {text[:300]!r}", e.doc, e.pos)
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(2 ** attempt)
    raise RuntimeError(f"OpenRouter call failed: {last}")


def strip_fence(text):
    """Models occasionally wrap JSON in ```json fences despite json_object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text.strip()


# A model describing a Putnam problem reaches for LaTeX, and LaTeX is nothing
# but backslashes.  JSON has opinions about those: "\pi" and "\{" are hard
# parse errors, while "\frac" and "\binom" quietly decode to a formfeed and a
# backspace.  The prompts ask for plain prose to avoid the whole business, but
# a reply that slips through should not cost a problem, so mis-escaped JSON is
# repaired rather than retried.

CONTROL_JUNK = "\b\f\v\x07"          # what \binom, \frac, \vec, \alpha decode to
JSON_ESCAPES = '"\\/bfnrtu'


def loads_lenient(text):
    """json.loads, but tolerant of raw LaTeX backslashes inside the strings."""
    text = strip_fence(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return json.loads(repair_escapes(text))
    # Parsed, but a swallowed \frac leaves a control character behind.
    return json.loads(repair_escapes(text)) if has_control_junk(data) else data


def has_control_junk(value):
    if isinstance(value, str):
        return any(ch in value for ch in CONTROL_JUNK)
    if isinstance(value, dict):
        return any(has_control_junk(v) for v in value.values())
    if isinstance(value, list):
        return any(has_control_junk(v) for v in value)
    return False


def repair_escapes(text):
    """Double every backslash inside a JSON string that isn't a real escape.

    Ambiguous one-letter escapes (\n, \t, \b, \f, \r) are read as LaTeX when a
    letter follows -- "\theta" and "\nabla" are far likelier here than a tab or
    a newline mid-sentence, and a stray literal beats a mangled word.
    """
    out = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if not in_string:
            in_string = ch == '"'
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = False
            out.append(ch)
            i += 1
            continue
        if ch != "\\":
            out.append(ch)
            i += 1
            continue

        nxt = text[i + 1] if i + 1 < len(text) else ""
        if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[i + 2:i + 6] or ""):
            out.append(text[i:i + 6])
            i += 6
        elif nxt in JSON_ESCAPES and not (nxt in "bfnrt" and text[i + 2:i + 3].isalpha()):
            out.append(text[i:i + 2])
            i += 2
        else:
            out.append("\\\\")            # a literal backslash the model meant to keep
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# pass 1 -- build the representation
# --------------------------------------------------------------------------

REPRESENT_SYSTEM = """You are a Putnam problem archivist with a research \
mathematician's breadth. You are given a competition problem together with its \
official solution and final answer.

Decide which of these five top-level topics the problem belongs to:

{topics}

Then write a short description -- three or four sentences, no more -- that says \
what the problem is actually about and how the solution works. Write it so that \
someone reading only your description could tell which specific area within the \
topic this problem lives in: name the machinery the solution turns on (the \
theorems, transforms, structures, or estimates it uses), not just the surface \
wording of the statement. Prefer the technique that does the real work over \
incidental steps.

Write the description as plain prose, and keep LaTeX out of it: no backslash \
commands, no dollar signs. Write pi, sin x, x^2, n choose k, the integral of f \
over [0,1]. Naming the technique is what matters, not reproducing the formula.

Reply with JSON only:
{{"topic": "<exactly one topic from the list>",
  "description": "<3-4 sentences, plain prose, no LaTeX>"}}"""


def represent(problem, api_key, model=MODEL):
    system = REPRESENT_SYSTEM.format(topics="\n".join(f"- {t}" for t in TOPICS))
    user = (
        f"PROBLEM ({problem['year']} {problem['session']}{problem['number']}):\n"
        f"{problem['problem_tex']}\n\n"
        f"SOLUTION AND ANSWER:\n{problem['solution_tex']}"
    )
    out = ask(system, user, api_key, model)

    topic = pick(out.get("topic"), TOPICS)
    if topic is None:
        raise ValueError(f"pass 1 returned an unknown topic: {out.get('topic')!r}")
    description = (out.get("description") or "").strip()
    if not description:
        raise ValueError("pass 1 returned an empty description")
    return topic, description


# --------------------------------------------------------------------------
# pass 2 -- place it in the taxonomy
# --------------------------------------------------------------------------

CLASSIFY_SYSTEM = """You are a Putnam problem archivist filing a problem into a \
fixed taxonomy. You are given the problem, {provenance}, and a representation of \
it (a topic and a description of what it is about and how it is solved) produced \
by an earlier pass.

The topic is already settled. Your job is the level below it.

Domains available under the topic "{topic}":
{domains}

Sub-domains, by domain:
{subdomains}

Proof methods (a problem may fit none of them):
{methods}

Rules:
- Choose exactly one domain from the list above.
- Choose the sub-domains within that domain that the solution genuinely turns \
on. Usually one. List more only when the solution really does rest on several, \
and never more than {max_subs}, most central first.
- Assign a proof method only when the solution's argument is structured that \
way; otherwise use null. A proof by contradiction, an induction, a pigeonhole \
count, an extremal choice, or a preserved invariant has to be doing the work, \
not merely appearing in passing.
- Give a confidence in [0, 1] that the problem really belongs in the domain and \
sub-domains you chose. Lower it when the problem straddles domains, when the \
solution's key step is not well described by any listed sub-domain, or when you \
had to name several sub-domains because no single one fits -- more sub-domains \
means less confidence, not more.

For the whole taxonomy in context:
{outline}

Write the reasoning as plain prose with no LaTeX: no backslash commands, no \
dollar signs.

Reply with JSON only:
{{"domain": "<one domain>",
  "subdomains": ["<1 to {max_subs} sub-domains of that domain>"],
  "proof_method": "<one proof method, or null>",
  "confidence": <number between 0 and 1>,
  "reasoning": "<one sentence>"}}"""


ARCHIVE_PROVENANCE = "its official solution and answer"

MODEL_PROVENANCE = (
    "a solution and answer worked out by another model rather than taken from "
    "the official archive, which means it may be incomplete or wrong"
)


def classify_one(problem, topic, description, api_key, model=MODEL,
                 solution=None, provenance=ARCHIVE_PROVENANCE,
                 solution_label="SOLUTION AND ANSWER"):
    domains = domains_of(topic)
    subs = "\n".join(
        f"  {d}:\n" + "\n".join(f"    - {s}" for s in subdomains_of(topic, d))
        for d in domains
    )
    system = CLASSIFY_SYSTEM.format(
        topic=topic,
        domains="\n".join(f"- {d}" for d in domains),
        subdomains=subs,
        methods="\n".join(f"- {m}" for m in PROOF_METHODS),
        max_subs=MAX_SUBDOMAINS,
        outline=topic_outline(),
        provenance=provenance,
    )
    user = (
        f"TOPIC: {topic}\n\n"
        f"DESCRIPTION: {description}\n\n"
        f"PROBLEM ({problem['year']} {problem['session']}{problem['number']}):\n"
        f"{problem['problem_tex']}\n\n"
        f"{solution_label}:\n{problem['solution_tex'] if solution is None else solution}"
    )
    out = ask(system, user, api_key, model)

    domain = pick(out.get("domain"), domains)
    if domain is None:
        raise ValueError(f"pass 2 returned an unknown domain: {out.get('domain')!r}")

    allowed = subdomains_of(topic, domain)
    chosen, seen = [], set()
    for raw in out.get("subdomains") or []:
        match = pick(raw, allowed)
        if match and match not in seen:
            seen.add(match)
            chosen.append(match)
    if not chosen:
        raise ValueError(f"pass 2 returned no usable sub-domain: {out.get('subdomains')!r}")
    chosen = chosen[:MAX_SUBDOMAINS]

    method = pick(out.get("proof_method"), PROOF_METHODS)  # None for null/"none"

    try:
        confidence = min(1.0, max(0.0, float(out.get("confidence"))))
    except (TypeError, ValueError):
        raise ValueError(f"pass 2 returned a bad confidence: {out.get('confidence')!r}")

    return {
        "topic": topic,
        "description": description,
        "domain": domain,
        "subdomains": chosen,
        "proof_method": method,
        "confidence": confidence,
        "reasoning": (out.get("reasoning") or "").strip() or None,
    }


def pick(value, allowed):
    """Match a model's label against the vocabulary, forgiving case and dashes."""
    if not isinstance(value, str):
        return None
    want = normalize_label(value)
    if want in ("", "none", "null", "n/a"):
        return None
    for option in allowed:
        if normalize_label(option) == want:
            return option
    return None


def normalize_label(text):
    out = unicodedata.normalize("NFKD", text.strip().lower())
    out = "".join(ch for ch in out if not unicodedata.combining(ch))
    for dash in ("–", "—", "−"):
        out = out.replace(dash, "-")
    for quote in ("‘", "’"):
        out = out.replace(quote, "'")
    out = out.replace("≥", ">=")
    return " ".join(out.split())


# --------------------------------------------------------------------------
# statistics -- where the entry-level problems live
# --------------------------------------------------------------------------

# A1, A2, B1 and B2 are the two openers of each session: the problems most
# people actually reach.  Pooling all four says which topics and domains a
# competitor is most likely to meet early, which is the useful thing to study
# by, and the sub-domain rankings say what to study within them.
ENTRY_POSITIONS = ("A1", "A2", "B1", "B2")

TOP_SUBDOMAINS = 3


def entry_rows(con, basis=None):
    """One row per classified A1/A2/B1/B2 problem, shaped like a fresh record.

    `basis` narrows to the archive-solved or the model-solved half."""
    sql = ("SELECT t.topic, t.domain, t.subdomains, t.confidence "
           "FROM problem_topics t JOIN problems p ON p.id = t.problem_id "
           "WHERE p.session || p.number IN (?, ?, ?, ?)")
    args = list(ENTRY_POSITIONS)
    if basis:
        sql += " AND t.basis = ?"
        args.append(basis)
    rows = con.execute(sql, args).fetchall()
    return [{"topic": r["topic"], "domain": r["domain"],
             "subdomains": json.loads(r["subdomains"]), "confidence": r["confidence"]}
            for r in rows]


def tally(rows, key):
    """Rank rows by how often `key` occurs, ties broken by confidence."""
    buckets = {}
    for row in rows:
        buckets.setdefault(key(row), []).append(row["confidence"])
    out = [{"name": name, "count": len(cs), "mean": sum(cs) / len(cs),
            "low": min(cs), "high": max(cs)}
           for name, cs in buckets.items()]
    out.sort(key=lambda r: (-r["count"], -r["mean"], r["name"]))
    return out


def subdomain_tally(rows):
    """{domain: ranked sub-domains}.  A problem tagged with two or three
    sub-domains counts once under each of them."""
    spread = {}
    for row in rows:
        for sub in row["subdomains"]:
            spread.setdefault(row["domain"], []).append(
                {"sub": sub, "confidence": row["confidence"]})
    return {domain: tally(rs, lambda r: r["sub"]) for domain, rs in spread.items()}


def rank_table(label, stats, total, subs=None):
    """A count / share / confidence table, optionally with each row's top
    sub-domains indented underneath it."""
    width = max([len(label)] + [len(r["name"]) for r in stats])
    rule = "-" * (width + 44)
    lines = [f"{label.ljust(width)}  {'count':>5}  {'share':>6}   confidence (mean, range)",
             rule]
    for row in stats:
        lines.append(
            f"{row['name'].ljust(width)}  {row['count']:5d}  {row['count'] / total:6.1%}"
            f"   {row['mean']:.2f}  ({row['low']:.2f}-{row['high']:.2f})"
        )
        for pick in (subs or {}).get(row["name"], [])[:TOP_SUBDOMAINS]:
            lines.append(f"    {pick['name']} ({pick['count']})")
    lines.append(rule)
    mean = sum(r["mean"] * r["count"] for r in stats) / total
    lines.append(f"{'total'.ljust(width)}  {total:5d}  {1:6.1%}   {mean:.2f}")
    return "\n".join(lines)


def plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def subdomain_section(domains, subs):
    """Every sub-domain of every domain, ranked, domains in the same order."""
    lines = ["Every sub-domain, by domain",
             "a problem tagged with several sub-domains is counted under each,",
             "so these run ahead of the problem counts above",
             ""]
    for row in domains:
        ranked = subs.get(row["name"], [])
        lines.append(f"{row['name']}  ({plural(row['count'], 'problem')}, "
                     f"{plural(len(ranked), 'sub-domain')} used)")
        for sub in ranked:
            lines.append(f"  {sub['count']:4d}  {sub['mean']:.2f}  {sub['name']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_entry_stats(rows, classified=None, scope="the database"):
    """The whole summary.  `classified` is how many problems were classified in
    all, which is the larger number these tables are drawn from: only the four
    entry positions are counted here, so the two rarely agree."""
    total = len(rows)
    if classified is None:
        classified = total
    header = ["A1 + A2 + B1 + B2",
              f"{total} of {classified} problems classified in {scope} "
              f"sit in those four positions"]
    if not rows:
        return "\n".join(header)

    topics = tally(rows, lambda r: r["topic"])
    domains = tally(rows, lambda r: r["domain"])
    subs = subdomain_tally(rows)

    return "\n\n".join([
        "\n".join(header),
        rank_table("topic", topics, total),
        f"by domain, with its {TOP_SUBDOMAINS} most common sub-domains\n"
        + rank_table("domain", domains, total, subs),
        subdomain_section(domains, subs),
    ])


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def run(problem, api_key, model=MODEL):
    topic, description = represent(problem, api_key, model)
    return classify_one(problem, topic, description, api_key, model)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--year", type=int, help="only this contest year")
    ap.add_argument("--limit", type=int, help="stop after this many problems")
    ap.add_argument("--model", default=MODEL, help=f"OpenRouter model (default {MODEL})")
    ap.add_argument("--redo", action="store_true", help="reclassify already-done problems")
    ap.add_argument("--dry-run", action="store_true", help="print results, write nothing")
    ap.add_argument("--smoke", action="store_true",
                    help=f"end-to-end check: the first {SMOKE_HEAD} problems plus "
                         f"{SMOKE_RANDOM} random others (ignores --year/--limit)")
    ap.add_argument("--seed", type=int, help="pin --smoke's random pick")
    ap.add_argument("--stats", action="store_true",
                    help="print the A1/A2/B1/B2 domain table and exit")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"{args.db} not found -- run: python3 scrape.py")

    con = connect(args.db)
    if args.stats:
        classified = con.execute("SELECT COUNT(*) FROM problem_topics").fetchone()[0]
        print(format_entry_stats(entry_rows(con), classified))
        con.close()
        return

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("set OPENROUTER_API_KEY (get one at https://openrouter.ai/keys)")

    if args.smoke:
        todo = smoke_sample(con, args.seed)
    else:
        todo = eligible(con, year=args.year, redo=args.redo, limit=args.limit)
    if not todo:
        print("nothing to classify")
        return

    if args.smoke:
        picks = " ".join(f"{p['year']}{p['session']}{p['number']}" for p in todo)
        print(f"smoke test: {picks}")
    print(f"classifying {len(todo)} problems with {args.model}")
    done = failed = 0
    fresh = []
    for problem in todo:
        label = f"{problem['year']} {problem['session']}{problem['number']}"
        try:
            record = run(problem, api_key, args.model)
        except (RuntimeError, ValueError) as e:
            failed += 1
            print(f"  {label:12} FAILED  {e}")
            continue
        if not args.dry_run:
            save(con, problem["id"], record)
        if f"{problem['session']}{problem['number']}" in ENTRY_POSITIONS:
            fresh.append(record)
        done += 1
        print(f"  {label:12} {record['domain']} / {', '.join(record['subdomains'])}"
              f"  [{record['proof_method'] or 'no proof tag'}]"
              f"  conf {record['confidence']:.2f}")

    print(f"\nclassified {done}, failed {failed}"
          + ("  (dry run -- nothing written)" if args.dry_run else ""))
    print()
    # A dry run writes nothing, so its table has to come from this run alone.
    if args.dry_run:
        print(format_entry_stats(fresh, done, "this run"))
    else:
        classified = con.execute("SELECT COUNT(*) FROM problem_topics").fetchone()[0]
        print(format_entry_stats(entry_rows(con), classified))
    con.close()


if __name__ == "__main__":
    main()
