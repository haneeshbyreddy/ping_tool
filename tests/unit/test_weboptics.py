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
        self.assertEqual(hub.seen[1]["headers"]["Cookie"], "SESSIONID=abc123")
        self.assertEqual(hub.seen[2]["headers"]["Cookie"], "SESSIONID=abc123")

    def test_repeated_set_cookie_headers_all_land(self):
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
        self.assertEqual(self.rows[0]["distance_m"], 4531)

    def test_unnamed_onu_is_none_not_empty_string(self):
        self.assertIsNone(self.rows[1]["name"])

    def test_onu_key_matches_the_snmp_roster_convention(self):
        self.assertEqual(self.rows[0]["onu_key"], "1.1")
        self.assertEqual(self.rows[3]["onu_key"], "3.29")
        self.assertEqual(self.rows[3]["pon_port"], "EPON0/3")
        self.assertEqual(self.rows[3]["onu_id"], 29)

    def test_an_overdriven_receiver_is_reported_as_measured(self):
        hot = [r for r in self.rows if r["serial"] == "8C:A3:99:17:D3:38"][0]
        self.assertEqual(hot["rx_dbm"], -2.93)

    def test_session_key_is_read_from_the_inline_script(self):
        self.assertEqual(session_key(OPM_PAGE), "kmwex")

    def test_missing_session_key_is_none_not_a_guess(self):
        self.assertIsNone(session_key("<html><body>login</body></html>"))

    def test_login_page_yields_no_readings(self):
        self.assertEqual(parse_opm_diag("<html><body>Please log in</body></html>"), [])

    def test_form_body_matches_the_captured_request(self):
        body = opm_form(2, "abc12")
        self.assertEqual(body["select"], "2")
        self.assertEqual(body["SessionKey"], "abc12")
        self.assertEqual(body["port_refresh"], "Refresh")
        self.assertEqual(body["onuid"], "0/")

def opm_reply(key, rows_html=""):
    body = (OPM_PAGE.replace("SessionKey.value = 'kmwex';",
                             f"SessionKey.value = '{key}';")).encode("gb2312")
    return reply(200, body)


_KEYLESS_PAGE = OPM_PAGE[:OPM_PAGE.index("<script>")] + "</body></html>"


def keyless_opm_reply():
    return reply(200, _KEYLESS_PAGE.encode("gb2312"))


def login_page():
    return reply(200, b"<html><body><form>login</form></body></html>")


def frameset_reply():
    return reply(200, b"<html><head><title>OLT</title></head><frameset>"
                      b"<frame src='/action/loginout.html'>"
                      b"<frame src='/action/systeminfo.html'></frameset></html>")


class ScrapeOpmTest(unittest.TestCase):
    def test_happy_path_walks_every_pon_chaining_the_key(self):
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2"), opm_reply("k3"), opm_reply("k4"),
                       opm_reply("k5")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3, 4))
        self.assertIsNone(err)
        self.assertEqual(len(rows), 16)

        self.assertEqual(hub.seen[0]["path"], "/action/login.html")
        self.assertEqual(hub.seen[1]["path"], "/action/main.html")
        self.assertEqual((hub.seen[2]["method"], hub.seen[2]["path"]),
                         ("GET", "/action/onuopmdiag.html"))
        opm = hub.seen[3:]
        self.assertEqual([s["path"] for s in opm], ["/action/onuopmdiag.html"] * 4)
        keys = [dict(p.split("=", 1) for p in s["body"].decode().split("&"))["SessionKey"]
                for s in opm]
        self.assertEqual(keys, ["k1", "k2", "k3", "k4"])
        pons = [dict(p.split("=", 1) for p in s["body"].decode().split("&"))["select"]
                for s in opm]
        self.assertEqual(pons, ["1", "2", "3", "4"])

    def test_a_build_that_mints_no_token_is_read_keyless(self):
        hub = FakeHub([login_page(), frameset_reply()]
                      + [keyless_opm_reply() for _ in range(5)])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3, 4))
        self.assertIsNone(err)
        self.assertEqual(len(rows), 16)
        for s in hub.seen[3:]:
            body = dict(p.split("=", 1) for p in s["body"].decode().split("&"))
            self.assertNotIn("SessionKey", body)
            self.assertEqual(body["port_refresh"], "Refresh")

    def test_a_page_that_is_NOT_the_table_is_still_refused_keyless(self):
        hub = FakeHub([login_page(), frameset_reply(),
                       reply(200, b"<html><title>OLT Web Management Interface"
                                  b"</title><body>session limit</body></html>")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2))
        self.assertEqual(rows, [])
        self.assertIn("login rejected", err)
        self.assertEqual(len(hub.seen), 3)

    def test_a_partial_heading_match_refuses_and_says_the_count(self):
        thin = _KEYLESS_PAGE.replace("RX Power", "Rx Optical Power")
        hub = FakeHub([login_page(), frameset_reply(),
                       reply(200, thin.encode("gb2312"))])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1,))
        self.assertEqual(rows, [])
        self.assertIn("8 of 9", err)
        self.assertEqual(len(hub.seen), 3)

    def test_a_keyless_session_lost_midway_keeps_what_it_read(self):
        hub = FakeHub([login_page(), frameset_reply(), keyless_opm_reply(),
                       keyless_opm_reply(),
                       reply(200, b"<html><body>Please log in</body></html>")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3))
        self.assertEqual(len(rows), 4)
        self.assertIn("session lost", err)

    def test_a_token_rotating_build_is_untouched_by_the_keyless_path(self):
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1,))
        self.assertIsNone(err)
        body = dict(p.split("=", 1) for p in hub.seen[3]["body"].decode().split("&"))
        self.assertEqual(body["SessionKey"], "k1")

    def test_bad_credentials_are_an_error_not_an_empty_optics_reading(self):
        hub = FakeHub([login_page(), frameset_reply(), reply(
            200, b'<html><form action="/action/login.html">'
                 b'<input name="password"></form></html>')])
        rows, err = scrape_opm(client(hub), "admin", "wrong")
        self.assertEqual(rows, [])
        self.assertIn("password was refused", err)

    def test_a_frameset_login_reply_is_success_not_rejection(self):
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1,))
        self.assertIsNone(err)
        self.assertEqual(len(rows), 4)

    def test_dormant_tunnel_fails_the_scrape_before_the_password_goes_out(self):
        hub = FakeHub([])
        rows, err = scrape_opm(client(hub), "admin", "pw")
        self.assertEqual(rows, [])
        self.assertIn("credentials NOT sent", err)
        self.assertEqual([(s["method"], s["path"]) for s in hub.seen],
                         [("GET", "/action/login.html")])

    def test_a_missing_login_page_stops_before_the_password(self):
        hub = FakeHub([reply(404, b"<html>Not Found</html>")])
        rows, err = scrape_opm(client(hub), "admin", "pw")
        self.assertEqual(rows, [])
        self.assertIn("credentials NOT sent", err)
        self.assertEqual(len(hub.seen), 1)
        self.assertNotIn("pw", str(hub.seen))

    def test_a_firmware_without_opm_diag_says_so_instead_of_404(self):
        hub = FakeHub([login_page(), frameset_reply(), reply(404, b"nope")])
        rows, err = scrape_opm(client(hub), "admin", "pw")
        self.assertEqual(rows, [])
        self.assertIn("no /action/onuopmdiag.html", err)
        self.assertIn("capture", err)

    def test_the_time_budget_keeps_the_pons_it_already_read(self):
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2"), opm_reply("k3")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3),
                               deadline=time.monotonic() - 1)
        self.assertEqual(rows, [])
        self.assertIn("time budget", err)
        self.assertIn("0 of 3", err)

    def test_partial_scrape_keeps_the_pons_that_worked(self):
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2"), opm_reply("k3"), reply(500, b"boom")])
        rows, err = scrape_opm(client(hub), "admin", "pw", pons=(1, 2, 3))
        self.assertEqual(len(rows), 8)
        self.assertIn("PON3", err)

    def test_session_stolen_midway_reports_it_plainly(self):
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
        hub = FakeHub([login_page(), frameset_reply(), opm_reply("k1"),
                       opm_reply("k2")])
        scrape_opm(client(hub), "admin", "pw", pons=(1,))
        self.assertEqual(hub.seen[0]["method"], "GET")
        self.assertEqual(hub.seen[0]["path"], "/action/login.html")
        self.assertEqual(hub.seen[1]["method"], "POST")
        self.assertEqual(hub.seen[1]["path"], "/action/main.html")


def _row(volts, tx, rx, temp="45.0", bias="9.0"):
    return (f"<tr><td class='hd'>EPON0/7:6</td><td>90:C6:82:14:48:90</td>"
            f"<td>sub</td><td>1187</td><td>{temp}</td><td>{volts}</td>"
            f"<td>{bias}</td><td>{tx}</td><td>{rx}</td></tr>")


class DdmRailTest(unittest.TestCase):

    def _one(self, html):
        rows = parse_opm_diag(f"<html><body><table>{html}</table></body></html>")
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_all_ones_rail_is_not_a_healthy_onu(self):
        row = self._one(_row("6.55", "8.16", "8.16", temp="0.0", bias="131.07"))
        self.assertIsNone(row["rx_dbm"])
        self.assertIsNone(row["tx_dbm"])
        self.assertIsNone(row["voltage_v"])
        self.assertIsNone(row["tx_bias_ma"])

    def test_the_all_zeroes_floor_would_have_paged_a_crew(self):
        row = self._one(_row("0.0", "-16.48", "-40.0", temp="56.25", bias="0.22"))
        self.assertIsNone(row["rx_dbm"])
        self.assertIsNone(row["tx_dbm"])

    def test_identity_survives_a_railed_block(self):
        row = self._one(_row("6.55", "8.16", "8.16"))
        self.assertEqual(row["onu_key"], "7.6")
        self.assertEqual(row["serial"], "90:C6:82:14:48:90")
        self.assertEqual(row["distance_m"], 1187)

    def test_a_real_reading_is_untouched(self):
        row = self._one(_row("3.24", "2.33", "-10.3", temp="47.0", bias="9.0"))
        self.assertEqual((row["voltage_v"], row["tx_dbm"], row["rx_dbm"]),
                         (3.24, 2.33, -10.3))

    def test_the_worst_real_drop_on_the_fleet_still_counts(self):
        row = self._one(_row("3.19", "1.02", "-28.24"))
        self.assertEqual(row["rx_dbm"], -28.24)

    def test_an_over_driven_onu_is_still_reported(self):
        row = self._one(_row("3.30", "2.10", "-2.87"))
        self.assertEqual(row["rx_dbm"], -2.87)


class PonIndicesTest(unittest.TestCase):

    def test_ports_come_from_the_rosters_own_labels(self):
        self.assertEqual(
            pon_indices(["EPON0/1", "EPON0/3", "EPON0/4", "EPON0/8"]),
            (1, 3, 4, 8))

    def test_gaps_are_preserved_not_filled_in(self):
        self.assertNotIn(2, pon_indices(["EPON0/1", "EPON0/3", "EPON0/4"]))

    def test_duplicates_and_order_collapse(self):
        self.assertEqual(pon_indices(["EPON0/4", "EPON0/1", "EPON0/4"]), (1, 4))

    def test_junk_labels_are_dropped_not_guessed_at(self):
        self.assertEqual(pon_indices(["", "60", None, "  ", "EPON0/2"]), (2,))

    def test_an_absurd_index_is_refused(self):
        self.assertEqual(pon_indices([f"EPON0/{10**6}"]), ())

    def test_gpon_labels_parse_the_same_way(self):
        self.assertEqual(pon_indices(["GPON0/12", "GPON0/2"]), (2, 12))

    def test_nothing_usable_yields_nothing(self):
        self.assertEqual(pon_indices([]), ())
        self.assertNotEqual(pon_indices(["?"]), DEFAULT_PONS)


class NonFiniteReadingTest(unittest.TestCase):

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
        row = self._row(tx="-inf", rx="-21.5")
        self.assertIsNone(row["tx_dbm"])
        self.assertEqual(row["rx_dbm"], -21.5)
        self.assertEqual(row["serial"], "44:C8:74:52:47:C2")

    def test_nan_and_inf_spellings_are_all_refused(self):
        for spelling in ("nan", "NaN", "inf", "-inf", "Infinity", "-Infinity"):
            with self.subTest(spelling=spelling):
                self.assertIsNone(self._row(tx=spelling)["tx_dbm"])

    def test_an_infinite_integer_cell_does_not_raise(self):
        self.assertIsNone(self._row(dist="inf")["distance_m"])

    def test_what_survives_is_json_serialisable(self):
        import json
        row = self._row(tx="-inf", dist="nan")
        json.dumps(row, allow_nan=False)


class DiagnoseLoginTest(unittest.TestCase):

    def test_a_key_in_an_unknown_format_is_named_as_such(self):
        why = diagnose_login('<script>form.SessionKey.value="abc";</script>')
        self.assertIn("js-double-quote", why)
        self.assertIn("js-single-quote", why)
        self.assertNotIn("abc", why)

    def test_a_key_shaped_page_that_is_also_the_login_page_says_so(self):
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
        why = diagnose_login(_KEYLESS_PAGE)
        self.assertIn("IS the optical page", why)
        self.assertIn("mints no token", why)
        self.assertNotIn("unrecognised", why)

    def test_a_partly_matching_page_is_called_a_profile_fault_not_a_build_quirk(self):
        why = diagnose_login(_KEYLESS_PAGE.replace("RX Power", "Rx Optical Power"))
        self.assertIn("8 of 9", why)
        self.assertIn("profile", why)
        self.assertNotIn("IS the optical page", why)

    def test_a_page_that_is_not_the_optical_page_says_that_too(self):
        why = diagnose_login("<html><title>OLT Web Management Interface</title>"
                             "<body>session limit reached</body></html>")
        self.assertIn("NOT the optical page", why)

    def test_an_i18n_login_page_still_reads_as_a_refused_password(self):
        why = diagnose_login(
            "<html><title>OLT Web Management Interface</title><body>"
            "<input type='password' id='pwd'><div id='btn'></div>"
            "</body></html>")
        self.assertIn("password was refused", why)

    def test_something_that_is_not_the_olt_says_so(self):
        why = diagnose_login("<html><title>RouterOS</title></html>")
        self.assertIn("router in front", why)

    def test_an_empty_body_is_called_empty(self):
        self.assertIn("empty", diagnose_login("   "))

    def test_an_unknown_page_reports_its_structure_not_just_its_size(self):
        why = diagnose_login(
            "<html><head><title>OLT Management</title></head><frameset>"
            "<frame src='/action/menu.html'><frame src='/action/systeminfo.html'>"
            "</frameset></html>")
        self.assertIn("OLT Management", why)
        self.assertIn("/action/systeminfo.html", why)

    def test_the_structural_fingerprint_is_bounded(self):
        why = diagnose_login("<html><title>" + "x" * 5000 + "</title>"
                             + "".join(f"<frame src='/p{i}.html'>"
                                       for i in range(50)) + "</html>")
        self.assertLess(len(why), 600)

    def test_the_diagnosis_never_quotes_the_page(self):
        secret = "admin_bob_2026"
        why = diagnose_login(f"<html><body>welcome {secret} to the OLT</body></html>")
        self.assertNotIn(secret, why)


NOW = "2026-07-22T12:00:00+00:00"
RECENT = "2026-07-22T11:58:00+00:00"
ANCIENT = "2026-07-21T12:00:00+00:00"


def roster(onu_key, serial, state="online", **kw):
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

    def test_online_onu_takes_the_scraped_readings(self):
        rows, n = merge_scraped(
            [roster("3.8", "8C:A3:99:17:D3:38")],
            [scraped("3.8", "8C:A3:99:17:D3:38", rx=-2.93)], NOW, 3600)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["rx_dbm"], -2.93)
        self.assertEqual(rows[0]["tx_dbm"], 2.4)

    def test_distance_is_stored_but_deliberately_not_merged(self):
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22", distance_m=2764)],
            [scraped("3.8", "AA:BB:CC:00:11:22", distance_m=4531)], NOW, 3600)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["distance_m"], 2764)

    def test_the_two_views_may_punctuate_the_mac_differently(self):
        rows, n = merge_scraped(
            [roster("3.8", "8ca3.9917.d338")],
            [scraped("3.8", "8C:A3:99:17:D3:38", rx=-19.0)], NOW, 3600)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["rx_dbm"], -19.0)

    def test_offline_zombie_slot_never_takes_a_reading(self):
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22"),
             roster("1.4", "AA:BB:CC:00:11:22", state="offline")],
            [scraped("3.8", "AA:BB:CC:00:11:22", rx=-18.2)], NOW, 3600)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["rx_dbm"], -18.2)
        self.assertIsNone(rows[1]["rx_dbm"])

    def test_two_live_slots_on_one_mac_are_skipped_not_guessed(self):
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22"),
             roster("1.4", "AA:BB:CC:00:11:22")],
            [scraped("3.8", "AA:BB:CC:00:11:22", rx=-18.2)], NOW, 3600)
        self.assertEqual(n, 0)
        self.assertIsNone(rows[0]["rx_dbm"])
        self.assertIsNone(rows[1]["rx_dbm"])

    def test_a_stale_scrape_is_dropped_whole(self):
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
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22", rx_dbm=-20.0, tx_dbm=1.1)],
            [scraped("3.8", "AA:BB:CC:00:11:22", rx=None, tx_dbm=None)],
            NOW, 3600)
        self.assertEqual(rows[0]["rx_dbm"], -20.0)
        self.assertEqual(rows[0]["tx_dbm"], 1.1)
        self.assertEqual(n, 0)

    def test_the_scrape_can_never_add_an_onu(self):
        rows, n = merge_scraped(
            [roster("3.8", "AA:BB:CC:00:11:22")],
            [scraped("3.8", "AA:BB:CC:00:11:22"),
             scraped("2.1", "DE:AD:BE:EF:00:01")], NOW, 3600)
        self.assertEqual(len(rows), 1)
        self.assertEqual(n, 1)

    def test_onus_the_scrape_missed_keep_their_walk_readings(self):
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
