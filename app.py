#!/usr/bin/env python3
"""Tiny local web UI for practicing Putnam problems.

    python3 app.py            # then open http://localhost:8000

Stdlib only.  Reads myputnam.db, which scrape.py fills.
"""

import html as html_mod
import json
import random
import re
import sqlite3
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DB_PATH = Path(__file__).with_name("myputnam.db")
PORT = 8000


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def query(sql, args=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def tex_to_html(tex):
    """Just enough LaTeX cleanup to hand the rest to MathJax."""
    if not tex:
        return ""
    out = html_mod.escape(tex)
    out = re.sub(r"\\begin\{(itemize|enumerate)\}|\\end\{(itemize|enumerate)\}", "", out)
    out = re.sub(r"\\item\s*\[([^\]]*)\]", r"\n\1 ", out)   # (i), (a), ...
    out = re.sub(r"\\item\b", "\n* ", out)
    out = re.sub(r"\\(?:emph|textit)\{([^{}]*)\}", r"<em>\1</em>", out)
    out = re.sub(r"\\textbf\{([^{}]*)\}", r"<strong>\1</strong>", out)
    out = re.sub(r"\\(?:noindent|smallskip|medskip|bigskip|par)\b", "", out)
    out = re.sub(r"\\label\{[^{}]*\}", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def problem_row(row):
    return {
        "id": row["id"],
        "year": row["year"],
        "session": row["session"],
        "number": row["number"],
        "label": f"{row['year']} {row['session']}{row['number']}",
        "difficulty": row["difficulty"],
        "problem": tex_to_html(row["problem_tex"]),
        "solution": tex_to_html(row["solution_tex"]),
    }


def api(path, params):
    if path == "/api/years":
        return [r["year"] for r in query("SELECT DISTINCT year FROM problems ORDER BY year DESC")]

    if path == "/api/index":
        sql = ("SELECT id, year, session, number, difficulty, "
               "solution_tex IS NOT NULL AS has_solution FROM problems")
        args = []
        if params.get("year"):
            sql += " WHERE year = ?"
            args.append(int(params["year"][0]))
        sql += " ORDER BY year DESC, session, number"
        return query(sql, args)

    if path == "/api/problem":
        rows = query("SELECT * FROM problems WHERE id = ?", (int(params["id"][0]),))
        return problem_row(rows[0]) if rows else None

    if path == "/api/random":
        sql, args = "SELECT * FROM problems", []
        if params.get("year"):
            sql += " WHERE year = ?"
            args.append(int(params["year"][0]))
        rows = query(sql, args)
        return problem_row(random.choice(rows)) if rows else None

    return None


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Putnam Practice</title>
<script>
MathJax = {tex: {inlineMath: [['$','$']], displayMath: [['\\[','\\]']],
                 processEnvironments: true}};
</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<style>
body { font-family: serif; max-width: 46em; margin: 2em auto; padding: 0 1em; line-height: 1.5; }
h1 { font-size: 1.4em; }
button { font: inherit; padding: .3em .8em; margin-right: .5em; cursor: pointer; }
input { font: inherit; padding: .3em; }
#timer { font-family: monospace; font-size: 1.6em; border-bottom: 1px solid #ccc;
         padding-bottom: .3em; margin-bottom: 1em; }
.grid { display: flex; flex-wrap: wrap; gap: .3em; margin-top: 1em; }
.grid a { display: block; padding: .3em .5em; border: 1px solid #999; text-decoration: none;
          color: #000; font-family: monospace; font-size: .9em; }
.grid a:hover { background: #eee; }
.easy   { background: #e8f5e9; }
.medium { background: #fff8e1; }
.hard   { background: #ffebee; }
.body { white-space: pre-wrap; }
.meta { color: #666; font-size: .9em; }
hr { margin: 2em 0; border: 0; border-top: 1px solid #ccc; }
</style>
</head>
<body>

<div id="home">
  <h1>Putnam Practice</h1>
  <p>
    <input id="year" placeholder="year, e.g. 2019" size="16">
    <button onclick="showIndex(document.getElementById('year').value)">Search</button>
  </p>
  <p>
    <button onclick="randomProblem(document.getElementById('year').value)">Random Problem</button>
    <button onclick="showIndex('')">Choose Problem</button>
  </p>
  <div id="index"></div>
</div>

<div id="view" style="display:none">
  <div id="timer">00:00</div>
  <h1 id="title"></h1>
  <div class="meta" id="meta"></div>
  <div class="body" id="problem"></div>
  <div id="solution"></div>
  <hr>
  <button onclick="showSolution()">Solution</button>
  <button onclick="exitProblem()">Exit</button>
</div>

<script>
let current = null, timerId = null, started = 0;

async function get(url) { return (await fetch(url)).json(); }

async function showIndex(year) {
  const rows = await get('/api/index' + (year ? '?year=' + encodeURIComponent(year) : ''));
  const box = document.getElementById('index');
  if (!rows.length) { box.innerHTML = '<p>No problems for that year.</p>'; return; }
  let out = '', lastYear = null;
  for (const r of rows) {
    if (r.year !== lastYear) {
      if (lastYear !== null) out += '</div>';
      out += '<h3>' + r.year + '</h3><div class="grid">';
      lastYear = r.year;
    }
    out += '<a href="#" class="' + (r.difficulty || '') + '" onclick="openProblem(' + r.id +
           ');return false" title="' + (r.difficulty || 'unrated') + '">' +
           r.session + r.number + '</a>';
  }
  box.innerHTML = out + '</div>';
}

async function randomProblem(year) {
  show(await get('/api/random' + (year ? '?year=' + encodeURIComponent(year) : '')));
}

async function openProblem(id) { show(await get('/api/problem?id=' + id)); }

function show(p) {
  if (!p) { alert('Nothing found.'); return; }
  current = p;
  document.getElementById('title').textContent = p.label;
  document.getElementById('meta').textContent = 'difficulty: ' + (p.difficulty || 'unrated');
  document.getElementById('problem').innerHTML = p.problem;
  document.getElementById('solution').innerHTML = '';
  document.getElementById('home').style.display = 'none';
  document.getElementById('view').style.display = '';
  window.scrollTo(0, 0);
  startTimer();
  MathJax.typesetPromise();
}

function showSolution() {
  const box = document.getElementById('solution');
  if (box.innerHTML) return;
  box.innerHTML = '<hr><h3>Solution</h3><div class="body">' +
                  (current.solution || 'No solution in the archive for this year.') + '</div>';
  MathJax.typesetPromise([box]);
}

function exitProblem() {
  stopTimer();
  document.getElementById('view').style.display = 'none';
  document.getElementById('home').style.display = '';
}

function startTimer() {
  stopTimer();
  started = Date.now();
  tick();
  timerId = setInterval(tick, 1000);
}

function stopTimer() { if (timerId) clearInterval(timerId); timerId = null; }

function tick() {
  const s = Math.floor((Date.now() - started) / 1000);
  document.getElementById('timer').textContent =
    String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
            return
        if url.path.startswith("/api/"):
            try:
                data = api(url.path, parse_qs(url.query))
            except Exception as e:
                self._send(400, "application/json", json.dumps({"error": str(e)}).encode())
                return
            self._send(200, "application/json", json.dumps(data).encode())
            return
        self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    if not DB_PATH.exists():
        sys.exit("myputnam.db not found -- run: python3 scrape.py")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    url = f"http://localhost:{port}"
    print(f"Putnam practice at {url}  (ctrl-c to stop)")
    webbrowser.open(url)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
