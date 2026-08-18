from __future__ import annotations

import gzip
import json
import logging
from typing import Protocol

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.edge.central")

WIRE_V = 1

# Level 1, and do NOT "improve" it. Measured on a real hot port sweep:
# level 1 = 87% saving in 4 ms, level 6 = 89% in 9 ms, level 9 = 90% in 49 ms.
# The last three points cost an order of magnitude, and some probes are very
# small boxes. The volume is all SNMP tables (ping payloads are ~0.4 KB and
# irrelevant), and repetitive JSON gives up nearly everything at level 1.
_GZIP_LEVEL = 1

class CentralClientError(RuntimeError):
    pass

class CentralBrainClient(Protocol):
    def fetch_devices(self) -> dict: ...
    def report(self, pings: dict, ts: str, *, mode: str = "full",
              ports: dict | None = None, optics: dict | None = None,
              health: dict | None = None,
              snmp_status: dict | None = None) -> dict: ...
    def heartbeat(self, body: dict) -> dict: ...
    def walk_result(self, walk_id: int, *, varbinds: list | None = None,
                    error: str | None = None, truncated: bool = False) -> dict: ...
    def proxy_next(self, hold_s: float) -> dict | None: ...
    def proxy_reply(self, sid: str, req_id: int, status: int, headers: dict,
                    body_b64: str, *, error: str | None = None) -> dict: ...
    def liveping_exchange(self, token: int, samples: dict, refusals: list,
                          hold_s: float) -> dict: ...
    def close(self) -> None: ...

class HttpCentralClient:

    def __init__(self, cfg: Config = CONFIG) -> None:
        self.base = cfg.central_url.rstrip("/")
        self.token = cfg.central_token
        self.org_id = cfg.org_id
        self.node_id = cfg.node_id
        self.timeout = cfg.ship_timeout_s
        self.gzip_min = cfg.ship_gzip_min_bytes
        self.client_cert = cfg.central_client_cert
        self.client_key = cfg.central_client_key
        self.ca_cert = cfg.central_ca_cert
        self._client = None

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

    def _http(self):
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:
                raise CentralClientError(f"httpx missing: {exc}") from exc
            self._client = httpx.Client(
                headers=self._headers(), timeout=self.timeout, **self._tls_kwargs())
        return self._client

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def fetch_devices(self) -> dict:
        client = self._http()
        try:
            resp = client.get(f"{self.base}/edge/devices",
                              params={"org_id": self.org_id, "node_id": self.node_id})
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise CentralClientError(str(exc)) from exc

    def _encode(self, env: dict) -> tuple[bytes, dict]:
        """Serialize the envelope, gzipping it once it is worth the CPU.

        Returns (body, per-request headers). `Content-Type: application/json`
        lives on the httpx client and is NOT repeated here — httpx merges
        per-request headers over the client's, so the type survives and, more
        importantly, an UNCOMPRESSED request carries no `Content-Encoding` at
        all rather than a stale one left over from the previous report.

        The compressor can never end a report cycle: central accepts both
        shapes, so falling back to the plain bytes is a complete answer and
        not a degraded one.
        """
        raw = json.dumps(env, separators=(",", ":")).encode("utf-8")
        if self.gzip_min <= 0 or len(raw) < self.gzip_min:
            return raw, {}
        try:
            # mtime=0: the header timestamp is noise that would leak the
            # probe's clock and make an otherwise identical body differ.
            return gzip.compress(raw, _GZIP_LEVEL, mtime=0), {"Content-Encoding": "gzip"}
        except Exception:
            log.warning("gzip failed; shipping %d bytes uncompressed", len(raw),
                        exc_info=True)
            return raw, {}

    def _post(self, path: str, env: dict, *, timeout: float | None = None) -> dict:
        client = self._http()
        try:
            body, headers = self._encode(env)
            resp = client.post(f"{self.base}{path}", content=body, headers=headers,
                               **({} if timeout is None else {"timeout": timeout}))
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except Exception as exc:
            raise CentralClientError(str(exc)) from exc

    def report(self, pings: dict, ts: str, *, mode: str = "full",
              ports: dict | None = None, optics: dict | None = None,
              health: dict | None = None,
              snmp_status: dict | None = None) -> dict:
        env = {"v": WIRE_V, "org_id": self.org_id, "node_id": self.node_id,
              "ts": ts, "mode": mode, "pings": pings}
        if ports:
            env["ports"] = ports
        if optics:
            env["optics"] = optics
        if health:
            env["health"] = health
        if snmp_status:
            env["snmp_status"] = snmp_status
        return self._post("/report", env)

    def heartbeat(self, body: dict) -> dict:
        env = {"v": WIRE_V, "org_id": self.org_id, "node_id": self.node_id, "body": body}
        return self._post("/heartbeat", env)

    def walk_result(self, walk_id: int, *, varbinds: list | None = None,
                    error: str | None = None, truncated: bool = False) -> dict:
        env = {"v": WIRE_V, "org_id": self.org_id, "node_id": self.node_id,
              "walk_id": walk_id}
        if error:
            env["error"] = error
        else:
            env["varbinds"] = varbinds or []
            env["truncated"] = bool(truncated)
        return self._post("/edge/snmp-walk", env)

    def proxy_next(self, hold_s: float) -> dict | None:
        client = self._http()
        try:
            resp = client.get(f"{self.base}/edge/proxy/next",
                              params={"org_id": self.org_id, "node_id": self.node_id},
                              timeout=hold_s + 10.0)
            resp.raise_for_status()
            return (resp.json() or {}).get("request")
        except Exception as exc:
            raise CentralClientError(str(exc)) from exc

    def proxy_reply(self, sid: str, req_id: int, status: int, headers: dict,
                    body_b64: str, *, error: str | None = None) -> dict:
        env = {"v": WIRE_V, "org_id": self.org_id, "node_id": self.node_id,
               "sid": sid, "req_id": req_id, "status": status,
               "headers": headers, "body_b64": body_b64}
        if error:
            env["error"] = error
        return self._post("/edge/proxy/reply", env)

    def liveping_exchange(self, token: int, samples: dict, refusals: list,
                          hold_s: float) -> dict:
        """The whole live-ping wire: echoes up, the current session set down.

        Deliberately NOT a mode on `report()`. `/report` is the FSM's ingest
        path, and a live stream that shared it would sit one field away from
        the state machine. This is its own endpoint carrying its own shape —
        `{sid: [[seq, rtt|null], …]}`, never `PingResult` — so there is no
        conversion anywhere that could feed a watched device's packets to the
        engine.
        """
        # `hold_s` MUST ride the envelope, not just the HTTP timeout. Central
        # parks for what it is ASKED for (clamped down to its own ceiling), so
        # a short hold is the edge saying "I have packets to ship, answer me
        # now". Sending only the timeout is how this deadlocked once: the edge
        # asked for 2 s, waited 12 s, and central parked the full 20 s — so
        # EVERY exchange timed out, the stream arrived in 14-second bursts
        # instead of one line per packet, and the panel's own silence alarm
        # fired on a perfectly healthy channel.
        env = {"v": WIRE_V, "org_id": self.org_id, "node_id": self.node_id,
               "token": int(token), "samples": samples or {},
               "hold_s": float(hold_s)}
        if refusals:
            env["refusals"] = refusals
        return self._post("/edge/liveping", env, timeout=hold_s + 10.0)

def build_central_client(cfg: Config = CONFIG) -> CentralBrainClient:
    return HttpCentralClient(cfg)
