import os
import re
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))

from wisp.central import pdf


COLS = [
    pdf.Column("a", "Alpha", 1.0),
    pdf.Column("b", "Beta", 2.0, mono=True),
]


def _pdf(rows, **kw):
    return pdf.table_pdf(title="Report", subtitle="sub", columns=COLS,
                         rows=rows, footer="foot", **kw)


class StructureTest(unittest.TestCase):
    def _xref(self, blob: bytes):
        start = int(re.search(rb"startxref\s+(\d+)", blob[-200:]).group(1))
        lines = blob[start:].split(b"\n")
        self.assertEqual(lines[0], b"xref")
        count = int(lines[1].split()[1])
        return count, [ln.split() for ln in lines[2:2 + count]]

    def test_every_xref_offset_points_at_its_own_object(self):
        blob = _pdf([{"a": "one", "b": "two"}])
        count, entries = self._xref(blob)
        self.assertEqual(entries[0][2], b"f")
        for num, entry in enumerate(entries):
            if entry[2] == b"f":
                continue
            offset = int(entry[0])
            self.assertTrue(blob[offset:].startswith(f"{num} 0 obj".encode()),
                            f"object {num} is not at its recorded offset")
        self.assertIn(b"/Size %d" % count, blob)

    def test_header_and_trailer(self):
        blob = _pdf([])
        self.assertTrue(blob.startswith(b"%PDF-1.4"))
        self.assertTrue(blob.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Root 1 0 R", blob)

    def test_an_empty_list_still_produces_one_page(self):
        blob = _pdf([])
        self.assertIn(b"/Count 1", blob)

    def test_long_lists_paginate(self):
        one = _pdf([{"a": "x", "b": "y"}])
        many = _pdf([{"a": "x", "b": "y"}] * 200)
        self.assertEqual(re.search(rb"/Count (\d+)", one).group(1), b"1")
        self.assertGreater(int(re.search(rb"/Count (\d+)", many).group(1)), 3)
        self.assertIn(b"page 1 of ", many)


class WidthTest(unittest.TestCase):

    def _need(self, col, rows, size=8.5):
        return max([pdf.text_width(col.title, size) * 1.06]
                   + [pdf._measure(pdf._cell(r, col), col, size) for r in rows]
                   ) + pdf._PAD

    def test_a_short_column_does_not_hoard_width(self):
        narrow = pdf.Column("s", "S", 1.0)
        greedy = pdf.Column("l", "L", 9.0)
        rows = [{"s": "N" * 40, "l": "x"}]
        w_narrow, w_greedy = pdf._solve_widths([narrow, greedy], rows, 400.0, 8.5)
        self.assertGreater(w_narrow, w_greedy)
        self.assertGreaterEqual(w_narrow, self._need(narrow, rows))

    def test_every_column_is_satisfied_when_the_content_fits(self):
        cols = [pdf.Column("a", "Alpha", 1.0), pdf.Column("b", "Beta", 3.0)]
        rows = [{"a": "short", "b": "also short"}]
        widths = pdf._solve_widths(cols, rows, 600.0, 8.5)
        for col, width in zip(cols, widths):
            self.assertGreaterEqual(width, self._need(col, rows))
        self.assertAlmostEqual(sum(widths), 600.0, places=3)

    def test_weight_decides_who_absorbs_a_shortfall(self):
        light = pdf.Column("a", "A", 1.0)
        heavy = pdf.Column("b", "B", 3.0)
        rows = [{"a": "N" * 80, "b": "N" * 80}]
        w_light, w_heavy = pdf._solve_widths([light, heavy], rows, 200.0, 8.5)
        self.assertGreater(w_heavy, w_light)
        self.assertAlmostEqual(w_light + w_heavy, 200.0, places=3)

    def test_one_long_column_never_starves_the_narrow_ones(self):
        cols = [pdf.Column(f"c{i}", f"C{i}", 1.0) for i in range(5)]
        cols.append(pdf.Column("free", "Free", 4.0))
        rows = [{**{f"c{i}": "abc" for i in range(5)}, "free": "N" * 300}]
        widths = pdf._solve_widths(cols, rows, 500.0, 8.5)
        for col, width in zip(cols[:5], widths[:5]):
            self.assertGreaterEqual(width, self._need(col, rows))
        self.assertAlmostEqual(sum(widths), 500.0, places=3)

    def test_an_empty_table_still_yields_usable_widths(self):
        widths = pdf._solve_widths(COLS, [], 700.0, 8.5)
        self.assertEqual(len(widths), 2)
        self.assertAlmostEqual(sum(widths), 700.0, places=3)

    def test_mono_cells_are_measured_against_courier(self):
        mac = "00:D3:9E:17:21:9E"
        self.assertEqual(pdf.fit(mac, 8.5, 60, mono=True)[-3:], "...")
        self.assertLessEqual(
            len(pdf.fit(mac, 8.5, 60, mono=True)) * pdf._MONO_W * 8.5, 60 + 0.01)


class TextTest(unittest.TestCase):

    def test_parens_and_backslashes_are_escaped(self):
        blob = _pdf([{"a": r"Gi1/0/24 (uplink)", "b": "C:\\path"}])
        self.assertIn(rb"Gi1/0/24 \(uplink\)", blob)
        self.assertIn(rb"C:\\path", blob)
        self.assertNotIn(b"(uplink)) Tj", blob)

    def test_unencodable_glyphs_degrade_instead_of_raising(self):
        blob = _pdf([{"a": "OLT-\u0928\u0917\u0930", "b": "ok"}])
        self.assertIn(b"OLT-???", blob)

    def test_typographic_characters_survive(self):
        blob = pdf.table_pdf(title="Open issues \u2014 ispA",
                             subtitle="3 issue(s) \u00b7 generated now",
                             columns=COLS, rows=[{"a": "a\u2026b", "b": "ok"}],
                             footer="WISP Central")
        self.assertIn("Open issues \u2014 ispA".encode("cp1252"), blob)
        self.assertIn("a\u2026b".encode("cp1252"), blob)
        self.assertNotIn(b"?", blob)

    def test_newlines_never_reach_the_content_stream(self):
        blob = _pdf([{"a": "line1\nline2", "b": "r\ns"}])
        self.assertIn(b"line1 line2", blob)

    def test_empty_cells_render_as_a_dash(self):
        blob = _pdf([{"a": None, "b": ""}])
        self.assertIn(b"(-) Tj", blob)

    def test_fit_truncates_to_the_column_width(self):
        wide = "N" * 200
        self.assertTrue(pdf.fit(wide, 8.5, 60).endswith("..."))
        self.assertLessEqual(pdf.text_width(pdf.fit(wide, 8.5, 60), 8.5), 60)
        self.assertEqual(pdf.fit("short", 8.5, 200), "short")

    def test_text_width_uses_real_helvetica_metrics(self):
        self.assertLess(pdf.text_width("i" * 10, 10), pdf.text_width("W" * 10, 10))


if __name__ == "__main__":
    unittest.main()
