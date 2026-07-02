"""Tests for the pure central device-inventory validation (Phase A). No DB, no network —
mirrors tests/unit/test_baseline.py.

Run:  python -m unittest discover -s tests   (from the project root)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.central.inventory import (
    InventoryError,
    clean_backup_link,
    clean_device_payload,
    clean_node_id,
    clean_port_bandwidth_payload,
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
        # 1 -> None (root), 2 -> 1. Making 1's parent 2 would close a loop.
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
        # `parents` is always scoped to one org by the caller — an id from another org
        # simply isn't in the map, so it's rejected the same as any nonexistent id.
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
        # Callers that don't care about assignment (most existing tests) shouldn't have
        # to thread a node set through just to exercise unrelated fields.
        clean = clean_device_payload(
            {"name": "CPE", "ip_address": "10.0.0.3", "assigned_node_id": "anything"},
            parents={}, device_id=None)
        self.assertEqual(clean["assigned_node_id"], "anything")


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
        # 1 -> None (root), 2 -> 1 (primary), 3 -> 2 (backup already). Backing 1 up to 3
        # would close a loop through the combined primary+backup graph.
        with self.assertRaises(InventoryError):
            clean_backup_link(1, 3, parents={1: None, 2: 1, 3: None}, backups={3: {2}})

    def test_valid_backup_accepted(self):
        clean_backup_link(2, 3, parents={1: None, 2: 1, 3: None}, backups={})   # no raise

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


class CleanNodeIdTest(unittest.TestCase):
    def test_valid_ids_pass_through(self):
        self.assertEqual(clean_node_id("edge-a1"), "edge-a1")
        self.assertEqual(clean_node_id("Tower_2.local"), "Tower_2.local")
        self.assertEqual(clean_node_id("  edge-a1  "), "edge-a1")   # trimmed

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
        clean_node_id("a" * 64)   # exactly the limit is fine


if __name__ == "__main__":
    unittest.main()
