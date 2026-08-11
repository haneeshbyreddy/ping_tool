import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.egress.notifiers import (
    WhatsAppFacts, WhatsAppNotifier, build_notifier, _wa_numbers, _wa_time,
)


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakePoster:
    def __init__(self, status_code=200, raise_exc=None):
        self.calls = []
        self.status_code = status_code
        self.raise_exc = raise_exc

    def __call__(self, url, *, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.raise_exc is not None:
            raise self.raise_exc
        return _Resp(self.status_code)


class _FakeStore:
    def __init__(self, settings):
        self._settings = settings

    def whatsapp_settings(self):
        return dict(self._settings)


_ENABLED = {"enabled": "1", "token": "TOK", "phone_id": "PID"}


def _cfg():
    return Config()


class NumberNormalizeTest(unittest.TestCase):
    def test_strips_to_digits_and_dedups(self):
        self.assertEqual(
            _wa_numbers(["+91 90000-00001", "(919) 000 000 002", "919000000001"]),
            ["919000000001", "919000000002"])

    def test_drops_too_short_and_blank(self):
        self.assertEqual(_wa_numbers(["", "123", None, "abc"]), [])


class FactsTest(unittest.TestCase):
    def test_four_params_collapse_whitespace_and_never_empty(self):
        f = WhatsAppFacts("a\nb", "", "  x\ty  ", "2026")
        self.assertEqual(f.params(), ["a b", "—", "x y", "2026"])

    def test_derive_fills_status_and_detail(self):
        f = WhatsAppFacts.derive("Title", "", status="PORT_DOWN", ts="t")
        self.assertEqual(f.params(), ["Title", "PORT_DOWN", "—", "t"])


class DisplayTimeTest(unittest.TestCase):
    def test_utc_instant_renders_in_the_display_zone(self):
        self.assertEqual(_wa_time("2026-07-25T09:12:03+00:00"),
                         "25 Jul 2026, 2:42 PM IST")

    def test_naive_sqlite_stamp_is_treated_as_utc(self):
        self.assertEqual(_wa_time("2026-07-25 18:45:00"),
                         "26 Jul 2026, 12:15 AM IST")

    def test_zone_is_configurable_and_unknown_zones_fall_back_to_utc(self):
        self.assertEqual(_wa_time("2026-07-25T09:12:03+00:00", "America/New_York"),
                         "25 Jul 2026, 5:12 AM EDT")
        self.assertEqual(_wa_time("2026-07-25T09:12:03+00:00", "Nowhere/Bogus"),
                         "25 Jul 2026, 9:12 AM UTC")

    def test_a_non_timestamp_passes_through_rather_than_raising(self):
        self.assertEqual(_wa_time("t"), "t")
        self.assertEqual(_wa_time(""), "")
        self.assertEqual(_wa_time(None), "")


class WhatsAppNotifierTest(unittest.TestCase):
    def _wa(self, settings=_ENABLED, poster=None, store=True):
        poster = poster or _FakePoster()
        st = _FakeStore(settings) if store else None
        return WhatsAppNotifier(_cfg(), st, post=poster), poster

    def test_payload_shape(self):
        wa, poster = self._wa()
        res = wa.send("🔴 DOWN", "10.0.0.1", 4,
                      whatsapp=["+91 90000 00001"],
                      facts=WhatsAppFacts("PYLON", "DOWN", "10.0.0.1",
                                          "2026-07-23T04:05:00+00:00"))
        self.assertTrue(res.ok)
        self.assertEqual(len(poster.calls), 1)
        call = poster.calls[0]
        self.assertEqual(call["url"], "https://graph.facebook.com/v20.0/PID/messages")
        self.assertEqual(call["headers"], {"Authorization": "Bearer TOK"})
        body = call["json"]
        self.assertEqual(body["messaging_product"], "whatsapp")
        self.assertEqual(body["to"], "919000000001")
        self.assertEqual(body["type"], "template")
        self.assertEqual(body["template"]["name"], "wisp_alert1")
        self.assertEqual(body["template"]["language"], {"code": "en"})
        params = [p["text"] for p in body["template"]["components"][0]["parameters"]]
        self.assertEqual(params, ["PYLON", "DOWN", "10.0.0.1",
                                  "23 Jul 2026, 9:35 AM IST"])

    def test_one_message_per_number(self):
        wa, poster = self._wa()
        res = wa.send("t", "b", 3, whatsapp=["919000000001", "919000000002"])
        self.assertTrue(res.ok)
        self.assertEqual([c["json"]["to"] for c in poster.calls],
                         ["919000000001", "919000000002"])

    def test_disabled_is_a_noop(self):
        wa, poster = self._wa(settings={"enabled": "0", "token": "T", "phone_id": "P"})
        res = wa.send("t", "b", 3, whatsapp=["919000000001"])
        self.assertFalse(res.ok)
        self.assertEqual(poster.calls, [])

    def test_unconfigured_is_a_noop(self):
        wa, poster = self._wa(settings={"enabled": "1"})
        res = wa.send("t", "b", 3, whatsapp=["919000000001"])
        self.assertFalse(res.ok)
        self.assertEqual(poster.calls, [])

    def test_no_numbers_is_a_noop(self):
        wa, poster = self._wa()
        res = wa.send("t", "b", 3, whatsapp=[])
        self.assertFalse(res.ok)
        self.assertEqual(poster.calls, [])

    def test_db_toggle_can_disable(self):
        cfg = Config()
        self.assertTrue(cfg.enable_whatsapp)
        wa = WhatsAppNotifier(cfg, _FakeStore({"enabled": "0", "token": "T",
                                               "phone_id": "P"}), post=_FakePoster())
        self.assertFalse(wa.send("t", "b", 3, whatsapp=["919000000001"]).ok)

    def test_4xx_fails_fast_5xx_retries(self):
        wa4, p4 = self._wa(poster=_FakePoster(status_code=400))
        self.assertFalse(wa4.send("t", "b", 3, whatsapp=["919000000001"]).ok)
        self.assertEqual(len(p4.calls), 1)

        wa5, p5 = self._wa(poster=_FakePoster(status_code=503))
        self.assertFalse(wa5.send("t", "b", 3, whatsapp=["919000000001"]).ok)
        self.assertGreater(len(p5.calls), 1)

    def test_poster_exception_never_raises(self):
        wa, _ = self._wa(poster=_FakePoster(raise_exc=RuntimeError("boom")))
        res = wa.send("t", "b", 3, whatsapp=["919000000001"])
        self.assertFalse(res.ok)


class WhatsAppFreeFormTest(unittest.TestCase):
    def _wa(self, settings=_ENABLED):
        poster = _FakePoster()
        return WhatsAppNotifier(_cfg(), _FakeStore(settings), post=poster), poster

    def test_send_text_shape_keeps_newlines(self):
        wa, poster = self._wa()
        res = wa.send_text("+91 90000 00001", "line1\nline2")
        self.assertTrue(res.ok)
        body = poster.calls[0]["json"]
        self.assertEqual(body["to"], "919000000001")
        self.assertEqual(body["type"], "text")
        self.assertEqual(body["text"], {"preview_url": False, "body": "line1\nline2"})

    def test_send_buttons_shape_and_caps_at_three(self):
        wa, poster = self._wa()
        res = wa.send_buttons("919000000001", "pick one", [
            ("refresh:1", "Refresh dBm"), ("map:1", "On map"),
            ("recent:1", "Recent"), ("extra:1", "Dropped")])
        self.assertTrue(res.ok)
        body = poster.calls[0]["json"]
        self.assertEqual(body["type"], "interactive")
        self.assertEqual(body["interactive"]["type"], "button")
        self.assertEqual(body["interactive"]["body"]["text"], "pick one")
        btns = body["interactive"]["action"]["buttons"]
        self.assertEqual(len(btns), 3)
        self.assertEqual(btns[0],
                         {"type": "reply", "reply": {"id": "refresh:1", "title": "Refresh dBm"}})

    def test_button_title_truncated_to_twenty(self):
        wa, poster = self._wa()
        wa.send_buttons("919000000001", "b", [("x", "A" * 30)])
        btn = poster.calls[0]["json"]["interactive"]["action"]["buttons"][0]
        self.assertEqual(btn["reply"]["title"], "A" * 20)

    def test_empty_buttons_falls_back_to_text(self):
        wa, poster = self._wa()
        wa.send_buttons("919000000001", "just text", [])
        self.assertEqual(poster.calls[0]["json"]["type"], "text")

    def test_disabled_free_form_is_a_noop(self):
        wa, poster = self._wa(settings={"enabled": "0", "token": "T", "phone_id": "P"})
        self.assertFalse(wa.send_text("919000000001", "hi").ok)
        self.assertEqual(poster.calls, [])

    def test_free_form_never_raises(self):
        wa = WhatsAppNotifier(_cfg(), _FakeStore(_ENABLED),
                              post=_FakePoster(raise_exc=RuntimeError("boom")))
        self.assertFalse(wa.send_text("919000000001", "hi").ok)


class BuildNotifierTest(unittest.TestCase):
    def test_edge_no_store_is_whatsapp_but_inert(self):
        n = build_notifier(_cfg())
        self.assertIsInstance(n, WhatsAppNotifier)
        self.assertEqual(n.channel, "whatsapp")

    def test_central_with_store(self):
        n = build_notifier(_cfg(), _FakeStore(_ENABLED))
        self.assertIsInstance(n, WhatsAppNotifier)
        self.assertEqual(n.channel, "whatsapp")


if __name__ == "__main__":
    unittest.main()
