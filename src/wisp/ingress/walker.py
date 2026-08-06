"""Diagnostic SNMP walk — central asks, the edge dumps a bounded subtree.

The edge is central's hands for vendor onboarding: a dashboard user queues a walk
against a device, central delivers it in the /report reply (apps/daemon/main.py's
_DiagWalkRunner), and this module does the actual bulk-walk. Deliberately dumb:
one root OID in, a bounded list of (oid, value) strings out — all interpretation
happens at central. Bounds are non-negotiable (a full enterprise tree on a loaded
OLT can be 100k+ varbinds); the walk stops at max_varbinds or the time budget,
whichever hits first, and reports itself truncated.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.walker")

# Hard ceiling regardless of what the directive asks for — central caps too
# (central/inventory.py WALK_CAP_MAX_VARBINDS); the edge must not trust the wire.
MAX_VARBINDS_CEILING = 20000
_WALK_BUDGET_S = 60.0
# GETBULK max-repetitions. Named because the RESUME below reasons about it: a
# walk that ends on an exact multiple of this ended on a batch boundary, which
# is the signature of an agent quitting rather than a subtree running out.
_MAX_REPETITIONS = 25


@dataclass
class WalkResult:
    varbinds: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False


class PysnmpDiagWalker:
    """One SnmpEngine per walker instance, NEVER one per walk (see CLAUDE.md —
    a per-walk engine leaks its UDP transport registration forever)."""

    def __init__(self, cfg: Config = CONFIG) -> None:
        self._timeout = cfg.snmp_request_timeout_s or cfg.snmp_timeout_s
        self._retries = max(1, cfg.snmp_request_retries)
        self._engine = None

    async def walk(self, target, root_oid: str, max_varbinds: int) -> WalkResult:
        try:
            from pysnmp.hlapi.asyncio import (
                SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
                ObjectType, ObjectIdentity, bulk_walk_cmd,
            )
        except ImportError as exc:
            raise RuntimeError(
                "DiagWalker needs 'pysnmp' (pip install pysnmp)."
            ) from exc

        if self._engine is None:
            self._engine = SnmpEngine()
        limit = max(1, min(int(max_varbinds), MAX_VARBINDS_CEILING))
        community = CommunityData(target.community, mpModel=1)
        try:
            transport = await UdpTransportTarget.create(
                (target.ip, target.port), timeout=self._timeout,
                retries=self._retries)
        except Exception as exc:
            raise RuntimeError(f"SNMP walk of {target.ip} failed: {exc}") from exc

        result = WalkResult()
        scope = root_oid.strip().strip(".")
        prefix = scope + "."

        async def sweep(start: str) -> bool:
            """One bulk-walk from `start`. True if it added anything.

            `lexicographicMode` has to be True to resume: with it False pysnmp
            bounds the walk to the SUBTREE OF THE START OID, so resuming from a
            leaf instance returns nothing at all (measured — that is what made
            the first resume attempt look like proof the agent had no more data).
            The subtree bound is therefore enforced here instead, by testing the
            prefix ourselves and stopping at the first OID outside it.
            """
            added = False
            async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
                self._engine, community, transport, ContextData(),
                0, _MAX_REPETITIONS, ObjectType(ObjectIdentity(start)),
                lexicographicMode=(start != scope),
            ):
                if errInd or errStat:
                    raise RuntimeError(str(errInd or errStat))
                for name, val in binds:
                    oid = str(name).strip().strip(".")
                    if start != scope and not (oid == scope or oid.startswith(prefix)):
                        return added          # walked out of the requested subtree
                    if len(result.varbinds) >= limit:
                        result.truncated = True
                        return added
                    result.varbinds.append((oid, val.prettyPrint()))
                    added = True
            return added

        async def run() -> None:
            # These C-Data/Syrotech agents silently END a GETBULK walk partway
            # through a table — pysnmp's generator just stops, so the walk looked
            # CLEAN while missing entire columns. Measured on badri_fiber: every
            # early stop landed on an exact multiple of 25 (400 / 800 / 2275 /
            # 2475 / 5700) while every genuine end did not (194 / 180 / 8 / 3).
            # A short walk reported as complete is the worst failure this tool
            # has: it reads as "that OID holds nothing", which is how a vendor
            # gets written off as unsupported. So: resume from the last OID and
            # let the agent prove it is finished.
            if not await sweep(scope):
                return
            while not result.truncated:
                if len(result.varbinds) % _MAX_REPETITIONS:
                    return                    # ended mid-batch = a real end
                if not await sweep(result.varbinds[-1][0]):
                    return
                log.info("walk of %s %s resumed past a batch-boundary stop"
                         " — now %d varbinds", target.ip, root_oid,
                         len(result.varbinds))

        try:
            await asyncio.wait_for(run(), _WALK_BUDGET_S)
        except asyncio.TimeoutError:
            result.truncated = True
            log.warning("diagnostic walk of %s %s hit the %.0fs budget at %d varbinds",
                        target.ip, root_oid, _WALK_BUDGET_S, len(result.varbinds))
        return result


def build_diag_walker(cfg: Config = CONFIG) -> PysnmpDiagWalker:
    return PysnmpDiagWalker(cfg)
