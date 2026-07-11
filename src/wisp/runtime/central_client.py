from __future__ import annotations

from typing import Protocol

from wisp.config import CONFIG, Config

WIRE_V = 1

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
                    error: str | None = None) -> dict: ...
    def close(self) -> None: ...

class HttpCentralClient:

    def __init__(self, cfg: Config = CONFIG) -> None:
        self.base = cfg.central_url.rstrip("/")
        self.token = cfg.central_token
        self.org_id = cfg.org_id
        self.node_id = cfg.node_id
        self.timeout = cfg.ship_timeout_s
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

    def _post(self, path: str, env: dict) -> dict:
        client = self._http()
        try:
            resp = client.post(f"{self.base}{path}", json=env)
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
                    error: str | None = None) -> dict:
        env = {"v": WIRE_V, "org_id": self.org_id, "node_id": self.node_id,
              "walk_id": walk_id}
        if error:
            env["error"] = error
        else:
            env["varbinds"] = varbinds or []
        return self._post("/edge/snmp-walk", env)

def build_central_client(cfg: Config = CONFIG) -> CentralBrainClient:
    return HttpCentralClient(cfg)
