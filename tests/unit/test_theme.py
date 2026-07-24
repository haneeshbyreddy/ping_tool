"""Server-wide theme overrides: validation, storage, CSS rendering."""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central import theme  # noqa: E402

_SPA_TOKENS = (Path(__file__).resolve().parents[2]
               / "web" / "src" / "lib" / "theme-tokens.ts")


class _FakeStore:
    """Minimal get_setting/set_setting pair — theme.py touches nothing else."""

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
        # An arbitrary custom property would let a caller restyle anything the
        # SPA reads, not just the palette.
        got = theme.clean_overrides({"dark": {"--not-a-real-token": "#fff"}})
        self.assertEqual(got, {})

    def test_drops_values_that_could_escape_the_style_block(self):
        # These land inside a <style> element; a value carrying `}` or `<` or a
        # url() is the whole reason _VALUE_RE exists.
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
        # Reset-to-shipped must leave NO row behind: a stored `{}` is a stale
        # marker, and the point of the sparse diff is that untouched
        # deployments keep following index.css.
        store = _FakeStore()
        theme.save(store, {"dark": {"--card": "#1c1f24"}})
        theme.save(store, {})
        self.assertNotIn(theme.SETTING_KEY, store.settings)
        self.assertEqual(theme.load(store), {})

    def test_load_revalidates_hostile_stored_row(self):
        # A row written by an older build or edited straight in SQLite must
        # still be filtered on the way OUT, not trusted because it is stored.
        store = _FakeStore({theme.SETTING_KEY: json.dumps(
            {"dark": {"--background": "#fff;}html{display:none}",
                      "--card": "#1c1f24"}})})
        self.assertEqual(theme.load(store), {"dark": {"--card": "#1c1f24"}})

    def test_load_survives_corrupt_json(self):
        store = _FakeStore({theme.SETTING_KEY: "{not json"})
        self.assertEqual(theme.load(store), {})


class RenderCssTest(unittest.TestCase):
    def test_empty_renders_nothing(self):
        # A stock install must inject no CSS at all.
        self.assertEqual(theme.render_css({}), "")
        self.assertEqual(theme.render_css({"dark": {}}), "")

    def test_mode_selectors_are_mutually_exclusive_and_outrank_the_bundle(self):
        """The regression that blew out dark mode.

        `:root` and `.dark` have EQUAL specificity (0,1,0). This block is
        injected AFTER the bundle stylesheet, so light overrides written as a
        plain `:root{}` beat the bundle's `.dark{}` on source order and apply
        in DARK mode — the operator's light surfaces under dark mode's light
        text, i.e. an unreadable white screen. `:root:not(.dark)` cannot match
        a dark document at all, and at (0,2,0) both selectors outrank the
        bundle wherever this lands.
        """
        css = theme.render_css({"dark": {"--card": "#1c1f24"},
                                "light": {"--card": "#ffffff"}})
        self.assertIn(":root.dark{--card:#1c1f24;}", css)
        self.assertIn(":root:not(.dark){--card:#ffffff;}", css)
        # a bare `:root{` would match a dark document
        self.assertNotIn(":root{", css)


class PreviewIsModeScopedTest(unittest.TestCase):
    """Source-level guard on the SPA's live preview.

    There is no frontend test suite (CLAUDE.md), and this specific mistake
    SHIPPED and broke a live dashboard, so it gets pinned from the Python side
    rather than left to review.

    The bug: `applyPreview` set the palette as INLINE styles on <html>. Inline
    styles on the root element outrank every stylesheet rule INCLUDING
    `.dark{}`, and they carry no notion of which mode they belong to — so a
    light-mode palette previewed (or saved) that way survived a switch to dark
    mode and painted the dark theme white, with no way for the theme class to
    win. The preview must emit mode-SCOPED CSS into a <style> element, using
    the same mutually-exclusive selectors the server does (see
    RenderCssTest above for why they are `:root:not(.dark)` / `:root.dark`).
    """

    def test_preview_never_writes_inline_root_styles(self):
        src = _SPA_TOKENS.read_text(encoding="utf-8")
        # Comments explain the bug by name; only look at real code.
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        self.assertNotIn("documentElement.style", code,
                         "applyPreview must not set inline styles on <html> — "
                         "they beat .dark{} and are not mode-scoped")

    def test_preview_uses_the_same_selectors_as_the_server(self):
        src = _SPA_TOKENS.read_text(encoding="utf-8")
        # The SPA's renderCss must map modes the way render_css does here, or
        # the preview shows something a save will not reproduce.
        self.assertIn('mode === "dark" ? ":root.dark" : ":root:not(.dark)"', src)


class AllowlistParityTest(unittest.TestCase):
    def test_allowlist_matches_spa(self):
        """theme.py's allowlist must equal the SPA's ALL_TOKENS.

        These are two hand-maintained lists in different languages. A token
        added only on the SPA side is silently dropped on save — which presents
        as "I changed the colour and it didn't stick", a bug that is very hard
        to trace back to a missing string in a Python set.
        """
        src = _SPA_TOKENS.read_text(encoding="utf-8")
        # Every "--token" literal the SPA declares as editable: the seed
        # derivations and ADVANCED_TOKENS both spell them out as strings.
        spa = set(re.findall(r'"(--[a-z0-9-]+)"', src))
        spa.discard("--font-sans")
        self.assertEqual(spa, set(theme._TOKENS))


if __name__ == "__main__":
    unittest.main()
