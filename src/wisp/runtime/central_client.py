"""Phase B — the edge's half of "central runs the brain": learn what to probe from
central, report raw per-IP results back. No FSM, no alerting — that's central/engine.py +
central/dispatch.py on the other end (central/server.py's `GET /edge/devices` and
`POST /report`).

Mirrors the `egress/shipper.py` adapter discipline exactly: a tiny `Protocol` with one
real impl (`HttpCentralClient`, httpx lazy-imported like `NtfyNotifier`/`HttpShipper`) so
the daemon stays importable without the venv and tests inject a recording double — no real
network to central in the suite.
"""
from __future__ import annotations

from typing import Protocol

from wisp.config import CONFIG, Config

# Wire envelope version for POST /report (and central's /ingest). Bump on a
# breaking change to the envelope shape; central accepts v <= MAX_WIRE_V so a
# staged fleet rollout with mixed edge versions never breaks ingest.
WIRE_V = 1


class CentralClientError(RuntimeError):
    """A central call failed outright (network/HTTP/bad response). The caller decides
    the fallback — refuse to start on the first fetch, or skip a cycle and retry next
    tick; this never crashes the poll loop."""


class CentralBrainClient(Protocol):
    def fetch_devices(self) -> dict: ...
    def report(self, pings: dict, ts: str, *, mode: str = "full",
              ports: dict | None = None) -> dict: ...


class HttpCentralClient:
    def __init__(self, cfg: Config = CONFIG) -> None:
        self.base = cfg.central_url.rstrip("/")
        self.token = cfg.central_token
        self.tenant_id = cfg.tenant_id
        self.node_id = cfg.node_id
        self.timeout = cfg.ship_timeout_s
        # mTLS enrollment (plan.md item 6): a cert issued by `central.admin enroll-edge`.
        # Coexists with the bearer token above — central accepts either, so setting
        # these isn't a hard cutover off the token (see central/server.py's `_ingest_ok`).
        self.client_cert = cfg.central_client_cert
        self.client_key = cfg.central_client_key
        self.ca_cert = cfg.central_ca_cert

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _tls_kwargs(self) -> dict:
        kwargs = {}
        if self.ca_cert:
            kwargs["verify"] = self.ca_cert
        if self.client_cert and self.client_key:
            kwargs["cert"] = (self.client_cert, self.client_key)
        return kwargs

    def fetch_devices(self) -> dict:
        """{'devices': [{'id','name','ip_address','region','parent_device_id'}, …],
        'canary_ip': …} — this tenant's ISP-managed topology (org_devices), the thing the
        edge now probes instead of a locally-configured device list."""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise CentralClientError(f"httpx missing: {exc}") from exc
        try:
            resp = httpx.get(f"{self.base}/edge/devices",
                             params={"tenant_id": self.tenant_id},
                             headers=self._headers(), timeout=self.timeout,
                             **self._tls_kwargs())
            resp.raise_for_status()
            return resp.json()
        except CentralClientError:
            raise
        except Exception as exc:
            raise CentralClientError(str(exc)) from exc

    def report(self, pings: dict, ts: str, *, mode: str = "full",
              ports: dict | None = None) -> dict:
        """Ship one cycle's raw per-IP results ({ip: {loss_pct, latency_ms, jitter_ms}}).
        `mode="full"` (default) is a normal poll; `mode="recheck"` carries samples for
        ONLY the fast-confirm suspect IPs central named in a prior reply's `recheck` hint.
        Either way central may reply with ANOTHER `recheck` hint
        ({'recheck': {'down_ips','up_ips','interval_s'}}) — the caller
        (apps/daemon/main.py's central-brain loop) follows it until a reply omits one.

        `ports` (optional) is this cycle's SNMP haul — {device_id: [port dict, ...]} —
        the edge's own slow SNMP cadence, independent of the ICMP poll interval (see
        `apps/daemon/main.py:_gather_snmp_ports`). Only ever attached to a "full"
        report; a recheck round is ICMP-only, so its caller never passes this."""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise CentralClientError(f"httpx missing: {exc}") from exc
        env = {"v": WIRE_V, "tenant_id": self.tenant_id, "node_id": self.node_id,
              "ts": ts, "mode": mode, "pings": pings}
        if ports:
            env["ports"] = ports
        try:
            resp = httpx.post(f"{self.base}/report", json=env,
                              headers=self._headers(), timeout=self.timeout,
                              **self._tls_kwargs())
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except CentralClientError:
            raise
        except Exception as exc:
            raise CentralClientError(str(exc)) from exc


def build_central_client(cfg: Config = CONFIG) -> CentralBrainClient:
    return HttpCentralClient(cfg)
