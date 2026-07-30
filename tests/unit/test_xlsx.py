import io
import os
import sys
import unittest
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))

from wisp.central import xlsx

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

COLS = [
    xlsx.Column("a", "Alpha"),
    xlsx.Column("b", "Beta"),
    xlsx.Column("t", "When"),
]


def _book(rows, sheet_name="Issues"):
    return xlsx.table_xlsx(sheet_name=sheet_name, columns=COLS, rows=rows)


def _parts(blob):
    return zipfile.ZipFile(io.BytesIO(blob))


def _sheet(blob):
    return ElementTree.fromstring(
        _parts(blob).read("xl/worksheets/sheet1.xml").decode())


class PackageTest(unittest.TestCase):
    """A hand-built OOXML package fails the way a hand-built PDF does — Excel
    says "needs repair" and names nothing. These pin the parts and the element
    order that actually decide whether it opens."""

    def test_it_is_a_zip_with_every_required_part(self):
        names = set(_parts(_book([{"a": "x"}])).namelist())
        self.assertEqual(names, {
            "[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels", "xl/styles.xml",
            "xl/worksheets/sheet1.xml"})

    def test_every_part_is_well_formed_xml(self):
        zf = _parts(_book([{"a": "x", "b": 3, "t": datetime(2026, 7, 26, 10, 3)}]))
        for name in zf.namelist():
            ElementTree.fromstring(zf.read(name).decode())  # raises if malformed

    def test_the_relationships_point_at_the_parts_that_exist(self):
        zf = _parts(_book([{"a": "x"}]))
        root = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels").decode())
        targets = {r.get("Target") for r in root}
        self.assertEqual(targets, {"worksheets/sheet1.xml", "styles.xml"})

    def test_autofilter_comes_after_sheetdata(self):
        # Schema element order: before sheetData, Excel reports the file as
        # needing repair.
        sheet = _sheet(_book([{"a": "x"}]))
        tags = [child.tag.split("}")[1] for child in sheet]
        self.assertLess(tags.index("sheetData"), tags.index("autoFilter"))
        self.assertLess(tags.index("cols"), tags.index("sheetData"))
        self.assertLess(tags.index("sheetViews"), tags.index("cols"))

    def test_an_empty_list_omits_the_autofilter_but_still_opens(self):
        sheet = _sheet(_book([]))
        tags = [child.tag.split("}")[1] for child in sheet]
        self.assertNotIn("autoFilter", tags)
        self.assertIn("sheetData", tags)

    def test_the_zip_itself_is_intact(self):
        self.assertIsNone(_parts(_book([{"a": "x"}])).testzip())

    def test_declared_style_counts_match_the_children(self):
        # Excel repairs a workbook whose count attribute disagrees with the
        # element it counts — a silent, easy slip when styles.xml is hand-written.
        root = ElementTree.fromstring(
            _parts(_book([{"a": "x"}])).read("xl/styles.xml").decode())
        for tag in ("numFmts", "fonts", "fills", "borders", "cellStyleXfs",
                    "cellXfs"):
            node = root.find(f"m:{tag}", NS)
            self.assertEqual(int(node.get("count")), len(list(node)), tag)
        # and the style indices the writer uses all exist
        self.assertGreater(len(list(root.find("m:cellXfs", NS))),
                           max(xlsx._S_BODY, xlsx._S_HEAD, xlsx._S_DATE))

    def test_output_is_deterministic(self):
        rows = [{"a": "x", "b": "y"}]
        self.assertEqual(_book(rows), _book(rows))

    def test_the_header_row_is_frozen_and_bold(self):
        blob = _book([{"a": "x"}])
        sheet = _sheet(blob)
        pane = sheet.find(".//m:pane", NS)
        self.assertEqual(pane.get("state"), "frozen")
        self.assertEqual(pane.get("ySplit"), "1")
        head = sheet.findall(".//m:row", NS)[0]
        self.assertTrue(all(c.get("s") == str(xlsx._S_HEAD) for c in head))


class CellTest(unittest.TestCase):

    def _values(self, blob, row_index):
        row = _sheet(blob).findall(".//m:row", NS)[row_index]
        out = []
        for c in row:
            inline = c.find("m:is/m:t", NS)
            num = c.find("m:v", NS)
            out.append(inline.text if inline is not None
                       else (num.text if num is not None else None))
        return out

    def test_headings_and_values_land_in_the_right_cells(self):
        blob = _book([{"a": "one", "b": "two"}])
        self.assertEqual(self._values(blob, 0), ["Alpha", "Beta", "When"])
        self.assertEqual(self._values(blob, 1)[:2], ["one", "two"])

    def test_a_timestamp_is_a_real_date_cell_not_text(self):
        # THE reason to ship xlsx over CSV: sorting by "Since" has to order by
        # time, and a text stamp sorts alphabetically ("26 Jul" before "3 Aug").
        blob = _book([{"t": datetime(2026, 7, 26, 10, 3)}])
        cell = _sheet(blob).findall(".//m:row", NS)[1][2]
        self.assertIsNone(cell.get("t"))                    # numeric, not inlineStr
        self.assertEqual(cell.get("s"), str(xlsx._S_DATE))  # carries a date format
        self.assertAlmostEqual(float(cell.find("m:v", NS).text),
                               xlsx._serial(datetime(2026, 7, 26, 10, 3)), places=4)

    def test_an_aware_timestamp_keeps_its_local_wall_clock(self):
        # The caller has already converted to the operator's zone; a spreadsheet
        # has no offset to carry, so the serial must be the wall clock we were
        # handed and never a silent shift back to UTC.
        aware = datetime(2026, 7, 26, 10, 3, tzinfo=timezone.utc)
        self.assertEqual(xlsx._serial(aware),
                         xlsx._serial(datetime(2026, 7, 26, 10, 3)))

    def test_the_epoch_offset_matches_excels(self):
        # 1899-12-30, not 1900-01-01 — the offset that cancels the format's
        # 1900-leap-year bug. 2026-07-26 is day 46229.
        self.assertEqual(xlsx._serial(datetime(2026, 7, 26)), 46229.0)

    def test_xml_special_characters_are_escaped(self):
        blob = _book([{"a": 'R&D <switch> "core"'}])
        self.assertEqual(self._values(blob, 1)[0], 'R&D <switch> "core"')

    def test_control_characters_are_stripped(self):
        # A firmware-sourced if_alias really does carry the odd 0x01, and one of
        # those makes Excel call the whole workbook corrupt.
        blob = _book([{"a": "Gi1/0/1\x01\x07 uplink"}])
        self.assertEqual(self._values(blob, 1)[0], "Gi1/0/1 uplink")

    def test_an_over_long_cell_is_truncated_not_rejected(self):
        blob = _book([{"a": "N" * 40000}])
        self.assertEqual(len(self._values(blob, 1)[0]), xlsx._CELL_MAX)

    def test_empty_and_none_render_as_a_blank_cell(self):
        row = _sheet(_book([{"a": None, "b": ""}])).findall(".//m:row", NS)[1]
        self.assertIsNone(row[0].find("m:is/m:t", NS))
        self.assertIsNone(row[0].find("m:v", NS))

    def test_numbers_stay_numbers(self):
        blob = _book([{"b": 12.5}])
        cell = _sheet(blob).findall(".//m:row", NS)[1][1]
        self.assertIsNone(cell.get("t"))
        self.assertEqual(cell.find("m:v", NS).text, "12.5")

    def test_column_letters_pass_z(self):
        self.assertEqual([xlsx._col_name(i) for i in (0, 25, 26, 27)],
                         ["A", "Z", "AA", "AB"])


class SheetNameTest(unittest.TestCase):

    def test_illegal_characters_and_length_are_fixed_not_trusted(self):
        # An org id reaches this; Excel refuses []:*?/\ and caps it at 31.
        self.assertEqual(xlsx._sheet_name("Issues a/b:c*d"), "Issues a-b-c-d")
        self.assertEqual(len(xlsx._sheet_name("I" * 60)), 31)
        self.assertEqual(xlsx._sheet_name(""), "Sheet1")


class WidthTest(unittest.TestCase):

    def test_widths_follow_content_and_respect_the_cap(self):
        cols = [xlsx.Column("a", "A"), xlsx.Column("b", "B", width_cap=20.0)]
        rows = [{"a": "N" * 30, "b": "N" * 300}]
        wide, capped = xlsx._widths(cols, rows)
        self.assertGreater(wide, 25)
        self.assertEqual(capped, 20.0)

    def test_a_short_column_keeps_a_readable_floor(self):
        self.assertGreaterEqual(xlsx._widths([xlsx.Column("a", "A")], [{"a": "x"}])[0], 8.0)


if __name__ == "__main__":
    unittest.main()
