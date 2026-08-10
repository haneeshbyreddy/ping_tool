"""Transport half of the web-UI optics scraper: HTTP over the proxy tunnel.

The tunnel forwards one request and returns one response, so cookies and
redirects are the caller's job. A device login is exactly those two things, so
getting them wrong means every scrape silently reads the login page instead of
the data — which would look like "the OLT reports no optics", the one wrong
answer this subsystem must never give.
"""

import base64
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central.weboptics import (DEFAULT_PONS, MAX_REDIRECTS, TunnelHttp,
                                    _redirect_path, diagnose_login, login_form,
                                    merge_scraped, opm_form, parse_opm_diag,
                                    pon_indices, scrape_opm, session_key)


class FakeHub:
    """Records submits, replies from a scripted queue."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def submit(self, sess, *, method, path, headers, body, timeout, extra=None):
        self.seen.append({"method": method, "path": path, "headers": dict(headers),
                          "body": body, "sid": sess.sid, "ip": sess.device_ip})
        return self.replies.pop(0) if self.replies else None


def reply(status=200, body=b"", headers=()):
    return {"status": status, "headers": [list(h) for h in headers],
            "body_b64": base64.b64encode(body).decode()}


def client(hub, **kw):
    return TunnelHttp(hub=hub, org_id="ispA", node_id="edge-1", device_id=7,
                      ip="10.0.0.9", port=80, **kw)


class TunnelHttpTest(unittest.TestCase):

    def test_cookies_from_login_ride_the_next_request(self):
        hub = FakeHub([
            reply(302, headers=[("Set-Cookie", "SESSIONID=abc123; Path=/"),
                                ("Location", "/main.html")]),
            reply(200, b"<html>ok</html>"),
            reply(200, b"<table>readings</table>"),
        ])
        c = client(hub)
        c.post_form("/login.cgi", {"user": "admin", "pass": "x"})
        res = c.get("/opm.cgi?pon=1")

        self.assertTrue(res.ok)
        self.assertEqual(res.body, b"<table>readings</table>")
        # Both post-login hops must carry the session cookie; a scrape that
        # drops it gets the login page back with a 200 and parses to nothing.
        self.assertEqual(hub.seen[1]["headers"]["Cookie"], "SESSIONID=abc123")
        self.assertEqual(hub.seen[2]["headers"]["Cookie"], "SESSIONID=abc123")

    def test_repeated_set_cookie_headers_all_land(self):
        # Header PAIRS (not a dict) exist so repeated names survive the edge.
        # Collapsing them keeps only the last, and the session id is as often
        # the first as the last.
        hub = FakeHub([
            reply(200, headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]),
            reply(200, b"x"),
        ])
        c = client(hub)
        c.get("/")
        c.get("/next")
        self.assertEqual(hub.seen[1]["headers"]["Cookie"], "a=1; b=2")

    def test_redirect_after_login_post_continues_as_get(self):
        hub = FakeHub([
            reply(302, headers=[("Location", "/dashboard.html")]),
            reply(200, b"<html>in</html>"),
        ])
        c = client(hub)
        res = c.post_form("/login.cgi", {"user": "admin"})
        self.assertTrue(res.ok)
        self.assertEqual(hub.seen[1]["method"], "GET")
        self.assertEqual(hub.seen[1]["path"], "/dashboard.html")
        self.assertEqual(hub.seen[1]["body"], b"")

    def test_absolute_redirect_is_reduced_to_a_path(self):
        # The tunnel addresses a device by (ip, port, scheme) and has no notion
        # of host, so an absolute Location must reduce to its path — which is
        # also what stops a device from steering the edge at another host.
        hub = FakeHub([
            reply(302, headers=[("Location", "http://10.0.0.9/panel/index.html")]),
            reply(200, b"ok"),
        ])
        c = client(hub)
        c.get("/")
        self.assertEqual(hub.seen[1]["path"], "/panel/index.html")
        self.assertEqual(hub.seen[1]["ip"], "10.0.0.9")

    def test_redirect_loop_is_bounded(self):
        hub = FakeHub([reply(302, headers=[("Location", "/loop")])] * 20)
        res = client(hub).get("/loop")
        self.assertFalse(res.ok)
        self.assertIn("redirect", res.error)
        self.assertLessEqual(len(hub.seen), MAX_REDIRECTS + 1)

    def test_dormant_tunnel_is_an_error_not_an_empty_reading(self):
        # submit() returns None when no edge answers. That must surface as a
        # failure: an empty body would parse to "this OLT has no ONUs".
        res = client(FakeHub([])).get("/opm.cgi")
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "tunnel timeout")
        self.assertEqual(res.body, b"")

    def test_edge_reported_error_surfaces(self):
        hub = FakeHub([{"status": 0, "headers": [], "body_b64": "",
                        "error": "connection refused"}])
        res = client(hub).get("/")
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "connection refused")

    def test_scrape_never_opens_a_browsing_session(self):
        # A background sweep must not appear in the sessions panel or carry a
        # human-renewable TTL. It builds an ad-hoc session like the preflight.
        hub = FakeHub([reply(200, b"x")])
        c = client(hub)
        c.get("/")
        self.assertEqual(hub.seen[0]["sid"], "weboptics")
        self.assertFalse(hasattr(hub, "opened"))


class RedirectPathTest(unittest.TestCase):

    def test_relative_resolves_against_the_current_directory(self):
        self.assertEqual(_redirect_path("/a/b/login.cgi", "main.html"),
                         "/a/b/main.html")

    def test_root_relative_replaces_the_path(self):
        self.assertEqual(_redirect_path("/a/b/login.cgi", "/x.html"), "/x.html")

    def test_absolute_url_keeps_only_the_path(self):
        self.assertEqual(_redirect_path("/", "https://host:8443/deep/p.html?q=1"),
                         "/deep/p.html?q=1")

    def test_absolute_url_with_no_path_becomes_root(self):
        self.assertEqual(_redirect_path("/", "http://10.0.0.9"), "/")



# Trimmed from a real PYLON-OLT capture 2026-07-22, markup quirks preserved
# EXACTLY: the ONU-ID cell carries class='hd' just like the header cells, the
# Description cell is empty for unnamed ONUs, and SessionKey is appended by
# inline JS rather than being a hidden input in the markup.
OPM_PAGE = """<html><head><meta charset="gb2312"/></head><body>
<form name=form method=post action="onuopmdiag.html">
<select id="s" name="select"><option value="1">PON1</option>
<option value=255>All</option></select>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td class='hd'>ONU ID</td><td class='hd'><font>MAC Address</font></td>
<td class='hd'><font>Description</font></td><td class='hd'><font>Distance</font>(m)</td>
<td class='hd'><font>Temperature</font>(&deg;C)</td><td class='hd'>Supply Voltage</td>
<td class='hd'>TX Bias Current</td><td class='hd'>TX Power</td><td class='hd'>RX Power</td></tr>
<tr><td class='hd'>EPON0/1:1</td><td>00:D3:9E:7E:01:4E</td><td>HCS_BABJI</td>
<td>4531</td><td>46.30</td><td>3.21</td><td>13.55</td><td>2.42</td><td>-24.56</td></tr>
<tr><td class='hd'>EPON0/1:2</td><td>98:2F:3C:B9:42:F8</td><td></td>
<td>4406</td><td>56.04</td><td>3.20</td><td>14.03</td><td>2.62</td><td>-19.24</td></tr>
<tr><td class='hd'>EPON0/1:8</td><td>8C:A3:99:17:D3:38</td><td></td>
<td>2121</td><td>45.13</td><td>3.29</td><td>9.28</td><td>2.36</td><td>-2.93</td></tr>
<tr><td class='hd'>EPON0/3:29</td><td>D0:1E:1D:1A:40:C8</td><td></td>
<td>3124</td><td>53.25</td><td>3.27</td><td>14.80</td><td>2.27</td><td>-13.85</td></tr>
</table>
<input name='onuid' id='onuid' type='hidden' value=0/>
<input type=hidden name=who value=100>
</form>
<script>
for (var i = 0; i < document.forms.length; i++) {
var SessionKey = document.createElement('input');
SessionKey.name = 'SessionKey';
SessionKey.value = 'kmwex';
document.forms[i].appendChild(SessionKey);
}
</script></body></html>"""


class OpmDiagParseTest(unittest.TestCase):

    def setUp(self):
        self.rows = parse_opm_diag(OPM_PAGE)

    def test_header_row_is_not_mistaken_for_an_onu(self):
        # The ONU-ID cell has class='hd' exactly like the header cells, so class
        # cannot separate them — the ONU id pattern is the only honest anchor.
        self.assertEqual(len(self.rows), 4)
        self.assertNotIn("ONU ID", [r["pon_port"] for r in self.rows])

    def test_readings_land_on_the_right_columns(self):
        r = self.rows[0]
        self.assertEqual(r["serial"], "00:D3:9E:7E:01:4E")
        self.assertEqual(r["name"], "HCS_BABJI")
        self.assertEqual(r["rx_dbm"], -24.56)
        self.assertEqual(r["tx_dbm"], 2.42)
        self.assertEqual(r["temp_c"], 46.30)
        self.assertEqual(r["voltage_v"], 3.21)
        self.assertEqual(r["tx_bias_ma"], 13.55)

    def test_distance_here_is_real_metres_not_time_quanta(self):
        # The SNMP roster's col13 is RTT in 16ns quanta (2860 for this ONU); the
        # web page publishes the OLT's own conversion. Scraped distance must
        # never be fed through the profile's TQ scaling.
        self.assertEqual(self.rows[0]["distance_m"], 4531)

    def test_unnamed_onu_is_none_not_empty_string(self):
        self.assertIsNone(self.rows[1]["name"])

    def test_onu_key_matches_the_snmp_roster_convention(self):
        # onu_optics is keyed (pon.onu) by the GPON path; a scrape that keys
        # differently would insert duplicate rows instead of enriching.
        self.assertEqual(self.rows[0]["onu_key"], "1.1")
        self.assertEqual(self.rows[3]["onu_key"], "3.29")
        self.assertEqual(self.rows[3]["pon_port"], "EPON0/3")
        self.assertEqual(self.rows[3]["onu_id"], 29)

    def test_an_overdriven_receiver_is_reported_as_measured(self):
        # -2.93 dBm exceeds the EPON PX20 overload point; it must arrive intact
        # rather than being clamped to a "sane" range.
        hot = [r for r in self.rows if r["serial"] == "8C:A3:99:17:D3:38"][0]
        self.assertEqual(hot["rx_dbm"], -2.93)

    def test_session_key_is_read_from_the_inline_script(self):
        # Not a cookie and not a hidden input in the markup — inline JS appends
        # it, and it rotates per response.
        self.assertEqual(session_key(OPM_PAGE), "kmwex")

    def test_missing_session_key_is_none_not_a_guess(self):
        self.assertIsNone(session_key("<html><body>login</body></html>"))

    def test_login_page_yields_no_readings(self):
        # The failure we must never mistake for "this OLT has no optics".
        self.assertEqual(parse_opm_diag("<html><body>Please log in</body></html>"), [])

    def test_form_body_matches_the_captured_request(self):
        body = opm_form(2, "abc12")
        self.assertEqual(body["select"], "2")
        self.assertEqual(body["SessionKey"], "abc12")
        self.assertEqual(body["port_refresh"], "Refresh")
        # Reproduced from the page's unquoted `value=0/>` attribute.
        self.assertEqual(body["onuid"], "0/")

def opm_reply(key, rows_html=""):
    body = (OPM_PAGE.replace("SessionKey.value = 'kmwex';",
                             f"SessionKey.value = '{key}';")).encode("gb2312")
    return reply(200, body)


_KEYLESS_PAGE = OPM_PAGE[:OPM_PAGE.index("<script>")] + "</body></html>"


def keyless_opm_reply():
    """chandana-network MAIN_OLT (2026-08-07): the SAME optical page, every
    heading and every row on it, and no SessionKey ANYWHERE — the build simply
    never mints one, where its sibling on the next IP does."""
    return reply(200, _KEYLESS_PAGE.encode("gb2312"))


def login_page():
    """The GET of /action/login.html that now precedes every login POST."""
    return reply(200, b"<html><body><form>login</form></body></html>")


def frameset_reply():
    """What /action/main.html actually returns on a SUCCESSFUL login: the shell
    frameset, carrying no SessionKey of its own. Reading this as a failure is
    what made a correct password look rejected on the first live run."""
    return reply(200, b"<html><head><title>OLT</title></head><frameset>"
                      b"<frame src='/action/loginout.html'>"
                      b"<frame src='/action/systeminfo.html'></frameset></html>")


class ScrapeOpmTest(unittest.TestCase):
    """The login -> rotating-key -> per-PON chain."""

    def test_happy_path_walks_every_pon_chaining_the_key(self):
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2"), opm_reply("k3"), opm_reply("k4"),
                       opm_reply("k5")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3, 4))
        self.assertIsNone(err)
        self.assertEqual(len(rows), 16)                 # 4 ONUs x 4 PONs

        # login page -> login POST -> GET the OPM page for the FIRST key -> the
        # per-PON refresh POSTs.
        self.assertEqual(hub.seen[0]["path"], "/action/login.html")
        self.assertEqual(hub.seen[1]["path"], "/action/main.html")
        self.assertEqual((hub.seen[2]["method"], hub.seen[2]["path"]),
                         ("GET", "/action/onuopmdiag.html"))
        opm = hub.seen[3:]
        self.assertEqual([s["path"] for s in opm], ["/action/onuopmdiag.html"] * 4)
        # Each PON must carry the key the PREVIOUS reply handed back, not the
        # one we logged in with — the firmware rotates it every response.
        keys = [dict(p.split("=", 1) for p in s["body"].decode().split("&"))["SessionKey"]
                for s in opm]
        self.assertEqual(keys, ["k1", "k2", "k3", "k4"])
        pons = [dict(p.split("=", 1) for p in s["body"].decode().split("&"))["select"]
                for s in opm]
        self.assertEqual(pons, ["1", "2", "3", "4"])

    def test_a_build_that_mints_no_token_is_read_keyless(self):
        # MAIN_OLT serves the optical page with all nine headings and no token.
        # Calling that "login rejected" cost a whole fleet's Rx column and read
        # as "check the password", which was never wrong.
        hub = FakeHub([login_page(), frameset_reply()]
                      + [keyless_opm_reply() for _ in range(5)])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3, 4))
        self.assertIsNone(err)
        self.assertEqual(len(rows), 16)                 # 4 ONUs x 4 PONs
        # The POSTs must go out shaped as this build's own page would send them:
        # the field is OMITTED, never sent empty or with a made-up value.
        for s in hub.seen[3:]:
            body = dict(p.split("=", 1) for p in s["body"].decode().split("&"))
            self.assertNotIn("SessionKey", body)
            self.assertEqual(body["port_refresh"], "Refresh")

    def test_a_page_that_is_NOT_the_table_is_still_refused_keyless(self):
        # The bar is EVERY mapped heading, so the page has to prove it is the
        # table before an admin session goes on to POST at it. A session-limit
        # notice clears none of them, and must not be scraped "just in case".
        hub = FakeHub([login_page(), frameset_reply(),
                       reply(200, b"<html><title>OLT Web Management Interface"
                                  b"</title><body>session limit</body></html>")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2))
        self.assertEqual(rows, [])
        self.assertIn("login rejected", err)
        self.assertEqual(len(hub.seen), 3)              # no per-PON POST went out

    def test_a_partial_heading_match_refuses_and_says_the_count(self):
        # A renamed column is a profile fault to report, not a reason to scrape a
        # page we may be misreading.
        thin = _KEYLESS_PAGE.replace("RX Power", "Rx Optical Power")
        hub = FakeHub([login_page(), frameset_reply(),
                       reply(200, thin.encode("gb2312"))])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1,))
        self.assertEqual(rows, [])
        self.assertIn("8 of 9", err)
        self.assertEqual(len(hub.seen), 3)

    def test_a_keyless_session_lost_midway_keeps_what_it_read(self):
        # A keyless build has no token to go missing, so the rotating-key check
        # cannot run — but a human logging in still displaces us, and the page
        # stops being the table when it happens.
        # The entry GET consumes one keyless page; PON1 then gets the second and
        # PON2 gets bounced to the login form.
        hub = FakeHub([login_page(), frameset_reply(), keyless_opm_reply(),
                       keyless_opm_reply(),
                       reply(200, b"<html><body>Please log in</body></html>")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3))
        self.assertEqual(len(rows), 4)                  # PON1's readings survive
        self.assertIn("session lost", err)

    def test_a_token_rotating_build_is_untouched_by_the_keyless_path(self):
        # MAIN_OLT2 sits on the next IP, rotates its key and works today. The
        # keyless path must be reachable ONLY when no key is on the page.
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1,))
        self.assertIsNone(err)
        body = dict(p.split("=", 1) for p in hub.seen[3]["body"].decode().split("&"))
        self.assertEqual(body["SessionKey"], "k1")

    def test_bad_credentials_are_an_error_not_an_empty_optics_reading(self):
        # The device re-serves the login page with HTTP 200. Read naively that
        # is "this OLT has no ONUs", which would clear every reading it has.
        hub = FakeHub([login_page(), frameset_reply(), reply(
            200, b'<html><form action="/action/login.html">'
                 b'<input name="password"></form></html>')])
        rows, err = scrape_opm(client(hub), "admin", "wrong")
        self.assertEqual(rows, [])
        self.assertIn("password was refused", err)

    def test_a_frameset_login_reply_is_success_not_rejection(self):
        # THE first-live-run bug: main.html returns the frameset shell, which
        # has no SessionKey, and that was read as a refused password.
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1,))
        self.assertIsNone(err)
        self.assertEqual(len(rows), 4)

    def test_dormant_tunnel_fails_the_scrape_before_the_password_goes_out(self):
        hub = FakeHub([])                      # every request times out
        rows, err = scrape_opm(client(hub), "admin", "pw")
        self.assertEqual(rows, [])
        self.assertIn("credentials NOT sent", err)
        # One hop, and it was the GET. The login POST is what carries the
        # password, and nothing has told us yet that this endpoint is even the
        # right vendor's web UI.
        self.assertEqual([(s["method"], s["path"]) for s in hub.seen],
                         [("GET", "/action/login.html")])

    def test_a_missing_login_page_stops_before_the_password(self):
        # The endpoint answers, but not with this vendor's login page. That is
        # the "the address does not reach the OLT" case, and an admin
        # credential must not be how we discover it.
        hub = FakeHub([reply(404, b"<html>Not Found</html>")])
        rows, err = scrape_opm(client(hub), "admin", "pw")
        self.assertEqual(rows, [])
        self.assertIn("credentials NOT sent", err)
        self.assertEqual(len(hub.seen), 1)
        self.assertNotIn("pw", str(hub.seen))

    def test_a_firmware_without_opm_diag_says_so_instead_of_404(self):
        # The C-Data GPON boxes: same vendor, same login, no OPM Diag page.
        # "404" reads as a transient fault and gets retried forever; naming it
        # is what tells an operator this box needs its own capture.
        hub = FakeHub([login_page(), frameset_reply(), reply(404, b"nope")])
        rows, err = scrape_opm(client(hub), "admin", "pw")
        self.assertEqual(rows, [])
        self.assertIn("no /action/onuopmdiag.html", err)
        self.assertIn("capture", err)

    def test_the_time_budget_keeps_the_pons_it_already_read(self):
        # A slow OLT must not hold the single sweep thread while the rest of
        # the fleet's readings age — but the PONs already read are real.
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2"), opm_reply("k3")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3),
                               deadline=time.monotonic() - 1)
        self.assertEqual(rows, [])
        self.assertIn("time budget", err)
        self.assertIn("0 of 3", err)

    def test_partial_scrape_keeps_the_pons_that_worked(self):
        # PON1 and PON2 readings are real; losing PON3 must not throw them away,
        # and must not blank PON3's ONUs either (caller merges by MAC).
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2"), opm_reply("k3"), reply(500, b"boom")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3))
        self.assertEqual(len(rows), 8)
        self.assertIn("PON3", err)

    def test_session_stolen_midway_reports_it_plainly(self):
        # No cookies means one session slot: a human logging into the OLT can
        # displace ours. Benign, but the operator should see why it stopped.
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2"), reply(200, b"<html>logged out</html>")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2))
        self.assertEqual(len(rows), 4)
        self.assertIn("session lost", err)

    def test_login_body_matches_the_captured_request(self):
        self.assertEqual(login_form("admin", "s3cret"),
                         {"user": "admin", "pass": "s3cret",
                          "button": "Login", "who": "100"})

    def test_the_login_page_is_fetched_before_it_is_posted_to(self):
        # Replicates the sequence a browser session that WORKED actually used
        # (proxy_audit on PYLON): the login page is fetched first.
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2")])
        scrape_opm(client(hub), "admin", "pw", pons=(1,))
        self.assertEqual(hub.seen[0]["method"], "GET")
        self.assertEqual(hub.seen[0]["path"], "/action/login.html")
        self.assertEqual(hub.seen[1]["method"], "POST")
        self.assertEqual(hub.seen[1]["path"], "/action/main.html")


def _row(volts, tx, rx, temp="45.0", bias="9.0"):
    """One OPM Diag data row, cells in the page's captured order."""
    return (f"<tr><td class='hd'>EPON0/7:6</td><td>90:C6:82:14:48:90</td>"
            f"<td>sub</td><td>1187</td><td>{temp}</td><td>{volts}</td>"
            f"<td>{bias}</td><td>{tx}</td><td>{rx}</td></tr>")


class DdmRailTest(unittest.TestCase):
    """A sensor RAIL is not a reading.

    Live values, first fleet-wide sweep 2026-07-23 (HILL-OLT-1). An ONU whose
    diagnostics are dead prints the raw register on every DDM field at once,
    and the two directions fail OPPOSITE ways — which is exactly why a range
    check on Rx alone would not have caught them.
    """

    def _one(self, html):
        rows = parse_opm_diag(f"<html><body><table>{html}</table></body></html>")
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_all_ones_rail_is_not_a_healthy_onu(self):
        # 0xFFFF: 6.55 V / 131.07 mA / +8.16 dBm. The nasty one — +8.16 grades
        # ABOVE the warn floor, so an ONU with dead optics would read as the
        # best drop on the PON and nobody would ever go looking.
        row = self._one(_row("6.55", "8.16", "8.16", temp="0.0", bias="131.07"))
        self.assertIsNone(row["rx_dbm"])
        self.assertIsNone(row["tx_dbm"])
        self.assertIsNone(row["voltage_v"])
        self.assertIsNone(row["tx_bias_ma"])

    def test_the_all_zeroes_floor_would_have_paged_a_crew(self):
        # 0x0000: 0.0 V / 0.22 mA / -40 dBm. Below the crit floor, so this one
        # pages OPTICAL_CRIT for an ONU that may be perfectly well lit.
        row = self._one(_row("0.0", "-16.48", "-40.0", temp="56.25", bias="0.22"))
        self.assertIsNone(row["rx_dbm"])
        self.assertIsNone(row["tx_dbm"])

    def test_identity_survives_a_railed_block(self):
        # The ONU is still there and still ours — only the readings are void.
        # Blanking them means the merge leaves the roster alone, which is the
        # honest answer: "we could not read this one".
        row = self._one(_row("6.55", "8.16", "8.16"))
        self.assertEqual(row["onu_key"], "7.6")
        self.assertEqual(row["serial"], "90:C6:82:14:48:90")
        self.assertEqual(row["distance_m"], 1187)

    def test_a_real_reading_is_untouched(self):
        # HILL-OLT-1 1.2, a textbook drop. Nothing here may be rounded off or
        # clamped by the guard.
        row = self._one(_row("3.24", "2.33", "-10.3", temp="47.0", bias="9.0"))
        self.assertEqual((row["voltage_v"], row["tx_dbm"], row["rx_dbm"]),
                         (3.24, 2.33, -10.3))

    def test_the_worst_real_drop_on_the_fleet_still_counts(self):
        # -28.24 dBm on PYLON is a genuine, actionable crit. The guard must
        # reject rails, never merely-bad optics — that is the alarm's whole job.
        row = self._one(_row("3.19", "1.02", "-28.24"))
        self.assertEqual(row["rx_dbm"], -28.24)

    def test_an_over_driven_onu_is_still_reported(self):
        # -2.87 dBm on PYLON: above the PX20 overload point and a real fault
        # the ISP needs told about. Negative is physically possible; it stays.
        row = self._one(_row("3.30", "2.10", "-2.87"))
        self.assertEqual(row["rx_dbm"], -2.87)


class PonIndicesTest(unittest.TestCase):
    """Which PONs to ask an OLT about.

    This is the difference between "works on the fleet" and "works on PYLON".
    The scrape is one POST per PON and the port count was a constant taken from
    the one OLT it was built against; the same firmware ships 3 to 8 PONs with
    GAPS, so a fixed 1..4 read as a clean success while never asking about
    rather more than half of the fleet's online ONUs. Blank Rx that means "we
    did not ask" is indistinguishable, on screen, from blank Rx that means
    "this vendor has none" — the exact false negative this subsystem exists to
    kill.
    """

    def test_ports_come_from_the_rosters_own_labels(self):
        self.assertEqual(
            pon_indices(["EPON0/1", "EPON0/3", "EPON0/4", "EPON0/8"]),
            (1, 3, 4, 8))

    def test_gaps_are_preserved_not_filled_in(self):
        # HILL-OLT-1 really runs 1,3,4,5,6,7,8. Asking about the missing 2
        # makes a weak OLT interrogate a port that is not there.
        self.assertNotIn(2, pon_indices(["EPON0/1", "EPON0/3", "EPON0/4"]))

    def test_duplicates_and_order_collapse(self):
        self.assertEqual(pon_indices(["EPON0/4", "EPON0/1", "EPON0/4"]), (1, 4))

    def test_junk_labels_are_dropped_not_guessed_at(self):
        # A partial walk really leaves these behind on the live fleet: an empty
        # label and a bare "60". Neither names a port.
        self.assertEqual(pon_indices(["", "60", None, "  ", "EPON0/2"]), (2,))

    def test_an_absurd_index_is_refused(self):
        self.assertEqual(pon_indices([f"EPON0/{10**6}"]), ())

    def test_gpon_labels_parse_the_same_way(self):
        self.assertEqual(pon_indices(["GPON0/12", "GPON0/2"]), (2, 12))

    def test_nothing_usable_yields_nothing(self):
        # The caller — not this function — decides what an empty answer means.
        # It falls back to DEFAULT_PONS, so this must not invent one itself.
        self.assertEqual(pon_indices([]), ())
        self.assertNotEqual(pon_indices(["?"]), DEFAULT_PONS)


class NonFiniteReadingTest(unittest.TestCase):
    """A cell that parses to inf/nan is NOT a reading.

    Found in production the day this shipped: one ONU reported Tx = -inf, which
    float() accepts happily. Stored, it is a false measurement; serialised, it
    is bare `-Infinity`, which is not valid JSON — so a single bad cell took out
    the entire Optical tab for that OLT with a JSON.parse error, not just its
    own row.
    """

    def _row(self, tx="-inf", rx="-21.5", dist="4531"):
        html = (
            "<table><tr>"
            "<td class='hd'>EPON0/4:51</td><td>44:C8:74:52:47:C2</td><td></td>"
            f"<td>{dist}</td><td>47.2</td><td>3.29</td><td>12.0</td>"
            f"<td>{tx}</td><td>{rx}</td>"
            "</tr></table>")
        rows = parse_opm_diag(html)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_negative_infinity_is_dropped_not_stored(self):
        self.assertIsNone(self._row(tx="-inf")["tx_dbm"])

    def test_the_rest_of_the_row_survives_one_bad_cell(self):
        # Dropping the cell, not the ONU: its Rx is a real measurement.
        row = self._row(tx="-inf", rx="-21.5")
        self.assertIsNone(row["tx_dbm"])
        self.assertEqual(row["rx_dbm"], -21.5)
        self.assertEqual(row["serial"], "44:C8:74:52:47:C2")

    def test_nan_and_inf_spellings_are_all_refused(self):
        for spelling in ("nan", "NaN", "inf", "-inf", "Infinity", "-Infinity"):
            with self.subTest(spelling=spelling):
                self.assertIsNone(self._row(tx=spelling)["tx_dbm"])

    def test_an_infinite_integer_cell_does_not_raise(self):
        # int(float('inf')) is an OverflowError, so this guard is load-bearing
        # for more than tidiness.
        self.assertIsNone(self._row(dist="inf")["distance_m"])

    def test_what_survives_is_json_serialisable(self):
        import json
        row = self._row(tx="-inf", dist="nan")
        json.dumps(row, allow_nan=False)        # raises if anything slipped by


class DiagnoseLoginTest(unittest.TestCase):
    """A refused login must say WHICH refusal it was.

    Three unrelated faults used to print one sentence — wrong password, a page
    shape the parser doesn't know, and an address that reaches something other
    than the OLT. The first response to all three was to re-type a password,
    which fixes exactly one of them.
    """

    def test_a_key_in_an_unknown_format_is_named_as_such(self):
        # The parser was written against OPM Diag's markup; the login page is a
        # different page and may well write the key differently. Naming WHICH
        # variant is what makes the next step "add that form" rather than
        # another restart spent guessing.
        why = diagnose_login('<script>form.SessionKey.value="abc";</script>')
        self.assertIn("js-double-quote", why)
        self.assertIn("js-single-quote", why)       # what we actually read
        self.assertNotIn("abc", why)                # shape, never content

    def test_a_key_shaped_page_that_is_also_the_login_page_says_so(self):
        # The trap this branch fell into on first fleet-wide contact: it won
        # outright over the login-page check, so 8 OLTs at once reported
        # "markup differs" when the likelier truth was a refused credential.
        # Those are opposite fixes — write a parser vs. correct a password —
        # so the message has to carry both facts.
        why = diagnose_login(
            '<form action="/action/login.html"><input name="password">'
            '<input name="SessionKey" value="x"></form>')
        self.assertIn("hidden-input", why)
        self.assertIn("refused password", why)

    def test_a_re_served_login_page_reads_as_a_refused_password(self):
        why = diagnose_login(
            '<form action="/action/login.html"><input name="password"></form>')
        self.assertIn("password was refused", why)

    def test_the_optical_page_without_a_token_is_NOT_called_unrecognised(self):
        # chandana-network MAIN_OLT, 2026-08-07: a ~14 KB page titled "OLT Web
        # Management Interface", no SessionKey, and it is NOT the login page.
        # "unrecognised reply · 14017 chars" is true and useless — it cannot
        # tell a build that hands out no token (fix: a profile row) from a held
        # session (fix: log somebody out), which are opposite actions.
        why = diagnose_login(_KEYLESS_PAGE)
        self.assertIn("IS the optical page", why)
        self.assertIn("mints no token", why)        # names the actual cause
        self.assertNotIn("unrecognised", why)

    def test_a_partly_matching_page_is_called_a_profile_fault_not_a_build_quirk(self):
        # "all headings, no token" is a working build to read keyless; "most
        # headings" is a renamed column. One sentence covering both said neither.
        why = diagnose_login(_KEYLESS_PAGE.replace("RX Power", "Rx Optical Power"))
        self.assertIn("8 of 9", why)
        self.assertIn("profile", why)
        self.assertNotIn("IS the optical page", why)

    def test_a_page_that_is_not_the_optical_page_says_that_too(self):
        # The other side of the same coin: saying which it is only helps if the
        # negative is stated as a finding rather than left as silence.
        why = diagnose_login("<html><title>OLT Web Management Interface</title>"
                             "<body>session limit reached</body></html>")
        self.assertIn("NOT the optical page", why)

    def test_an_i18n_login_page_still_reads_as_a_refused_password(self):
        # This firmware builds its login form from /i18N/login_en_US.properties,
        # so the WORD "password" need never appear on a page that is plainly the
        # login form. The input's type attribute is markup, not copy, and no
        # translation bundle can hide it.
        why = diagnose_login(
            "<html><title>OLT Web Management Interface</title><body>"
            "<input type='password' id='pwd'><div id='btn'></div>"
            "</body></html>")
        self.assertIn("password was refused", why)

    def test_something_that_is_not_the_olt_says_so(self):
        # The NAT'd box: whatever answers on 443 may be a router in front.
        why = diagnose_login("<html><title>RouterOS</title></html>")
        self.assertIn("router in front", why)

    def test_an_empty_body_is_called_empty(self):
        self.assertIn("empty", diagnose_login("   "))

    def test_an_unknown_page_reports_its_structure_not_just_its_size(self):
        # "unrecognised reply (55509 chars)" cost a whole deploy cycle and said
        # nothing. The frame targets are what tell you where the real page went.
        why = diagnose_login(
            "<html><head><title>OLT Management</title></head><frameset>"
            "<frame src='/action/menu.html'><frame src='/action/systeminfo.html'>"
            "</frameset></html>")
        self.assertIn("OLT Management", why)
        self.assertIn("/action/systeminfo.html", why)

    def test_the_structural_fingerprint_is_bounded(self):
        # A broken or hostile page must not flood a log that runs forever.
        why = diagnose_login("<html><title>" + "x" * 5000 + "</title>"
                             + "".join(f"<frame src='/p{i}.html'>"
                                       for i in range(50)) + "</html>")
        self.assertLess(len(why), 600)

    def test_the_diagnosis_never_quotes_the_page(self):
        # It runs unattended on a schedule, and a login page can echo the
        # username back. Shape only, never content.
        secret = "admin_bob_2026"
        why = diagnose_login(f"<html><body>welcome {secret} to the OLT</body></html>")
        self.assertNotIn(secret, why)


NOW = "2026-07-22T12:00:00+00:00"
RECENT = "2026-07-22T11:58:00+00:00"
ANCIENT = "2026-07-21T12:00:00+00:00"


def roster(onu_key, serial, state="online", **kw):
    """An SNMP-walk roster row, as the optics fold hands it to sync_device."""
    row = {"onu_key": onu_key, "serial": serial, "state": state,
           "rx_dbm": None, "tx_dbm": None, "distance_m": None,
           "pon_port": f"EPON0/{onu_key.split('.')[0]}", "name": "sub"}
    row.update(kw)
    return row


def scraped(onu_key, serial, rx=-21.5, at=RECENT, **kw):
    row = {"onu_key": onu_key, "serial": serial, "rx_dbm": rx,
           "tx_dbm": 2.4, "distance_m": 4531, "scraped_at": at}
    row.update(kw)
    return row


class MergeScrapedTest(unittest.TestCase):
    """Folding a scrape into the SNMP roster.

    The roster is the truth about WHICH ONUs exist and what state they are in;
    the scrape only supplies numbers this firmware hides from SNMP. Every test
    here is really the same question from a different side: can a reading ever
    end up attached to the wrong ONU, or blank one that was fine?
    """

    def test_online_onu_takes_the_scraped_readings(self):
        rows, n = merge_scraped(
            [roster("3.8", "8C:A3:99:17:D3:38")],
            [scraped("3.8", "8C:A3:99:17:D3:38", rx=-2.93)], NOW, 3600)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["rx_dbm"], -2.93)
        self.assertEqual(rows[0]["tx_dbm"], 2.4)

    def test_distance_is_stored_but_deliberately_not_merged(self):
        # The page has REAL METRES and the dbc SNMP profile has time quanta, so
        # merging looks like a free fix — it isn't. The page only returns ONLINE
        # ONUs, so onu_optics.distance_m would end up metres for survivors and
        # quanta for dark ONUs, and ponfault brackets a cut between exactly
        # those two groups. A mixed-unit interval inverts; a uniformly wrong one
        # is at least monotonic. Fix the unit first (dbc scales.distance).
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22", distance_m=2764)],
            [scraped("3.8", "AA:BB:CC:00:11:22", distance_m=4531)], NOW, 3600)
        self.assertEqual(n, 1)                       # rx/tx still merged
        self.assertEqual(rows[0]["distance_m"], 2764)

    def test_the_two_views_may_punctuate_the_mac_differently(self):
        # The reg table and the HTML page are different subsystems of the same
        # firmware. Separator-exact matching would merge NOTHING here while
        # looking perfectly healthy — a blank Rx column reads as "this vendor
        # has no Rx", which is the false negative this feature exists to kill.
        rows, n = merge_scraped(
            [roster("3.8", "8ca3.9917.d338")],
            [scraped("3.8", "8C:A3:99:17:D3:38", rx=-19.0)], NOW, 3600)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["rx_dbm"], -19.0)

    def test_offline_zombie_slot_never_takes_a_reading(self):
        # C-Data reg tables keep every slot an ONU ever occupied — the byreddy
        # fleet's 178 "duplicates". The live ONU gets the reading; the ghost it
        # left on another PON must stay blank.
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22"),
             roster("1.4", "AA:BB:CC:00:11:22", state="offline")],
            [scraped("3.8", "AA:BB:CC:00:11:22", rx=-18.2)], NOW, 3600)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["rx_dbm"], -18.2)
        self.assertIsNone(rows[1]["rx_dbm"])

    def test_two_live_slots_on_one_mac_are_skipped_not_guessed(self):
        # A genuine clone or loop (2 of them in that fleet). We cannot tell
        # which ONU answered, and a reading pinned to the wrong drop sends a
        # tech to the wrong house — so neither slot gets it.
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22"),
             roster("1.4", "AA:BB:CC:00:11:22")],
            [scraped("3.8", "AA:BB:CC:00:11:22", rx=-18.2)], NOW, 3600)
        self.assertEqual(n, 0)
        self.assertIsNone(rows[0]["rx_dbm"])
        self.assertIsNone(rows[1]["rx_dbm"])

    def test_a_stale_scrape_is_dropped_whole(self):
        # A scrape that quietly stopped working must not keep yesterday's dBm
        # alive under a badge that claims to describe now.
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22")],
            [scraped("3.8", "AA:BB:CC:00:11:22", at=ANCIENT)], NOW, 3600)
        self.assertEqual(n, 0)
        self.assertIsNone(rows[0]["rx_dbm"])

    def test_a_slightly_future_stamp_survives_a_clock_step(self):
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22")],
            [scraped("3.8", "AA:BB:CC:00:11:22",
                     at="2026-07-22T12:00:30+00:00")], NOW, 3600)
        self.assertEqual(n, 1)

    def test_a_blank_scraped_column_never_erases_a_walk_value(self):
        # A gap in the scrape is not a claim that the walk was wrong.
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22", rx_dbm=-20.0, tx_dbm=1.1)],
            [scraped("3.8", "AA:BB:CC:00:11:22", rx=None, tx_dbm=None)],
            NOW, 3600)
        self.assertEqual(rows[0]["rx_dbm"], -20.0)
        self.assertEqual(rows[0]["tx_dbm"], 1.1)
        self.assertEqual(n, 0)          # nothing mergeable was actually present

    def test_the_scrape_can_never_add_an_onu(self):
        # SNMP owns roster membership. A MAC the walk never reported is not
        # evidence of a subscriber — it is evidence the walk and the page
        # disagree, and inventing a row would put an ONU on the Optical tab
        # that nothing else in the system knows about.
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22")],
            [scraped("3.8", "AA:BB:CC:00:11:22"),
             scraped("2.1", "DE:AD:BE:EF:00:01")], NOW, 3600)
        self.assertEqual(len(rows), 1)
        self.assertEqual(n, 1)

    def test_onus_the_scrape_missed_keep_their_walk_readings(self):
        # This is what makes a partial scrape safe: PON3 failing must not blank
        # PON3's ONUs, it must leave them exactly as the walk found them.
        rows, n = merge_scraped(
            [roster("1.1", "AA:AA:AA:00:00:01"),
             roster("3.9", "BB:BB:BB:00:00:02", rx_dbm=-15.0)],
            [scraped("1.1", "AA:AA:AA:00:00:01", rx=-17.0)], NOW, 3600)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["rx_dbm"], -17.0)
        self.assertEqual(rows[1]["rx_dbm"], -15.0)

    def test_same_mac_twice_in_one_scrape_resolves_by_slot(self):
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22")],
            [scraped("1.4", "AA:BB:CC:00:11:22", rx=-9.0),
             scraped("3.8", "AA:BB:CC:00:11:22", rx=-22.0)], NOW, 3600)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["rx_dbm"], -22.0)

    def test_same_mac_twice_with_no_slot_match_is_skipped(self):
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22")],
            [scraped("1.4", "AA:BB:CC:00:11:22", rx=-9.0),
             scraped("2.2", "AA:BB:CC:00:11:22", rx=-22.0)], NOW, 3600)
        self.assertEqual(n, 0)
        self.assertIsNone(rows[0]["rx_dbm"])

    def test_the_caller_s_rows_are_not_mutated(self):
        # The fold hands us the very list it is about to sync; a merge that
        # wrote through would make "what did the walk say?" unanswerable.
        original = [roster("3.8", "AA:BB:CC:00:11:22")]
        merge_scraped(original, [scraped("3.8", "AA:BB:CC:00:11:22")],
                      NOW, 3600)
        self.assertIsNone(original[0]["rx_dbm"])

    def test_no_scrape_at_all_is_a_clean_passthrough(self):
        original = [roster("3.8", "AA:BB:CC:00:11:22")]
        rows, n = merge_scraped(original, [], NOW, 3600)
        self.assertEqual(n, 0)
        self.assertEqual(rows, original)

    def test_a_row_without_a_mac_is_left_alone(self):
        rows, n = merge_scraped(
            [roster("3.8", None)],
            [scraped("3.8", None, rx=-12.0)], NOW, 3600)
        self.assertEqual(n, 0)
        self.assertIsNone(rows[0]["rx_dbm"])

    def test_an_unparseable_timestamp_is_unusable_not_fresh(self):
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22")],
            [scraped("3.8", "AA:BB:CC:00:11:22", at="not-a-time")], NOW, 3600)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
