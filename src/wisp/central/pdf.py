"""A minimal, dependency-free PDF table writer.

Central is pure stdlib (see CLAUDE.md) and that is not negotiable for a report
button, so this writes the PDF by hand rather than pulling in reportlab. It does
exactly one thing: a paginated landscape table with a title block, column
headings and a page footer — enough for "export this list", nothing more.

Scope, deliberately small:
  * three base-14 fonts (Helvetica, Helvetica-Bold, Courier) so nothing is
    embedded and the file stays a few KB;
  * WinAnsi (cp1252) text only — anything outside it degrades to '?' rather than
    writing a byte the viewer would render as mojibake (see `_winansi`);
  * no wrapping: a cell too wide for its column is truncated with an ellipsis,
    because a table whose row height varies is a layout engine, and this isn't
    one. Columns are sized by the caller, who knows which one deserves the room.

The one thing that must be exactly right is the xref table: every object's byte
offset is recorded as it is written, so `build` assembles the file in one pass
and no offset is ever guessed.
"""
from __future__ import annotations

# A4 landscape, in points.
PAGE_W, PAGE_H = 842.0, 595.0
MARGIN = 36.0

# Helvetica advance widths, per 1000 units of font size, for the printable ASCII
# range — the real metrics, so truncation lands where the viewer draws it rather
# than a character or two off. Non-ASCII falls back to the average.
_W = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "  # 32-47
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "  # 48-63
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "  # 64-79
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "   # 80-95
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "   # 96-111
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"        # 112-126
).split()
_WIDTHS = {chr(32 + i): int(w) for i, w in enumerate(_W)}
_FALLBACK_W = 556


def text_width(s: str, size: float) -> float:
    return sum(_WIDTHS.get(ch, _FALLBACK_W) for ch in s) * size / 1000.0


def _winansi(s: str) -> str:
    """Fold to WinAnsi — cp1252, which is what the fonts above declare, NOT
    latin-1. The difference is the 0x80–0x9F band: cp1252 has the typographic
    characters this app's own strings are full of (em dash, middot, ellipsis,
    curly quotes) and latin-1 does not, so folding to latin-1 printed a report
    titled "Open issues ? ispA".

    A device name carrying a genuinely unencodable glyph still degrades to '?' —
    visibly missing beats a corrupt file or an exception raised inside a
    download."""
    out = []
    for ch in str(s):
        try:
            ch.encode("cp1252")
        except UnicodeEncodeError:
            out.append("?")
        else:
            out.append(ch)
    return "".join(out)


def _esc(s: str) -> str:
    return (_winansi(s).replace("\\", r"\\").replace("(", r"\(")
            .replace(")", r"\)").replace("\r", " ").replace("\n", " "))


def fit(s: str, size: float, width: float, *, mono: bool = False) -> str:
    """`s` truncated with an ellipsis so it draws inside `width`.

    `mono` measures against Courier's single advance — a mono cell measured with
    the Helvetica table fits a couple of characters too many and overruns its
    column, which on a MAC address means a report that looks subtly wrong."""
    s = str(s or "")
    char_w = (lambda ch: _MONO_W * size) if mono else \
        (lambda ch: _WIDTHS.get(ch, _FALLBACK_W) * size / 1000.0)
    if sum(char_w(ch) for ch in s) <= width:
        return s
    ell = "..."
    room = width - sum(char_w(ch) for ch in ell)
    if room <= 0:
        return ""
    out = []
    used = 0.0
    for ch in s:
        w = char_w(ch)
        if used + w > room:
            break
        out.append(ch)
        used += w
    return "".join(out).rstrip() + ell


class Column:
    """One table column.

    `weight` is NOT the width. Widths are measured from the actual content (see
    `_solve_widths`); weight only decides who wins when the content doesn't fit,
    i.e. which column keeps its room and which gets truncated."""

    def __init__(self, key: str, title: str, weight: float, *,
                 mono: bool = False) -> None:
        self.key = key
        self.title = title
        self.weight = weight
        self.mono = mono


# Courier is 600/1000 for every glyph — one number, no table needed.
_MONO_W = 600 / 1000.0
_PAD = 6.0  # 3pt of breathing room each side of a cell


def _cell(row: dict, col: Column) -> str:
    """The string a cell renders. Used by BOTH the measuring pass and the drawing
    pass, so a column can never be sized against text it doesn't end up showing."""
    val = row.get(col.key)
    return "-" if val is None or val == "" else str(val)


def _measure(s: str, col: Column, size: float) -> float:
    return (len(s) * _MONO_W * size) if col.mono else text_width(s, size)


def _solve_widths(columns: list[Column], rows: list[dict], avail: float,
                  size: float) -> list[float]:
    """Give every column the width its widest cell actually needs, and share out
    what's left (or what's missing) from there.

    Proportional-to-weight sizing — the obvious approach — gets a report exactly
    wrong: it hands a fixed share to a column whose values are all short (a `Rx
    -28.86 dBm` detail sitting on 180pt of white) while truncating the identifier
    a tech is reading the report FOR (a 17-character MAC plus its PON). Both
    happened in the first cut of the issues export.

    So: measure first. When everything fits, the surplus is shared in proportion
    to need, which keeps a wide column wide without letting it hoard. When it
    doesn't fit, small columns are satisfied outright and only the genuinely
    hungry ones (`weight` breaks that tie) absorb the shortfall — a
    water-filling pass, so one long free-text column can never starve five
    narrow ones."""
    need = []
    for c in columns:
        # the heading is drawn BOLD, which runs a little wider than the metrics
        # table above (they're the regular face) — hence the small allowance
        widest = text_width(c.title, size) * 1.06
        for r in rows:
            widest = max(widest, _measure(_cell(r, c), c, size))
        need.append(widest + _PAD)
    total = sum(need)
    if total <= 0:
        return [avail / max(1, len(columns))] * len(columns)
    if total <= avail:
        extra = avail - total
        return [n + extra * n / total for n in need]

    # Contended: satisfy what fits inside a fair share, then split the rest by
    # weight among the columns that are still hungry.
    widths = [0.0] * len(columns)
    left = set(range(len(columns)))
    remaining = avail
    while left:
        fair = remaining / len(left)
        small = {i for i in left if need[i] <= fair}
        if not small:
            wsum = sum(columns[i].weight for i in left) or float(len(left))
            for i in left:
                widths[i] = remaining * (columns[i].weight / wsum)
            break
        for i in small:
            widths[i] = need[i]
            remaining -= need[i]
        left -= small
    return widths


def table_pdf(*, title: str, subtitle: str, columns: list[Column],
              rows: list[dict], footer: str = "",
              title_size: float = 15.0, body_size: float = 8.5) -> bytes:
    """Render `rows` as a paginated table. Cell values are read by column key and
    stringified; an absent or empty value renders as "-", so a blank cell reads as
    deliberately empty rather than as a rendering slip."""
    avail = PAGE_W - 2 * MARGIN
    widths = _solve_widths(columns, rows, avail, body_size)
    xs, x = [], MARGIN
    for w in widths:
        xs.append(x)
        x += w

    row_h = body_size + 6.0
    head_h = 16.0
    top = PAGE_H - MARGIN
    first_body_top = top - 46.0      # title block only exists on page 1
    later_body_top = top - 18.0
    bottom = MARGIN + 16.0

    pages: list[list[dict]] = []
    cur: list[dict] = []
    body_top = first_body_top
    room = int((body_top - head_h - bottom) // row_h)
    for r in rows:
        if len(cur) >= max(1, room):
            pages.append(cur)
            cur = []
            body_top = later_body_top
            room = int((body_top - head_h - bottom) // row_h)
        cur.append(r)
    pages.append(cur)  # always at least one page, even with no rows

    streams = []
    for page_no, page_rows in enumerate(pages, start=1):
        ops: list[str] = []
        y = later_body_top if page_no > 1 else first_body_top
        if page_no == 1:
            ops.append("BT /F2 %.1f Tf %.1f %.1f Td (%s) Tj ET"
                       % (title_size, MARGIN, top - title_size, _esc(title)))
            if subtitle:
                ops.append("BT /F1 9 Tf %.1f %.1f Td (%s) Tj ET"
                           % (MARGIN, top - title_size - 14, _esc(subtitle)))
        # heading band: a filled rule under the labels, matching the app's
        # "the header frames the rows" table treatment
        ops.append("0.90 0.90 0.90 rg %.1f %.1f %.1f %.1f re f"
                   % (MARGIN, y - head_h + 3, avail, head_h - 2))
        ops.append("0.10 0.10 0.10 rg")
        for col, cx, cw in zip(columns, xs, widths):
            ops.append("BT /F2 %.1f Tf %.1f %.1f Td (%s) Tj ET"
                       % (body_size, cx + 3, y - head_h + 8,
                          _esc(fit(col.title, body_size, cw - _PAD))))
        y -= head_h + 2
        for i, r in enumerate(page_rows):
            if i % 2 == 1:
                ops.append("0.965 0.965 0.965 rg %.1f %.1f %.1f %.1f re f"
                           % (MARGIN, y - row_h + 4, avail, row_h))
            ops.append("0.10 0.10 0.10 rg")
            for col, cx, cw in zip(columns, xs, widths):
                font = "/F3" if col.mono else "/F1"
                ops.append("BT %s %.1f Tf %.1f %.1f Td (%s) Tj ET"
                           % (font, body_size, cx + 3, y - body_size + 1,
                              _esc(fit(_cell(r, col), body_size, cw - _PAD,
                                       mono=col.mono))))
            y -= row_h
        foot = f"{footer}   ·   page {page_no} of {len(pages)}" if footer \
            else f"page {page_no} of {len(pages)}"
        ops.append("0.45 0.45 0.45 rg")
        ops.append("BT /F1 7.5 Tf %.1f %.1f Td (%s) Tj ET"
                   % (MARGIN, MARGIN, _esc(foot)))
        streams.append("\n".join(ops).encode("cp1252", "replace"))

    return _build(streams)


def _build(streams: list[bytes]) -> bytes:
    """Assemble page content streams into a PDF file.

    Object layout: 1 Catalog, 2 Pages, 3 Font(Helvetica), 4 Font(Helvetica-Bold),
    5 Font(Courier), then a Page + Contents pair per page. Offsets are captured as
    each object is appended — the xref must describe the bytes we actually wrote,
    and computing it any other way is how a hand-built PDF ends up unopenable."""
    n_pages = len(streams)
    first_page_obj = 6
    kids = " ".join(f"{first_page_obj + 2 * i} 0 R" for i in range(n_pages))

    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (f"<< /Type /Pages /Count {n_pages} /Kids [{kids}] >>").encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
        b"/Encoding /WinAnsiEncoding >>",
    ]
    for i, stream in enumerate(streams):
        content_obj = first_page_obj + 2 * i + 1
        objs.append((
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}]"
            f" /Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >>"
            f" /Contents {content_obj} 0 R >>").encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                    + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)
