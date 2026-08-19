#!/usr/bin/env python3
"""Classify the problems the archive has no solution for (1985-1994).

    export OPENROUTER_API_KEY=sk-or-...
    python3 solve.py                    # every unclassified solution-less problem
    python3 solve.py --smoke --dry-run  # 15-problem end-to-end check

classify.py needs a published solution, because its whole premise is that the
solution says what the problem is really about.  120 problems do not have one.
This fills the hole the only way available: the model solves them first.

Pass 1 (solve)
    problem  ->  topic + the answer + a short account of how to get it,
    preferring the simplest and most established argument when the problem is
    a known one.

Pass 2 (classify)
    the same second pass classify.py uses, handed the model's own solution in
    place of the archive's and told that is what it is, so it can discount a
    shaky one in its confidence.

Rows land in problem_topics alongside the archive-solved ones, marked
basis='model' and carrying the attempted solution, so the two populations can
be reported apart (STUDY_GUIDE_HOLE.md) or together (STUDY_GUIDE_TOTAL.md).
"""

import argparse
import json
import os
import random
import sys
from pathlib import Path

import classify
from classify import (
    DB_PATH,
    ENTRY_POSITIONS,
    MODEL,
    MODEL_PROVENANCE,
    MODEL_SOLVED,
    ask,
    classify_one,
    connect,
    format_entry_stats,
    pick,
    save,
)
from taxonomy import TOPICS

SMOKE_HEAD = 5
SMOKE_RANDOM = 10


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def unsolved(con, year=None, redo=False, limit=None):
    """Problems with a statement but no solution, not yet classified."""
    sql = ["SELECT id, year, session, number, problem_tex",
           "FROM problems",
           "WHERE COALESCE(TRIM(problem_tex), '') != ''",
           "  AND COALESCE(TRIM(solution_tex), '') = ''"]
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


def smoke_sample(con, seed=None):
    """The first few unsolved problems plus a random spread of the rest."""
    pool = unsolved(con, redo=True)
    tail = random.Random(seed).sample(pool[SMOKE_HEAD:],
                                      min(SMOKE_RANDOM, max(0, len(pool) - SMOKE_HEAD)))
    return pool[:SMOKE_HEAD] + sorted(tail, key=lambda p: (-p["year"], p["session"], p["number"]))


# --------------------------------------------------------------------------
# pass 1 -- solve it, then describe the solution
# --------------------------------------------------------------------------

SOLVE_SYSTEM = """You are a Putnam problem archivist with a research \
mathematician's breadth. You are given a competition problem with no published \
solution attached. Solve it.

Decide which of these five top-level topics the problem belongs to:

{topics}

Then give the answer and how it is obtained. Where the problem is a known one \
with a standard treatment, give the established solution rather than an \
ingenious alternative: the simplest and most usual argument is what is wanted. \
Keep it to a short paragraph -- the steps that carry the argument, the theorems \
or structures it turns on, and the final answer stated plainly. If the problem \
asks for a proof rather than a value, say what the answer field should hold in \
words ("the statement holds for all n", and so on).

Then write a separate description, three or four sentences, of what the problem \
is about and how it is solved, in the terms a classifier would need: name the \
machinery the solution turns on, not the surface wording of the statement.

If you cannot solve it, say so in the answer field rather than inventing one, \
and describe the machinery the problem plainly calls for.

Write all three fields as plain prose, and keep LaTeX out of them: no backslash \
commands, no dollar signs. Write pi, sin x, x^2, n choose k, the integral of f \
over [0,1].

Reply with JSON only:
{{"topic": "<exactly one topic from the list>",
  "answer": "<the answer, stated plainly>",
  "solution": "<a short paragraph: the established argument>",
  "description": "<3-4 sentences, plain prose, no LaTeX>"}}"""


def solve(problem, api_key, model=MODEL):
    """-> (topic, attempted solution text, description)"""
    system = SOLVE_SYSTEM.format(topics="\n".join(f"- {t}" for t in TOPICS))
    user = (f"PROBLEM ({problem['year']} {problem['session']}{problem['number']}):\n"
            f"{problem['problem_tex']}")
    out = ask(system, user, api_key, model)

    topic = pick(out.get("topic"), TOPICS)
    if topic is None:
        raise ValueError(f"pass 1 returned an unknown topic: {out.get('topic')!r}")
    answer = (out.get("answer") or "").strip()
    solution = (out.get("solution") or "").strip()
    description = (out.get("description") or "").strip()
    if not solution or not description:
        raise ValueError("pass 1 returned an empty solution or description")

    attempt = f"ANSWER: {answer}\n\nSOLUTION: {solution}" if answer else solution
    return topic, attempt, description


def run(problem, api_key, model=MODEL):
    topic, attempt, description = solve(problem, api_key, model)
    record = classify_one(
        problem, topic, description, api_key, model,
        solution=attempt,
        provenance=MODEL_PROVENANCE,
        solution_label="ATTEMPTED SOLUTION AND ANSWER (worked out by a model, unverified)",
    )
    record["basis"] = MODEL_SOLVED
    record["attempt"] = attempt
    return record


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

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
    ap.add_argument("--show", action="store_true", help="print each attempted solution")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"{args.db} not found -- run: python3 scrape.py")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("set OPENROUTER_API_KEY (get one at https://openrouter.ai/keys)")

    con = connect(args.db)
    todo = smoke_sample(con, args.seed) if args.smoke else unsolved(
        con, year=args.year, redo=args.redo, limit=args.limit)
    if not todo:
        print("nothing to solve")
        return

    if args.smoke:
        print("smoke test: " + " ".join(
            f"{p['year']}{p['session']}{p['number']}" for p in todo))
    print(f"solving and classifying {len(todo)} problems with {args.model}")

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
            save(con, problem["id"], record, args.model)
        if f"{problem['session']}{problem['number']}" in ENTRY_POSITIONS:
            fresh.append(record)
        done += 1
        print(f"  {label:12} {record['domain']} / {', '.join(record['subdomains'])}"
              f"  [{record['proof_method'] or 'no proof tag'}]"
              f"  conf {record['confidence']:.2f}")
        if args.show:
            print(f"               {record['attempt'].splitlines()[0][:110]}")

    print(f"\nsolved {done}, failed {failed}"
          + ("  (dry run -- nothing written)" if args.dry_run else ""))
    print()
    if args.dry_run:
        print(format_entry_stats(fresh, done, "this run"))
    else:
        rows = classify.entry_rows(con, basis=MODEL_SOLVED)
        total = con.execute("SELECT COUNT(*) FROM problem_topics WHERE basis = ?",
                            (MODEL_SOLVED,)).fetchone()[0]
        print(format_entry_stats(rows, total, "the model-solved set"))
    con.close()


if __name__ == "__main__":
    main()
