# Putnam Tool

Tools for collecting, classifying, and studying Putnam problems.

## Setup

The project uses Python 3 and `myputnam.db`.

Set an OpenRouter API key before running the classification pipelines:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

## Commands

```bash
python3 app.py
python3 classify.py
python3 solve.py
python3 guide.py --all
python3 sheet.py
python3 fetch_papers.py
```

- `app.py` starts the local Putnam practice site.
- `classify.py` classifies problems with archived solutions.
- `solve.py` solves and classifies problems without archived solutions.
- `guide.py --all` creates the study guides.
- `sheet.py` creates the Excel study tracker.
- `fetch_papers.py` downloads the arXiv links in `papers.txt`.

Run a classification smoke test before a full run:

```bash
python3 classify.py --smoke --dry-run
python3 solve.py --smoke --dry-run
```
