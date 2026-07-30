"""Field survey: a worker recording where plant physically stands.

The subsystem widens the worker role's write surface for the first time since it
was created, so the tests here are mostly about what it does NOT open. Three
properties, each of which is the whole reason the routes are separate from the
owner's:

  * a worker can place a pin and create passive plant, and nothing else;
  * a field route can never CLEAR a pin, so a missing UI guard cannot erase a
    surveyed fleet;
  * provenance is stamped on every field write and WIPED by a desktop drag —
    the map must not claim a 9 m GPS fix for a point somebody dragged.

The fourth property is the one that makes handing this to the field acceptable
at all: a passive created here reaches no engine. `test_a_field_passive_never
_touches_the_engine_fingerprint` pins it.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from support import RecordingNotifier
from wisp.central import auth, inventory
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.config import Config

# Somewhere in Hyderabad, where the fleet this was built for actually lives.
LAT, LNG = 17.385044, 78.486671


class _Base(unittest.TestCase):
    """One org, an owner and a worker, one placed switch and one unplaced OLT."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA", name="A")
        self.store.set_org("ispB", name="B")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "ravi", "ravipassword", "worker")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.wan = self.store.create_org_device("ispA", {
            "name": "WAN-SW", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": "north", "parent_device_id": None,
            "assigned_node_id": "probe1"})
        self.olt = self.store.create_org_device("ispA", {
            "name": "PYLON-OLT", "ip_address": "10.0.0.2", "device_type": "olt",
            "region": "north", "parent_device_id": self.wan,
            "assigned_node_id": "probe1"})
        self.foreign = self.store.create_org_device("ispB", {
            "name": "B1", "ip_address": "10.9.9.9", "device_type": "switch",
            "region": None, "parent_device_id": None})

        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def _req(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def _login(self, username="owner", password="ownerpassword"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username,
                                      "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        return cookie.split(";")[0] if cookie else None

    def _row(self, device_id, org="ispA"):
        return next(d for d in self.store.list_org_devices(org)
                    if d["id"] == device_id)


class FieldLocationTest(_Base):

    def test_a_worker_can_place_a_device(self):
        # The point of the whole feature: the person standing at the pole is the
        # one who knows where it is.
        status, body = self._req(
            "POST", "/api/inventory/field-location",
            {"id": self.olt, "lat": LAT, "lng": LNG, "accuracy_m": 8.4,
             "source": "gps"},
            cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        row = self._row(self.olt)
        self.assertAlmostEqual(row["lat"], LAT, places=6)
        self.assertAlmostEqual(row["lng"], LNG, places=6)

    def test_the_capture_records_who_when_and_how_well(self):
        # A 40 m fix and a surveyed point are different claims about the same two
        # numbers. Losing that distinction is unrecoverable once pins exist.
        self._req("POST", "/api/inventory/field-location",
                  {"id": self.olt, "lat": LAT, "lng": LNG, "accuracy_m": 8.4},
                  cookie=self._login("ravi", "ravipassword"))
        row = self._row(self.olt)
        self.assertEqual(row["accuracy_m"], 8.4)
        self.assertEqual(row["place_source"], "gps")
        self.assertEqual(row["placed_by"], "ravi")
        self.assertTrue(row["placed_at"])

    def test_a_gps_claim_with_no_accuracy_is_downgraded_not_rejected(self):
        # Every browser that can produce a fix produces coords.accuracy with it,
        # so an absent one means the number came from somewhere else. The
        # coordinates are still worth keeping — just not as a measurement.
        self._req("POST", "/api/inventory/field-location",
                  {"id": self.olt, "lat": LAT, "lng": LNG, "source": "gps"},
                  cookie=self._login("ravi", "ravipassword"))
        row = self._row(self.olt)
        self.assertEqual(row["place_source"], "manual")
        self.assertIsNone(row["accuracy_m"])

    def test_a_field_route_can_never_clear_a_pin(self):
        # `clean_location_payload` treats both-null as DELETE. The field payload
        # is a separate function precisely so a worker-facing route is not one
        # missing UI guard away from erasing a surveyed fleet.
        self.store.set_org_device_location("ispA", self.olt, LAT, LNG)
        status, _ = self._req(
            "POST", "/api/inventory/field-location",
            {"id": self.olt, "lat": None, "lng": None},
            cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 422)
        self.assertAlmostEqual(self._row(self.olt)["lat"], LAT, places=6)

    def test_a_desktop_drag_wipes_the_provenance_it_supersedes(self):
        # Keeping the stamp would leave the map claiming a tight GPS fix for a
        # point dragged across a village. "Unknown" is the honest reading.
        self._req("POST", "/api/inventory/field-location",
                  {"id": self.olt, "lat": LAT, "lng": LNG, "accuracy_m": 8.4},
                  cookie=self._login("ravi", "ravipassword"))
        self._req("POST", "/api/inventory/location",
                  {"id": self.olt, "lat": 17.4, "lng": 78.5},
                  cookie=self._login())
        row = self._row(self.olt)
        self.assertIsNone(row["accuracy_m"])
        self.assertIsNone(row["place_source"])
        self.assertIsNone(row["placed_by"])

    def test_an_owner_may_also_survey(self):
        status, _ = self._req(
            "POST", "/api/inventory/field-location",
            {"id": self.olt, "lat": LAT, "lng": LNG, "accuracy_m": 5.0},
            cookie=self._login())
        self.assertEqual(status, 200)

    def test_a_worker_cannot_survey_another_org(self):
        status, _ = self._req(
            "POST", "/api/inventory/field-location",
            {"id": self.foreign, "lat": LAT, "lng": LNG, "accuracy_m": 5.0},
            cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 403)
        self.assertIsNone(self._row(self.foreign, "ispB")["lat"])

    def test_an_absurd_accuracy_is_refused(self):
        status, _ = self._req(
            "POST", "/api/inventory/field-location",
            {"id": self.olt, "lat": LAT, "lng": LNG, "accuracy_m": 999_999},
            cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 422)

    def test_a_poor_but_real_fix_is_accepted(self):
        # Deliberately NOT a refusal. A worker under dense canopy still needs to
        # record something, and blocking the save is how coordinates end up in a
        # WhatsApp message instead of the database. The UI demotes the button;
        # the server keeps the number and its accuracy.
        status, _ = self._req(
            "POST", "/api/inventory/field-location",
            {"id": self.olt, "lat": LAT, "lng": LNG, "accuracy_m": 78.0},
            cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 200)
        self.assertEqual(self._row(self.olt)["accuracy_m"], 78.0)


class FieldPassiveTest(_Base):

    def _create(self, cookie, **over):
        body = {"name": "SPL-POLE-12", "device_type": "splitter",
                "lat": LAT, "lng": LNG, "accuracy_m": 6.0, "split_ratio": "1:8"}
        body.update(over)
        return self._req("POST", "/api/inventory/field-passive", body,
                         cookie=cookie)

    def test_a_worker_can_record_plant_it_finds(self):
        # Most splitters have no row until somebody walks to one. Without this
        # the passive plant — the thing branch-fault localization runs on —
        # never gets mapped.
        status, body = self._create(self._login("ravi", "ravipassword"))
        self.assertEqual(status, 200)
        row = self._row(body["id"])
        self.assertEqual(row["name"], "SPL-POLE-12")
        self.assertEqual(row["device_type"], "splitter")
        self.assertEqual(row["split_ratio"], 8)
        self.assertAlmostEqual(row["lat"], LAT, places=6)
        self.assertEqual(row["placed_by"], "ravi")

    def test_field_created_plant_has_no_parent_ip_or_probe(self):
        # The absent fields are what make this safe to hand to the field. The
        # parent link — the one that would give it consequences — is the owner's
        # job on the desktop.
        _, body = self._create(self._login("ravi", "ravipassword"),
                               parent_device_id=self.olt,
                               ip_address="10.0.0.55",
                               assigned_node_id="probe1")
        row = self._row(body["id"])
        self.assertIsNone(row["parent_device_id"])
        self.assertEqual(row["ip_address"], "")
        self.assertIsNone(row["assigned_node_id"])

    def test_a_worker_cannot_create_a_monitored_device(self):
        # The refusal that keeps this from being an inventory write: a switch has
        # an FSM, an outage and a page. A splitter has none of the three.
        status, _ = self._create(self._login("ravi", "ravipassword"),
                                 device_type="switch")
        self.assertEqual(status, 422)

    def test_a_field_passive_never_touches_the_engine_fingerprint(self):
        # The property the whole subsystem rests on. `org_device_topology` is the
        # single choke point the engine, the rebuild fingerprint and /edge/devices
        # all read — a passive is excluded from it, so recording one cannot
        # rebuild an engine or re-page a fleet.
        before = self.store.org_device_topology("ispA")
        self._create(self._login("ravi", "ravipassword"))
        self.assertEqual(self.store.org_device_topology("ispA"), before)

    def test_the_device_cap_does_not_apply_to_plant(self):
        # Passives are documentation, never metered — the same rule `create`
        # applies. A plan limit turning a survey into a half-mapped network is
        # not a paywall anyone intended.
        self.store.set_org_plan("ispA", "free")
        for i in range(8):
            status, _ = self._create(self._login("ravi", "ravipassword"),
                                     name=f"SPL-{i}")
            self.assertEqual(status, 200)

    def test_a_free_form_split_ratio_is_refused(self):
        # SPLIT_RATIOS is closed: the ratio feeds the load bar and the cumulative
        # split down a cascade, so "1:7" produces arithmetic nobody can act on.
        status, _ = self._create(self._login("ravi", "ravipassword"),
                                 split_ratio="1:7")
        self.assertEqual(status, 422)

    def test_plant_lands_in_the_creating_orgs_fleet(self):
        _, body = self._create(self._login("ravi", "ravipassword"))
        self.assertEqual(self.store.device_org(body["id"]), "ispA")


class WorkerGateTest(_Base):
    """The write surface a worker gained, and everything it did not."""

    def test_the_survey_routes_are_the_only_inventory_writes_a_worker_has(self):
        cookie = self._login("ravi", "ravipassword")
        for route, body in (
                ("/api/inventory", {"name": "X", "ip_address": "10.0.0.9",
                                    "device_type": "switch"}),
                ("/api/inventory/update", {"id": self.olt, "name": "renamed"}),
                ("/api/inventory/delete", {"id": self.olt}),
                ("/api/inventory/location", {"id": self.olt, "lat": LAT,
                                             "lng": LNG}),
                ("/api/inventory/route", {"child_id": self.olt,
                                          "parent_id": self.wan}),
                ("/api/inventory/maintenance", {"id": self.olt, "on": True}),
        ):
            status, _ = self._req("POST", route, body, cookie=cookie)
            self.assertEqual(status, 403, f"{route} should stay owner-only")

    def test_a_survey_cannot_rename_the_device_it_places(self):
        # The payload carries only coordinates by construction — a name in the
        # body is ignored rather than honoured.
        self._req("POST", "/api/inventory/field-location",
                  {"id": self.olt, "lat": LAT, "lng": LNG, "accuracy_m": 5.0,
                   "name": "OWNED"},
                  cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(self._row(self.olt)["name"], "PYLON-OLT")

    def test_signed_out_callers_get_nothing(self):
        for route in ("/api/inventory/field-location",
                      "/api/inventory/field-passive"):
            status, _ = self._req("POST", route,
                                  {"id": self.olt, "lat": LAT, "lng": LNG})
            self.assertEqual(status, 401)


class FieldOnuTest(_Base):
    """Locating a subscriber, and the witness flag it must not touch.

    This is the sharpest edge in the feature. `onu_places` already existed as the
    REFERENCE-ONU table, where placing a pin IS the operator's claim that the
    subscriber's power is reliable — and `ponfault` reads that claim to decide
    whether a dark PON is an area power cut (no crew) or a fibre cut (roll a
    splicing van). Letting the field drop location pins into the same table
    without separating the two would enrol every geo-tagged subscriber as a
    witness, and the next dark one would read as proof of a cut.
    """

    def setUp(self):
        super().setUp()
        # Two ONUs on the OLT's roster. `onu_search`/the field route both resolve
        # against this, so a MAC nobody has walked is not locatable.
        for key, oid, serial, name in (
                ("1/1", 1, "AA:BB:CC:00:00:01", "hc_kiran"),
                ("1/2", 2, "AA:BB:CC:00:00:02", "hc_ravi")):
            self.store.upsert_onu_optics(
                "ispA", self.olt, key, pon_port="EPON0/1", onu_id=oid,
                name=name, serial=serial, state="online", rx_dbm=-22.0,
                tx_dbm=None, olt_rx_dbm=None, distance_m=900,
                rx_ref_dbm=None, rx_ref_at=None, severity="ok",
                ts="2026-07-28T04:00:00+00:00")

    def _locate(self, mac, cookie, **over):
        body = {"mac": mac, "lat": LAT, "lng": LNG, "accuracy_m": 7.0}
        body.update(over)
        return self._req("POST", "/api/inventory/field-onu", body, cookie=cookie)

    def _place(self, mac):
        return next(p for p in self.store.list_onu_places("ispA") if p["mac"] == mac)

    def test_a_worker_can_locate_a_subscriber(self):
        status, body = self._locate("AA:BB:CC:00:00:01",
                                    self._login("ravi", "ravipassword"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        p = self._place("AA:BB:CC:00:00:01")
        self.assertAlmostEqual(p["lat"], LAT, places=6)
        self.assertEqual(p["placed_by"], "ravi")
        self.assertEqual(p["accuracy_m"], 7.0)

    def test_locating_does_NOT_create_a_witness(self):
        # The property the whole split exists for. A located subscriber must be
        # invisible to ponfault.
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        self.assertEqual(self._place("AA:BB:CC:00:00:01")["witness"], 0)
        self.assertEqual(self.store.onu_place_macs("ispA"), set())

    def test_locating_a_reference_ONU_does_not_cancel_its_claim(self):
        # The opposite failure, and the worse one: a tech recording where a box
        # sits must never silently strip the operator's power claim, because
        # that claim is invisible on a handset and losing it flips a PON verdict
        # from "power cut" to "fibre cut" — rolling a crew for the DISCOM.
        self.store.set_onu_place("ispA", "AA:BB:CC:00:00:01", 17.0, 78.0,
                                 "UPS site", None)
        self.assertEqual(self.store.onu_place_macs("ispA"), {"AA:BB:CC:00:00:01"})
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        p = self._place("AA:BB:CC:00:00:01")
        self.assertEqual(p["witness"], 1)
        self.assertAlmostEqual(p["lat"], LAT, places=6)   # moved
        self.assertEqual(p["label"], "UPS site")          # kept
        self.assertEqual(self.store.onu_place_macs("ispA"), {"AA:BB:CC:00:00:01"})

    def test_a_witness_flag_cannot_be_asked_for_in_the_payload(self):
        # Not merely ignored by the handler — the payload has no key for it, so
        # a future caller cannot smuggle one in.
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"),
                     witness=True)
        self.assertEqual(self._place("AA:BB:CC:00:00:01")["witness"], 0)
        self.assertEqual(self.store.onu_place_macs("ispA"), set())

    def test_an_unknown_mac_is_refused(self):
        # A scrape can never add an ONU and neither can this. A pin on a typo'd
        # sticker would render at a coordinate with nothing behind it.
        status, _ = self._locate("DE:AD:BE:EF:00:00",
                                 self._login("ravi", "ravipassword"))
        self.assertEqual(status, 404)
        self.assertEqual(self.store.list_onu_places("ispA"), [])

    def test_a_field_locate_cannot_clear_a_pin(self):
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        status, _ = self._locate("AA:BB:CC:00:00:01",
                                 self._login("ravi", "ravipassword"),
                                 lat=None, lng=None)
        self.assertEqual(status, 422)
        self.assertEqual(len(self.store.list_onu_places("ispA")), 1)

    def test_a_worker_cannot_reach_the_reference_ONU_route(self):
        # Placing a REFERENCE point stays owner-only — it is a claim about a
        # power supply, not an observation.
        status, _ = self._req(
            "POST", "/api/inventory/onu-place",
            {"mac": "AA:BB:CC:00:00:01", "lat": LAT, "lng": LNG,
             "org_id": "ispA"},
            cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 403)

    def test_the_owners_reference_dialog_still_means_witness(self):
        # The default the desktop path relies on. If this flipped, the whole
        # reference-ONU feature would quietly stop working.
        status, _ = self._req(
            "POST", "/api/inventory/onu-place",
            {"mac": "AA:BB:CC:00:00:02", "lat": LAT, "lng": LNG,
             "label": "tower", "org_id": "ispA"},
            cookie=self._login())
        self.assertEqual(status, 200)
        self.assertEqual(self.store.onu_place_macs("ispA"), {"AA:BB:CC:00:00:02"})

    def test_org_isolation(self):
        status, _ = self._locate("AA:BB:CC:00:00:01",
                                 self._login("bowner", "bownerpassword"),
                                 org_id="ispA")
        self.assertEqual(status, 403)


class FieldOnuNameTest(FieldOnuTest):
    """Naming a subscriber from the field.

    The name goes to `onu_places.label`, never `onu_optics.name`: the roster's
    name is walk-owned (`name=excluded.name` on every sweep), so anything typed
    into it would vanish within ~300s. And renaming is its own route because
    re-placing would restamp the pin's provenance.
    """

    def _name(self, mac, label, cookie):
        return self._req("POST", "/api/inventory/field-onu-name",
                         {"mac": mac, "label": label}, cookie=cookie)

    def test_a_worker_can_name_a_located_subscriber(self):
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        status, _ = self._name("AA:BB:CC:00:00:01", "Babu — 2nd floor",
                               self._login("ravi", "ravipassword"))
        self.assertEqual(status, 200)
        # UPPERCASE: a customer name always reads as caps whatever the phone
        # keyboard produced (operator's call, 2026-07-29) — normalized on the
        # write path so search and every screen see one spelling.
        self.assertEqual(self._place("AA:BB:CC:00:00:01")["label"],
                         "BABU — 2ND FLOOR")

    def test_a_rename_never_restamps_the_pin_or_its_provenance(self):
        # The reason this is a separate route. Fixing a spelling must not
        # downgrade a real 6 m GPS fix to a hand-placed point, move the pin, or
        # reattribute the visit to whoever corrected the typo.
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"),
                     accuracy_m=6.0)
        before = self._place("AA:BB:CC:00:00:01")
        self._name("AA:BB:CC:00:00:01", "Babu", self._login())
        after = self._place("AA:BB:CC:00:00:01")
        for field in ("lat", "lng", "accuracy_m", "place_source", "placed_by",
                      "placed_at"):
            self.assertEqual(after[field], before[field], field)

    def test_a_rename_cannot_create_a_placement(self):
        # A name with no location is not a placement, and inventing a pin-less
        # row would put a subscriber in the coverage count nobody has visited.
        status, _ = self._name("AA:BB:CC:00:00:02", "Nobody",
                               self._login("ravi", "ravipassword"))
        self.assertEqual(status, 404)
        self.assertEqual(self.store.list_onu_places("ispA"), [])

    def test_a_name_can_ride_the_placement_itself(self):
        # A first visit records both in one press rather than saving twice.
        status, _ = self._locate("AA:BB:CC:00:00:01",
                                 self._login("ravi", "ravipassword"),
                                 label="Kiran")
        self.assertEqual(status, 200)
        self.assertEqual(self._place("AA:BB:CC:00:00:01")["label"], "KIRAN")

    def test_a_blank_name_clears_it(self):
        # Allowed for a label, unlike a pin: descriptive text can honestly be
        # absent, so "I don't know who this is" must be sayable.
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"),
                     label="Wrong Person")
        self._name("AA:BB:CC:00:00:01", "", self._login("ravi", "ravipassword"))
        self.assertIsNone(self._place("AA:BB:CC:00:00:01")["label"])

    def test_the_roster_name_is_never_touched(self):
        # The whole reason the label exists. If this ever wrote through to
        # onu_optics.name, the next SNMP sweep would silently erase it.
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        self._name("AA:BB:CC:00:00:01", "Renamed", self._login())
        row = next(r for r in self.store.org_onu_rows("ispA")
                   if r["serial"] == "AA:BB:CC:00:00:01")
        self.assertEqual(row["name"], "hc_kiran")

    def test_a_placement_without_a_label_keeps_the_existing_one(self):
        # Re-pinning a subscriber whose name somebody already recorded must not
        # blank it just because the capture sheet sent no label.
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"),
                     label="Kiran")
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        self.assertEqual(self._place("AA:BB:CC:00:00:01")["label"], "KIRAN")

    def test_a_name_typed_in_the_field_APPEARS_ON_THE_OLTS_OPTICAL_TAB(self):
        # The bug this fixes (2026-07-29, reported from a live survey): the name
        # a worker typed saved correctly into onu_places and then rendered
        # NOWHERE — the Optical tab named every ONU off `onu_optics.name`, which
        # is blank on this fleet, so a subscriber somebody had just stood at and
        # named still read "unnamed" on the OLT that carries it. A name only
        # visible on the screen that captured it is indistinguishable from a name
        # that was never saved.
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        self._name("AA:BB:CC:00:00:01", "hcs babu",
                   self._login("ravi", "ravipassword"))
        status, body = self._req(
            "GET", f"/api/inventory/optics?device_id={self.olt}",
            cookie=self._login())
        self.assertEqual(status, 200, body)
        row = next(o for o in body["onus"]
                   if o["serial"] == "AA:BB:CC:00:00:01")
        self.assertEqual(row["label"], "HCS BABU")
        # …and the WALKED name is still there beside it. The operator's name wins
        # on screen, but "what does the OLT call this" stays answerable.
        self.assertEqual(row["name"], "hc_kiran")

    def test_a_name_typed_in_the_field_is_SEARCHABLE(self):
        # The other half: a tech looks a subscriber up by the name they know,
        # which after a survey is the one they typed. Matching only the walked
        # column answered "no such subscriber" about a drop in the roster.
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        self._name("AA:BB:CC:00:00:01", "hcs babu",
                   self._login("ravi", "ravipassword"))
        status, body = self._req("GET", "/api/inventory/onu-search?q=babu",
                                 cookie=self._login())
        self.assertEqual(status, 200, body)
        hits = [o for m in body["matches"] for o in m["onus"]]
        self.assertEqual([o["serial"] for o in hits], ["AA:BB:CC:00:00:01"])
        self.assertEqual(hits[0]["label"], "HCS BABU")

    def test_an_overlong_name_is_refused(self):
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        status, _ = self._name("AA:BB:CC:00:00:01", "x" * 200,
                               self._login("ravi", "ravipassword"))
        self.assertEqual(status, 422)

    def test_another_orgs_subscriber_is_not_renameable(self):
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        status, _ = self._req(
            "POST", "/api/inventory/field-onu-name",
            {"mac": "AA:BB:CC:00:00:01", "label": "hijacked", "org_id": "ispA"},
            cookie=self._login("bowner", "bownerpassword"))
        # A WRITE refuses outright rather than silently rescoping (unlike the
        # coverage GET, which pins a reader to its own org): the body named an
        # org this caller has no rights in, and answering 404 would imply the
        # subscriber merely doesn't exist.
        self.assertEqual(status, 403)
        self.assertIsNone(self._place("AA:BB:CC:00:00:01")["label"])


class OnuCoverageTest(FieldOnuTest):
    """The survey's headline number, and the queue behind it.

    Exists because the first cut counted only `org_devices` and so reported
    "0 left" the moment the gear was placed — while every subscriber in the
    roster still had no pin. A coverage figure nobody can see is a survey nobody
    finishes.
    """

    def _cov(self, cookie, **qs):
        q = "".join(f"&{k}={v}" for k, v in qs.items())
        return self._req("GET", f"/api/inventory/onu-coverage?org=ispA{q}",
                         cookie=cookie)

    def test_coverage_counts_the_roster_not_the_devices(self):
        status, body = self._cov(self._login("ravi", "ravipassword"))
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["placed"], 0)

    def test_placing_moves_the_number(self):
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        _, body = self._cov(self._login("ravi", "ravipassword"))
        self.assertEqual(body["placed"], 1)
        self.assertEqual(body["total"], 2)

    def test_a_reference_ONU_counts_as_located_too(self):
        # Coverage asks "does this subscriber have a pin", which a witness does.
        # `onu_place_macs` defaults to witness-only for the ALERTING callers, so
        # this is the one place that must pass witness_only=False — getting it
        # wrong would report a placed subscriber as still needing a visit.
        self.store.set_onu_place("ispA", "AA:BB:CC:00:00:02", LAT, LNG, None, None)
        _, body = self._cov(self._login())
        self.assertEqual(body["placed"], 1)

    def test_per_olt_rows_carry_their_own_progress(self):
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        _, body = self._cov(self._login("ravi", "ravipassword"))
        olt = next(o for o in body["olts"] if o["device_id"] == self.olt)
        self.assertEqual((olt["total"], olt["placed"]), (2, 1))
        self.assertEqual(olt["device_name"], "PYLON-OLT")

    def test_the_unplaced_list_only_arrives_for_one_named_OLT(self):
        # The fleet's whole unplaced set is thousands of rows; a handset asks for
        # the OLT it is standing under.
        _, no_id = self._cov(self._login("ravi", "ravipassword"))
        self.assertEqual(no_id["unplaced"], [])
        _, one = self._cov(self._login("ravi", "ravipassword"), device_id=self.olt)
        self.assertEqual(len(one["unplaced"]), 2)
        self.assertEqual({u["mac"] for u in one["unplaced"]},
                         {"AA:BB:CC:00:00:01", "AA:BB:CC:00:00:02"})

    def test_a_located_subscriber_leaves_the_queue(self):
        self._locate("AA:BB:CC:00:00:01", self._login("ravi", "ravipassword"))
        _, body = self._cov(self._login("ravi", "ravipassword"), device_id=self.olt)
        self.assertEqual([u["mac"] for u in body["unplaced"]],
                         ["AA:BB:CC:00:00:02"])

    def test_the_queue_is_in_slot_order_not_whoever_is_up(self):
        # A tech reads down a stable list; one shuffled by ONU state loses their
        # place between visits.
        _, body = self._cov(self._login("ravi", "ravipassword"), device_id=self.olt)
        self.assertEqual([u["onu_id"] for u in body["unplaced"]], [1, 2])

    def test_a_worker_may_read_coverage(self):
        status, _ = self._cov(self._login("ravi", "ravipassword"))
        self.assertEqual(status, 200)

    def test_another_orgs_roster_is_never_reachable(self):
        # `_scope_org` pins a non-superadmin to its OWN org and ignores ?org=,
        # so the answer is ispB's empty coverage rather than a 403. Asserting on
        # the CONTENT, not the status: the property that matters is that ispA's
        # subscribers never appear, and a status check would still pass if the
        # scoping were dropped and the org echoed back.
        status, body = self._req("GET", "/api/inventory/onu-coverage?org=ispA",
                                 cookie=self._login("bowner", "bownerpassword"))
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["olts"], [])


class PayloadTest(unittest.TestCase):
    """The validation itself, without a server in the way."""

    def test_both_coordinates_are_required(self):
        for body in ({"lat": LAT}, {"lng": LNG}, {},
                     {"lat": None, "lng": None}):
            with self.assertRaises(inventory.InventoryError):
                inventory.clean_field_location_payload(body)

    def test_source_is_a_closed_vocabulary(self):
        with self.assertRaises(inventory.InventoryError):
            inventory.clean_field_location_payload(
                {"lat": LAT, "lng": LNG, "accuracy_m": 5, "source": "vibes"})

    def test_coordinates_round_to_ten_centimetres(self):
        # ~1e-6° ≈ 0.1 m; anything longer is float noise from a sensor.
        out = inventory.clean_field_location_payload(
            {"lat": 17.38504412345, "lng": 78.48667198765, "accuracy_m": 5})
        self.assertEqual(out["lat"], 17.385044)
        self.assertEqual(out["lng"], 78.486672)

    def test_every_writer_of_a_subscriber_name_uppercases_it(self):
        # Operator's call (2026-07-29): a customer name reads as CAPS whatever
        # case it was typed in. Pinned across ALL THREE writers to
        # onu_places.label — the field capture, the field rename and the desktop
        # reference-ONU dialog — because one of them normalizing and the others
        # not is how the same subscriber ends up spelled two ways in one list,
        # and search then matches only one of them.
        self.assertEqual(inventory.clean_field_onu_payload(
            {"mac": "AA:BB", "lat": LAT, "lng": LNG, "accuracy_m": 5,
             "label": "hcs babu"})["label"], "HCS BABU")
        self.assertEqual(inventory.clean_field_onu_name_payload(
            {"mac": "AA:BB", "label": "hcs babu"})["label"], "HCS BABU")
        self.assertEqual(inventory.clean_onu_place_payload(
            {"mac": "AA:BB", "lat": LAT, "lng": LNG,
             "label": "water tank"})["label"], "WATER TANK")

    def test_a_blank_subscriber_name_stays_absent_rather_than_becoming_empty(self):
        # A label may honestly be absent (unlike a pin), and None is what CLEARS
        # it — an empty string would be a name nobody typed.
        self.assertIsNone(inventory.clean_field_onu_name_payload(
            {"mac": "AA:BB", "label": "   "})["label"])

    def test_a_passive_payload_pins_the_absent_fields(self):
        out = inventory.clean_field_passive_payload(
            {"name": "C-01", "device_type": "closure", "lat": LAT, "lng": LNG,
             "accuracy_m": 4.0})
        self.assertIsNone(out["parent_device_id"])
        self.assertEqual(out["ip_address"], "")
        self.assertIsNone(out["assigned_node_id"])
        self.assertIsNone(out["split_ratio"])


if __name__ == "__main__":
    unittest.main()
