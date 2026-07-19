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


if __name__ == "__main__":
    unittest.main()
