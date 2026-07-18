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

        async def run() -> None:
            async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
                self._engine, community, transport, ContextData(),
                0, 25, ObjectType(ObjectIdentity(root_oid)),
                lexicographicMode=False,
            ):
                if errInd or errStat:
                    raise RuntimeError(str(errInd or errStat))
                for name, val in binds:
                    if len(result.varbinds) >= limit:
                        result.truncated = True
                        return
                    result.varbinds.append((str(name), val.prettyPrint()))

        try:
            await asyncio.wait_for(run(), _WALK_BUDGET_S)
        except asyncio.TimeoutError:
            result.truncated = True
            log.warning("diagnostic walk of %s %s hit the %.0fs budget at %d varbinds",
                        target.ip, root_oid, _WALK_BUDGET_S, len(result.varbinds))
        return result


def build_diag_walker(cfg: Config = CONFIG) -> PysnmpDiagWalker:
    return PysnmpDiagWalker(cfg)
