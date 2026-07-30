import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central import proxy

SID = "test-sid-123"
LOGIN = (b"<html><body><form>"
         b"<input name='username'><input type='password' name='pwd'>"
         b"<img src='/captcha.cgi'><input type='text' name='code'>"
         b"</form></body></html>")
# a device UI whose login form is rendered by JS: NO password field in the shell
SPA_SHELL = b"<!doctype html><html><head></head><body><div id='app'></div></body></html>"


class InjectAutofillTest(unittest.TestCase):
    def test_bootstrap_injected_before_body_close(self):
        out = proxy.inject_autofill("text/html", LOGIN, SID)
        self.assertIn(b"/* wisp-autofill */", out)
        self.assertLess(out.index(b"wisp-autofill"), out.rindex(b"</body>"))

    def test_bootstrap_carries_the_session_creds_url(self):
        # the bootstrap references central's reserved same-origin endpoint; it
        # takes no credentials, so there is nothing sensitive to embed here (the
        # "creds never ship in the page" guarantee is proven in the integration test)
        out = proxy.inject_autofill("text/html", LOGIN, SID)
        self.assertIn(f"/api/proxy/{SID}/{proxy.AUTOFILL_PATH}".encode(), out)

    def test_spa_shell_without_password_field_still_gets_bootstrap(self):
        # the whole point of the rewrite: SPA login forms appear after load, so the
        # bootstrap must ship even when the shell has no password field yet
        out = proxy.inject_autofill("text/html", SPA_SHELL, SID)
        self.assertIn(b"wisp-autofill", out)

    def test_non_html_untouched(self):
        self.assertEqual(proxy.inject_autofill("application/json", LOGIN, SID), LOGIN)
        self.assertEqual(proxy.inject_autofill("text/css", LOGIN, SID), LOGIN)

    def test_html_fragment_untouched(self):
        # an AJAX HTML partial (no document markers) must not get a <script> tacked on
        frag = b"<div class='row'><span>hello</span></div>"
        self.assertEqual(proxy.inject_autofill("text/html", frag, SID), frag)

    def test_empty_body_untouched(self):
        self.assertEqual(proxy.inject_autofill("text/html", b"", SID), b"")

    def test_appends_when_no_body_close(self):
        frag = b"<html><head><title>x</title></head>"  # has a doc marker, no </body>
        out = proxy.inject_autofill("text/html", frag, SID)
        self.assertTrue(out.startswith(frag))
        self.assertIn(b"wisp-autofill", out)

    def test_xhtml_content_type(self):
        out = proxy.inject_autofill("application/xhtml+xml", LOGIN, SID)
        self.assertIn(b"wisp-autofill", out)

    def test_missing_content_type_falls_back_to_sniff(self):
        # old firmware serves the login page with no Content-Type; the doc sniff
        # must still catch it
        out = proxy.inject_autofill("", LOGIN, SID)
        self.assertIn(b"wisp-autofill", out)
        # ...but a bodiless/non-doc payload with no type is still left alone
        self.assertEqual(proxy.inject_autofill("", b"{\"ok\":true}", SID), b"{\"ok\":true}")

    def test_content_type_with_charset(self):
        out = proxy.inject_autofill("text/html; charset=utf-8", LOGIN, SID)
        self.assertIn(b"wisp-autofill", out)

    def test_case_insensitive_body_close(self):
        page = b"<HTML><BODY><div>x</div></BODY></HTML>"
        out = proxy.inject_autofill("text/html", page, SID)
        self.assertLess(out.index(b"wisp-autofill"), out.index(b"</BODY>"))


class InjectionPointTest(unittest.TestCase):
    """Where the bootstrap lands. Splicing it into JavaScript breaks the page's
    own script — which reads as "the device UI doesn't work through the proxy",
    with every request still answering 200 (SAGAR-LAN-SW, 2026-07-25)."""

    def test_body_close_inside_a_script_string_is_not_the_injection_point(self):
        # DCN .asp shape: the frame markup is written from JS, so the LAST
        # </body> in the file sits inside a string literal
        page = (b"<html><body><div id='tab'></div></body>\n"
                b"<script>\n"
                b"document.write('<html><body>' + rows + '</body></html>');\n"
                b"</script></html>")
        out = proxy.inject_autofill("text/html", page, SID)
        at = out.index(b"wisp-autofill")
        self.assertLess(at, out.index(b"<script>\ndocument.write"))

    def test_page_whose_only_body_close_is_in_js_appends_at_the_end(self):
        # nothing to splice before, so it falls back to the end of the document
        # — outside the (closed) script, which is what makes that safe
        page = (b"<html><head><script>\n"
                b"document.write('<body>x</body>');\n"
                b"</script></head>")
        out = proxy.inject_autofill("text/html", page, SID)
        self.assertTrue(out.startswith(page))
        self.assertIn(b"wisp-autofill", out)

    def test_unterminated_script_at_eof_blocks_the_append_fallback(self):
        page = b"<html><head><script>\nvar s = '<html>';\n"
        self.assertEqual(proxy.inject_autofill("text/html", page, SID), page)

    def test_script_file_served_without_a_content_type_is_not_a_document(self):
        # old firmware ships common.js with no Content-Type; it contains HTML
        # markers only inside strings, so the doc sniff alone waves it through
        js = b"function render(){ document.write('<html><body>x</body></html>'); }\n"
        self.assertEqual(proxy.inject_autofill("", js, SID), js)
        self.assertEqual(proxy.inject_autofill("text/html", js, SID), js)

    def test_leading_whitespace_and_bom_still_count_as_a_document(self):
        page = b"\xef\xbb\xbf\n  <html><body>x</body></html>"
        self.assertIn(b"wisp-autofill", proxy.inject_autofill("text/html", page, SID))


if __name__ == "__main__":
    unittest.main()
