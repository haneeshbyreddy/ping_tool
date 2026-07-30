"""The paging-responsibility rules, as pure math and against a live store.

What these pin, in one sentence each: responsibility flows DOWN the tree, it
UNIONS rather than overriding, an unassigned device still pages everybody, and a
malformed parent chain can't hang a page.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wisp.central.assignment import (PagingAudience, audience_for,
                                     responsible_users, scope_of)
from wisp.central.store import CentralStore


# olt ─ splitter ─ onu, with a wan switch above the olt
PARENTS = {1: None, 2: 1, 3: 2, 4: 3}


class ScopeMathTest(unittest.TestCase):

    def test_responsibility_flows_down_the_tree(self):
        # Assigned on the WAN switch (1) — responsible for everything below it,
        # which is the whole point: one row covers a region.
        a = {1: {7}}
        for did in (1, 2, 3, 4):
            self.assertEqual(responsible_users(did, PARENTS, a), {7}, did)

    def test_it_does_not_flow_up(self):
        # Assigned on the ONU (4): the switch above is somebody else's problem.
        a = {4: {7}}
        self.assertEqual(responsible_users(4, PARENTS, a), {7})
        for did in (1, 2, 3):
            self.assertEqual(responsible_users(did, PARENTS, a), set(), did)

    def test_inheritance_unions_and_never_overrides(self):
        # THE invariant: naming a second worker on the OLT must not un-page the
        # worker who owns the region head. Nearest-ancestor-wins would return
        # {8} here and silently drop 7 off every page below the OLT.
        a = {1: {7}, 2: {8}}
        self.assertEqual(responsible_users(2, PARENTS, a), {7, 8})
        self.assertEqual(responsible_users(4, PARENTS, a), {7, 8})

    def test_unassigned_is_none_not_empty(self):
        # None means "nobody is responsible, so page every worker" — the
        # pre-feature behaviour. Collapsing it into set() would read as
        # "page nobody", which is the one failure this must never introduce.
        self.assertIsNone(audience_for(3, PARENTS, {}))
        self.assertIsNone(audience_for(3, PARENTS, {4: {7}}))
        self.assertEqual(audience_for(3, PARENTS, {2: {7}}), {7})

    def test_a_cycle_terminates(self):
        # Inventory validation rejects cycles on the way in, but a page is the
        # last thing that may spin on a bad row.
        looped = {1: 2, 2: 1}
        self.assertEqual(responsible_users(1, looped, {2: {9}}), {9})

    def test_scope_of_is_the_inverse(self):
        a = {2: {7}, 4: {8}}
        self.assertEqual(scope_of(7, PARENTS, a), {2, 3, 4})
        self.assertEqual(scope_of(8, PARENTS, a), {4})
        self.assertEqual(scope_of(99, PARENTS, a), set())

    def test_scope_keeps_a_row_on_a_dead_device(self):
        # The device was deactivated but the row survives; a count that dropped
        # it would hide an assignment the operator can still see and clear.
        self.assertEqual(scope_of(7, PARENTS, {404: {7}}), {404})


class AudienceResolverTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.store.set_org("ispA", name="A")
        self.owner = self.store.add_user("ispA", "own", "h", "s", "owner")
        self.w1 = self.store.add_user("ispA", "w1", "h", "s", "worker")
        self.w2 = self.store.add_user("ispA", "w2", "h", "s", "worker")
        self.store.set_user_whatsapp(self.owner, "919000000001")
        self.store.set_user_whatsapp(self.w1, "919000000002")
        self.store.set_user_whatsapp(self.w2, "919000000003")
        self.olt = self.store.create_org_device("ispA", {
            "name": "OLT", "ip_address": "10.0.0.1", "device_type": "olt",
            "region": "Hansa", "parent_device_id": None})
        self.onu = self.store.create_org_device("ispA", {
            "name": "SW", "ip_address": "10.0.0.2", "device_type": "switch",
            "region": "Hansa", "parent_device_id": self.olt})

    def _numbers(self, device_id):
        return PagingAudience(self.store, "ispA").for_device(device_id)

    def test_unassigned_device_pages_everyone(self):
        self.assertEqual(sorted(self._numbers(self.olt)),
                         ["919000000001", "919000000002", "919000000003"])

    def test_assignment_narrows_to_owner_plus_assignee(self):
        self.store.set_device_assignees("ispA", self.olt, [self.w1], "own")
        self.assertEqual(sorted(self._numbers(self.olt)),
                         ["919000000001", "919000000002"])

    def test_a_child_inherits_the_narrowing(self):
        self.store.set_device_assignees("ispA", self.olt, [self.w2], "own")
        self.assertEqual(sorted(self._numbers(self.onu)),
                         ["919000000001", "919000000003"])

    def test_clearing_restores_the_whole_team(self):
        self.store.set_device_assignees("ispA", self.olt, [self.w1], "own")
        self.store.set_device_assignees("ispA", self.olt, [], "own")
        self.assertEqual(len(self._numbers(self.olt)), 3)

    def test_a_deactivated_assignee_does_not_narrow_to_nobody(self):
        # The row survives (so the operator can see it) but must not count as
        # "somebody is responsible" — that would leave the device paging owners
        # only, silently, because an account was switched off.
        self.store.set_device_assignees("ispA", self.olt, [self.w1], "own")
        self.store.set_user_active(self.w1, False)
        self.assertEqual(len(self._numbers(self.olt)), 2)  # owner + w2
        self.assertIn("919000000003", self._numbers(self.olt))

    def test_org_level_alert_stays_org_wide(self):
        self.store.set_device_assignees("ispA", self.olt, [self.w1], "own")
        self.assertEqual(len(self._numbers(None)), 3)

    def test_numberless_assignee_leaves_owners_only(self):
        # Reported by the assign API as `unreachable`; the resolver does NOT
        # widen back to the whole team, or an assignment would be undone by the
        # assignee's own missing profile field.
        self.store.set_user_whatsapp(self.w1, None)
        self.store.set_device_assignees("ispA", self.olt, [self.w1], "own")
        self.assertEqual(self._numbers(self.olt), ["919000000001"])

    def test_probe_audience_follows_what_the_probe_carries(self):
        probed = self.store.create_org_device("ispA", {
            "name": "OnN1", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": "Hansa", "parent_device_id": None,
            "assigned_node_id": "n1"})
        self.store.set_device_assignees("ispA", probed, [self.w1], "own")
        aud = PagingAudience(self.store, "ispA")
        self.assertEqual(sorted(aud.for_node("n1")),
                         ["919000000001", "919000000002"])

    def test_a_probe_with_nothing_assigned_behind_it_stays_org_wide(self):
        aud = PagingAudience(self.store, "ispA")
        self.assertEqual(len(aud.for_node("n-unknown")), 3)


class AssignmentStoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.store.set_org("ispA", name="A")
        self.store.set_org("ispB", name="B")
        self.w1 = self.store.add_user("ispA", "w1", "h", "s", "worker")
        self.foreign = self.store.add_user("ispB", "wB", "h", "s", "worker")
        self.d1 = self.store.create_org_device("ispA", {
            "name": "A1", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.d2 = self.store.create_org_device("ispA", {
            "name": "A2", "ip_address": "10.0.0.2", "device_type": "switch",
            "region": None, "parent_device_id": None})

    def test_a_foreign_account_is_dropped_not_stored(self):
        self.store.set_device_assignees("ispA", self.d1, [self.foreign], "own")
        self.assertEqual(self.store.device_assignment_map("ispA"), {})

    def test_unknown_device_is_refused(self):
        self.assertFalse(self.store.set_device_assignees("ispA", 9999,
                                                         [self.w1], "own"))

    def test_bulk_assign_is_additive(self):
        w2 = self.store.add_user("ispA", "w2", "h", "s", "worker")
        self.store.set_device_assignees("ispA", self.d1, [w2], "own")
        n = self.store.bulk_assign_devices("ispA", [self.d1, self.d2],
                                           [self.w1], "own")
        self.assertEqual(n, 2)
        # w2 survives on d1 — a bulk hand-over must not strip an audience it was
        # never asked about.
        self.assertEqual(self.store.device_assignment_map("ispA"),
                         {self.d1: {w2, self.w1}, self.d2: {self.w1}})

    def test_bulk_remove(self):
        self.store.bulk_assign_devices("ispA", [self.d1, self.d2], [self.w1], "own")
        self.store.bulk_assign_devices("ispA", [self.d1], [self.w1], "own",
                                       remove=True)
        self.assertEqual(self.store.device_assignment_map("ispA"),
                         {self.d2: {self.w1}})

    def test_bulk_ignores_a_device_from_another_org(self):
        other = self.store.create_org_device("ispB", {
            "name": "B1", "ip_address": "10.9.9.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.store.bulk_assign_devices("ispA", [self.d1, other], [self.w1], "own")
        self.assertEqual(set(self.store.device_assignment_map("ispA")), {self.d1})

    def test_reassigning_keeps_the_original_stamp(self):
        self.store.set_device_assignees("ispA", self.d1, [self.w1], "own")
        first = self.store.list_device_assignments("ispA")[0]["assigned_at"]
        w2 = self.store.add_user("ispA", "w2", "h", "s", "worker")
        self.store.set_device_assignees("ispA", self.d1, [self.w1, w2], "own")
        rows = {r["user_id"]: r for r in self.store.list_device_assignments("ispA")}
        self.assertEqual(rows[self.w1]["assigned_at"], first)

    def test_deleting_an_account_drops_its_rows(self):
        self.store.set_device_assignees("ispA", self.d1, [self.w1], "own")
        self.store.delete_user(self.w1)
        self.assertEqual(self.store.list_device_assignments("ispA"), [])

    def test_deleting_a_device_drops_its_rows(self):
        self.store.set_device_assignees("ispA", self.d1, [self.w1], "own")
        self.assertTrue(self.store.delete_org_device("ispA", self.d1)["ok"])
        self.assertEqual(self.store.list_device_assignments("ispA"), [])

    def test_device_list_carries_the_explicit_assignees(self):
        self.store.set_device_assignees("ispA", self.d1, [self.w1], "own")
        rows = {d["id"]: d for d in self.store.list_org_devices("ispA")}
        self.assertEqual(rows[self.d1]["assignee_ids"], [self.w1])
        self.assertEqual(rows[self.d2]["assignee_ids"], [])


if __name__ == "__main__":
    unittest.main()
