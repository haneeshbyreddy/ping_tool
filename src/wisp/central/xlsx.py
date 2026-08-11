from __future__ import annotations

import zipfile
from datetime import date, datetime
from io import BytesIO

_EPOCH = datetime(1899, 12, 30)

_CELL_MAX = 32767

_S_BODY, _S_HEAD, _S_DATE = 0, 1, 2

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"

_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _text(value) -> str:
    s = str(value)
    s = "".join(ch for ch in s
                if ch in "\t\n\r" or ord(ch) >= 0x20)
    if len(s) > _CELL_MAX:
        s = s[:_CELL_MAX - 1] + "…"
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _col_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _serial(value: datetime | date) -> float:
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=None) if value.tzinfo else value
    else:
        dt = datetime(value.year, value.month, value.day)
    return (dt - _EPOCH).total_seconds() / 86400.0


class Column:
    def __init__(self, key: str, title: str, *, width_cap: float = 60.0) -> None:
        self.key = key
        self.title = title
        self.width_cap = width_cap


def _sheet_name(raw: str) -> str:
    name = "".join("-" if ch in "[]:*?/\\" else ch for ch in str(raw or ""))
    return name.strip()[:31] or "Sheet1"


def _cell(ref: str, value, style: int) -> str:
    if isinstance(value, (datetime, date)):
        return f'<c r="{ref}" s="{_S_DATE}"><v>{_serial(value):.6f}</v></c>'
    if isinstance(value, bool):
        value = "yes" if value else "no"
    elif isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = _text("" if value is None else value)
    if not text:
        return f'<c r="{ref}" s="{style}"/>'
    return (f'<c r="{ref}" s="{style}" t="inlineStr">'
            f'<is><t xml:space="preserve">{text}</t></is></c>')


def _widths(columns: list[Column], rows: list[dict]) -> list[float]:
    out = []
    for c in columns:
        widest = len(c.title)
        for r in rows:
            val = r.get(c.key)
            if isinstance(val, (datetime, date)):
                widest = max(widest, 22)
            elif val is not None:
                widest = max(widest, len(str(val)))
        out.append(min(max(widest + 2.0, 8.0), c.width_cap))
    return out


def _styles() -> str:
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
    if rows:
        parts.append(f'<autoFilter ref="A1:{last_col}{last_row}"/>')
    parts.append('</worksheet>')
    return "".join(parts)


def table_xlsx(*, sheet_name: str, columns: list[Column],
               rows: list[dict]) -> bytes:
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
