"""Staged, health-gated rollout — central is the version authority (Phase 10 Part D).

"Update every node" must never mean "brick every node at once." A rollout to a target version
goes out to a **canary** subset first; central promotes it fleet-wide only once the canaries come
back **healthy on the target version**, and **auto-halts** if a canary fails to within a window.
The edge supervisor pulls only when its heartbeat reply carries a directive for a NEWER version
than it runs (see `runtime/supervisor.py`), so version skew during a rollout is normal and safe.

This module is the decision logic, kept pure-ish (store + injected clock) so it unit-tests:
  * `directive_for(...)` — what (if anything) THIS node should be told to install right now;
  * `evaluate(...)`     — advance the canary→promoted→done state machine, or halt.

State machine (per org, one active rollout):
  canary ──(all canaries healthy on target)──▶ promoted ──(all nodes on target)──▶ done
     └────(a canary not healthy on target past the window)────▶ halted
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse

log = logging.getLogger("wisp.central.rollout")

_TERMINAL = ("done", "halted")


def _now(now):
    return now or datetime.now(timezone.utc).replace(tzinfo=None)


def _on_target_alive(node: dict, target: str, now: datetime, fresh_s: int) -> bool:
    """The node reports the target version AND its heartbeat is fresh (it actually came back
    healthy, not just acked the directive then died)."""
    if node.get("version") != target or not node.get("last_seen"):
        return False
    return (now - _parse(node["last_seen"])).total_seconds() <= fresh_s


def directive_for(store, tenant_id: str, node_id: str, reported_version: str | None,
                  platform: str | None, *, now=None) -> dict | None:
    """The update this node should pull now, or None. None when: no rollout / it's terminal /
    the node already runs the target / the node isn't eligible in the current wave / there is
    no artifact for the node's platform (we can't update what we can't build for)."""
    rollout = store.get_rollout(tenant_id)
    if not rollout or rollout["state"] in _TERMINAL:
        return None
    target = rollout["target_version"]
    if reported_version == target:
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


def evaluate(store, tenant_id: str, *, cfg: Config = CONFIG, now=None) -> str:
    """Advance the rollout state machine for one org; persists + returns the new state."""
    rollout = store.get_rollout(tenant_id)
    if not rollout or rollout["state"] in _TERMINAL:
        return rollout["state"] if rollout else "none"
    now = _now(now)
    target = rollout["target_version"]
    fresh_s = cfg.central_node_stale_s
    nodes = store.node_versions(tenant_id)
    state = rollout["state"]

    if state == "canary":
        canary_nodes = [n for n in nodes if n["node_id"] in rollout["canary"]]
        # An empty canary list = no canary wave; promote straight away.
        all_ok = all(_on_target_alive(n, target, now, fresh_s) for n in canary_nodes) \
            and len(canary_nodes) == len(rollout["canary"])
        if all_ok:
            store.update_rollout_state(tenant_id, "promoted")
            log.info("rollout %s -> %s: canaries healthy, PROMOTED fleet-wide",
                     tenant_id, target)
            return "promoted"
        # Past the health window with canaries still not healthy on target -> auto-halt.
        elapsed = (now - _parse(rollout["started_at"])).total_seconds()
        if elapsed > cfg.rollout_health_window_s:
            store.update_rollout_state(tenant_id, "halted")
            log.warning("rollout %s -> %s: canaries unhealthy after %ds — HALTED",
                        tenant_id, target, int(elapsed))
            return "halted"
        return "canary"

    # promoted: finish when every node is on the target and alive.
    if nodes and all(_on_target_alive(n, target, now, fresh_s) for n in nodes):
        store.update_rollout_state(tenant_id, "done")
        log.info("rollout %s -> %s: all nodes updated, DONE", tenant_id, target)
        return "done"
    return "promoted"
