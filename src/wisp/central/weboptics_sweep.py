from __future__ import annotations

import logging
import threading
import time as _time

from wisp.central import nvr, webmacs, weboptics
from wisp.central.api.proxy import preflight_endpoint
from wisp.central.nvr_profiles import ProfileSet as NvrProfileSet
from wisp.central.secretbox import DecryptError
from wisp.central.webmacs_profiles import ProfileSet as MacProfileSet
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
    def __init__(self, store, proxy, secretbox, cfg: Config = CONFIG,
                 notifier=None) -> None:
        self.store = store
        self.proxy = proxy
        self.secretbox = secretbox
        self.cfg = cfg
        self.notifier = notifier
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

    def mac_profiles(self) -> MacProfileSet:

        try:
            rows = self.store.list_web_mac_profiles(None)
        except Exception:
            log.exception("web optics: could not load address-table profiles")
            rows = []
        return MacProfileSet.build(rows)

    def _targets(self, profiles: ProfileSet,
                 mac_profiles: MacProfileSet) -> list[dict]:


        merged: dict[int, dict] = {}
        try:
            for dev in self.store.web_optics_targets(profiles.names()):
                merged[int(dev["id"])] = dev
        except Exception:
            log.exception("web optics: could not list targets")
        try:
            for dev in self.store.user_mac_targets(mac_profiles.names()):
                merged.setdefault(int(dev["id"]), dev)
        except Exception:
            log.exception("web optics: could not list address-table targets")
        return [merged[k] for k in sorted(merged)]

    def sweep_once(self) -> list[tuple[int, int, str | None]]:
        out: list[tuple[int, int, str | None]] = []
        self.reap_proxy_sessions()
        profiles = self.profiles()
        mac_profiles = self.mac_profiles()
        for dev in self._targets(profiles, mac_profiles):
            try:
                res = self.scrape_device(dev, profiles, mac_profiles)
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

    def _record_macs(self, org_id: str, device_id: int, vendor: str, state: str,
                     detail: str | None, rows: int = 0,
                     declared: int | None = None) -> None:

        try:
            self.store.set_web_mac_status(
                org_id, device_id, vendor, state, detail, rows, declared)
        except Exception:
            log.exception("web macs: could not record status for device=%d",
                          device_id)

    def _scrape_macs(self, http, org_id: str, device_id: int, name: str,
                     vendor: str, profile, username: str, password: str) -> None:


        started = _time.monotonic()
        try:
            table, err = webmacs.scrape_macs(http, username, password, profile)
        except Exception:
            log.exception("web macs: address-table read failed for %s", name)
            self._record_macs(org_id, device_id, vendor, "error",
                              "the address table could not be read")
            return
        took = _time.monotonic() - started
        rows = table.rows if table is not None else []
        declared = table.declared_total if table is not None else None

        if rows:
            try:
                kept = self.store.upsert_user_macs(
                    org_id, device_id, rows, _now_iso())
            except Exception:
                log.exception("web macs: could not store addresses for %s", name)
                self._record_macs(org_id, device_id, vendor, "error",
                                  "addresses were read but could not be stored")
                return
        else:
            kept = 0

        slots = len({r["onu_key"] for r in rows})
        if err:
            log.warning("web macs: %s — %s (%d address(es) kept, %.1fs)",
                        name, err, kept, took)
            self._record_macs(org_id, device_id, vendor,
                              "partial" if rows else _fault_state(err),
                              err, kept, declared)
        else:
            log.info("web macs: %s — %d address(es) on %d ONU(s) in %.1fs",
                     name, kept, slots, took)
            self._record_macs(org_id, device_id, vendor, "ok", None, kept, declared)

    def scrape_device(self, dev: dict,
                      profiles: ProfileSet | None = None,
                      mac_profiles: MacProfileSet | None = None
                      ) -> tuple[int, int, str | None] | None:
        device_id = int(dev["id"])
        org_id = str(dev["org_id"])
        node_id = str(dev.get("assigned_node_id") or "")
        name = dev.get("name") or f"#{device_id}"
        vendor = str(dev.get("vendor") or "dbc").strip().lower()

        profiles = profiles if profiles is not None else self.profiles()
        mac_profiles = mac_profiles if mac_profiles is not None else self.mac_profiles()
        profile = profiles.resolve(org_id, vendor)
        mac_profile = mac_profiles.resolve(org_id, vendor)
        if profile is None:
            self._record(org_id, device_id, vendor, "no_profile",
                         f"no web-UI optics profile for vendor {vendor!r}")
        if mac_profile is None:
            self._record_macs(org_id, device_id, vendor, "no_profile",
                              f"no address-table profile for vendor {vendor!r}")
        if profile is None and mac_profile is None:
            return None

        def _gate(state: str, detail: str) -> None:

            if profile is not None:
                self._record(org_id, device_id, vendor, state, detail)
            if mac_profile is not None:
                self._record_macs(org_id, device_id, vendor, state, detail)

        target = endpoint(dev)
        if target is None:
            log.debug("web optics: %s has no usable web address — skipped", name)
            _gate("no_credentials", "this device has no usable web address")
            return None
        ip, port, scheme = target

        if not self.proxy.polled_recently(
                org_id, node_id, self.cfg.proxy_poll_hold_s + 5.0):
            log.debug("web optics: tunnel dormant for %s/%s — skipped %s",
                      org_id, node_id, name)
            _gate("skipped",
                  f"the probe {node_id} is not holding its web tunnel open")
            return None
        if self.proxy.active_sessions_for(
                org_id, node_id, idle_s=max(30, int(self.cfg.web_optics_browse_idle_s))):
            log.info("web optics: %s is being browsed — skipping this pass", name)
            _gate("skipped",
                  "someone is browsing a device on this probe. The OLT "
                  "holds one web session, so we wait for them to finish.")
            return None

        creds = self._credentials(org_id, device_id)
        if creds is None:
            _gate("no_credentials", "no usable stored web-UI login for this OLT")
            return None
        username, password = creds

        lock = self._lock_for(device_id)
        if not lock.acquire(blocking=False):
            log.info("web optics: a scrape of %s is still running — skipped", name)
            _gate("skipped", "the previous scrape of this OLT is still running")
            return None
        rows: list[dict] = []
        err = None
        took = 0.0
        pons: tuple[int, ...] = ()
        try:
            ip, port, scheme, err = preflight_endpoint(
                self.proxy, self.cfg, org_id, node_id, device_id, dev,
                ip, port, scheme)
            if err:
                log.warning("web optics: %s — %s", name, err)
                _gate("unreachable", err)
                return device_id, 0, err
            http = weboptics.TunnelHttp(
                hub=self.proxy, org_id=org_id, node_id=node_id,
                device_id=device_id, ip=ip, port=port, scheme=scheme)
            if profile is not None:
                pons = _pons_for(dev, profile)
                started = _time.monotonic()
                rows, err = weboptics.scrape_optics(
                    http, username, password, profile, pons=pons,
                    deadline=started
                    + max(30, int(self.cfg.web_optics_device_budget_s)))
                took = _time.monotonic() - started
            if mac_profile is not None:
                self._scrape_macs(http, org_id, device_id, name, vendor,
                                  mac_profile, username, password)
        finally:
            lock.release()

        if profile is None:
            return device_id, 0, None

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

    def nvr_profile_set(self) -> NvrProfileSet:

        try:
            rows = self.store.list_nvr_profiles(None)
        except Exception:
            log.exception("nvr: could not load vendor profiles")
            rows = []
        return NvrProfileSet.build(rows)

    def nvr_target(self, org_id: str, device_id: int) -> dict | None:

        try:
            rows = self.store.nvr_targets(
                self.nvr_profile_set().names(), device_id=device_id)
        except Exception:
            log.exception("nvr: could not resolve target device=%d", device_id)
            return None
        return next((r for r in rows if str(r.get("org_id")) == org_id), None)

    def _record_nvr(self, org_id: str, device_id: int, vendor: str, state: str,
                    detail: str | None, channels: int = 0) -> None:

        try:
            self.store.set_nvr_status(
                org_id, device_id, vendor, state, detail, channels)
        except Exception:
            log.exception("nvr: could not record status for device=%d", device_id)

    def _page_cameras(self, org_id: str, device_id: int, nvr_name: str,
                      kind: str, rows: list[dict], ts: str) -> None:

        if self.notifier is None or not rows:
            return
        from wisp.central.notify_policy import AlertRouter
        n = len(rows)
        detail = nvr.batch_detail(rows)
        if kind == "CAMERA_DOWN":
            what = "1 camera dark" if n == 1 else f"{n} cameras dark"
            title = f"📷 {nvr_name}: {what}"
        else:
            what = "1 camera back" if n == 1 else f"{n} cameras back"
            title = f"✅ {nvr_name}: {what}"
        try:
            router = AlertRouter(self.store, org_id, self.notifier, self.cfg)
            router.emit(kind, title=title, body=detail, priority=3, ts=ts,
                        device_id=device_id, cooldown_min=0)
        except Exception:
            log.exception("nvr: camera page failed for %s", nvr_name)

    def sweep_nvrs(self) -> list[tuple[int, int, str | None]]:
        out: list[tuple[int, int, str | None]] = []
        profiles = self.nvr_profile_set()
        try:
            targets = self.store.nvr_targets(profiles.names())
        except Exception:
            log.exception("nvr: could not list targets")
            return out
        for dev in targets:
            try:
                res = self.scrape_nvr(dev, profiles)
            except Exception:
                log.exception("nvr: sweep failed for device=%s", dev.get("id"))
                continue
            if res is not None:
                out.append(res)
        return out

    def scrape_nvr(self, dev: dict, profiles: NvrProfileSet | None = None
                   ) -> tuple[int, int, str | None] | None:
        device_id = int(dev["id"])
        org_id = str(dev["org_id"])
        node_id = str(dev.get("assigned_node_id") or "")
        name = dev.get("name") or f"#{device_id}"
        vendor = str(dev.get("vendor") or "").strip().lower()

        profiles = profiles if profiles is not None else self.nvr_profile_set()
        profile = profiles.resolve(org_id, vendor)
        if profile is None:
            self._record_nvr(org_id, device_id, vendor, "no_profile",
                             f"no NVR recipe for brand {vendor!r}")
            return None

        target = endpoint(dev)
        if target is None:
            self._record_nvr(org_id, device_id, vendor, "no_credentials",
                             "this NVR has no usable web address")
            return None
        ip, port, scheme = target

        if not self.proxy.polled_recently(
                org_id, node_id, self.cfg.proxy_poll_hold_s + 5.0):
            self._record_nvr(org_id, device_id, vendor, "skipped",
                             f"the probe {node_id} is not holding its web "
                             "tunnel open")
            return None
        if self.proxy.active_sessions_for(
                org_id, node_id,
                idle_s=max(30, int(self.cfg.web_optics_browse_idle_s))):
            self._record_nvr(org_id, device_id, vendor, "skipped",
                             "someone is browsing a device on this probe, so "
                             "this pass waits for them to finish")
            return None

        creds = self._credentials(org_id, device_id)
        if creds is None:
            self._record_nvr(org_id, device_id, vendor, "no_credentials",
                             "no usable stored web login for this NVR")
            return None
        username, password = creds

        lock = self._lock_for(device_id)
        if not lock.acquire(blocking=False):
            self._record_nvr(org_id, device_id, vendor, "skipped",
                             "the previous read of this NVR is still running")
            return None
        try:
            ip, port, scheme, err = preflight_endpoint(
                self.proxy, self.cfg, org_id, node_id, device_id, dev,
                ip, port, scheme)
            if err:
                log.warning("nvr: %s — %s", name, err)
                self._record_nvr(org_id, device_id, vendor, "unreachable", err)
                return device_id, 0, err
            http = weboptics.TunnelHttp(
                hub=self.proxy, org_id=org_id, node_id=node_id,
                device_id=device_id, ip=ip, port=port, scheme=scheme)
            started = _time.monotonic()
            rows, err = nvr.read_channels(http, username, password, profile)
            took = _time.monotonic() - started
        finally:
            lock.release()

        ts = _now_iso()
        if rows is None:
            log.warning("nvr: %s — %s", name, err)
            self._record_nvr(org_id, device_id, vendor, _fault_state(err or ""),
                             err)
            return device_id, 0, err

        if rows:
            try:
                res = self.store.upsert_nvr_channels(
                    org_id, device_id, rows, ts, prune=True)
            except Exception:
                log.exception("nvr: could not store channels for %s", name)
                self._record_nvr(org_id, device_id, vendor, "error",
                                 "channels were read but could not be stored")
                return device_id, 0, "store failed"
            trans = nvr.transitions(res.get("prior") or {}, rows,
                                    res.get("unwatched") or set())
            self._page_cameras(org_id, device_id, name, "CAMERA_DOWN",
                               trans["dark"], ts)
            self._page_cameras(org_id, device_id, name, "CAMERA_RESTORED",
                               trans["restored"], ts)

        if err:
            log.warning("nvr: %s — %s (%d channel(s) kept, %.1fs)",
                        name, err, len(rows), took)
            self._record_nvr(org_id, device_id, vendor,
                             "partial" if rows else _fault_state(err),
                             err, len(rows))
        else:
            log.info("nvr: %s — %d channel(s) in %.1fs", name, len(rows), took)
            self._record_nvr(org_id, device_id, vendor, "ok", None, len(rows))
        return device_id, len(rows), err

    def snapshot(self, org_id: str, device_id: int, channel_no: int
                 ) -> tuple[bytes | None, str | None, int]:

        dev = self.nvr_target(org_id, device_id)
        if dev is None:
            return None, ("this NVR isn't set up for camera reads. See the "
                          "Cameras tab for what's missing."), 400
        profile = self.nvr_profile_set().resolve(
            org_id, str(dev.get("vendor") or ""))
        if profile is None or not profile.snapshot_path:
            return None, "this NVR's recipe has no snapshot page", 400
        node_id = str(dev.get("assigned_node_id") or "")
        target = endpoint(dev)
        if target is None:
            return None, "this NVR has no usable web address", 400
        ip, port, scheme = target
        if not self.proxy.polled_recently(
                org_id, node_id, self.cfg.proxy_poll_hold_s + 5.0):
            return None, (f"the probe {node_id} is not holding its web "
                          "tunnel open"), 503
        creds = self._credentials(org_id, device_id)
        if creds is None:
            return None, "no usable stored web login for this NVR", 400
        username, password = creds
        lock = self._lock_for(device_id)
        if not lock.acquire(timeout=8.0):
            return None, "a read of this NVR is already running", 409
        try:
            ip, port, scheme, err = preflight_endpoint(
                self.proxy, self.cfg, org_id, node_id, device_id, dev,
                ip, port, scheme)
            if err:
                return None, err, 502
            http = weboptics.TunnelHttp(
                hub=self.proxy, org_id=org_id, node_id=node_id,
                device_id=device_id, ip=ip, port=port, scheme=scheme,
                timeout_s=nvr.SNAPSHOT_TIMEOUT_S)
            frame, err = nvr.fetch_snapshot(
                http, username, password, channel_no, profile)
        finally:
            lock.release()
        if frame is None:
            return None, err, 502
        return frame, None, 200

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
                  secretbox=None, notifier=None) -> WebOpticsSweeper | None:

    if not (cfg.web_optics_enabled and cfg.proxy_enabled):
        return None
    if store is None or proxy is None or secretbox is None:
        log.warning("web optics sweeper unavailable — store/proxy/secretbox missing")
        return None
    return WebOpticsSweeper(store, proxy, secretbox, cfg, notifier)


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


def start_nvr_thread(cfg: Config = CONFIG, store=None, proxy=None,
                     secretbox=None, notifier=None,
                     sweeper: WebOpticsSweeper | None = None
                     ) -> threading.Thread | None:

    if int(cfg.nvr_interval_s) <= 0:
        return None
    sweeper = sweeper or build_sweeper(cfg, store, proxy, secretbox, notifier)
    if sweeper is None:
        return None
    interval = max(60, int(cfg.nvr_interval_s))

    def _loop() -> None:
        log.info("nvr sweeper started (every %ss)", interval)
        _time.sleep(min(45.0, interval))
        while True:
            try:
                sweeper.sweep_nvrs()
            except Exception:
                log.exception("nvr sweep failed")
            _time.sleep(interval)

    t = threading.Thread(target=_loop, name="wisp-central-nvr", daemon=True)
    t.start()
    return t
