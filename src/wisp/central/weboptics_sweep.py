"""The web-UI optics sweeper: central's slow clock over the OPM Diag scrape.

The paging shell's counterpart for `weboptics.py` — that module knows how to
talk to one OLT, this one decides which OLTs, how often, and what to do when it
goes wrong. Nothing here can page anybody: it is handed no notifier at all,
which is a stronger guarantee than remembering not to call one. A scrape is an
enrichment; the ICMP path is what wakes people up.

THE CLOCK IS SLOW ON PURPOSE (default 900s). Rx power drifts over days — a dirty
connector or a bending fibre shows up across a week, not across a minute — so
there is nothing to win by asking often, and plenty to lose: opening OPM Diag
makes the OLT query every ONU on a PON live over EPON-OAM, and these are the
same weak C-Data boxes whose ifTable walk had to be made adaptive because they
drop a big GETBULK. The scrape must never look like polling.

WHAT IT WILL NOT SCRAPE, and why each gate is load-bearing:

- a device this vendor's profile does not claim. The login form and page path
  here are one vendor's, so "probably C-Data" must never be enough to start
  POSTing credentials at an admin UI. What counts as claimed is the operator's
  explicit `gpon_vendor='dbc'` OR the edge's own sysObjectID match reported in
  `device_snmp_status` — see `store_snmp.web_optics_targets`, which is also
  where the "the edge never tells central" belief is corrected. It stops there:
  central still infers nothing itself.
- a device with no SNMP roster. Readings merge ONTO walked slots and can never
  create one, so there is nothing a scrape could surface.
- a node whose tunnel is not currently long-polling. Every request would just
  burn its timeout.
- a node with a proxy session someone is ACTIVELY using. This firmware keeps no
  cookie and holds ONE session slot, so a sweep landing mid-browse would
  silently log the operator out of the box they are working on — and them, us.
  A human at a keyboard wins; we come back in 15 minutes. "Actively" is
  load-bearing and used not to be: a session outlives its browser tab by
  proxy_session_ttl_s (nothing tells central a tab was closed), and the gate is
  per-NODE, so one tab left open on one device suppressed the optical read of
  every OLT behind that probe — indefinitely, if the device UI auto-refreshes.
  The window is `cfg.web_optics_browse_idle_s`, and abandoned sessions are
  reaped at the top of each pass so they stop being advertised as live.

The sweep is also not the only way in: `scrape_device` is what the dashboard's
manual refresh calls (`api/devices.py:rx_refresh`) for one OLT, on the operator's
click, when someone is standing in front of the plant and wants a reading NOW
rather than at the next quarter-hour. The gates above apply unchanged — a manual
read that logs an operator out of the box they are debugging is exactly the
harm they exist to prevent, and each of them records a status the panel explains.
"""

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

# Standard web ports, used only to pick a default scheme when the operator
# declared a port but not a scheme.
_HTTPS_PORTS = (443, 8443)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def endpoint(dev: dict) -> tuple[str, int, str] | None:
    """(ip, port, scheme) for a device's web UI, or None if it has no address.

    Same precedence the proxy's session-open uses: a per-device override
    (web_ip/web_port/web_scheme, any one set) is an owner-declared endpoint and
    wins; otherwise the probe IP on plain HTTP, which is what this vendor's
    OLTs serve. Deliberately NOT clamped to proxy_mgmt_ports — that list bounds
    what a BROWSER may be pointed at, and this is not a browsing session.
    """
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
    """The PON ports to ask this OLT about, from its SNMP roster.

    `pon_ports` is the GROUP_CONCAT of the roster's distinct labels; anything
    unparseable is dropped by `pon_indices`, and a roster that yields nothing
    usable falls back to the vendor profile's declared list. The fallback is the
    OLD behaviour and it is deliberately still here: a box whose walk labels its
    ports in some shape we have not seen should scrape its first few PONs, not
    none of them.
    """
    raw = dev.get("pon_ports")
    labels = str(raw).split(",") if raw else []
    fallback = profile.default_pons if profile is not None else weboptics.DEFAULT_PONS
    return weboptics.pon_indices(labels) or fallback


def _fault_state(err: str) -> str:
    """Sort a scrape error into the closed status vocabulary.

    Coarse ON PURPOSE — three buckets, because they are the three different
    things an operator would DO about it: fix the password, fix the address (or
    wait for the box), or read the detail. A finer taxonomy derived from message
    text would drift the first time a message is reworded, and the message
    itself is carried verbatim alongside anyway.
    """
    low = (err or "").lower()
    if "login rejected" in low or "login failed" in low or "password" in low:
        return "login"
    if ("no login page" in low or "timeout" in low or "tunnel" in low
            or "could not open" in low or "404" in low):
        return "unreachable"
    return "error"


class WebOpticsSweeper:
    """Scrapes each eligible OLT's optics page onto its own slow clock."""

    def __init__(self, store, proxy, secretbox, cfg: Config = CONFIG) -> None:
        self.store = store
        self.proxy = proxy
        self.secretbox = secretbox
        self.cfg = cfg
        # One lock per OLT. The sweep is sequential, so this is not what keeps
        # the fleet gentle — that is the single thread. It is here because this
        # box holds ONE web session: two overlapping scrapes of the same OLT
        # would knock each other out, so any second caller (a future "scrape
        # now" button, a test, a slow sweep meeting the next tick) must find the
        # device busy and leave rather than queue behind it.
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, device_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(device_id)
            if lock is None:
                lock = self._locks[device_id] = threading.Lock()
            return lock

    def busy(self, device_id: int) -> bool:
        """Is a scrape of this OLT running right now? Read by the manual-refresh
        route so a second click is refused outright instead of racing the lock
        and overwriting a good status with 'still running'."""
        lock = self._lock_for(device_id)
        if lock.acquire(blocking=False):
            lock.release()
            return False
        return True

    # -- one pass ------------------------------------------------------------

    def reap_proxy_sessions(self) -> int:
        """Retire web-UI sessions that have timed out, hub AND database.

        Nothing else does it. A session is only removed from the hub when
        something happens to look it up, and the one thing that would — the
        browser driving it — is precisely what has stopped. So a closed tab left
        a session sitting in memory, still counted by the browse gate below,
        still advertised to the dashboard as a live pulsing globe, until the
        next session on that probe happened to replace it. Doing it here, at the
        top of the sweep, keeps it on a clock that already exists rather than
        buying a thread for a dictionary scan.
        """
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
        """The vendor recipes in force for this sweep.

        Built ONCE per pass rather than per device: validation is cheap but not
        free, and every OLT in a pass is judged against the same set. A stored
        row that no longer validates is skipped inside ProfileSet.build, so a bad
        row can never take the sweep down with it.
        """
        try:
            rows = self.store.list_web_optics_profiles(None)
        except Exception:
            log.exception("web optics: could not load vendor profiles")
            rows = []
        return ProfileSet.build(rows)

    def sweep_once(self) -> list[tuple[int, int, str | None]]:
        """Scrape every eligible OLT once, in series. Returns one
        (device_id, rows_stored, error) per device actually attempted."""
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
                # Belt and braces: scrape_device already catches, but one bad
                # OLT must never end the sweep for the rest of the fleet.
                log.exception("web optics: sweep failed for device=%s", dev.get("id"))
                continue
            if res is not None:
                out.append(res)
        return out

    def target(self, org_id: str, device_id: int) -> dict | None:
        """This OLT as a scrape target, or None if the sweep wouldn't touch it.

        THE answer to "can this box be read", for the dashboard as well as for
        the sweep — the manual-refresh route both gates on it and draws its
        button off it. One source deliberately: a second copy of the rule
        (assembled in the API from the facts `rx_status` already reports, say)
        drifts into a button that promises a reading nothing will take, or a
        click that reaches an OLT the sweep refuses.
        """
        try:
            rows = self.store.web_optics_targets(
                self.profiles().names(), device_id=device_id)
        except Exception:
            log.exception("web optics: could not resolve target device=%d", device_id)
            return None
        return next((r for r in rows if str(r.get("org_id")) == org_id), None)

    def scrape_one(self, org_id: str, device_id: int
                   ) -> tuple[int, int, str | None] | None:
        """Scrape ONE OLT now, on an operator's click (api/devices.py).

        Eligibility is re-resolved rather than trusted from the caller, so a
        manual read can never reach a box the sweep would have refused. None =
        not a target, and NOTHING is recorded for that: a status row is the
        report of an attempt, and overwriting a real verdict with "you can't
        read this" would erase the last thing that actually happened. The route
        refuses such a device up front instead.
        """
        dev = self.target(org_id, device_id)
        return None if dev is None else self.scrape_device(dev)

    def _record(self, org_id: str, device_id: int, vendor: str, state: str,
                detail: str | None, rows: int = 0) -> None:
        """Persist the outcome so the dashboard can explain a blank dBm column.

        Best-effort by design and wrapped: a status write failing must not turn
        a successful scrape into an exception, and it must certainly not stop
        the sweep reaching the next OLT.
        """
        try:
            self.store.set_web_optics_status(
                org_id, device_id, vendor, state, detail, rows)
        except Exception:
            log.exception("web optics: could not record status for device=%d",
                          device_id)

    def scrape_device(self, dev: dict,
                      profiles: ProfileSet | None = None
                      ) -> tuple[int, int, str | None] | None:
        """Scrape one OLT. None = skipped before any request was made."""
        device_id = int(dev["id"])
        org_id = str(dev["org_id"])
        node_id = str(dev.get("assigned_node_id") or "")
        name = dev.get("name") or f"#{device_id}"
        vendor = str(dev.get("vendor") or "dbc").strip().lower()

        profiles = profiles if profiles is not None else self.profiles()
        profile = profiles.resolve(org_id, vendor)
        if profile is None:
            # Reachable when a target list outlives the profile that produced it
            # (a row disabled mid-sweep). Recorded rather than dropped: "no
            # recipe for this vendor" is exactly the thing the Optical tab needs
            # to be able to say out loud.
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

        # The two SKIP states below are transient and expected — a dormant
        # tunnel or a human at the keyboard — so they are recorded with their
        # own state rather than as failures, and they never touch last_ok_at.
        if not self.proxy.polled_recently(
                org_id, node_id, self.cfg.proxy_poll_hold_s + 5.0):
            log.debug("web optics: tunnel dormant for %s/%s — skipped %s",
                      org_id, node_id, name)
            self._record(org_id, device_id, vendor, "skipped",
                         f"the probe {node_id} is not holding its web tunnel open")
            return None
        # In USE, not merely open — see the module docstring. A session whose
        # tab is gone still exists for the rest of its TTL and this gate is
        # per-node, so the old membership test let one forgotten tab mute every
        # OLT on the probe.
        if self.proxy.active_sessions_for(
                org_id, node_id, idle_s=max(30, int(self.cfg.web_optics_browse_idle_s))):
            log.info("web optics: %s is being browsed — skipping this pass", name)
            self._record(org_id, device_id, vendor, "skipped",
                         "someone is browsing a device on this probe — the OLT "
                         "holds one web session, so we wait for them to finish")
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
            # Ask the edge which endpoint actually answers before sending
            # anything — the SAME preflight the browser's session-open runs. A
            # device with no declared override only gives us a port⇒scheme
            # guess, and this vendor's OLTs are not all on :80; without this the
            # sweep POSTs an admin login at a guessed endpoint and reports
            # "login failed" for a box that is perfectly reachable one port
            # over. Inconclusive keeps the heuristic, never fails the scrape.
            #
            # It goes HERE, after the local gates and inside the lock, because
            # it is the first thing that costs the tunnel a round trip: probing
            # for a device we have no password for, or that is already being
            # scraped, is traffic aimed at a weak OLT for no possible result.
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
            # Partial results are stored: PONs that answered carry real
            # readings, and the merge is per-ONU, so a missed PON simply keeps
            # whatever the SNMP walk last said about it.
            try:
                self.store.upsert_web_optics(org_id, device_id, rows, _now_iso())
            except Exception:
                log.exception("web optics: could not store readings for %s", name)
                self._record(org_id, device_id, vendor, "error",
                             "readings were read but could not be stored")
                return device_id, 0, "store failed"
        # The endpoint is part of every outcome line: without it a failure can't
        # be told apart from the same failure one port over, which is exactly
        # how the first live run burned a restart.
        where = f"{scheme}://{ip}:{port} PON{','.join(str(p) for p in pons)}"
        if err:
            # A failed scrape still pages nobody. The ONUs keep their previous
            # readings until those age out of the merge window, and the optical
            # badge goes on being driven by SNMP exactly as it was before this
            # subsystem existed. It is no longer a log line ONLY, though: the
            # verdict is recorded so the Optical tab can say why the dBm column
            # is empty instead of leaving it indistinguishable from a vendor
            # that has no Rx at all.
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
        """The device's stored web-UI login, or None.

        Same shape as the proxy's autofill resolution, and the same verdict on a
        password that will not decrypt: skip the device, log it, carry on. A
        rotated secret key must not turn a whole sweep into an exception.
        """
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
    """The sweeper for this process, or None when it can't do anything.

    Separate from starting the thread because the sweeper is no longer only a
    background clock: the dashboard's manual refresh drives the same object for
    one device, and it MUST be the same object — the per-OLT lock that stops two
    scrapes colliding on a box with one session slot lives on the instance.
    """
    if not (cfg.web_optics_enabled and cfg.proxy_enabled):
        return None
    if store is None or proxy is None or secretbox is None:
        log.warning("web optics sweeper unavailable — store/proxy/secretbox missing")
        return None
    return WebOpticsSweeper(store, proxy, secretbox, cfg)


def start_web_optics_thread(cfg: Config = CONFIG, store=None, proxy=None,
                            secretbox=None, sweeper: WebOpticsSweeper | None = None
                            ) -> threading.Thread | None:
    """Start the sweeper's background thread, or None when it is switched off.

    Returns None rather than starting an idle thread when the feature is
    disabled or the pieces it needs are missing, so a deployment without the
    proxy tunnel costs nothing. Pass the process's `sweeper` so the sweep and
    the dashboard's manual refresh share one instance (and one per-OLT lock).
    """
    sweeper = sweeper or build_sweeper(cfg, store, proxy, secretbox)
    if sweeper is None:
        return None
    interval = max(60, int(cfg.web_optics_interval_s))

    def _loop() -> None:
        log.info("web optics sweeper started (every %ss)", interval)
        # Let the fleet's edges get their tunnels long-polling before the first
        # pass; a sweep at t=0 would skip every device as dormant.
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
