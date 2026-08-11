from __future__ import annotations

import logging
import threading
import time as _time

from wisp.central import weboptics
from wisp.central.api.proxy import preflight_endpoint
from wisp.central.secretbox import DecryptError
from wisp.central.weboptics_profiles import ProfileSet
from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.central.weboptics")

_HTTPS_PORTS = (443, 8443)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def endpoint(dev: dict) -> tuple[str, int, str] | None:

    web_ip = (dev.get("web_ip") or "").strip()
    web_port = dev.get("web_port")
    web_scheme = (dev.get("web_scheme") or "").strip().lower()
    ip = web_ip or (dev.get("ip_address") or "").strip()
    if not ip:
        return None
    try:
        port = int(web_port) if web_port else (443 if web_scheme == "https" else 80)
    except (TypeError, ValueError):
        return None
    if not (1 <= port <= 65535):
        return None
    scheme = web_scheme or ("https" if port in _HTTPS_PORTS else "http")
    return ip, port, scheme


def _pons_for(dev: dict, profile=None) -> tuple[int, ...]:

    raw = dev.get("pon_ports")
    labels = str(raw).split(",") if raw else []
    fallback = profile.default_pons if profile is not None else weboptics.DEFAULT_PONS
    return weboptics.pon_indices(labels) or fallback


def _fault_state(err: str) -> str:

    low = (err or "").lower()
    if "login rejected" in low or "login failed" in low or "password" in low:
        return "login"
    if ("no login page" in low or "timeout" in low or "tunnel" in low
            or "could not open" in low or "404" in low):
        return "unreachable"
    return "error"


class WebOpticsSweeper:
    def __init__(self, store, proxy, secretbox, cfg: Config = CONFIG) -> None:
        self.store = store
        self.proxy = proxy
        self.secretbox = secretbox
        self.cfg = cfg
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, device_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(device_id)
            if lock is None:
                lock = self._locks[device_id] = threading.Lock()
            return lock

    def busy(self, device_id: int) -> bool:
        lock = self._lock_for(device_id)
        if lock.acquire(blocking=False):
            lock.release()
            return False
        return True


    def reap_proxy_sessions(self) -> int:

        try:
            gone = self.proxy.reap_expired()
        except Exception:
            log.exception("web optics: could not reap expired proxy sessions")
            return 0
        for sid in gone:
            try:
                self.store.close_proxy_session(sid, "expired")
            except Exception:
                log.exception("web optics: could not retire proxy session %s",
                              sid[:8])
        if gone:
            log.info("web optics: reaped %d timed-out web-UI session(s)", len(gone))
        return len(gone)

    def profiles(self) -> ProfileSet:

        try:
            rows = self.store.list_web_optics_profiles(None)
        except Exception:
            log.exception("web optics: could not load vendor profiles")
            rows = []
        return ProfileSet.build(rows)

    def sweep_once(self) -> list[tuple[int, int, str | None]]:
        out: list[tuple[int, int, str | None]] = []
        self.reap_proxy_sessions()
        profiles = self.profiles()
        try:
            targets = self.store.web_optics_targets(profiles.names())
        except Exception:
            log.exception("web optics: could not list targets")
            return out
        for dev in targets:
            try:
                res = self.scrape_device(dev, profiles)
            except Exception:
                log.exception("web optics: sweep failed for device=%s", dev.get("id"))
                continue
            if res is not None:
                out.append(res)
        return out

    def target(self, org_id: str, device_id: int) -> dict | None:

        try:
            rows = self.store.web_optics_targets(
                self.profiles().names(), device_id=device_id)
        except Exception:
            log.exception("web optics: could not resolve target device=%d", device_id)
            return None
        return next((r for r in rows if str(r.get("org_id")) == org_id), None)

    def scrape_one(self, org_id: str, device_id: int
                   ) -> tuple[int, int, str | None] | None:

        dev = self.target(org_id, device_id)
        return None if dev is None else self.scrape_device(dev)

    def _record(self, org_id: str, device_id: int, vendor: str, state: str,
                detail: str | None, rows: int = 0) -> None:

        try:
            self.store.set_web_optics_status(
                org_id, device_id, vendor, state, detail, rows)
        except Exception:
            log.exception("web optics: could not record status for device=%d",
                          device_id)

    def scrape_device(self, dev: dict,
                      profiles: ProfileSet | None = None
                      ) -> tuple[int, int, str | None] | None:
        device_id = int(dev["id"])
        org_id = str(dev["org_id"])
        node_id = str(dev.get("assigned_node_id") or "")
        name = dev.get("name") or f"#{device_id}"
        vendor = str(dev.get("vendor") or "dbc").strip().lower()

        profiles = profiles if profiles is not None else self.profiles()
        profile = profiles.resolve(org_id, vendor)
        if profile is None:
            self._record(org_id, device_id, vendor, "no_profile",
                         f"no web-UI optics profile for vendor {vendor!r}")
            return None

        target = endpoint(dev)
        if target is None:
            log.debug("web optics: %s has no usable web address — skipped", name)
            self._record(org_id, device_id, vendor, "no_credentials",
                         "this device has no usable web address")
            return None
        ip, port, scheme = target

        if not self.proxy.polled_recently(
                org_id, node_id, self.cfg.proxy_poll_hold_s + 5.0):
            log.debug("web optics: tunnel dormant for %s/%s — skipped %s",
                      org_id, node_id, name)
            self._record(org_id, device_id, vendor, "skipped",
                         f"the probe {node_id} is not holding its web tunnel open")
            return None
        if self.proxy.active_sessions_for(
                org_id, node_id, idle_s=max(30, int(self.cfg.web_optics_browse_idle_s))):
            log.info("web optics: %s is being browsed — skipping this pass", name)
            self._record(org_id, device_id, vendor, "skipped",
                         "someone is browsing a device on this probe. The OLT "
                         "holds one web session, so we wait for them to finish.")
            return None

        creds = self._credentials(org_id, device_id)
        if creds is None:
            self._record(org_id, device_id, vendor, "no_credentials",
                         "no usable stored web-UI login for this OLT")
            return None
        username, password = creds

        lock = self._lock_for(device_id)
        if not lock.acquire(blocking=False):
            log.info("web optics: a scrape of %s is still running — skipped", name)
            self._record(org_id, device_id, vendor, "skipped",
                         "the previous scrape of this OLT is still running")
            return None
        try:
            ip, port, scheme, err = preflight_endpoint(
                self.proxy, self.cfg, org_id, node_id, device_id, dev,
                ip, port, scheme)
            if err:
                log.warning("web optics: %s — %s", name, err)
                self._record(org_id, device_id, vendor, "unreachable", err)
                return device_id, 0, err
            http = weboptics.TunnelHttp(
                hub=self.proxy, org_id=org_id, node_id=node_id,
                device_id=device_id, ip=ip, port=port, scheme=scheme)
            pons = _pons_for(dev, profile)
            started = _time.monotonic()
            rows, err = weboptics.scrape_optics(
                http, username, password, profile, pons=pons,
                deadline=started + max(30, int(self.cfg.web_optics_device_budget_s)))
            took = _time.monotonic() - started
        finally:
            lock.release()

        if rows:
            try:
                self.store.upsert_web_optics(org_id, device_id, rows, _now_iso())
            except Exception:
                log.exception("web optics: could not store readings for %s", name)
                self._record(org_id, device_id, vendor, "error",
                             "readings were read but could not be stored")
                return device_id, 0, "store failed"
        where = f"{scheme}://{ip}:{port} PON{','.join(str(p) for p in pons)}"
        if err:
            log.warning("web optics: %s (%s) — %s (%d reading(s) kept, %.1fs)",
                        name, where, err, len(rows), took)
            self._record(org_id, device_id, vendor,
                         "partial" if rows else _fault_state(err), err, len(rows))
        else:
            log.info("web optics: %s (%s) — %d reading(s) in %.1fs",
                     name, where, len(rows), took)
            self._record(org_id, device_id, vendor, "ok", None, len(rows))
        return device_id, len(rows), err

    def _credentials(self, org_id: str, device_id: int) -> tuple[str, str] | None:

        try:
            row = self.store.get_device_webui_credentials(org_id, device_id)
        except Exception:
            log.exception("web optics: credential lookup failed for device=%d",
                          device_id)
            return None
        if not row:
            return None
        user = (row.get("username") or "").strip()
        enc = row.get("password_enc")
        if not user or not enc:
            return None
        try:
            return user, self.secretbox.decrypt(enc)
        except DecryptError:
            log.warning("web optics: stored login for device=%d will not decrypt "
                        "(key rotated?) — skipping this OLT", device_id)
            return None


def build_sweeper(cfg: Config = CONFIG, store=None, proxy=None,
                  secretbox=None) -> WebOpticsSweeper | None:

    if not (cfg.web_optics_enabled and cfg.proxy_enabled):
        return None
    if store is None or proxy is None or secretbox is None:
        log.warning("web optics sweeper unavailable — store/proxy/secretbox missing")
        return None
    return WebOpticsSweeper(store, proxy, secretbox, cfg)


def start_web_optics_thread(cfg: Config = CONFIG, store=None, proxy=None,
                            secretbox=None, sweeper: WebOpticsSweeper | None = None
                            ) -> threading.Thread | None:

    sweeper = sweeper or build_sweeper(cfg, store, proxy, secretbox)
    if sweeper is None:
        return None
    interval = max(60, int(cfg.web_optics_interval_s))

    def _loop() -> None:
        log.info("web optics sweeper started (every %ss)", interval)
        _time.sleep(min(60.0, interval))
        while True:
            try:
                sweeper.sweep_once()
            except Exception:
                log.exception("web optics sweep failed")
            _time.sleep(interval)

    t = threading.Thread(target=_loop, name="wisp-central-weboptics", daemon=True)
    t.start()
    return t
