#!/usr/bin/env python3
"""Difficulty estimation from Putnam score distributions.

The archive's stats pages give, for each problem, how many of the top N
contestants earned each score (10, 9, 8, ... 1, 0, and blank).  This module
turns that distribution into 'easy' / 'medium' / 'hard'.

Nothing here is served; scrape.py calls classify() while loading, and
`python3 rank.py` recomputes the difficulty column in place.

Scoring model
-------------
Putnam scores are famously bimodal: 10/9/8 means essentially solved, 2/1/0
means essentially nothing, and the middle is rare.  So two signals matter and
they say different things:

  solve_rate   = fraction of contestants scoring >= 8   (did anyone finish it)
  mean_credit  = mean score / 10                        (partial credit earned)

Blank papers count as 0 -- not attempting is evidence of difficulty.

    score = SOLVE_WEIGHT * solve_rate + MEAN_WEIGHT * mean_credit

Note this is difficulty *relative to the top of the field*, since the stats
tables only cover the top few hundred contestants.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).with_name("myputnam.db")

SOLVE_WEIGHT = 0.65
MEAN_WEIGHT = 0.35

# thresholds on the weighted score
EASY_CUTOFF = 0.40
MEDIUM_CUTOFF = 0.12

MAX_SCORE = 10


def normalize(dist):
    """Coerce a raw distribution into {int score: count}, blanks folded into 0."""
    out = {}
    for key, count in dist.items():
        count = int(count)
        label = str(key).strip().lower()
        if label in ("blank", "na", "n/a", "-", ""):
            score = 0
        else:
            try:
                score = int(float(label))
            except ValueError:
                continue
        if 0 <= score <= MAX_SCORE:
            out[score] = out.get(score, 0) + count
    return out


def score_problem(dist):
    """Weighted difficulty score in [0, 1].  1.0 = everybody aced it."""
    counts = normalize(dist)
    total = sum(counts.values())
    if total == 0:
        return None

    solved = sum(c for s, c in counts.items() if s >= 8)
    points = sum(s * c for s, c in counts.items())

    solve_rate = solved / total
    mean_credit = points / (total * MAX_SCORE)
    return SOLVE_WEIGHT * solve_rate + MEAN_WEIGHT * mean_credit


def classify(dist):
    """'easy' | 'medium' | 'hard', or None when there's no usable data."""
    value = score_problem(dist)
    if value is None:
        return None
    if value >= EASY_CUTOFF:
        return "easy"
    if value >= MEDIUM_CUTOFF:
        return "medium"
    return "hard"


def explain(dist):
    """Full breakdown, handy when tuning the cutoffs."""
    counts = normalize(dist)
    total = sum(counts.values())
    if total == 0:
        return {"contestants": 0, "difficulty": None}
    solved = sum(c for s, c in counts.items() if s >= 8)
    points = sum(s * c for s, c in counts.items())
    return {
        "contestants": total,
        "solve_rate": solved / total,
        "mean_score": points / total,
        "score": score_problem(dist),
        "difficulty": classify(dist),
    }


# --------------------------------------------------------------------------
# recompute the difficulty column from what scrape.py stored
# --------------------------------------------------------------------------

def recompute(db_path=DB_PATH):
    con = sqlite3.connect(db_path)
    dists = {}
    for year, session, number, score, count in con.execute(
        "SELECT year, session, number, score, count FROM score_dist"
    ):
        dists.setdefault((year, session, number), {})[score] = count

    updated = 0
    for (year, session, number), dist in dists.items():
        difficulty = classify(dist)
        if difficulty is None:
            continue
        con.execute(
            "UPDATE problems SET difficulty=? WHERE year=? AND session=? AND number=?",
            (difficulty, year, session, number),
        )
        updated += 1
    con.commit()

    tally = dict(
        con.execute(
            "SELECT COALESCE(difficulty,'unrated'), COUNT(*) FROM problems GROUP BY 1"
        )
    )
    con.close()
    return updated, tally


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    updated, tally = recompute(path)
    print(f"rated {updated} problems")
    for name in ("easy", "medium", "hard", "unrated"):
        if name in tally:
            print(f"  {name:8} {tally[name]}")
