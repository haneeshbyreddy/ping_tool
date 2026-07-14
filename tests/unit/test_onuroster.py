import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central.onuroster import (capacity_faults, current_roster,
                                    duplicate_macs, fresh_device_ids)

NOW = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _onu(onu_key, *, device_id=1, device_name="OLT-A", pon_port="0/1",
         onu_id=0, serial=None, state="online", updated_min_ago=1):
    return {
        "device_id": device_id, "device_name": device_name, "onu_key": onu_key,
        "pon_port": pon_port, "onu_id": onu_id, "serial": serial, "state": state,
        "distance_m": None, "last_online_at": _iso(NOW - timedelta(minutes=2)),
        "updated_at": _iso(NOW - timedelta(minutes=updated_min_ago)),
    }


def _pon(device_id, port, n, *, serial_prefix="AA:BB:CC:00:00:", **kw):
    return [_onu(f"{port}.{i}", device_id=device_id, pon_port=port, onu_id=i,
                 serial=f"{serial_prefix}{i:02x}", **kw) for i in range(n)]


class CurrentRosterTest(unittest.TestCase):

    def test_stale_olt_dropped(self):
        rows = _pon(1, "0/1", 2, updated_min_ago=1)
        rows += _pon(2, "0/2", 2, updated_min_ago=20)  # >15 min stale
        keep = current_roster(rows, NOW)
        self.assertTrue(all(r["device_id"] == 1 for r in keep))

    def test_zombie_rows_from_an_older_walk_excluded(self):
        # OLT 1's latest walk is 1 min ago; a leftover row from an older walk
        # (5 min ago) is a removed-ONU zombie and must not count
        rows = _pon(1, "0/1", 3, updated_min_ago=1)
        rows.append(_onu("0/1.9", device_id=1, onu_id=9, updated_min_ago=5,
                         serial="ZZ"))
        keep = current_roster(rows, NOW)
        self.assertEqual(len(keep), 3)
        self.assertNotIn("0/1.9", {r["onu_key"] for r in keep})


class CapacityTest(unittest.TestCase):

    def test_fires_at_the_limit_not_below(self):
        rows = _pon(1, "0/1", 4)
        self.assertEqual(capacity_faults(rows, NOW, lambda d: 5), [])
        caps = capacity_faults(_pon(1, "0/1", 5), NOW, lambda d: 5)
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].onus, 5)
        self.assertEqual(caps[0].pon_port, "0/1")
        self.assertEqual(caps[0].limit, 5)

    def test_over_limit_still_fires(self):
        caps = capacity_faults(_pon(1, "0/1", 7), NOW, lambda d: 5)
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].onus, 7)

    def test_per_olt_limit_override(self):
        rows = _pon(1, "0/1", 6) + _pon(2, "0/2", 6, device_name="OLT-B",
                                        serial_prefix="DD:00:00:00:00:")
        # OLT 1 cap 5 (over), OLT 2 cap 128 (fine)
        caps = capacity_faults(rows, NOW, lambda d: 5 if d == 1 else 128)
        self.assertEqual([c.device_id for c in caps], [1])

    def test_null_port_skipped(self):
        rows = _pon(1, None, 9)
        self.assertEqual(capacity_faults(rows, NOW, lambda d: 3), [])

    def test_zombie_rows_do_not_inflate_the_count(self):
        rows = _pon(1, "0/1", 5, updated_min_ago=1)
        # two removed-ONU zombies from an old walk share the port
        rows += [_onu("0/1.90", device_id=1, onu_id=90, updated_min_ago=6,
                      serial="X1"),
                 _onu("0/1.91", device_id=1, onu_id=91, updated_min_ago=6,
                      serial="X2")]
        caps = capacity_faults(rows, NOW, lambda d: 6)
        self.assertEqual(caps, [])  # 5 current, not 7


class DuplicateMacTest(unittest.TestCase):

    def test_single_slot_mac_never_flagged(self):
        rows = _pon(1, "0/1", 3)  # all distinct serials
        self.assertEqual(duplicate_macs(rows, NOW), [])

    def test_dup_across_two_olts(self):
        rows = [_onu("0/1.0", device_id=1, serial="AA:BB:CC:00:00:00"),
                _onu("0/2.0", device_id=2, device_name="OLT-B", pon_port="0/2",
                     serial="aa:bb:cc:00:00:00")]  # case-insensitive match
        dups = duplicate_macs(rows, NOW)
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0].mac, "AA:BB:CC:00:00:00")
        self.assertEqual(len(dups[0].members), 2)
        self.assertEqual({m["device_id"] for m in dups[0].members}, {1, 2})

    def test_dup_on_two_pons_of_one_olt(self):
        rows = [_onu("0/1.0", device_id=1, pon_port="0/1", serial="DEAD"),
                _onu("0/5.3", device_id=1, pon_port="0/5", onu_id=3, serial="DEAD")]
        dups = duplicate_macs(rows, NOW)
        self.assertEqual(len(dups), 1)
        self.assertEqual({m["pon_port"] for m in dups[0].members}, {"0/1", "0/5"})

    def test_blank_serial_ignored(self):
        rows = [_onu("0/1.0", device_id=1, serial=None),
                _onu("0/1.1", device_id=1, onu_id=1, serial="")]
        self.assertEqual(duplicate_macs(rows, NOW), [])

    def test_zombie_dup_excluded_by_freshness(self):
        # a live ONU and an old zombie sharing a MAC (the re-registration ghost) —
        # the zombie fell out of the current roster, so no duplicate stands
        rows = [_onu("0/1.0", device_id=1, updated_min_ago=1, serial="CAFE"),
                _onu("0/2.0", device_id=1, pon_port="0/2", updated_min_ago=20,
                     serial="CAFE")]
        self.assertEqual(duplicate_macs(rows, NOW), [])

    def test_online_members_counts_live_slots_only(self):
        # C-Data reg tables keep every slot an ONU ever occupied; an offline
        # ghost row makes members=2 but online_members=1 — history, not a fault
        rows = [_onu("0/1.0", device_id=1, serial="CAFE", state="online"),
                _onu("0/2.0", device_id=1, pon_port="0/2", serial="CAFE",
                     state="offline")]
        d = duplicate_macs(rows, NOW)[0]
        self.assertEqual(len(d.members), 2)
        self.assertEqual(d.online_members, 1)
        rows[1]["state"] = "online"
        self.assertEqual(duplicate_macs(rows, NOW)[0].online_members, 2)

    def test_stale_blind_view_keeps_stale_olts(self):
        # stale_s=None is the shadow view alerting uses to tell "genuinely
        # gone" from "walk went stale" — the stale OLT's dup must still show
        rows = [_onu("0/1.0", device_id=1, serial="CAFE"),
                _onu("0/2.0", device_id=2, device_name="OLT-B", pon_port="0/2",
                     serial="CAFE", updated_min_ago=30)]
        self.assertEqual(duplicate_macs(rows, NOW), [])
        shadow = duplicate_macs(rows, NOW, stale_s=None)
        self.assertEqual(len(shadow), 1)
        self.assertEqual(shadow[0].mac, "CAFE")


class FreshDeviceIdsTest(unittest.TestCase):

    def test_only_recently_walked_olts_are_fresh(self):
        rows = _pon(1, "0/1", 2, updated_min_ago=1) + _pon(2, "0/2", 2,
                                                           updated_min_ago=30)
        self.assertEqual(fresh_device_ids(rows, NOW), {1})

    def test_no_rows_means_nothing_fresh(self):
        self.assertEqual(fresh_device_ids([], NOW), set())


if __name__ == "__main__":
    unittest.main()
