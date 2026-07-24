import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.egress.notifiers import (
    MultiNotifier, NotifyResult, NtfyNotifier, WhatsAppFacts, WhatsAppNotifier,
    build_notifier, _wa_numbers,
)


class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakePoster:
    """Stand-in for httpx.post — records every call, returns a canned status."""
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
    """Minimal store exposing only whatsapp_settings, like the notifier reads."""
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


class WhatsAppNotifierTest(unittest.TestCase):
    def _wa(self, settings=_ENABLED, poster=None, store=True):
        poster = poster or _FakePoster()
        st = _FakeStore(settings) if store else None
        return WhatsAppNotifier(_cfg(), st, post=poster), poster

    def test_payload_shape(self):
        wa, poster = self._wa()
        res = wa.send("ntfy-topic", "🔴 DOWN", "10.0.0.1", 4,
                      whatsapp=["+91 90000 00001"],
                      facts=WhatsAppFacts("PYLON", "DOWN", "10.0.0.1", "2026-07-23"))
        self.assertTrue(res.ok)
        self.assertEqual(len(poster.calls), 1)
        call = poster.calls[0]
        self.assertEqual(call["url"], "https://graph.facebook.com/v20.0/PID/messages")
        self.assertEqual(call["headers"], {"Authorization": "Bearer TOK"})
        body = call["json"]
        self.assertEqual(body["messaging_product"], "whatsapp")
        self.assertEqual(body["to"], "919000000001")          # digits only, no '+'
        self.assertEqual(body["type"], "template")
        self.assertEqual(body["template"]["name"], "wisp_alert")
        self.assertEqual(body["template"]["language"], {"code": "en"})
        params = [p["text"] for p in body["template"]["components"][0]["parameters"]]
        self.assertEqual(params, ["PYLON", "DOWN", "10.0.0.1", "2026-07-23"])

    def test_one_message_per_number(self):
        wa, poster = self._wa()
        res = wa.send(None, "t", "b", 3,
                      whatsapp=["919000000001", "919000000002"])
        self.assertTrue(res.ok)
        self.assertEqual([c["json"]["to"] for c in poster.calls],
                         ["919000000001", "919000000002"])

    def test_disabled_is_a_noop(self):
        wa, poster = self._wa(settings={"enabled": "0", "token": "T", "phone_id": "P"})
        res = wa.send(None, "t", "b", 3, whatsapp=["919000000001"])
        self.assertFalse(res.ok)
        self.assertEqual(poster.calls, [])

    def test_unconfigured_is_a_noop(self):
        wa, poster = self._wa(settings={"enabled": "1"})  # no token/phone
        res = wa.send(None, "t", "b", 3, whatsapp=["919000000001"])
        self.assertFalse(res.ok)
        self.assertEqual(poster.calls, [])

    def test_no_numbers_is_a_noop(self):
        wa, poster = self._wa()
        res = wa.send(None, "t", "b", 3, whatsapp=[])
        self.assertFalse(res.ok)
        self.assertEqual(poster.calls, [])

    def test_settings_override_env(self):
        # env default is enable_whatsapp False; the DB toggle turns it on
        cfg = Config()
        self.assertFalse(cfg.enable_whatsapp)
        wa = WhatsAppNotifier(cfg, _FakeStore(_ENABLED), post=_FakePoster())
        res = wa.send(None, "t", "b", 3, whatsapp=["919000000001"])
        self.assertTrue(res.ok)

    def test_4xx_fails_fast_5xx_retries(self):
        wa4, p4 = self._wa(poster=_FakePoster(status_code=400))
        self.assertFalse(wa4.send(None, "t", "b", 3, whatsapp=["919000000001"]).ok)
        self.assertEqual(len(p4.calls), 1)                    # no retry on 4xx

        wa5, p5 = self._wa(poster=_FakePoster(status_code=503))
        self.assertFalse(wa5.send(None, "t", "b", 3, whatsapp=["919000000001"]).ok)
        self.assertGreater(len(p5.calls), 1)                  # retried on 5xx

    def test_poster_exception_never_raises(self):
        wa, _ = self._wa(poster=_FakePoster(raise_exc=RuntimeError("boom")))
        res = wa.send(None, "t", "b", 3, whatsapp=["919000000001"])   # must not raise
        self.assertFalse(res.ok)


class MultiNotifierTest(unittest.TestCase):
    def test_channel_is_the_primary(self):
        multi = MultiNotifier([NtfyNotifier(_cfg()),
                               WhatsAppNotifier(_cfg(), _FakeStore(_ENABLED))])
        self.assertEqual(multi.channel, "ntfy")

    def test_bad_whatsapp_never_downgrades_a_good_ntfy_page(self):
        # ntfy succeeds, whatsapp's poster raises — result stays ok (ntfy's).
        from tests.support import RecordingNotifier  # type: ignore
        ntfy = RecordingNotifier(ok=True)
        wa = WhatsAppNotifier(_cfg(), _FakeStore(_ENABLED),
                              post=_FakePoster(raise_exc=RuntimeError("bad token")))
        multi = MultiNotifier([ntfy, wa])
        res = multi.send("topic", "t", "b", 4, whatsapp=["919000000001"])
        self.assertTrue(res.ok)
        self.assertEqual(len(ntfy.sent), 1)                   # ntfy still fired

    def test_whatsapp_fails_but_channel_reports_ntfy(self):
        from tests.support import RecordingNotifier  # type: ignore
        ntfy = RecordingNotifier(ok=True)
        wa = WhatsAppNotifier(_cfg(), _FakeStore({"enabled": "0"}))
        multi = MultiNotifier([ntfy, wa])
        res = multi.send("topic", "t", "b", 4, whatsapp=["919000000001"])
        self.assertTrue(res.ok)


class BuildNotifierTest(unittest.TestCase):
    def test_edge_no_store_is_bare_ntfy(self):
        n = build_notifier(_cfg())                            # no store
        self.assertIsInstance(n, NtfyNotifier)

    def test_central_with_store_fans_out(self):
        n = build_notifier(_cfg(), _FakeStore(_ENABLED))
        self.assertIsInstance(n, MultiNotifier)
        self.assertEqual(n.channel, "ntfy")


if __name__ == "__main__":
    unittest.main()
