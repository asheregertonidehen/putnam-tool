#!/usr/bin/env python3
"""Turn the classifications into a study guide, ranked all the way down.

    python3 guide.py                 # STUDY_GUIDE.md        archive solutions
    python3 guide.py --basis model   # STUDY_GUIDE_HOLE.md   model solutions
    python3 guide.py --basis all     # STUDY_GUIDE_TOTAL.md  both together
    python3 guide.py --all           # all three

Topics ordered by how often they appear, each topic's domains ordered inside
it, each domain's sub-domains ordered inside that.  Two counts sit next to
every label:

    entry   appearances among A1, A2, B1 and B2 -- the problems you will reach
    all     appearances anywhere on the exam, entry positions included

Entry is what the ranking follows; `all` is there to show when something is
common overall but lives in the hard half (linear algebra) or the reverse.

Topic and domain counts are one per problem.  Sub-domain counts are one per
tag, so a problem filed under two sub-domains is counted in both and a
domain's sub-domain counts can add up past its problem count.
"""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from classify import ARCHIVE, DB_PATH, ENTRY_POSITIONS, MODEL_SOLVED
from taxonomy import DOMAINS, PROOF_METHODS, SUBDOMAINS, TAXONOMY

# basis -> (file, what the sheet is drawn from)
SHEETS = {
    ARCHIVE: ("STUDY_GUIDE.md",
              "the 372 problems the archive publishes a solution for (1995 onwards), "
              "classified from that solution"),
    MODEL_SOLVED: ("STUDY_GUIDE_HOLE.md",
                   "the 120 problems the archive has no solution for (1985-1994), "
                   "classified from a solution the model worked out itself -- "
                   "unverified, and wrong somewhere"),
    "all": ("STUDY_GUIDE_TOTAL.md",
            "every classified problem, archive-solved and model-solved together"),
}


def load(db_path=DB_PATH, basis="all"):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    sql = ("SELECT p.session || p.number AS pos, t.topic, t.domain, t.subdomains, "
           "       t.proof_method "
           "FROM problem_topics t JOIN problems p ON p.id = t.problem_id")
    args = []
    if basis != "all":
        sql += " WHERE t.basis = ?"
        args.append(basis)
    rows = [dict(r) for r in con.execute(sql, args)]
    con.close()
    for row in rows:
        row["subdomains"] = json.loads(row["subdomains"])
        row["entry"] = row["pos"] in ENTRY_POSITIONS
    return rows


def counts(rows, key):
    """(entry count, total count) per label, where key yields zero or more."""
    everywhere, entry = Counter(), Counter()
    for row in rows:
        labels = key(row)
        everywhere.update(labels)
        if row["entry"]:
            entry.update(labels)
    return {label: (entry[label], everywhere[label]) for label in everywhere}


def rank(pairs, names):
    """Names ordered by entry count, then total, then alphabetically."""
    return sorted(names, key=lambda n: (-pairs.get(n, (0, 0))[0],
                                        -pairs.get(n, (0, 0))[1], n))


def build(rows, basis="all"):
    topics = counts(rows, lambda r: [r["topic"]])
    domains = counts(rows, lambda r: [r["domain"]])
    subs = counts(rows, lambda r: r["subdomains"])
    methods = counts(rows, lambda r: [r["proof_method"]] if r["proof_method"] else [])

    entry_total = sum(1 for r in rows if r["entry"])
    out = [
        "# Putnam study guide",
        "",
        f"Drawn from {SHEETS[basis][1]}.",
        "",
        f"Ranked from {len(rows)} classified problems, {entry_total} of them in the "
        "four entry positions A1, A2, B1 and B2.",
        "",
        "- **entry** -- appearances among A1/A2/B1/B2, the problems you will reach",
        "- **all** -- appearances anywhere on the exam",
        "",
        "The ranking follows *entry* at every level: topics first, then the domains "
        "inside each topic, then the sub-domains inside each domain. Where *all* runs "
        "far ahead of *entry*, the subject is real but lives in the hard half.",
        "",
        "Sub-domain counts are per tag rather than per problem, so a problem filed "
        "under two of them is counted in both.",
        "",
    ]

    # one label column for the whole sheet, so every block lines up
    width = max([len(d) for d in DOMAINS] + [len(s) + 4 for s in SUBDOMAINS])

    for i, topic in enumerate(rank(topics, TAXONOMY), 1):
        t_entry, t_all = topics.get(topic, (0, 0))
        share = t_entry / entry_total if entry_total else 0
        out.append(f"## {i}. {topic} — {t_entry} entry ({share:.0%}), {t_all} all")
        out.append("")

        block = [f"{'':<{width}} {'entry':>5} {'all':>5}"]
        for domain in rank(domains, TAXONOMY[topic]):
            d_entry, d_all = domains.get(domain, (0, 0))
            block.append(f"{domain:<{width}} {d_entry:>5} {d_all:>5}")
            for sub in rank(subs, TAXONOMY[topic][domain]):
                s_entry, s_all = subs.get(sub, (0, 0))
                marker = "  " if s_all else "· "     # never seen anywhere
                block.append(f"  {marker}{sub:<{width - 4}} {s_entry:>5} {s_all:>5}")
            block.append("")
        out.append("```")
        out.extend(line.rstrip() for line in block[:-1])
        out.append("```")
        out.append("")

    out.append("## Proof methods")
    out.append("")
    out.append("```")
    out.append(f"{'':<40} {'entry':>5} {'all':>5}")
    for method in rank(methods, PROOF_METHODS):
        m_entry, m_all = methods.get(method, (0, 0))
        out.append(f"{method:<40} {m_entry:>5} {m_all:>5}")
    untagged = sum(1 for r in rows if not r["proof_method"])
    untagged_entry = sum(1 for r in rows if r["entry"] and not r["proof_method"])
    out.append(f"{'(no method tagged)':<40} {untagged_entry:>5} {untagged:>5}")
    out.append("```")
    out.append("")

    never = [s for s in SUBDOMAINS if not subs.get(s, (0, 0))[1]]
    out.append("## Never seen")
    out.append("")
    out.append(f"{len(never)} of the {len(SUBDOMAINS)} sub-domains do not appear on a "
               "single classified problem, at any difficulty. Marked `·` above.")
    out.append("")
    out.extend(f"- {s}" for s in sorted(never))
    out.append("")
    out.append("Read these as *not the primary technique of any problem*, not as "
               "useless: Fermat's little theorem, for one, gets absorbed into "
               "Modular Arithmetic rather than tagged on its own.")
    out.append("")
    return "\n".join(out)


def check(rows):
    """Every total the sheet prints, recomputed a second way."""
    entry = [r for r in rows if r["entry"]]
    problems = len(rows)
    topics = counts(rows, lambda r: [r["topic"]])
    domains = counts(rows, lambda r: [r["domain"]])

    assert sum(e for e, _ in topics.values()) == len(entry), "topic entry counts"
    assert sum(a for _, a in topics.values()) == problems, "topic totals"
    assert sum(e for e, _ in domains.values()) == len(entry), "domain entry counts"
    assert sum(a for _, a in domains.values()) == problems, "domain totals"

    # every domain sits under exactly the topic the taxonomy gives it
    for row in rows:
        assert row["domain"] in TAXONOMY[row["topic"]], f"{row['domain']} / {row['topic']}"
        for sub in row["subdomains"]:
            assert sub in TAXONOMY[row["topic"]][row["domain"]], sub
        assert 1 <= len(row["subdomains"]) <= 3, row["subdomains"]

    # each topic's domain counts add back up to the topic's own
    for topic, subtree in TAXONOMY.items():
        for column in (0, 1):
            got = sum(domains.get(d, (0, 0))[column] for d in subtree)
            assert got == topics.get(topic, (0, 0))[column], f"{topic} column {column}"
    return len(entry), problems


def write(basis, db_path=DB_PATH):
    name, _ = SHEETS[basis]
    rows = load(db_path, basis)
    if not rows:
        print(f"{name}: nothing classified on this basis yet -- skipped")
        return
    entry, problems = check(rows)
    Path(__file__).with_name(name).write_text(build(rows, basis))
    print(f"{name}: {problems} problems, {entry} in entry positions")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--basis", choices=list(SHEETS), default=ARCHIVE,
                    help="which population to rank (default archive)")
    ap.add_argument("--all", action="store_true", help="write all three sheets")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    for basis in (list(SHEETS) if args.all else [args.basis]):
        write(basis, args.db)
