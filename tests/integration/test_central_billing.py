"""Billing v2 over HTTP: the route surface and the end-to-end wiring.

The pure math lives in tests/unit/test_metering.py and the engine glue in
tests/unit/test_billing.py. THIS file only asserts what a browser, a gateway
or an edge probe can actually reach: who may read the ledger, what a locked
org can still do, and whether money posted by a webhook moves the number the
owner is looking at.

Two rules are pinned harder than anything else here, because both are the kind
of own-goal that is invisible in review and fatal in the field:

  * a LOCKED org must still reach the pay screen. Gating /api/billing behind
    the paywall it exists to clear leaves an owner with a 402 and no way out;
  * edge ingest, monitoring and paging are NEVER gated. A lapsed bill must not
    silence an alarm.

Nothing here reaches the network. The gateway is configured through the
install's own SecretBox and exercised on the two paths that need no HTTP call
out: the signed webhook and the signature-only browser return.
"""

import hashlib
import hmac
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central import auth, billing, metering, payments, secretbox
from wisp.central.store import CentralStore
from wisp.central.server import make_server
from wisp.egress.notifiers import _display_zone
from support import RecordingNotifier

ORG = "ispA"
PASSWORDS = {"root": "rootpassword", "owner": "ownerpassword",
             "field": "fieldpassword"}

# The gateway's two secrets. Both are OURS in these tests: the webhook body is
# signed with the same string the store holds, which is the whole point of the
# signature check.
KEY_SECRET = "rzp_secret_forthetests"
WEBHOOK_SECRET = "whsec_forthetests"


class BillingHttp(unittest.TestCase):
    """One org, an owner, a worker and a superadmin, all over real HTTP.

    The sweep runs on a thread in production; here it is driven by hand so a
    day boundary is an argument rather than a wait.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0,
                          central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", PASSWORDS["root"])
        auth.create_user(self.store, ORG, "owner", PASSWORDS["owner"], "owner")
        auth.create_user(self.store, ORG, "field", PASSWORDS["field"], "worker")
        self.store.set_org(ORG, name="Acme")
        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.sweeper = billing.BillingSweeper(self.store, self.cfg,
                                              self.notifier)
        self.today = billing.operator_today(self.cfg)
        self._cookies: dict = {}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    # ------------------------------------------------------------- the wire

    def _req(self, method, path, body=None, cookie=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _get_bytes(self, path, cookie=None):
        """A binary GET: the invoice is a PDF, not JSON."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        resp = conn.getresponse()
        raw = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, raw, headers

    def _post_raw(self, path, raw: bytes, headers=None):
        """A POST of EXACT bytes. The webhook's HMAC covers the body as sent,
        so the signed bytes and the sent bytes have to be the same object."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        head = {"Content-Type": "application/json"}
        head.update(headers or {})
        conn.request("POST", path, body=raw, headers=head)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, (json.loads(body) if body else {})

    def _login(self, username="owner"):
        if username in self._cookies:
            return self._cookies[username]
        status, _, setcookie = self._req(
            "POST", "/api/login",
            {"username": username, "password": PASSWORDS[username]})
        self.assertEqual(status, 200)
        self._cookies[username] = setcookie.split(";")[0]
        return self._cookies[username]

    def _doc(self, who="owner", path="/api/billing"):
        status, body, _ = self._req("GET", path, cookie=self._login(who))
        self.assertEqual(status, 200, body)
        return body

    def _admin(self, body):
        status, reply, _ = self._req("POST", "/api/admin/billing", body,
                                     cookie=self._login("root"))
        return status, reply

    # ---------------------------------------------------------- the fixture

    def _at(self, day, hour=12):
        """An instant that lands on `day` in the OPERATOR's zone.

        The billing day boundary is WISP_DISPLAY_TZ, so a naive "noon UTC"
        would land on the wrong day for any install west of Greenwich.
        """
        return datetime(day.year, day.month, day.day, hour,
                        tzinfo=_display_zone(self.cfg.display_tz))

    def _days_ago(self, n):
        return self.today - timedelta(days=n)

    def _device(self, name="Tower A", ip="10.0.0.1", device_type=None):
        return self.store.create_org_device(ORG, {
            "name": name, "ip_address": ip, "device_type": device_type,
            "region": None, "parent_device_id": None,
            "assigned_node_id": "edge-1"})

    def _accrue(self, day, paise, **kw):
        """One accrual row written straight to the ledger. Stamping money by
        hand keeps the invoice and lock tests independent of whatever the
        counting feeds happen to answer."""
        self.store.insert_accrual(ORG, metering.AccrualRow(
            day=day, paise=paise, conn_count=kw.get("conn_count", 0),
            conn_source=kw.get("conn_source", "none"),
            device_count=kw.get("device_count", 0),
            winning_side=kw.get("winning_side", "conn"),
            conn_rate_paise=metering.DEFAULT_CONN_PAISE,
            floor_paise=metering.DEFAULT_FLOOR_PAISE,
            flags=kw.get("flags", {})))

    def _overdue_invoice(self, paise=120000, months_back=2):
        """An invoice old enough that the ladder has certainly passed the
        banner. Anchored to the operator's REAL today: server.py's gate calls
        org_locked with no clock of its own, so a fixed date would pass or
        fail depending on the calendar."""
        month = metering.month_key(self.today)
        for _ in range(months_back):
            month = metering.prev_month(month)
        self.store.ensure_invoice(ORG, month, paise)
        return month

    # -- the connection-count feeds, driven through the real store ----------

    def _panel(self, label, *, enabled=True):
        return self.store.set_radius_account(
            ORG, profile="cbp", base_url=f"https://{label}.example.in",
            username=label, password_enc="enc", label=label, enabled=enabled,
            updated_by="t")

    def _book(self, account, customers):
        """ONE read carrying the whole book; an entry is a username or a
        (username, status) pair.

        One upsert call is ONE read: a customer per call leaves every row but
        the last outside the panel's latest seen_seq, which is true of the
        fixture and never of a sync."""
        rows = []
        for entry in customers:
            username, status = (entry if isinstance(entry, tuple)
                                else (entry, "active"))
            rows.append({"username": username, "name": username.upper(),
                         "status": status, "expiry": "06/01/2027 09:24",
                         "package": "PLAN"})
        self.store.upsert_radius_customers(ORG, account, rows)

    @staticmethod
    def _stamp(when):
        """The shape every real writer stores: UTC, seconds, +00:00."""
        return when.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _panel_synced(self, account, when):
        """Stamp when this panel last synced successfully.

        set_radius_status stamps the wall clock, so the age the latch reads is
        written straight afterwards. Ageing the STAMP is how a failing sync is
        modelled: the latch's whole input is that timestamp, and reaching past
        it into metering would test the double instead of the wiring."""
        self.store.set_radius_status(ORG, account, "ok", customers=1)
        with self.store._connect() as conn:
            conn.execute("UPDATE radius_status SET last_ok_at=?"
                         " WHERE account_id=?",
                         (self._stamp(when), int(account)))
            conn.commit()

    def _roster(self, device_id, serials, when, state="online"):
        for i, serial in enumerate(serials, start=1):
            self.store.upsert_onu_optics(
                ORG, device_id, f"1.{i}", pon_port="EPON0/1", onu_id=i,
                name=f"sub{i}", serial=serial, state=state, rx_dbm=-21.0,
                tx_dbm=None, olt_rx_dbm=None, distance_m=None, rx_ref_dbm=None,
                rx_ref_at=None, severity="ok", ts=self._stamp(when))

    # -- the gateway --------------------------------------------------------

    def _enable_payments(self):
        """Point the install at a gateway without touching the network.

        The secrets go through the SAME box make_server built: from_config
        reads the key file beside central_db, so this is the server's own key
        and not a second one that would decrypt to nothing."""
        box = secretbox.from_config(self.cfg)
        self.store.set_setting(payments.PROVIDER_KEY, "razorpay")
        self.store.set_setting(payments.KEY_ID_KEY, "rzp_test_key")
        self.store.set_setting(payments.KEY_SECRET_KEY, box.encrypt(KEY_SECRET))
        self.store.set_setting(payments.WEBHOOK_SECRET_KEY,
                               box.encrypt(WEBHOOK_SECRET))

    @staticmethod
    def _capture_body(*, payment_id="pay_ABC123", paise=50000, org=ORG,
                      event="payment.captured", order_id="order_1") -> bytes:
        entity = {"id": payment_id, "amount": paise, "order_id": order_id,
                  "notes": ({"org_id": org} if org else {})}
        return json.dumps({"event": event,
                           "payload": {"payment": {"entity": entity}}}).encode()

    def _webhook(self, raw: bytes, *, signature=None,
                 secret=WEBHOOK_SECRET):
        sig = signature if signature is not None else hmac.new(
            secret.encode(), raw, hashlib.sha256).hexdigest()
        return self._post_raw("/payments/webhook", raw,
                              {"X-Razorpay-Signature": sig})


# ---------------------------------------------------------------- the document

class BillingDocumentTest(BillingHttp):
    """GET /api/billing: one shape for every state, owner and superadmin."""

    def test_a_fresh_org_reads_clear_and_owes_nothing(self):
        doc = self._doc()
        self.assertEqual(doc["org_id"], ORG)
        self.assertEqual(doc["org_name"], "Acme")
        self.assertEqual(doc["stage"], "clear")
        self.assertFalse(doc["locked"])
        self.assertEqual(doc["outstanding_paise"], 0)
        self.assertIsNone(doc["open_invoice"])
        self.assertEqual(doc["month"], metering.month_key(self.today))
        self.assertEqual(doc["accruals"], [])
        self.assertEqual(doc["month_to_date_paise"], 0)

    def test_the_ledger_is_not_readable_without_a_session(self):
        status, _, _ = self._req("GET", "/api/billing")
        self.assertEqual(status, 401)

    def test_the_superadmin_reads_one_named_org_and_never_guesses(self):
        """_scope_org yields None for an org-less superadmin: an unscoped read
        must ask rather than pick an org's ledger at random."""
        root = self._login("root")
        self.assertEqual(self._req("GET", "/api/billing", cookie=root)[0], 400)
        status, doc, _ = self._req("GET", f"/api/billing?org={ORG}",
                                   cookie=root)
        self.assertEqual(status, 200)
        self.assertEqual(doc["org_id"], ORG)

    def test_the_document_carries_the_gateway_state_not_a_broken_button(self):
        """Dormant payments are a sentence the SPA composes, so the facts have
        to ride the document rather than being inferred from a 503."""
        self.store.set_setting("whatsapp_admin_number", "919999999999")
        doc = self._doc()
        self.assertFalse(doc["payment"]["enabled"])
        self.assertIsNone(doc["payment"]["provider"])
        self.assertEqual(doc["payment"]["admin_contact"], "919999999999")
        self._enable_payments()
        doc = self._doc()
        self.assertTrue(doc["payment"]["enabled"])
        self.assertEqual(doc["payment"]["provider"], "razorpay")
        self.assertEqual(doc["payment"]["key_id"], "rzp_test_key")
        # The key id is public by design; the secrets never leave the box.
        self.assertNotIn(KEY_SECRET, json.dumps(doc))
        self.assertNotIn(WEBHOOK_SECRET, json.dumps(doc))

    def test_passives_never_count_toward_the_device_floor(self):
        self._device("Tower A")
        # Passive plant carries no IP by construction, and it never pages, so
        # it never counts toward the floor either.
        self._device("Splitter 1", ip="", device_type="splitter")
        self.assertEqual(self._doc()["device_count"], 1)

    def test_the_gone_plan_route_answers_a_SENTENCE_not_a_404(self):
        """The SPA deploys instantly and central needs a restart, so a stale
        tab posts here for a window. 404 renders as a broken button; the
        sentence tells the operator to reload."""
        status, body, _ = self._req("POST", "/api/billing/plan",
                                    {"plan": "pro"}, cookie=self._login())
        self.assertEqual(status, 422)
        self.assertIn("metered", body["error"].lower())


class NoDeclarationTest(BillingHttp):
    """The owner-facing declaration was REMOVED on the operator's call
    (2026-08-17): a customer typing their own billable number is not a
    measurement. There must be no owner write that moves the bill at all."""

    def test_the_declare_route_is_gone(self):
        self.assertEqual(self._req("POST", "/api/billing/declare",
                                   {"count": 40}, cookie=self._login())[0], 404)

    def test_no_owner_write_can_move_what_the_org_is_billed_on(self):
        """Everything an owner may POST is either paying or verifying a
        payment. Nothing changes the count, the rate or the flags."""
        cookie = self._login()
        for path, body in (("/api/billing/declare", {"count": 40}),
                           ("/api/billing/rate", {"conn_rate_paise": 1}),
                           ("/api/billing/exempt", {"exempt": True})):
            self.assertEqual(self._req("POST", path, body, cookie=cookie)[0],
                             404, path)
        self.assertIsNone(self.store.org_billing(ORG)["self_declared_conns"])

    def test_an_org_with_no_measurable_source_pays_the_device_FLOOR(self):
        """The honest answer once the declaration is gone: nothing measured
        the connections, so the count is zero and the floor carries the bill.
        It is never a guess and never an invented number."""
        self._device()
        self.sweeper.sweep()
        today = self._doc()["today"]
        self.assertEqual(today["conn_source"], "none")
        self.assertEqual(today["conn_count"], 0)
        self.assertEqual(today["winning_side"], "floor")
        self.assertGreater(today["paise"], 0)


class AccrualOverHttpTest(BillingHttp):
    """The sweep's output as the dashboard and the console read it back."""

    def test_sweeping_twice_in_one_day_leaves_ONE_accrual_row(self):
        self._device()
        self.sweeper.sweep()
        self.sweeper.sweep()
        doc = self._doc()
        self.assertEqual([a["day"] for a in doc["accruals"]],
                         [self.today.isoformat()])
        self.assertEqual(doc["month_to_date_paise"], doc["accruals"][0]["paise"])
        self.assertGreater(doc["month_to_date_paise"], 0)

    def test_a_gap_backfills_and_the_console_NAMES_the_backfilled_days(self):
        """Central was down for two days. The days are charged forward from
        the last known counts and every one of them says so: a backfilled day
        and a measured day must never read alike."""
        self._device()
        self.sweeper.sweep(self._at(self._days_ago(3)))
        self.sweeper.sweep()
        status, console, _ = self._req("GET", f"/api/admin/billing?org={ORG}",
                                       cookie=self._login("root"))
        self.assertEqual(status, 200)
        rows = {a["day"]: a for a in console["ledger"]["accruals"]}
        for n in (2, 1):
            day = self._days_ago(n).isoformat()
            self.assertTrue(rows[day]["flags"].get("backfilled"), day)
        self.assertNotIn("backfilled",
                         rows[self.today.isoformat()]["flags"])
        self.assertNotIn("backfilled",
                         rows[self._days_ago(3).isoformat()]["flags"])


class OnuCountTest(BillingHttp):
    """THE BILL IS PER ONU (2026-08-17), driven through the real roster.

    One measuring rung: distinct ONUs seen online inside the window. RADIUS is
    not a metering input any more, and the fall-through lands on the device
    floor rather than on a second opinion."""

    MACS = ["AA:11:22:33:44:55", "AA:11:22:33:44:66", "AA:11:22:33:44:77"]

    def test_the_roster_is_the_count(self):
        olt = self._device("HLY-OLT-1")
        self._roster(olt, self.MACS, self._at(self.today))
        self.sweeper.sweep()
        today = self._doc()["today"]
        self.assertEqual(today["conn_source"], "onu")
        self.assertEqual(today["conn_count"], 3)

    def test_one_ONU_on_TWO_slots_is_ONE_connection(self):
        """These OLTs never drop a vacated registration, so a re-registered box
        sits on its old slot and its new one. Counting slots bills the same
        subscriber twice for the ISP's own RMA."""
        olt = self._device("HLY-OLT-1")
        for key in ("1.4", "1.9"):
            self.store.upsert_onu_optics(
                ORG, olt, key, pon_port="EPON0/1", onu_id=1, name="sub",
                serial="AA:11:22:33:44:55", state="online", rx_dbm=-21.0,
                tx_dbm=None, olt_rx_dbm=None, distance_m=None, rx_ref_dbm=None,
                rx_ref_at=None, severity="ok", ts=self._stamp(self._at(self.today)))
        self.sweeper.sweep()
        self.assertEqual(self._doc()["today"]["conn_count"], 1)

    def test_the_count_keys_on_the_SAME_identity_as_the_roster(self):
        """`wisp_norm_mac` and nothing else: case and surrounding whitespace
        collapse, separators do NOT. That is deliberate and it is not this
        module's decision to revisit — a punctuation-blind identity fabricated
        duplicate-MAC pages once, and one ONU registers on one OLT, so both
        readings of a box come off one firmware printing one way."""
        olt = self._device("HLY-OLT-1")
        for key, serial in (("1.4", "AA:11:22:33:44:55"),
                            ("1.9", " aa:11:22:33:44:55 ")):
            self.store.upsert_onu_optics(
                ORG, olt, key, pon_port="EPON0/1", onu_id=1, name="sub",
                serial=serial, state="online", rx_dbm=-21.0, tx_dbm=None,
                olt_rx_dbm=None, distance_m=None, rx_ref_dbm=None,
                rx_ref_at=None, severity="ok", ts=self._stamp(self._at(self.today)))
        self.sweeper.sweep()
        self.assertEqual(self._doc()["today"]["conn_count"], 1)

    def test_an_ONU_dark_longer_than_the_window_is_outside_the_count(self):
        """last_online_at freezes off-online, so a box that has not come up in
        over a week stops being billable while the walk itself stays fresh —
        which is the difference between a subscriber and a dead slot."""
        olt = self._device("HLY-OLT-1")
        gone = metering.ONU_ONLINE_WINDOW_DAYS + 3
        self._roster(olt, ["AA:11:22:33:44:55"], self._at(self._days_ago(gone)))
        self._roster(olt, ["AA:11:22:33:44:55"], self._at(self.today),
                     state="offline")
        self._roster(olt, ["AA:11:22:33:44:66"], self._at(self.today))
        self.sweeper.sweep()
        today = self._doc()["today"]
        self.assertEqual(today["conn_source"], "onu")
        self.assertEqual(today["conn_count"], 1)

    def test_a_stalled_walk_HOLDS_its_last_good_count(self):
        """An OLT fleet that stopped answering keeps its last good count while
        it is broken, and the row says so. With one rung there is nothing
        underneath to catch a walk that breaks, so this latch is the whole
        protection against a bill moving on its own."""
        olt = self._device("HLY-OLT-1")
        self._roster(olt, self.MACS, self._at(self._days_ago(3)))
        self.sweeper.sweep()
        today = self._doc()["today"]
        self.assertEqual(today["conn_source"], "held")
        self.assertEqual(today["flags"]["held"], "onu")
        self.assertEqual(today["conn_count"], 3)

    def test_past_the_hold_window_the_count_falls_through_to_the_FLOOR(self):
        """Past HOLD_DAYS the hold expires. There is no second source to fall
        onto, so the day is billed on the device floor and the row carries the
        downgrade for the console and the digest to report."""
        olt = self._device("HLY-OLT-1")
        self._roster(olt, self.MACS, self._at(self._days_ago(1)))
        self.sweeper.sweep(self._at(self._days_ago(1)))
        self._roster(olt, self.MACS,
                     self._at(self._days_ago(metering.HOLD_DAYS + 2)))
        self.sweeper.sweep()
        today = self._doc()["today"]
        self.assertEqual(today["conn_source"], "none")
        self.assertEqual(today["conn_count"], 0)
        self.assertEqual(today["flags"]["downgraded"],
                         {"from": "onu", "to": "none"})
        # The floor is what stops a stalled walk from billing an org nothing.
        self.assertEqual(today["winning_side"], "floor")
        self.assertGreater(today["paise"], 0)

    def test_a_full_RADIUS_BOOK_does_not_move_the_bill(self):
        """The 2026-08-17 basis change, pinned. RADIUS sync still runs for the
        customers page, and a healthy panel with a book of 500 must not add a
        paise to the meter: this org has no roster, so it counts zero and pays
        the device floor."""
        account = self._panel("cbp")
        self._book(account, [f"sub{i}" for i in range(500)])
        self._panel_synced(account, self._at(self.today))
        self._device("HLY-OLT-1")
        self.sweeper.sweep()
        today = self._doc()["today"]
        self.assertEqual(today["conn_count"], 0)
        self.assertEqual(today["conn_source"], "none")
        self.assertEqual(today["winning_side"], "floor")


# ----------------------------------------------------------------- invoices

class InvoiceTest(BillingHttp):
    """Closing a month, and the PDF an owner downloads afterwards."""

    def _closed_month(self, amounts=(4000, 4000, 4000)):
        month = metering.prev_month(metering.month_key(self.today))
        for i, paise in enumerate(amounts, start=15):
            self._accrue(f"{month}-{i:02d}", paise, conn_count=12,
                         conn_source="radius", device_count=2)
        # A row for today as well, so the catch-up pass has nothing to
        # backfill and the month's total is exactly what was stamped.
        self._accrue(self.today.isoformat(), 0)
        self.sweeper.sweep()
        return month

    def test_a_finished_month_closes_into_ONE_invoice_for_the_SUM(self):
        month = self._closed_month()
        doc = self._doc()
        self.assertEqual([i["month"] for i in doc["invoices"]], [month])
        self.assertEqual(doc["invoices"][0]["paise"], 12000)
        self.assertEqual(doc["invoices"][0]["status"], "open")
        self.assertEqual(doc["open_invoice"]["month"], month)

    def test_sweeping_again_does_not_double_invoice(self):
        month = self._closed_month()
        self.sweeper.sweep()
        self.sweeper.sweep()
        invoices = [i for i in self._doc()["invoices"] if i["month"] == month]
        self.assertEqual(len(invoices), 1)

    def test_the_invoice_downloads_as_a_real_PDF_named_for_its_month(self):
        month = self._closed_month()
        status, raw, headers = self._get_bytes(
            f"/api/billing/invoice?month={month}", cookie=self._login())
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"%PDF"), raw[:16])
        self.assertEqual(headers["content-type"], "application/pdf")
        self.assertEqual(headers["content-disposition"],
                         f'attachment; filename="invoice-{ORG}-{month}.pdf"')
        self.assertEqual(headers["cache-control"], "no-store")

    def test_a_month_with_no_invoice_is_a_404_and_a_bad_month_a_400(self):
        """Not billed yet, and not a month at all, take different fixes: they
        cannot share a status code."""
        self._closed_month()
        cookie = self._login()
        self.assertEqual(self._get_bytes(
            f"/api/billing/invoice?month={metering.month_key(self.today)}",
            cookie=cookie)[0], 404)
        for bad in ("2026-13", "nonsense", ""):
            self.assertEqual(
                self._get_bytes(f"/api/billing/invoice?month={bad}",
                                cookie=cookie)[0], 400, bad)


# ------------------------------------------------------------------ payments

class PaymentWebhookTest(BillingHttp):
    """POST /payments/webhook: no session, signature-verified, replay-safe."""

    def setUp(self):
        super().setUp()
        self._enable_payments()
        self._device()
        self.sweeper.sweep()

    def test_a_signed_capture_records_the_money_and_outstanding_FALLS(self):
        before = self._doc()["outstanding_paise"]
        status, body = self._webhook(self._capture_body(paise=50000))
        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])
        doc = self._doc()
        self.assertEqual(doc["outstanding_paise"], before - 50000)
        payment = doc["payments"][0]
        self.assertEqual(payment["kind"], "gateway")
        self.assertEqual(payment["paise"], 50000)
        self.assertEqual(payment["provider"], "razorpay")
        self.assertEqual(payment["provider_payment_id"], "pay_ABC123")

    def test_a_REPLAYED_capture_is_a_200_no_op_and_never_double_credits(self):
        """Gateways re-deliver. A 4xx would make it retry forever; a second
        ledger row would credit the org twice for one payment."""
        raw = self._capture_body(paise=50000)
        self.assertTrue(self._webhook(raw)[1]["recorded"])
        after_first = self._doc()["outstanding_paise"]
        status, body = self._webhook(raw)
        self.assertEqual(status, 200)
        self.assertFalse(body["recorded"])
        self.assertEqual(body["reason"], "replay")
        doc = self._doc()
        self.assertEqual(doc["outstanding_paise"], after_first)
        self.assertEqual(len(doc["payments"]), 1)

    def test_a_webhook_we_cannot_verify_DOES_NOT_EXIST(self):
        raw = self._capture_body()
        self.assertEqual(self._webhook(raw, signature="deadbeef")[0], 400)
        self.assertEqual(self._webhook(raw, secret="the-wrong-secret")[0], 400)
        self.assertEqual(self._post_raw("/payments/webhook", raw)[0], 400)
        self.assertEqual(self._doc()["payments"], [])

    def test_a_FAILED_payment_records_nothing(self):
        """An authorization that failed is not money."""
        status, body = self._webhook(
            self._capture_body(event="payment.failed"))
        self.assertEqual(status, 200)
        self.assertFalse(body["recorded"])
        self.assertEqual(self._doc()["payments"], [])

    def test_an_unattributable_capture_is_accepted_and_posted_NOWHERE(self):
        """No org on the notes, or an org this install has never heard of:
        nothing to post, and nothing a retry would fix."""
        for raw in (self._capture_body(org=None, payment_id="pay_noorg"),
                    self._capture_body(org="nosuchorg", payment_id="pay_bad")):
            status, body = self._webhook(raw)
            self.assertEqual(status, 200)
            self.assertFalse(body["recorded"])
        self.assertEqual(self._doc()["payments"], [])

    def test_a_gateway_with_no_webhook_secret_verifies_NOTHING(self):
        """Checkout only needs the key pair, so the operator can have a live
        pay button and no webhook secret. Every event then arrives unverifiable
        and the honest answer is to refuse it rather than credit on trust."""
        self.store.set_setting(payments.WEBHOOK_SECRET_KEY, "")
        self.assertEqual(self._webhook(self._capture_body())[0], 400)
        self.assertEqual(self._doc()["payments"], [])
        # ...and the pay button is still live: the gateway is not dormant.
        self.assertTrue(self._doc()["payment"]["enabled"])

    def test_the_webhook_needs_no_session_and_no_worker_gate(self):
        """It carries no cookie by construction. This is the assertion that
        stops it being moved under /api/*, where both gates would eat it."""
        status, body = self._webhook(self._capture_body(payment_id="pay_anon"))
        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])


class DormantPaymentTest(BillingHttp):
    """What the pay screen answers before the operator has a gateway."""

    def test_the_webhook_is_a_503_while_payments_are_unconfigured(self):
        """Transient, not a refusal: the operator has not finished setting the
        gateway up, and the gateway should keep retrying."""
        status, _ = self._webhook(self._capture_body())
        self.assertEqual(status, 503)

    def test_paying_answers_an_honest_sentence_with_who_to_contact(self):
        self.store.set_setting("whatsapp_admin_number", "919999999999")
        status, body, _ = self._req("POST", "/api/billing/pay",
                                    {"paise": 5000}, cookie=self._login())
        self.assertEqual(status, 503)
        self.assertFalse(body["enabled"])
        self.assertIn("919999999999", body["error"])

    def test_the_browser_return_is_a_503_too(self):
        status, _, _ = self._req("POST", "/api/billing/return",
                                 {"razorpay_payment_id": "pay_1"},
                                 cookie=self._login())
        self.assertEqual(status, 503)


class PaymentReturnTest(BillingHttp):
    """POST /api/billing/return: instant feedback, and NOT a ledger write."""

    def setUp(self):
        super().setUp()
        self._enable_payments()

    def _signed_return(self, order_id="order_1", payment_id="pay_1"):
        return {"razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": hmac.new(
                    KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(),
                    hashlib.sha256).hexdigest()}

    def test_a_verified_return_reports_success_but_records_NO_MONEY(self):
        """The return signature covers the ids and not the amount, so posting
        a ledger row from here would let a client name its own figure. The
        webhook is the only path that records money."""
        status, body, _ = self._req("POST", "/api/billing/return",
                                    self._signed_return(),
                                    cookie=self._login())
        self.assertEqual(status, 200)
        self.assertTrue(body["verified"])
        self.assertEqual(body["outstanding_paise"], 0)
        self.assertEqual(self._doc()["payments"], [])

    def test_an_unverifiable_return_is_a_200_that_says_so(self):
        """Money may still have left the payer's account. A 4xx here reads as
        "your payment failed", which is a claim we cannot make."""
        bad = self._signed_return()
        bad["razorpay_signature"] = "deadbeef"
        status, body, _ = self._req("POST", "/api/billing/return", bad,
                                    cookie=self._login())
        self.assertEqual(status, 200)
        self.assertFalse(body["verified"])
        self.assertIn("could not verify", body["error"].lower())

    def test_an_amount_is_validated_before_the_gateway_is_ever_called(self):
        cookie = self._login()
        for paise in (0, -1, "", 100_000_001):
            status, body, _ = self._req("POST", "/api/billing/pay",
                                        {"paise": paise}, cookie=cookie)
            self.assertEqual(status, 422, f"paise={paise!r}")
            self.assertTrue(body["error"])


# --------------------------------------------------------------- the 402 gate

class LockGateTest(BillingHttp):
    """server.py's 402, and everything that must survive it."""

    def test_a_locked_org_can_still_reach_the_pay_screen(self):
        """The single most important test in this file. An owner who cannot
        open the ledger cannot pay the bill that locked him out, and the only
        way back in becomes a phone call."""
        month = self._overdue_invoice()
        self._enable_payments()
        cookie = self._login()

        # The dashboard is shut.
        status, body, _ = self._req("GET", "/api/inventory", cookie=cookie)
        self.assertEqual(status, 402)
        self.assertTrue(body["locked"])

        # ...and every route the pay screen is built from still answers.
        doc = self._doc()
        self.assertTrue(doc["locked"])
        self.assertEqual(doc["stage"], "locked")
        self.assertEqual(doc["open_invoice"]["month"], month)
        self.assertGreaterEqual(doc["days_overdue"], 4)

        status, raw, _ = self._get_bytes(
            f"/api/billing/invoice?month={month}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"%PDF"))

        # The amount is rejected on its own merits, never with a 402.
        self.assertEqual(self._req("POST", "/api/billing/pay", {"paise": 0},
                                   cookie=cookie)[0], 422)
        self.assertEqual(self._req("POST", "/api/billing/return",
                                   {"razorpay_payment_id": "pay_1"},
                                   cookie=cookie)[0], 200)
        # /api/me and logout stay open or the SPA cannot even render the shell.
        self.assertEqual(self._req("GET", "/api/me", cookie=cookie)[0], 200)

    def test_monitoring_and_edge_ingest_are_NEVER_gated(self):
        """A lapsed bill must not silence an alarm. The gate guards /api/*
        only, and this is the assertion that keeps it there."""
        device = self._device()
        self._overdue_invoice()
        self.assertEqual(self._req("GET", "/api/inventory",
                                   cookie=self._login())[0], 402)
        self.assertEqual(self._req("GET", f"/edge/devices?org_id={ORG}",
                                   token="tok")[0], 200)
        status, _, _ = self._req(
            "POST", "/report",
            {"v": 1, "org_id": ORG, "node_id": "edge-1",
             "pings": {"10.0.0.1": {"loss_pct": 0, "latency_ms": 5.0}}},
            token="tok")
        self.assertEqual(status, 200)
        self.assertEqual(self.store.device_states(ORG)[device]["state"], "UP")

    def test_an_OVERDUE_org_keeps_its_full_topology(self):
        """The companion to the stand-down below, and the more important half:
        being locked out of the dashboard must never cost an org its
        monitoring, however far behind it falls."""
        self._device()
        self._overdue_invoice()
        status, body, _ = self._req("GET", f"/edge/devices?org_id={ORG}",
                                    token="tok")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["devices"]), 1)

    def test_DEACTIVATION_stands_the_probes_down(self):
        """The one place billing reaches the monitoring path. Safe only
        because it cannot happen automatically: it takes a superadmin typing
        the org id into a confirm dialog. The node still gets a 200 and still
        heartbeats, so it stays live and updatable rather than reading as a
        dead probe."""
        self._device()
        root = self._login("root")
        self.assertEqual(self._req("POST", "/api/admin/billing",
                                   {"org_id": ORG, "deactivated": True},
                                   cookie=root)[0], 200)
        status, body, _ = self._req(f"GET", f"/edge/devices?org_id={ORG}",
                                    token="tok")
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"], [])
        # Reactivating hands the network straight back: nothing was deleted.
        self.assertEqual(self._req("POST", "/api/admin/billing",
                                   {"org_id": ORG, "deactivated": False},
                                   cookie=root)[0], 200)
        self.assertEqual(len(self._req("GET", f"/edge/devices?org_id={ORG}",
                                       token="tok")[1]["devices"]), 1)

    def test_the_superadmin_is_never_locked_out_of_a_locked_org(self):
        self._overdue_invoice()
        root = self._login("root")
        self.assertEqual(self._req("GET", f"/api/inventory?org={ORG}",
                                   cookie=root)[0], 200)
        self.assertEqual(self._req("GET", "/api/admin/billing",
                                   cookie=root)[0], 200)

    def test_a_new_org_is_NEVER_locked_before_its_first_invoice(self):
        """Postpaid means outstanding is nonzero from day one of usage. A
        ladder keyed on the balance would shut a signup out in its first week,
        before it has ever been sent a bill."""
        self._device()
        self.sweeper.sweep()
        doc = self._doc()
        self.assertGreater(doc["outstanding_paise"], 0)
        self.assertIsNone(doc["open_invoice"])
        self.assertEqual(doc["stage"], "clear")
        self.assertFalse(doc["locked"])
        self.assertEqual(self._req("GET", "/api/inventory",
                                   cookie=self._login())[0], 200)

    def test_paying_by_webhook_UNLOCKS_the_dashboard(self):
        """The whole point of leaving the pay screen open: the money lands and
        the org is working again without anybody being called."""
        self._overdue_invoice(paise=120000)
        self._enable_payments()
        cookie = self._login()
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0],
                         402)
        status, body = self._webhook(self._capture_body(paise=120000))
        self.assertEqual((status, body["recorded"]), (200, True))
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0],
                         200)
        self.assertFalse(self._doc()["locked"])

    def test_an_exempt_org_never_locks(self):
        self._overdue_invoice()
        self._admin({"org_id": ORG, "exempt": True})
        doc = self._doc()
        self.assertEqual(doc["stage"], "exempt")
        self.assertFalse(doc["locked"])
        self.assertEqual(self._req("GET", "/api/inventory",
                                   cookie=self._login())[0], 200)


# ------------------------------------------------------------------- credit

class CreditTest(BillingHttp):
    """Advance payment IS the credit mechanism, and the projection is the
    no-reminder switch."""

    def test_overpaying_reads_as_credit_with_the_date_it_runs_out(self):
        self._device()
        self.sweeper.sweep()
        daily = self._doc()["today"]["paise"]
        self.assertGreater(daily, 0, "the fixture must actually be accruing")
        status, _ = self._admin({"org_id": ORG,
                                 "payment": {"kind": "manual",
                                             "paise": daily * 11,
                                             "note": "paid ahead"}})
        self.assertEqual(status, 200)
        doc = self._doc()
        self.assertEqual(doc["outstanding_paise"], -daily * 10)
        self.assertEqual(doc["credit_paise"], daily * 10)
        self.assertEqual(doc["credit_lasts_until"],
                         (self.today + timedelta(days=10)).isoformat())

    def test_credit_with_nothing_accruing_projects_NO_date(self):
        """Nothing is being charged, so the credit lasts forever. Printing a
        date would be a lie with a decimal point on it."""
        self._admin({"org_id": ORG,
                     "payment": {"kind": "manual", "paise": 10000,
                                 "note": "advance"}})
        doc = self._doc()
        self.assertEqual(doc["credit_paise"], 10000)
        self.assertIsNone(doc["credit_lasts_until"])


# -------------------------------------------------------- superadmin console

class AdminConsoleTest(BillingHttp):
    """GET/POST /api/admin/billing: the fleet table and the ledger writes."""

    def test_the_console_is_superadmin_only(self):
        owner = self._login()
        self.assertEqual(self._req("GET", "/api/admin/billing",
                                   cookie=owner)[0], 403)
        status, _, _ = self._req("POST", "/api/admin/billing",
                                 {"org_id": ORG, "exempt": True},
                                 cookie=owner)
        self.assertEqual(status, 403)
        self.assertFalse(self.store.org_billing(ORG)["exempt"])

    def test_the_fleet_table_carries_every_number_its_chips_filter_on(self):
        self._device()
        self.sweeper.sweep()
        status, body, _ = self._req("GET", "/api/admin/billing",
                                    cookie=self._login("root"))
        self.assertEqual(status, 200)
        self.assertEqual(body["today"], self.today.isoformat())
        self.assertEqual(body["rates"]["conn_paise"],
                         metering.DEFAULT_CONN_PAISE)
        row = next(r for r in body["orgs"] if r["org_id"] == ORG)
        self.assertEqual(row["name"], "Acme")
        self.assertEqual(row["stage"], "clear")
        self.assertGreater(row["outstanding_paise"], 0)
        self.assertEqual(row["today"]["day"], self.today.isoformat())
        self.assertEqual(row["today"]["device_count"], 1)

    def test_an_unknown_org_is_a_404_on_every_write(self):
        status, _ = self._admin({"org_id": "nosuchorg", "exempt": True})
        self.assertEqual(status, 404)

    def test_a_manual_payment_lands_with_WHO_recorded_it(self):
        status, reply = self._admin({"org_id": ORG,
                                     "payment": {"kind": "manual",
                                                 "paise": 50000,
                                                 "note": "bank transfer"}})
        self.assertEqual(status, 200)
        self.assertEqual(reply["org"]["outstanding_paise"], -50000)
        payment = self._doc()["payments"][0]
        self.assertEqual(payment["kind"], "manual")
        self.assertEqual(payment["paise"], 50000)
        self.assertEqual(payment["note"], "bank transfer")
        self.assertEqual(payment["recorded_by"], "root")

    def test_a_manual_payment_may_not_be_negative(self):
        """Money that came IN is a payment. Money going the other way is an
        adjustment, and it has to say why."""
        for paise in (-2500, 0, "lots"):
            status, _ = self._admin({"org_id": ORG,
                                     "payment": {"kind": "manual",
                                                 "paise": paise}})
            self.assertEqual(status, 422, f"paise={paise!r}")
        self.assertEqual(self._doc()["payments"], [])

    def test_an_adjustment_may_be_negative_but_REQUIRES_a_note(self):
        status, _ = self._admin({"org_id": ORG,
                                 "payment": {"kind": "adjustment",
                                             "paise": -2500}})
        self.assertEqual(status, 422)
        status, _ = self._admin({"org_id": ORG,
                                 "payment": {"kind": "adjustment",
                                             "paise": -2500,
                                             "note": "disputed month"}})
        self.assertEqual(status, 200)
        doc = self._doc()
        self.assertEqual(doc["outstanding_paise"], 2500)
        self.assertEqual(doc["payments"][0]["recorded_by"], "root")
        self.assertEqual(doc["payments"][0]["note"], "disputed month")

    def test_an_unknown_payment_kind_is_refused(self):
        status, _ = self._admin({"org_id": ORG,
                                 "payment": {"kind": "cheque", "paise": 100}})
        self.assertEqual(status, 422)

    def test_voiding_an_invoice_stops_it_holding_the_lock(self):
        month = self._overdue_invoice()
        cookie = self._login()
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0],
                         402)
        status, _ = self._admin({"org_id": ORG,
                                 "invoice": {"month": month, "status": "void"}})
        self.assertEqual(status, 200)
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0],
                         200)
        # 'paid' is derived from the payments and may not be typed in.
        status, _ = self._admin({"org_id": ORG,
                                 "invoice": {"month": month, "status": "paid"}})
        self.assertEqual(status, 422)


class DeactivationTest(BillingHttp):
    """Deactivation is a superadmin CLICK, never a sweep's decision."""

    def test_sixty_days_overdue_LISTS_an_org_and_deactivates_NOTHING(self):
        self._overdue_invoice(months_back=4)
        self.sweeper.sweep()
        status, body, _ = self._req("GET", "/api/admin/billing",
                                    cookie=self._login("root"))
        self.assertEqual(status, 200)
        row = next(r for r in body["orgs"] if r["org_id"] == ORG)
        self.assertGreaterEqual(row["days_overdue"],
                                metering.DEACTIVATE_LIST_DAYS)
        self.assertTrue(row["deactivation_candidate"])
        # The list is a list. Nothing in the sweep may stand a fleet down.
        self.assertFalse(row["deactivated"])
        self.assertFalse(self.store.org_billing(ORG)["deactivated"])
        self.assertFalse(self._doc()["deactivated"])

    def test_only_the_superadmin_click_deactivates_and_it_is_REVERSIBLE(self):
        cookie = self._login()
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0],
                         200)
        status, reply = self._admin({"org_id": ORG, "deactivated": True})
        self.assertEqual(status, 200)
        self.assertEqual(reply["org"]["stage"], "deactivated")
        doc = self._doc()
        self.assertTrue(doc["deactivated"])
        self.assertTrue(doc["locked"])
        self.assertEqual(doc["stage"], "deactivated")
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0],
                         402)

        status, _ = self._admin({"org_id": ORG, "deactivated": False})
        self.assertEqual(status, 200)
        self.assertEqual(self._doc()["stage"], "clear")
        self.assertEqual(self._req("GET", "/api/inventory", cookie=cookie)[0],
                         200)

    def test_the_gate_and_the_document_can_never_DISAGREE_about_locked(self):
        """`locked` in the document is what the SPA renders; the 402 is what
        the server actually does. They come from two different functions
        (billing.org_locked ranks exempt first, metering.ladder_stage ranks
        deactivated first), and an org carrying BOTH flags is where they part
        company. Whichever flag is meant to win, both layers have to pick the
        same one: otherwise the console tells the operator an account is
        switched off while every one of its calls still answers."""
        self._admin({"org_id": ORG, "exempt": True, "deactivated": True})
        doc = self._doc()
        self.assertEqual(
            self._req("GET", "/api/inventory", cookie=self._login())[0],
            402 if doc["locked"] else 200,
            f"the document says locked={doc['locked']} "
            f"and stage={doc['stage']!r}")

    def test_a_deactivated_org_accrues_nothing_and_leaves_a_HOLE(self):
        """The days an org spent switched off are a deliberate hole in the
        ledger, and the catch-up pass must not charge across them as if
        central had merely been down."""
        self._device()
        self.sweeper.sweep(self._at(self._days_ago(3)))
        self._admin({"org_id": ORG, "deactivated": True})
        self.sweeper.sweep(self._at(self._days_ago(2)))
        self._admin({"org_id": ORG, "deactivated": False})
        self.sweeper.sweep()
        for n in (2, 1):
            self.assertIsNone(
                self.store.accrual_on(ORG, self._days_ago(n).isoformat()),
                f"{self._days_ago(n)} should stay a hole")
        self.assertIsNotNone(
            self.store.accrual_on(ORG, self.today.isoformat()))


class RateOverrideTest(BillingHttp):
    """Per-org rates, and the forward-only rule that keeps the chart and the
    invoice agreeing."""

    def test_a_per_org_override_beats_the_global_default(self):
        self.store.set_setting("billing_conn_paise", "250")
        doc = self._doc()
        self.assertEqual(doc["rates"]["conn_paise"], 250)
        self.assertFalse(doc["rates"]["conn_override"])

        status, _ = self._admin({"org_id": ORG, "conn_rate_paise": 200,
                                 "floor_paise": 5000})
        self.assertEqual(status, 200)
        doc = self._doc()
        self.assertEqual(doc["rates"]["conn_paise"], 200)
        self.assertEqual(doc["rates"]["floor_paise"], 5000)
        self.assertTrue(doc["rates"]["conn_override"])
        self.assertTrue(doc["rates"]["floor_override"])

        # Clearing an override falls back to the global, it does not zero it.
        status, _ = self._admin({"org_id": ORG, "conn_rate_paise": None,
                                 "floor_paise": None})
        self.assertEqual(status, 200)
        doc = self._doc()
        self.assertEqual(doc["rates"]["conn_paise"], 250)
        self.assertFalse(doc["rates"]["conn_override"])

    def test_a_rate_must_be_a_whole_number_of_paise_and_never_negative(self):
        for value in (-1, "free"):
            status, _ = self._admin({"org_id": ORG, "conn_rate_paise": value})
            self.assertEqual(status, 422, f"rate={value!r}")
        self.assertIsNone(self.store.org_billing(ORG)["conn_rate_paise"])

    def test_a_rate_change_applies_FORWARD_only(self):
        """An accrual row stores the rate it was charged at and is never
        rewritten: the invoice is the sum of its rows, so a retroactive rate
        would detach the bill from the chart the operator was shown."""
        self._device()
        yesterday = self._days_ago(1)
        self.sweeper.sweep(self._at(yesterday))
        self._admin({"org_id": ORG, "conn_rate_paise": 111,
                     "floor_paise": 22200})
        self.sweeper.sweep()

        before = self.store.accrual_on(ORG, yesterday.isoformat())
        after = self.store.accrual_on(ORG, self.today.isoformat())
        self.assertEqual(before["conn_rate_paise"], metering.DEFAULT_CONN_PAISE)
        self.assertEqual(before["floor_paise"], metering.DEFAULT_FLOOR_PAISE)
        self.assertEqual(after["conn_rate_paise"], 111)
        self.assertEqual(after["floor_paise"], 22200)
        self.assertNotEqual(after["paise"], before["paise"])


# ------------------------------------------------------------------- workers

class WorkerTest(BillingHttp):
    """A worker never sees billing. Owner-only on BOTH layers, the
    customers-page rule: the route table refuses before the handler runs."""

    def test_a_worker_is_refused_the_ledger_and_the_console(self):
        cookie = self._login("field")
        for path in ("/api/billing", "/api/admin/billing"):
            status, body, _ = self._req("GET", path, cookie=cookie)
            self.assertEqual(status, 403, path)
            self.assertEqual(body["error"], "forbidden")

    def test_a_worker_cannot_download_an_invoice(self):
        self._accrue(f"{metering.prev_month(metering.month_key(self.today))}-15",
                     4000)
        self._accrue(self.today.isoformat(), 0)
        self.sweeper.sweep()
        month = metering.prev_month(metering.month_key(self.today))
        status, _, _ = self._req("GET", f"/api/billing/invoice?month={month}",
                                 cookie=self._login("field"))
        self.assertEqual(status, 403)

    def test_a_worker_cannot_write_anything_billing_shaped(self):
        cookie = self._login("field")
        for path, body in (("/api/billing/pay", {"paise": 100}),
                           ("/api/billing/return", {}),
                           ("/api/admin/billing", {"org_id": ORG,
                                                   "exempt": True})):
            status, _, _ = self._req("POST", path, body, cookie=cookie)
            self.assertEqual(status, 403, path)
        self.assertFalse(self.store.org_billing(ORG)["exempt"])


if __name__ == "__main__":
    unittest.main()
