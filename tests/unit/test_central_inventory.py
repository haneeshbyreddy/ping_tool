import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central.inventory import (
    InventoryError,
    clean_backup_link,
    clean_device_payload,
    clean_location_payload,
    clean_node_id,
    clean_port_bandwidth_payload,
    clean_region_name,
    clean_snmp_payload,
)

class CleanDevicePayloadTest(unittest.TestCase):
    def test_requires_name_and_ip(self):
        with self.assertRaises(InventoryError):
            clean_device_payload({"ip_address": "10.0.0.1"}, parents={}, device_id=None)
        with self.assertRaises(InventoryError):
            clean_device_payload({"name": "Tower"}, parents={}, device_id=None)

    def test_rejects_bad_ip(self):
        with self.assertRaises(InventoryError):
            clean_device_payload({"name": "Tower", "ip_address": "not-an-ip"},
                                 parents={}, device_id=None)

    def test_rejects_bad_device_type(self):
        with self.assertRaises(InventoryError):
            clean_device_payload(
                {"name": "Tower", "ip_address": "10.0.0.1", "device_type": "spaceship"},
                parents={}, device_id=None)

    def test_accepts_valid_minimal_payload(self):
        clean = clean_device_payload(
            {"name": "Tower", "ip_address": "10.0.0.1"}, parents={}, device_id=None)
        self.assertEqual(clean["name"], "Tower")
        self.assertIsNone(clean["parent_device_id"])

    def test_parent_must_exist(self):
        with self.assertRaises(InventoryError):
            clean_device_payload(
                {"name": "CPE", "ip_address": "10.0.0.2", "parent_device_id": 99},
                parents={1: None}, device_id=None)

    def test_self_parent_rejected(self):
        with self.assertRaises(InventoryError):
            clean_device_payload(
                {"name": "Tower", "ip_address": "10.0.0.1", "parent_device_id": 1},
                parents={1: None}, device_id=1)

    def test_cycle_rejected(self):
        with self.assertRaises(InventoryError):
            clean_device_payload(
                {"name": "Root", "ip_address": "10.0.0.1", "parent_device_id": 2},
                parents={1: None, 2: 1}, device_id=1)

    def test_valid_parent_accepted(self):
        clean = clean_device_payload(
            {"name": "CPE", "ip_address": "10.0.0.3", "parent_device_id": 1},
            parents={1: None}, device_id=None)
        self.assertEqual(clean["parent_device_id"], 1)

    def test_cross_org_id_looks_like_missing_parent(self):
        with self.assertRaises(InventoryError):
            clean_device_payload(
                {"name": "CPE", "ip_address": "10.0.0.3", "parent_device_id": 42},
                parents={1: None}, device_id=None)

    def test_assigned_node_defaults_to_unassigned(self):
        clean = clean_device_payload(
            {"name": "CPE", "ip_address": "10.0.0.3"}, parents={}, device_id=None,
            registered_nodes={"edge-1"})
        self.assertIsNone(clean["assigned_node_id"])

    def test_assigned_node_must_be_registered(self):
        with self.assertRaises(InventoryError):
            clean_device_payload(
                {"name": "CPE", "ip_address": "10.0.0.3", "assigned_node_id": "ghost"},
                parents={}, device_id=None, registered_nodes={"edge-1"})

    def test_assigned_node_accepted_when_registered(self):
        clean = clean_device_payload(
            {"name": "CPE", "ip_address": "10.0.0.3", "assigned_node_id": "edge-1"},
            parents={}, device_id=None, registered_nodes={"edge-1"})
        self.assertEqual(clean["assigned_node_id"], "edge-1")

    def test_assigned_node_skips_validation_when_registered_nodes_omitted(self):
        clean = clean_device_payload(
            {"name": "CPE", "ip_address": "10.0.0.3", "assigned_node_id": "anything"},
            parents={}, device_id=None)
        self.assertEqual(clean["assigned_node_id"], "anything")

    def test_gpon_vendor_defaults_to_none(self):
        clean = clean_device_payload(
            {"name": "OLT-1", "ip_address": "10.0.0.4", "device_type": "OLT"},
            parents={}, device_id=None)
        self.assertIsNone(clean["gpon_vendor"])

    def test_gpon_vendor_accepted_and_lowercased_on_an_olt(self):
        clean = clean_device_payload(
            {"name": "OLT-1", "ip_address": "10.0.0.4", "device_type": "OLT",
             "gpon_vendor": "Huawei"}, parents={}, device_id=None)
        self.assertEqual(clean["gpon_vendor"], "huawei")

    def test_gpon_vendor_rejected_on_non_olt(self):
        with self.assertRaises(InventoryError):
            clean_device_payload(
                {"name": "SW", "ip_address": "10.0.0.4", "device_type": "switch",
                 "gpon_vendor": "huawei"}, parents={}, device_id=None)

    def test_unknown_gpon_vendor_rejected(self):
        with self.assertRaises(InventoryError):
            clean_device_payload(
                {"name": "OLT-1", "ip_address": "10.0.0.4", "device_type": "OLT",
                 "gpon_vendor": "acme-optics"}, parents={}, device_id=None)

class CleanSnmpPayloadTest(unittest.TestCase):
    def test_disabled_needs_no_community(self):
        clean = clean_snmp_payload({"snmp_enabled": 0})
        self.assertEqual(clean["snmp_enabled"], 0)

    def test_enabled_requires_community(self):
        with self.assertRaises(InventoryError):
            clean_snmp_payload({"snmp_enabled": 1})

    def test_enabled_with_community_ok(self):
        clean = clean_snmp_payload({"snmp_enabled": 1, "snmp_community": "public"})
        self.assertEqual(clean["snmp_community"], "public")
        self.assertEqual(clean["snmp_port"], 161)

    def test_bad_version_rejected(self):
        with self.assertRaises(InventoryError):
            clean_snmp_payload({"snmp_enabled": 1, "snmp_community": "x", "snmp_version": "3"})

    def test_bad_port_rejected(self):
        with self.assertRaises(InventoryError):
            clean_snmp_payload({"snmp_enabled": 1, "snmp_community": "x", "snmp_port": 70000})

class CleanBackupLinkTest(unittest.TestCase):
    def test_child_must_exist(self):
        with self.assertRaises(InventoryError):
            clean_backup_link(99, 1, parents={1: None}, backups={})

    def test_parent_must_exist(self):
        with self.assertRaises(InventoryError):
            clean_backup_link(1, 99, parents={1: None}, backups={})

    def test_self_backup_rejected(self):
        with self.assertRaises(InventoryError):
            clean_backup_link(1, 1, parents={1: None}, backups={})

    def test_already_primary_parent_rejected(self):
        with self.assertRaises(InventoryError):
            clean_backup_link(2, 1, parents={1: None, 2: 1}, backups={})

    def test_duplicate_backup_rejected(self):
        with self.assertRaises(InventoryError):
            clean_backup_link(2, 3, parents={1: None, 2: 1, 3: None}, backups={2: {3}})

    def test_cycle_over_full_edge_set_rejected(self):
        with self.assertRaises(InventoryError):
            clean_backup_link(1, 3, parents={1: None, 2: 1, 3: None}, backups={3: {2}})

    def test_valid_backup_accepted(self):
        clean_backup_link(2, 3, parents={1: None, 2: 1, 3: None}, backups={})

    def test_cross_org_id_looks_like_missing_parent(self):
        with self.assertRaises(InventoryError):
            clean_backup_link(1, 42, parents={1: None}, backups={})

class CleanPortBandwidthPayloadTest(unittest.TestCase):
    def test_no_threshold_disables_the_alarm(self):
        clean = clean_port_bandwidth_payload({})
        self.assertIsNone(clean["threshold_mbps"])
        self.assertEqual(clean["direction"], "either")

    def test_valid_threshold_and_direction(self):
        clean = clean_port_bandwidth_payload({"threshold_mbps": 10, "direction": "in"})
        self.assertEqual(clean["threshold_mbps"], 10.0)
        self.assertEqual(clean["direction"], "in")

    def test_bad_direction_rejected(self):
        with self.assertRaises(InventoryError):
            clean_port_bandwidth_payload({"threshold_mbps": 10, "direction": "sideways"})

    def test_non_positive_threshold_rejected(self):
        with self.assertRaises(InventoryError):
            clean_port_bandwidth_payload({"threshold_mbps": 0})
        with self.assertRaises(InventoryError):
            clean_port_bandwidth_payload({"threshold_mbps": -5})

    def test_non_numeric_threshold_rejected(self):
        with self.assertRaises(InventoryError):
            clean_port_bandwidth_payload({"threshold_mbps": "fast"})

    def test_max_mbps_defaults_to_disabled(self):
        clean = clean_port_bandwidth_payload({"threshold_mbps": 10})
        self.assertIsNone(clean["max_mbps"])

    def test_valid_max_mbps(self):
        clean = clean_port_bandwidth_payload({"max_mbps": 500, "direction": "out"})
        self.assertEqual(clean["max_mbps"], 500.0)
        self.assertIsNone(clean["threshold_mbps"])

    def test_non_positive_max_mbps_rejected(self):
        with self.assertRaises(InventoryError):
            clean_port_bandwidth_payload({"max_mbps": 0})

    def test_max_must_exceed_threshold(self):
        with self.assertRaises(InventoryError):
            clean_port_bandwidth_payload({"threshold_mbps": 100, "max_mbps": 50})
        with self.assertRaises(InventoryError):
            clean_port_bandwidth_payload({"threshold_mbps": 100, "max_mbps": 100})

    def test_min_and_max_both_set(self):
        clean = clean_port_bandwidth_payload({"threshold_mbps": 10, "max_mbps": 500})
        self.assertEqual(clean["threshold_mbps"], 10.0)
        self.assertEqual(clean["max_mbps"], 500.0)

class CleanLocationPayloadTest(unittest.TestCase):
    def test_accepts_valid_coordinates(self):
        clean = clean_location_payload({"lat": 17.385044, "lng": 78.486671})
        self.assertEqual(clean, {"lat": 17.385044, "lng": 78.486671})

    def test_rounds_drag_noise(self):
        clean = clean_location_payload({"lat": "17.3850441234", "lng": "78.4866719876"})
        self.assertEqual(clean, {"lat": 17.385044, "lng": 78.486672})

    def test_both_null_clears_pin(self):
        self.assertEqual(clean_location_payload({}), {"lat": None, "lng": None})
        self.assertEqual(clean_location_payload({"lat": None, "lng": None}),
                         {"lat": None, "lng": None})

    def test_one_missing_rejected(self):
        with self.assertRaises(InventoryError):
            clean_location_payload({"lat": 17.4})
        with self.assertRaises(InventoryError):
            clean_location_payload({"lng": 78.5})

    def test_non_numeric_rejected(self):
        with self.assertRaises(InventoryError):
            clean_location_payload({"lat": "north", "lng": 78.5})

    def test_out_of_range_rejected(self):
        with self.assertRaises(InventoryError):
            clean_location_payload({"lat": 91, "lng": 0})
        with self.assertRaises(InventoryError):
            clean_location_payload({"lat": 0, "lng": -181})

class CleanRegionNameTest(unittest.TestCase):
    def test_trims_and_returns(self):
        self.assertEqual(clean_region_name("  north-dc "), "north-dc")

    def test_rejects_empty_and_none(self):
        for raw in (None, "", "   "):
            with self.assertRaises(InventoryError):
                clean_region_name(raw)

    def test_rejects_overlong(self):
        with self.assertRaises(InventoryError):
            clean_region_name("x" * 65)
        self.assertEqual(len(clean_region_name("x" * 64)), 64)

class CleanNodeIdTest(unittest.TestCase):
    def test_valid_ids_pass_through(self):
        self.assertEqual(clean_node_id("edge-a1"), "edge-a1")
        self.assertEqual(clean_node_id("Tower_2.local"), "Tower_2.local")
        self.assertEqual(clean_node_id("  edge-a1  "), "edge-a1")

    def test_empty_rejected(self):
        with self.assertRaises(InventoryError):
            clean_node_id("")
        with self.assertRaises(InventoryError):
            clean_node_id("   ")
        with self.assertRaises(InventoryError):
            clean_node_id(None)

    def test_must_start_with_alnum(self):
        with self.assertRaises(InventoryError):
            clean_node_id("-edge-a1")
        with self.assertRaises(InventoryError):
            clean_node_id("_edge")

    def test_bad_characters_rejected(self):
        for bad in ("edge a1", "edge/a1", "edge;drop table", "edge$a1"):
            with self.assertRaises(InventoryError):
                clean_node_id(bad)

    def test_too_long_rejected(self):
        with self.assertRaises(InventoryError):
            clean_node_id("a" * 65)
        clean_node_id("a" * 64)

class CleanOidAndWalkPayloadTest(unittest.TestCase):
    def test_valid_oids_pass_and_normalise(self):
        from wisp.central.inventory import clean_oid
        self.assertEqual(clean_oid("1.3.6.1.4.1"), "1.3.6.1.4.1")
        self.assertEqual(clean_oid(".1.3.6.1."), "1.3.6.1")
        self.assertEqual(clean_oid("1"), "1")

    def test_bad_oids_rejected(self):
        from wisp.central.inventory import clean_oid
        for bad in ("not.an.oid", "1.3.6.1.4.x", "1..3", "", None, "1.3;drop"):
            with self.assertRaises(InventoryError):
                clean_oid(bad)

    def test_walk_payload_defaults_and_caps(self):
        from wisp.central.inventory import (
            WALK_CAP_MAX_VARBINDS, WALK_DEFAULT_MAX_VARBINDS, clean_walk_payload)
        clean = clean_walk_payload({})
        self.assertEqual(clean["root_oid"], "1.3.6.1")
        self.assertEqual(clean["max_varbinds"], WALK_DEFAULT_MAX_VARBINDS)
        clean = clean_walk_payload({"root_oid": "1.3.6.1.4.1.5651",
                                    "max_varbinds": 10**9})
        self.assertEqual(clean["max_varbinds"], WALK_CAP_MAX_VARBINDS)
        with self.assertRaises(InventoryError):
            clean_walk_payload({"max_varbinds": -5})


class CleanProfilePayloadTest(unittest.TestCase):
    @staticmethod
    def _payload(**overrides):
        base = {"name": "fiberhome", "match_sysobjectid": "1.3.6.1.4.1.5651",
                "metrics": {"cpu_pct": {"oid": "1.3.6.1.4.1.5651.3.901.2.0"}}}
        base.update(overrides)
        return base

    def test_defaults_fill_decode_and_select(self):
        from wisp.central.inventory import clean_profile_payload
        clean = clean_profile_payload(self._payload())
        spec = clean["metrics"]["cpu_pct"]
        self.assertEqual(spec["decode"], "as_is")
        self.assertEqual(spec["select"], "first")
        self.assertTrue(clean["enabled"])

    def test_unknown_metric_decode_select_rejected(self):
        from wisp.central.inventory import clean_profile_payload
        with self.assertRaises(InventoryError):
            clean_profile_payload(self._payload(metrics={
                "fan_rpm": {"oid": "1.3.6.1.4.1.5651.1"}}))
        with self.assertRaises(InventoryError):
            clean_profile_payload(self._payload(metrics={
                "cpu_pct": {"oid": "1.3.6.1.4.1.5651.1", "decode": "times9000"}}))
        with self.assertRaises(InventoryError):
            clean_profile_payload(self._payload(metrics={
                "cpu_pct": {"oid": "1.3.6.1.4.1.5651.1", "select": "median"}}))

    def test_requires_name_match_and_a_metric(self):
        from wisp.central.inventory import clean_profile_payload
        with self.assertRaises(InventoryError):
            clean_profile_payload(self._payload(name=""))
        with self.assertRaises(InventoryError):
            clean_profile_payload(self._payload(match_sysobjectid="vendor"))
        with self.assertRaises(InventoryError):
            clean_profile_payload(self._payload(metrics={}))


if __name__ == "__main__":
    unittest.main()
