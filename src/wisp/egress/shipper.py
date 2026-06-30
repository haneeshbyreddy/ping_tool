"""Central shipper — drains the outbox to the central server and heartbeats liveness.

This is the network/egress side of the store-and-forward path (the durable queue glue is
`database/outbox.py`). It mirrors the existing adapter discipline exactly:

  * `Shipper` is a tiny interface with one real impl (`HttpShipper`, httpx lazy-imported
    like `NtfyNotifier`), so the dashboard + the test suite stay pure-stdlib and tests
    inject a recording-shipper double — never any real network to central.
  * `ShipperWorker` is the pure-ish logic (drain one batch / send one heartbeat / evict),
    clock- and shipper-injectable so it unit-tests against a temp DB with a fake shipper.
  * `start_shipper_thread` runs the worker on a daemon thread, mirroring
    `start_watchdog_thread` — isolated in its own try/except so a shipper hiccup (or a dead
    central, or a WAN cut) never touches the poll loop. The monitor stays the hardest thing
    in the system to kill.

The wire format is a versioned envelope (`v`, tenant_id, node_id, kind, …) so central ingest
can accept old + new `v` during a rollout (version skew is normal in a fleet). At-least-once
delivery + idempotent central storage (a per-node record id) = effectively-once: a lost ack
just re-ships rows central already holds, and it re-acks them.
"""
from __future__ import annotations

import json
import logging
import threading
import time as _time
from datetime import datetime, timezone
from typing import NamedTuple, Protocol

from wisp.config import CONFIG, Config
from wisp.database import outbox
from wisp.database.client import connect, write_with_retry
from wisp.version import VERSION

log = logging.getLogger("wisp.shipper")

# Wire protocol version. Bump only on an incompatible envelope change; central ingest is
# expected to accept a window of versions so a staged fleet rollout never breaks ingest.
WIRE_V = 1


class ShipResult(NamedTuple):
    ok: bool
    accepted_ids: list[int] = []   # outbox ids central durably holds (-> safe to delete)
    status: int = 0
    detail: str = ""


class Shipper(Protocol):
    def ship(self, envelope: dict) -> ShipResult: ...
    def heartbeat(self, envelope: dict) -> ShipResult: ...


class HttpShipper:
    """Real edge→central transport: POST JSON over HTTPS with a bearer token.

    Edge-initiated outbound only (central never dials in — zero inbound holes behind ISP
    NAT). The bearer token is the Part A stopgap for identity/authn; the envelope is shaped
    so Part C's mTLS client cert replaces the token without touching the record format."""

    def __init__(self, cfg: Config = CONFIG) -> None:
        self.base = cfg.central_url.rstrip("/")
        self.token = cfg.central_token
        self.timeout = cfg.ship_timeout_s

    def _post(self, path: str, envelope: dict) -> tuple[bool, int, dict, str]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            return False, 0, {}, f"httpx missing: {exc}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = httpx.post(f"{self.base}{path}", json=envelope,
                              headers=headers, timeout=self.timeout)
        except Exception as exc:  # connection/timeout — transient, the queue waits
            return False, 0, {}, str(exc)
        body: dict = {}
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {}
        ok = 200 <= resp.status_code < 300
        return ok, resp.status_code, body, "" if ok else f"HTTP {resp.status_code}"

    def ship(self, envelope: dict) -> ShipResult:
        ok, status, body, detail = self._post("/ingest", envelope)
        if not ok:
            return ShipResult(False, [], status, detail)
        # Central echoes the ids it now durably holds. If a lenient central 2xx's without a
        # body, fall back to "everything we sent" (it accepted the batch wholesale).
        accepted = body.get("accepted")
        if accepted is None:
            accepted = [r["id"] for r in envelope.get("records", [])]
        return ShipResult(True, [int(i) for i in accepted], status, "")

    def heartbeat(self, envelope: dict) -> ShipResult:
        ok, status, _body, detail = self._post("/heartbeat", envelope)
        return ShipResult(ok, [], status, detail)


def build_shipper(cfg: Config = CONFIG) -> Shipper:
    return HttpShipper(cfg)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ShipperWorker:
    """The drain/heartbeat/evict logic, decoupled from the thread so it is unit-testable
    with a temp DB + an injected `Shipper` double + an injected clock."""

    def __init__(self, cfg: Config = CONFIG, shipper: Shipper | None = None,
                 clock=_time.monotonic) -> None:
        self.cfg = cfg
        self.shipper = shipper or build_shipper(cfg)
        self._clock = clock
        self._stop = threading.Event()

    # --- envelopes ---
    def _envelope(self, kind: str) -> dict:
        return {"v": WIRE_V, "tenant_id": self.cfg.tenant_id,
                "node_id": self.cfg.node_id, "kind": kind, "sent_at": _now_iso()}

    def heartbeat_body(self) -> dict:
        """Live liveness + a cheap health snapshot, read fresh from the DB each beat. This
        doubles as the central cross-edge watchdog's signal (Part B): a missing heartbeat =
        box dead or WAN cut. Facts only — central decides staleness from last_seen."""
        with connect(self.cfg) as conn:
            last_poll = conn.execute(
                "SELECT MAX(timestamp) AS t FROM poll_results").fetchone()["t"]
            fleet = conn.execute(
                "SELECT COUNT(*) FROM devices WHERE is_active = 1").fetchone()[0]
            open_outages = conn.execute(
                "SELECT COUNT(*) FROM outages WHERE resolved_at IS NULL").fetchone()[0]
            backlog = outbox.count(conn)
        return {"version": VERSION, "last_poll_ts": last_poll, "fleet_size": int(fleet),
                "open_outages": int(open_outages), "outbox_backlog": int(backlog)}

    # --- one-shot operations (the unit-test seams) ---
    def heartbeat_once(self) -> bool:
        env = self._envelope("heartbeat")
        env["body"] = self.heartbeat_body()
        res = self.shipper.heartbeat(env)
        if not res.ok:
            log.debug("heartbeat to central failed: %s", res.detail)
        return res.ok

    def drain_once(self) -> tuple[bool, int]:
        """Ship one batch. Returns (healthy, rows_shipped). healthy is False only when
        there were rows and central rejected/was unreachable (so the loop backs off);
        an empty queue is healthy with 0 shipped."""
        with connect(self.cfg) as conn:
            rows = outbox.pending(conn, self.cfg.ship_batch)
        if not rows:
            return True, 0
        env = self._envelope("batch")
        env["records"] = [
            {"id": r["id"], "kind": r["kind"], "body": json.loads(r["payload"])}
            for r in rows
        ]
        res = self.shipper.ship(env)
        ids = [r["id"] for r in rows]
        if not res.ok:
            def _bump():
                with connect(self.cfg) as conn:
                    outbox.bump_attempts(conn, ids)
                    conn.commit()
            write_with_retry(_bump)
            log.debug("ship of %d record(s) failed: %s", len(ids), res.detail)
            return False, 0

        accepted = [i for i in res.accepted_ids if i in set(ids)]

        def _ack():
            with connect(self.cfg) as conn:
                outbox.mark_sent(conn, accepted)
                conn.commit()
        write_with_retry(_ack)
        # Central acked < what we sent (partial accept) — not fully healthy; retry the rest.
        return len(accepted) == len(ids), len(accepted)

    def evict_once(self) -> int:
        """Enforce the outbox high-water mark by shedding the oldest rollups (never an
        event). 0 = unlimited. Returns rows evicted."""
        cap = self.cfg.outbox_max_rows
        if cap <= 0:
            return 0

        def _do() -> int:
            with connect(self.cfg) as conn:
                over = outbox.count(conn) - cap
                if over <= 0:
                    return 0
                n = outbox.evict_rollups(conn, over)
                conn.commit()
                return n
        evicted = int(write_with_retry(_do) or 0)
        if evicted:
            log.warning("outbox over %d rows — evicted %d oldest rollup record(s) "
                        "(events are never dropped)", cap, evicted)
        return evicted

    # --- the loop ---
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("central shipper started -> %s (node=%s tenant=%s)",
                 self.cfg.central_url, self.cfg.node_id, self.cfg.tenant_id)
        backoff = self.cfg.ship_backoff_s
        next_hb = self._clock()
        while not self._stop.is_set():
            healthy = True
            try:
                if self._clock() >= next_hb:
                    healthy = self.heartbeat_once() and healthy
                    next_hb = self._clock() + self.cfg.heartbeat_interval_s
                ok, _n = self.drain_once()
                healthy = ok and healthy
                self.evict_once()
            except Exception:
                log.exception("shipper cycle failed; will retry")
                healthy = False
            if healthy:
                delay = self.cfg.ship_interval_s
                backoff = self.cfg.ship_backoff_s
            else:
                delay = backoff
                backoff = min(backoff * 2, self.cfg.ship_backoff_max_s)
            self._stop.wait(delay)


def start_shipper_thread(cfg: Config = CONFIG,
                         shipper: Shipper | None = None) -> threading.Thread | None:
    """Run the shipper on a daemon thread from the poll daemon. No-op (returns None) when
    central is not configured — the back-compat anchor: WISP_CENTRAL_URL empty ⇒ nothing
    starts and the edge is byte-for-byte standalone."""
    if not cfg.central_enabled():
        return None
    worker = ShipperWorker(cfg, shipper)
    t = threading.Thread(target=worker.run, name="wisp-shipper", daemon=True)
    t.worker = worker  # type: ignore[attr-defined]  (lets a caller stop it for shutdown/tests)
    t.start()
    return t
