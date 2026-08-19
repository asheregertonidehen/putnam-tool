#!/usr/bin/env python3
"""STUDY_GUIDE_TOTAL as a spreadsheet, for ticking things off.

    python3 sheet.py            # writes STUDY_GUIDE_TOTAL.xlsx

Same rows as the markdown sheet and in the same order -- topics ranked, domains
ranked inside each topic, sub-domains inside each domain -- flattened one per
line with a Level column so a filter can pull out just the sub-domains.  The
Studied column is left empty; that one is yours.

The .xlsx is assembled by hand from stdlib zipfile, which keeps this project
free of dependencies.  It is the minimum Excel accepts: no styles, no shared
strings, every cell inline.
"""

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from classify import DB_PATH
from guide import counts, load, rank
from taxonomy import TAXONOMY

OUT_PATH = Path(__file__).with_name("STUDY_GUIDE_TOTAL.xlsx")

HEADERS = ["Level", "Topic", "Domain", "Sub-domain", "Entry", "All", "Studied"]
WIDTHS = [10, 30, 52, 56, 8, 8, 12]


def guide_rows(rows):
    """The whole ranked tree, flattened, in the order the markdown prints it."""
    topics = counts(rows, lambda r: [r["topic"]])
    domains = counts(rows, lambda r: [r["domain"]])
    subs = counts(rows, lambda r: r["subdomains"])

    out = []
    for topic in rank(topics, TAXONOMY):
        t_entry, t_all = topics.get(topic, (0, 0))
        out.append(["Topic", topic, "", "", t_entry, t_all, ""])
        for domain in rank(domains, TAXONOMY[topic]):
            d_entry, d_all = domains.get(domain, (0, 0))
            out.append(["Domain", topic, domain, "", d_entry, d_all, ""])
            for sub in rank(subs, TAXONOMY[topic][domain]):
                s_entry, s_all = subs.get(sub, (0, 0))
                out.append(["Sub-domain", topic, domain, sub, s_entry, s_all, ""])
    return out


# --------------------------------------------------------------------------
# the smallest workbook Excel will open
# --------------------------------------------------------------------------

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Study guide" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""


def column_name(index):
    """0 -> A, 25 -> Z, 26 -> AA."""
    name = ""
    while True:
        name = chr(ord("A") + index % 26) + name
        index = index // 26 - 1
        if index < 0:
            return name


def cell(ref, value):
    if isinstance(value, int):
        return f'<c r="{ref}"><v>{value}</v></c>'
    if value == "":
        return ""                                   # an empty cell is no cell
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def sheet_xml(table):
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(WIDTHS)
    )
    rows = []
    for r, values in enumerate(table, 1):
        cells = "".join(cell(f"{column_name(i)}{r}", v) for i, v in enumerate(values))
        rows.append(f'<row r="{r}">{cells}</row>')
    last = f"{column_name(len(HEADERS) - 1)}{len(table)}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        f'<cols>{cols}</cols>'
        f'<sheetData>{"".join(rows)}</sheetData>'
        f'<autoFilter ref="A1:{last}"/>'
        '</worksheet>'
    )


def write_xlsx(table, path=OUT_PATH):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", CONTENT_TYPES)
        book.writestr("_rels/.rels", ROOT_RELS)
        book.writestr("xl/workbook.xml", WORKBOOK)
        book.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        book.writestr("xl/worksheets/sheet1.xml", sheet_xml(table))
    return path


if __name__ == "__main__":
    rows = load(DB_PATH, "all")
    if not rows:
        raise SystemExit("nothing classified yet -- run: python3 classify.py")
    table = [HEADERS] + guide_rows(rows)
    write_xlsx(table)
    print(f"{OUT_PATH.name}: {len(table) - 1} rows from {len(rows)} problems")
