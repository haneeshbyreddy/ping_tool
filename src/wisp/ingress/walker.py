from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.walker")

MAX_VARBINDS_CEILING = 20000
_WALK_BUDGET_S = 60.0
_MAX_REPETITIONS = 25


@dataclass
class WalkResult:
    varbinds: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False


class PysnmpDiagWalker:
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
                        return added
                    if len(result.varbinds) >= limit:
                        result.truncated = True
                        return added
                    result.varbinds.append((oid, val.prettyPrint()))
                    added = True
            return added

        async def run() -> None:
            if not await sweep(scope):
                return
            while not result.truncated:
                if len(result.varbinds) % _MAX_REPETITIONS:
                    return
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
