import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central import theme  # noqa: E402

_SPA_TOKENS = (Path(__file__).resolve().parents[2]
               / "web" / "src" / "lib" / "theme-tokens.ts")
_PUBLIC = Path(__file__).resolve().parents[2] / "web" / "public"
_LANDING = _PUBLIC / "landing.html"
_OVERLAY = _PUBLIC / "showcase.js"


class _FakeStore:
    def __init__(self, initial=None):
        self.settings = dict(initial or {})

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        if value is None:
            self.settings.pop(key, None)
        else:
            self.settings[key] = value


class CleanOverridesTest(unittest.TestCase):
    def test_keeps_known_tokens_and_colour_values(self):
        got = theme.clean_overrides({
            "dark": {"--background": "#0c0e12",
                     "--primary-soft": "rgba(116,174,201,0.14)"},
            "light": {"--card": "#ffffff"},
        })
        self.assertEqual(got, {
            "dark": {"--background": "#0c0e12",
                     "--primary-soft": "rgba(116,174,201,0.14)"},
            "light": {"--card": "#ffffff"},
        })

    def test_drops_unknown_token(self):
        got = theme.clean_overrides({"dark": {"--not-a-real-token": "#fff"}})
        self.assertEqual(got, {})

    def test_drops_values_that_could_escape_the_style_block(self):
        for bad in ("#fff;}body{display:none}",
                    "red</style><script>alert(1)</script>",
                    "url(https://evil.example/x.png)",
                    "expression(alert(1))",
                    "#fff !important",
                    "var(--something)"):
            with self.subTest(bad=bad):
                got = theme.clean_overrides({"dark": {"--background": bad}})
                self.assertEqual(got, {}, f"{bad!r} survived validation")

    def test_ignores_unknown_modes_and_bad_shapes(self):
        self.assertEqual(theme.clean_overrides(None), {})
        self.assertEqual(theme.clean_overrides([1, 2, 3]), {})
        self.assertEqual(theme.clean_overrides({"dark": "not-a-dict"}), {})
        self.assertEqual(theme.clean_overrides({"sepia": {"--card": "#fff"}}), {})
        self.assertEqual(theme.clean_overrides({"dark": {"--card": 42}}), {})

    def test_caps_tokens_per_mode(self):
        flood = {f"--tok{i}": "#ffffff" for i in range(500)}
        flood["--card"] = "#ffffff"
        got = theme.clean_overrides({"dark": flood})
        self.assertLessEqual(len(got.get("dark", {})), 64)


class StorageTest(unittest.TestCase):
    def test_round_trip(self):
        store = _FakeStore()
        theme.save(store, {"dark": {"--card": "#1c1f24"}})
        self.assertEqual(theme.load(store), {"dark": {"--card": "#1c1f24"}})

    def test_empty_save_clears_the_row(self):
        store = _FakeStore()
        theme.save(store, {"dark": {"--card": "#1c1f24"}})
        theme.save(store, {})
        self.assertNotIn(theme.SETTING_KEY, store.settings)
        self.assertEqual(theme.load(store), {})

    def test_load_revalidates_hostile_stored_row(self):
        store = _FakeStore({theme.SETTING_KEY: json.dumps(
            {"dark": {"--background": "#fff;}html{display:none}",
                      "--card": "#1c1f24"}})})
        self.assertEqual(theme.load(store), {"dark": {"--card": "#1c1f24"}})

    def test_load_survives_corrupt_json(self):
        store = _FakeStore({theme.SETTING_KEY: "{not json"})
        self.assertEqual(theme.load(store), {})


class RenderCssTest(unittest.TestCase):
    def test_empty_renders_nothing(self):
        self.assertEqual(theme.render_css({}), "")
        self.assertEqual(theme.render_css({"dark": {}}), "")

    def test_mode_selectors_are_mutually_exclusive_and_outrank_the_bundle(self):

        css = theme.render_css({"dark": {"--card": "#1c1f24"},
                                "light": {"--card": "#ffffff"}})
        self.assertIn(":root.dark{--card:#1c1f24;}", css)
        self.assertIn(":root:not(.dark){--card:#ffffff;}", css)
        self.assertNotIn(":root{", css)


class PreviewIsModeScopedTest(unittest.TestCase):


    def test_preview_never_writes_inline_root_styles(self):
        src = _SPA_TOKENS.read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        self.assertNotIn("documentElement.style", code,
                         "applyPreview must not set inline styles on <html> — "
                         "they beat .dark{} and are not mode-scoped")

    def test_preview_uses_the_same_selectors_as_the_server(self):
        src = _SPA_TOKENS.read_text(encoding="utf-8")
        self.assertIn('mode === "dark" ? ":root.dark" : ":root:not(.dark)"', src)


class AllowlistParityTest(unittest.TestCase):
    def test_allowlist_matches_spa(self):

        src = _SPA_TOKENS.read_text(encoding="utf-8")
        spa = set(re.findall(r'"(--[a-z0-9-]+)"', src))
        spa.discard("--font-sans")
        self.assertEqual(spa, set(theme._TOKENS))


class LandingArtifactTest(unittest.TestCase):

    def _template(self) -> str:
        src = _LANDING.read_text(encoding="utf-8")
        m = re.search(r'<script type="__bundler/template">\n(.*?)\n  </script>',
                      src, re.S)
        self.assertIsNotNone(m, "landing.html has no bundler template")
        return json.loads(m.group(1))

    def test_landing_claims_the_dark_half_of_the_override_map(self):
        self.assertIn('<html class="dark">', _LANDING.read_text(encoding="utf-8"))
        self.assertIn('<html class="dark">', self._template())

    def test_the_unpacker_carries_the_style_block_across_the_dom_swap(self):
        src = _LANDING.read_text(encoding="utf-8")
        self.assertIn("document.getElementById('wisp-theme')", src)
        swap = src.index("document.documentElement.replaceWith(doc.documentElement)")
        reattach = src.index("document.head.appendChild(themeStyle)")
        self.assertLess(swap, reattach, "re-attached before the swap wipes it")

    def test_every_page_colour_reads_a_token(self):

        tpl = self._template()
        body = tpl[tpl.index("</style>", tpl.index("html, body { margin")):]
        for allowed in ("cssColor('--lp-accent', this.props.accent ?? '#5680bd')",
                        "cssColor('--lp-ok', '#43d68c')"):
            self.assertIn(allowed, body)
            body = body.replace(allowed, "")
        body = re.sub(r'data-props="[^"]*"', "", body)
        strays = set(re.findall(r'#[0-9a-fA-F]{6}\b', body))
        strays |= set(re.findall(r'rgba?\((?!0,0,0)[0-9.,\s]+\)', body))
        self.assertEqual(strays, set(), f"hardcoded colours on the landing page: {strays}")

    def test_every_lp_token_maps_to_one_central_validates(self):
        tpl = self._template()
        referenced = set(re.findall(r'var\((--(?!lp-)[a-z0-9-]+),', tpl))
        self.assertTrue(referenced, "the mapping layer reads no tokens at all")
        self.assertEqual(referenced - set(theme._TOKENS), set())

    def test_the_overlay_borrows_the_page_palette(self):
        src = _OVERLAY.read_text(encoding="utf-8")
        for line in src.splitlines():
            for lit in re.findall(r'#[0-9a-fA-F]{3,6}\b|rgba?\([0-9.,\s]+\)', line):
                if lit in ("#000", "rgba(0,0,0,.4)"):
                    continue
                self.assertIn("var(--lp-", line, f"unthemed colour {lit}: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
