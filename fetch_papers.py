#!/usr/bin/env python3
"""Download every arXiv paper listed in papers.txt.

    python3 fetch_papers.py                 # -> "downloaded papers/"
    python3 fetch_papers.py mylist.txt      # some other list

One paper per line, in whatever form is handy -- the id, an abs link, a pdf
link, a versioned id, with or without the "arXiv:" prefix:

    2401.12345
    arXiv:2401.12345v2
    https://arxiv.org/abs/math.CO/0601001
    https://arxiv.org/pdf/2401.12345

Blank lines and lines starting with # are ignored, as is anything after the id
on a line, so you can keep titles next to them.  Files already downloaded are
skipped, which makes a re-run a cheap way to pick up whatever failed.

arXiv asks for a few seconds between requests; that is what DELAY is for.
"""

import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT_DIR = Path(__file__).with_name("downloaded papers")
LIST_PATH = Path(__file__).with_name("papers.txt")

DELAY = 3          # seconds between downloads, per arXiv's request
TIMEOUT = 30       # per socket read: how long a silent connection is tolerated
DEADLINE = 150     # per paper, wall clock: catches a connection that trickles
RETRIES = 3

# 2401.12345 / 2401.12345v2, and the pre-2007 form math.CO/0601001
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?)|([a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)",
                      re.IGNORECASE)


def read_ids(path):
    """Every arXiv id in the file, in order, without repeats."""
    ids, seen = [], set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        found = ARXIV_ID.search(line)
        if not found:
            print(f"  no arXiv id on line: {line[:70]}")
            continue
        paper = found.group(0)
        if paper not in seen:
            seen.add(paper)
            ids.append(paper)
    return ids


def read_with_deadline(request):
    """Read the whole response, giving up if it takes absurdly long.

    urlopen's timeout is per socket operation, so a CDN edge that dribbles a
    few bytes every so often resets it forever and the download hangs with the
    connection still ESTABLISHED.  A wall-clock deadline across the whole read
    is what actually bounds it; abandoning the socket and retrying lands on a
    different edge, which is generally healthy.
    """
    started = time.monotonic()
    chunks, got = [], 0
    with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            got += len(chunk)
            if time.monotonic() - started > DEADLINE:
                raise TimeoutError(
                    f"stalled after {DEADLINE}s with {got / 1e6:.1f} MB read")


def download(paper, out_dir):
    """-> True if a file landed, False if it was already there."""
    target = out_dir / (paper.replace("/", "_") + ".pdf")
    if target.exists() and target.stat().st_size:
        return False

    url = f"https://arxiv.org/pdf/{paper}"
    request = urllib.request.Request(url, headers={
        # arXiv blocks the default urllib agent
        "User-Agent": "putnam-tool paper fetcher (mailto:cancernomoreafrica@gmail.com)",
    })
    for attempt in range(RETRIES):
        try:
            body = read_with_deadline(request)
            if not body.startswith(b"%PDF"):
                raise ValueError("that was not a PDF")
            target.write_bytes(body)
            return True
        except (urllib.error.URLError, ValueError, TimeoutError) as e:
            if attempt == RETRIES - 1:
                raise RuntimeError(f"{e}") from None
            time.sleep(2 ** attempt)


def main():
    list_path = Path(sys.argv[1]) if len(sys.argv) > 1 else LIST_PATH
    if not list_path.exists():
        sys.exit(f"{list_path} not found")

    ids = read_ids(list_path)
    if not ids:
        sys.exit(f"no arXiv ids found in {list_path.name} -- one per line, "
                 f"e.g. 2401.12345 or https://arxiv.org/abs/2401.12345")

    OUT_DIR.mkdir(exist_ok=True)
    print(f"{len(ids)} papers -> {OUT_DIR.name}/", flush=True)

    got = skipped = failed = 0
    for i, paper in enumerate(ids, 1):
        try:
            fresh = download(paper, OUT_DIR)
        except RuntimeError as e:
            failed += 1
            print(f"  [{i}/{len(ids)}] {paper:24} FAILED  {e}", flush=True)
            continue
        if fresh:
            got += 1
            print(f"  [{i}/{len(ids)}] {paper:24} ok", flush=True)
            if i < len(ids):
                time.sleep(DELAY)
        else:
            skipped += 1
            print(f"  [{i}/{len(ids)}] {paper:24} already had it", flush=True)

    print(f"\ndownloaded {got}, already had {skipped}, failed {failed}")


if __name__ == "__main__":
    main()
