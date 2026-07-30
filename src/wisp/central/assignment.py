"""Which field accounts are responsible for which devices.

This is a **paging rule and nothing else**. Assigning a device narrows who gets
its WhatsApp page; it does not hide a row, a KPI, a map pin or an export from
anybody (operator choice 2026-07-26 — every account keeps seeing the whole
fleet). Nothing here is a permission check, so a bug in this module can lose a
notification but can never leak or withhold data.

Three rules, each load-bearing:

* **Responsibility flows DOWN the tree.** An account named on a device owns that
  device and every device beneath it, so one row on an aggregation switch covers
  the region hanging off it — which is the only way this stays maintainable on a
  fleet that grows a device most weeks. Rows are stored only where the operator
  clicked; the inheritance is derived here, every time, from the live parent
  chain. So re-parenting a device moves its responsibility with it, and adding a
  splitter under an assigned OLT needs no second click.

* **Inheritance UNIONS, it does not override.** A worker assigned the region head
  keeps getting pages for an OLT below it even after someone else is named on
  that OLT specifically. Nearest-ancestor-wins would let a narrow assignment
  silently un-page whoever owns the region — a page nobody expected to lose is
  exactly the failure this whole subsystem must not introduce.

* **An UNASSIGNED device pages every worker**, i.e. exactly what happened before
  this feature existed (``audience_for`` returns ``None`` to say so, which is
  NOT the same as the empty set). Assignment therefore narrows paging only where
  an operator has actually assigned somebody, and switching it on cannot silence
  a fleet. Same instinct as the notify governor writing state rows regardless of
  its allowlist: a suppressed page must always be a deliberate, visible choice.

Owners are never narrowed — they page for everything, as they always did. Only
`worker` accounts are filtered, and only for device-scoped alerts (a device
up/down, one of its ports, or the probe carrying it). Org-level pages with no
device behind them — billing, the digest, a test alert — still go to the whole
audience through ``store.org_alert_recipients``.
"""
from __future__ import annotations


def responsible_users(device_id: int, parents: dict[int, int | None],
                      assignments: dict[int, set[int]]) -> set[int]:
    """User ids explicitly assigned to ``device_id`` or to any ancestor of it.

    Walks the primary parent chain, unioning as it climbs (see the module
    docstring on why this unions rather than stopping at the nearest hit).
    Carries a ``seen`` set because a malformed chain must not hang a report
    cycle: inventory validation rejects cycles on the way in, but a page is the
    last thing that may spin on bad data.

    Backup and peer edges are deliberately ignored — a backup parent is a
    failover path, not a chain of command, and peers are cabling. Responsibility
    follows the tree the operator actually reads on the Network page.
    """
    seen: set[int] = set()
    out: set[int] = set()
    cur: int | None = device_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        out |= assignments.get(cur, set())
        cur = parents.get(cur)
    return out


def audience_for(device_id: int, parents: dict[int, int | None],
                 assignments: dict[int, set[int]]) -> set[int] | None:
    """``None`` = nobody is responsible, so page every worker (the pre-feature
    behaviour); otherwise the exact set of user ids to page.

    The distinction between ``None`` and ``set()`` is the whole safety property —
    collapsing them would turn "not assigned yet" into "page nobody".
    """
    users = responsible_users(device_id, parents, assignments)
    return users or None


def scope_of(user_id: int, parents: dict[int, int | None],
             assignments: dict[int, set[int]]) -> set[int]:
    """Every device ``user_id`` is responsible for: the rows naming them plus
    everything below those rows. The inverse of ``responsible_users``, used for
    the UI's "N devices" count and for node-level (probe) paging."""
    roots = {did for did, users in assignments.items() if user_id in users}
    if not roots:
        return set()
    children: dict[int, list[int]] = {}
    for did, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(did)
    # A row can name a device that has since been deactivated (`parents` carries
    # only live ones); the root stays in the answer either way, so a count never
    # silently drops an assignment the operator can still see and clear.
    out: set[int] = set(roots)
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for kid in children.get(cur, ()):
            if kid not in out:
                out.add(kid)
                stack.append(kid)
    return out


class PagingAudience:
    """Resolves "who gets paged about this device" against one org's live tree.

    Built per sweep (a report cycle, a watchdog tick) and cached for its
    lifetime: a port sweep asks about many devices on one switch, and re-reading
    the tree per port would put a query storm behind every alarm. Deliberately
    short-lived rather than invalidated — a sweep is short and an assignment made
    mid-sweep can wait for the next one.

    Every method returns E.164 numbers, deduped, owners first, ready to hand to
    the notifier. A store read failing here must never take down a page, so the
    resolver falls back to the org-wide audience on any surprise.
    """

    def __init__(self, store, org_id: str) -> None:
        self.store = store
        self.org_id = org_id
        self._loaded = False
        self._parents: dict[int, int | None] = {}
        self._assignments: dict[int, set[int]] = {}
        self._owner_numbers: list[str] = []
        self._worker_numbers: dict[int, str] = {}
        self._cache: dict[int, list[str]] = {}

    # --- lazy load ---------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._parents = self.store.device_parent_map(self.org_id)
        self._assignments = self.store.device_assignment_map(self.org_id)
        numbers = self.store.org_paging_numbers(self.org_id)
        self._owner_numbers = numbers["owners"]
        self._worker_numbers = numbers["workers"]

    @property
    def _all_workers(self) -> list[str]:
        return list(self._worker_numbers.values())

    def _compose(self, worker_numbers) -> list[str]:
        return list(dict.fromkeys([*self._owner_numbers, *worker_numbers]))

    # --- resolution --------------------------------------------------------

    def org_wide(self) -> list[str]:
        """Owners + every worker — an alert with no device behind it (uplink
        frozen, digest, billing). Same set as ``store.org_alert_recipients``,
        resolved off the load this instance already did."""
        self._load()
        return self._compose(self._all_workers)

    def for_device(self, device_id: int | None) -> list[str]:
        """Numbers to page about one device. No device id (an org-level event)
        means the whole audience."""
        if device_id is None:
            return self.org_wide()
        cached = self._cache.get(device_id)
        if cached is not None:
            return cached
        self._load()
        users = audience_for(device_id, self._parents, self._assignments)
        if users is None:
            # Unassigned: everyone, exactly as before the feature.
            out = self._compose(self._all_workers)
        else:
            assigned = [self._worker_numbers[u] for u in sorted(users)
                        if u in self._worker_numbers]
            # An owner assigned to a device is already in _owner_numbers, and an
            # assignee with no WhatsApp number simply isn't reachable — the API
            # reports that at assign time rather than silently widening here.
            out = self._compose(assigned)
        self._cache[device_id] = out
        return out

    def for_node(self, node_id: str) -> list[str]:
        """Numbers to page about a PROBE (node) going silent.

        A probe is not a device, so responsibility is derived from what it
        carries: every account responsible for any device assigned to this node.
        With nothing assigned behind it the page stays org-wide — a probe going
        dark blinds a whole slice of the fleet and is the last alarm to narrow on
        a guess. Owners always get it.
        """
        self._load()
        device_ids = self.store.node_device_ids(self.org_id, node_id)
        if not device_ids:
            return self.org_wide()
        users: set[int] = set()
        for did in device_ids:
            users |= responsible_users(did, self._parents, self._assignments)
        if not users:
            return self.org_wide()
        return self._compose([self._worker_numbers[u] for u in sorted(users)
                              if u in self._worker_numbers])
