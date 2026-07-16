from __future__ import annotations

import logging
from datetime import datetime, timezone

from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.version import is_newer

log = logging.getLogger("wisp.central.rollout")

_TERMINAL = ("done", "halted")

def _now(now):
    return now or datetime.now(timezone.utc).replace(tzinfo=None)

def _on_target_alive(node: dict, target: str, now: datetime, fresh_s: int) -> bool:
    if node.get("version") != target or not node.get("last_seen"):
        return False
    return (now - _parse(node["last_seen"])).total_seconds() <= fresh_s

def maybe_auto_rollout(store, org_id: str, node_id: str,
                       reported_version: str | None) -> bool:
    """Auto-update trigger (orgs.auto_update): when the mirror's newest release
    is ahead of a heartbeating node and no rollout is live, start one with THAT
    node as canary — the same staged, health-gated machinery the dashboard's
    manual Update uses, just without the click. Returns True when it armed one.

    A HALTED rollout for the same target is never auto-retried — halting means
    the build failed its health gate, and only a human (dashboard Retry) should
    re-arm a failed build. A DONE rollout re-arms freely: a node reporting an
    older version after "done" is a fresh/reinstalled box that missed it.
    """
    if not store.org_auto_update(org_id):
        return False
    releases = store.list_releases()
    if not releases:
        return False
    target = releases[0]["version"]
    if not is_newer(target, reported_version):
        return False
    ro = store.get_rollout(org_id)
    if ro and ro["state"] not in _TERMINAL:
        return False
    if ro and ro["state"] == "halted" and ro["target_version"] == target:
        return False
    store.set_rollout(org_id, target, [node_id],
                      note=f"auto-update (canary: {node_id})")
    log.info("auto-update %s -> %s: armed, canary %s", org_id, target, node_id)
    return True


def directive_for(store, org_id: str, node_id: str, reported_version: str | None,
                  platform: str | None, *, now=None) -> dict | None:
    rollout = store.get_rollout(org_id)
    if not rollout or rollout["state"] in _TERMINAL:
        return None
    target = rollout["target_version"]
    if not is_newer(target, reported_version):
        return None
    eligible = rollout["state"] == "promoted" or node_id in rollout["canary"]
    if not eligible:
        return None
    release = store.get_release(target)
    if not release:
        return None
    art = release["artifacts"].get(platform or "")
    if not art:
        return None
    return {"target_version": target, "url": art["url"], "sha256": art["sha256"]}

def evaluate(store, org_id: str, *, cfg: Config = CONFIG, now=None) -> str:
    rollout = store.get_rollout(org_id)
    if not rollout or rollout["state"] in _TERMINAL:
        return rollout["state"] if rollout else "none"
    now = _now(now)
    target = rollout["target_version"]
    fresh_s = cfg.central_node_stale_s
    nodes = store.node_versions(org_id)
    state = rollout["state"]

    if state == "canary":
        canary_nodes = [n for n in nodes if n["node_id"] in rollout["canary"]]
        all_ok = all(_on_target_alive(n, target, now, fresh_s) for n in canary_nodes) \
            and len(canary_nodes) == len(rollout["canary"])
        if all_ok:
            store.update_rollout_state(org_id, "promoted")
            log.info("rollout %s -> %s: canaries healthy, PROMOTED fleet-wide",
                     org_id, target)
            return "promoted"
        elapsed = (now - _parse(rollout["started_at"])).total_seconds()
        if elapsed > cfg.rollout_health_window_s:
            store.update_rollout_state(org_id, "halted")
            log.warning("rollout %s -> %s: canaries unhealthy after %ds — HALTED",
                        org_id, target, int(elapsed))
            return "halted"
        return "canary"

    if nodes and all(_on_target_alive(n, target, now, fresh_s) for n in nodes):
        store.update_rollout_state(org_id, "done")
        log.info("rollout %s -> %s: all nodes updated, DONE", org_id, target)
        return "done"
    return "promoted"
