#!/usr/bin/env python3
"""Two-pass topic classification of Putnam problems via OpenRouter.

    export OPENROUTER_API_KEY=sk-or-...
    python3 classify.py                 # every unclassified problem
    python3 classify.py --year 2019     # just one year
    python3 classify.py --limit 5 --dry-run

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

Stdlib only -- the HTTP call is urllib.
"""

import argparse
import json
import os
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
    model        TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def connect(db_path=DB_PATH):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
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


def save(con, problem_id, record):
    con.execute(
        "INSERT OR REPLACE INTO problem_topics "
        "(problem_id, topic, description, domain, subdomains, proof_method, "
        " confidence, reasoning, model, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))",
        (
            problem_id,
            record["topic"],
            record["description"],
            record["domain"],
            json.dumps(record["subdomains"]),
            record["proof_method"],
            record["confidence"],
            record.get("reasoning"),
            MODEL,
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
            return json.loads(strip_fence(text))
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError) as e:
            last = e
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

Reply with JSON only:
{{"topic": "<exactly one topic from the list>",
  "description": "<3-4 sentences>"}}"""


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
fixed taxonomy. You are given the problem, its official solution and answer, and \
a representation of it (a topic and a description of what it is about and how it \
is solved) produced by an earlier pass.

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

Reply with JSON only:
{{"domain": "<one domain>",
  "subdomains": ["<1 to {max_subs} sub-domains of that domain>"],
  "proof_method": "<one proof method, or null>",
  "confidence": <number between 0 and 1>,
  "reasoning": "<one sentence>"}}"""


def classify_one(problem, topic, description, api_key, model=MODEL):
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
    )
    user = (
        f"TOPIC: {topic}\n\n"
        f"DESCRIPTION: {description}\n\n"
        f"PROBLEM ({problem['year']} {problem['session']}{problem['number']}):\n"
        f"{problem['problem_tex']}\n\n"
        f"SOLUTION AND ANSWER:\n{problem['solution_tex']}"
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
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("set OPENROUTER_API_KEY (get one at https://openrouter.ai/keys)")
    if not args.db.exists():
        sys.exit(f"{args.db} not found -- run: python3 scrape.py")

    con = connect(args.db)
    todo = eligible(con, year=args.year, redo=args.redo, limit=args.limit)
    if not todo:
        print("nothing to classify")
        return

    print(f"classifying {len(todo)} problems with {args.model}")
    done = failed = 0
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
        done += 1
        print(f"  {label:12} {record['domain']} / {', '.join(record['subdomains'])}"
              f"  [{record['proof_method'] or 'no proof tag'}]"
              f"  conf {record['confidence']:.2f}")

    con.close()
    print(f"\nclassified {done}, failed {failed}"
          + ("  (dry run -- nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
