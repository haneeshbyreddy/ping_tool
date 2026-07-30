"""A minimal, dependency-free .xlsx table writer.

Same reasoning as `central/pdf.py`: central is pure stdlib, so rather than add
openpyxl for one export button this writes the OOXML package by hand —
`zipfile` and string formatting are all it takes. A REAL .xlsx, not a CSV wearing
the name: an operator who asks for Excel wants to sort, filter and pivot, and a
CSV gives them a re-import dialog and text-typed dates.

What it produces: one sheet, a bold frozen header row, an autofilter over the
whole table, per-column widths measured from the content, and **real date cells**
for timestamps — so sorting by "Since" orders by time instead of alphabetically,
which is the entire reason to hand someone a spreadsheet.

Deliberately absent: shared strings (inline strings cost a few bytes and remove a
whole index to keep consistent), merged cells, formulas, charts, multiple sheets.

Cell values may be `str`, `int`, `float`, `datetime` or None. A `datetime` is
written as an Excel serial with a date format; everything else is an inline
string. Nothing here may raise on real-world content — a name with a `&`, a
control character out of a firmware string, or a 40k-character detail all have to
come out as a file that opens.
"""
from __future__ import annotations

import zipfile
from datetime import date, datetime
from io import BytesIO

# Excel's day-zero. 1899-12-30, not 1900-01-01, because the format carries the
# 1900-leap-year bug and this offset is what cancels it for every date after
# 1900-03-01 — every timestamp this app produces.
_EPOCH = datetime(1899, 12, 30)

# Excel caps a cell at 32767 characters; past that the file is rejected outright
# rather than truncated, so the truncation happens here.
_CELL_MAX = 32767

# Style indices into the cellXfs table written by `_styles`.
_S_BODY, _S_HEAD, _S_DATE = 0, 1, 2

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"

_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _text(value) -> str:
    """XML-safe cell text. Strips the control characters XML 1.0 forbids — a
    firmware-sourced `if_alias` really does carry the odd 0x01, and one of those
    makes Excel call the whole workbook corrupt."""
    s = str(value)
    s = "".join(ch for ch in s
                if ch in "\t\n\r" or ord(ch) >= 0x20)
    if len(s) > _CELL_MAX:
        s = s[:_CELL_MAX - 1] + "…"
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _col_name(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _serial(value: datetime | date) -> float:
    """An Excel date serial. A tz-aware value is converted to naive first — the
    caller has already put it in the operator's zone (notifiers._wa_local), and a
    spreadsheet has no concept of an offset to carry."""
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=None) if value.tzinfo else value
    else:
        dt = datetime(value.year, value.month, value.day)
    return (dt - _EPOCH).total_seconds() / 86400.0


class Column:
    """One column: which key to read, what to head it, and how wide to let it
    grow. `width_cap` is in Excel's character units — a Detail column of free
    text would otherwise stretch to a screen and a half."""

    def __init__(self, key: str, title: str, *, width_cap: float = 60.0) -> None:
        self.key = key
        self.title = title
        self.width_cap = width_cap


def _sheet_name(raw: str) -> str:
    """Excel refuses `[]:*?/\\` in a sheet name and caps it at 31 characters. An
    org id reaches this, so it is sanitised rather than trusted."""
    name = "".join("-" if ch in "[]:*?/\\" else ch for ch in str(raw or ""))
    return name.strip()[:31] or "Sheet1"


def _cell(ref: str, value, style: int) -> str:
    if isinstance(value, (datetime, date)):
        return f'<c r="{ref}" s="{_S_DATE}"><v>{_serial(value):.6f}</v></c>'
    if isinstance(value, bool):  # before int — bool IS an int in Python
        value = "yes" if value else "no"
    elif isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = _text("" if value is None else value)
    if not text:
        return f'<c r="{ref}" s="{style}"/>'
    return (f'<c r="{ref}" s="{style}" t="inlineStr">'
            f'<is><t xml:space="preserve">{text}</t></is></c>')


def _widths(columns: list[Column], rows: list[dict]) -> list[float]:
    """Column widths in Excel character units, measured from content like the PDF
    does — the same lesson: a fixed width truncates the identifier someone opened
    the file to read while a short column sits on empty space."""
    out = []
    for c in columns:
        widest = len(c.title)
        for r in rows:
            val = r.get(c.key)
            if isinstance(val, (datetime, date)):
                widest = max(widest, 22)      # "26 Jul 2026 10:03 AM"
            elif val is not None:
                widest = max(widest, len(str(val)))
        out.append(min(max(widest + 2.0, 8.0), c.width_cap))
    return out


def _styles() -> str:
    # The two default fills (none, gray125) are mandatory — Excel repairs a file
    # that omits them. numFmtId 164 is the first id available to a custom format.
    return (
        f'{_DECL}<styleSheet xmlns="{_NS}">'
        '<numFmts count="1">'
        '<numFmt numFmtId="164" formatCode="dd mmm yyyy hh:mm AM/PM"/>'
        '</numFmts>'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="2">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '</fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        '</cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0"'
        ' applyNumberFormat="1"/>'
        '</cellXfs>'
        '</styleSheet>')


def _sheet(columns: list[Column], rows: list[dict]) -> str:
    last_col = _col_name(len(columns) - 1)
    last_row = len(rows) + 1
    parts = [
        _DECL, f'<worksheet xmlns="{_NS}">',
        f'<dimension ref="A1:{last_col}{last_row}"/>',
        # Freeze the header so scrolling 900 ONU rows keeps the column names.
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>',
        '<cols>',
    ]
    for i, w in enumerate(_widths(columns, rows), start=1):
        parts.append(f'<col min="{i}" max="{i}" width="{w:.2f}" customWidth="1"/>')
    parts.append('</cols><sheetData>')
    parts.append('<row r="1">' + "".join(
        _cell(f"{_col_name(i)}1", c.title, _S_HEAD)
        for i, c in enumerate(columns)) + '</row>')
    for n, row in enumerate(rows, start=2):
        parts.append(f'<row r="{n}">' + "".join(
            _cell(f"{_col_name(i)}{n}", row.get(c.key), _S_BODY)
            for i, c in enumerate(columns)) + '</row>')
    parts.append('</sheetData>')
    # autoFilter belongs AFTER sheetData in the schema's element order; before it,
    # Excel reports the file as needing repair. Only when there is data to filter.
    if rows:
        parts.append(f'<autoFilter ref="A1:{last_col}{last_row}"/>')
    parts.append('</worksheet>')
    return "".join(parts)


def table_xlsx(*, sheet_name: str, columns: list[Column],
               rows: list[dict]) -> bytes:
    """One sheet of `rows`, keyed by column. Deterministic: the same rows produce
    the same bytes (fixed zip timestamps), so re-exporting an unchanged fleet
    doesn't look like a changed report."""
    sheet = _sheet_name(sheet_name)
    parts = {
        "[Content_Types].xml":
            f'{_DECL}<Types xmlns="{_NS_CT}">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
            'package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels":
            f'{_DECL}<Relationships xmlns="{_NS_PKG}">'
            f'<Relationship Id="rId1" Type="{_NS_R}/officeDocument"'
            ' Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml":
            f'{_DECL}<workbook xmlns="{_NS}" xmlns:r="{_NS_R}">'
            f'<sheets><sheet name="{_text(sheet)}" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>',
        "xl/_rels/workbook.xml.rels":
            f'{_DECL}<Relationships xmlns="{_NS_PKG}">'
            f'<Relationship Id="rId1" Type="{_NS_R}/worksheet"'
            ' Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{_NS_R}/styles" Target="styles.xml"/>'
            '</Relationships>',
        "xl/styles.xml": _styles(),
        "xl/worksheets/sheet1.xml": _sheet(columns, rows),
    }
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in parts.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, body.encode("utf-8"))
    return buf.getvalue()
