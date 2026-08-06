from __future__ import annotations

import logging
import math
from datetime import timedelta

from wisp.central.notify_policy import AlertRouter
from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse

log = logging.getLogger(__name__)

SEV_OK = "ok"
SEV_WARN = "warn"
SEV_CRIT = "crit"

_REF_MAX_AGE_DAYS = 7

# --- Physical bounds on an optical reading --------------------------------
#
# These live HERE, not in weboptics, because `sync_device` below is the ONE
# place every reading passes through whatever transport carried it — an SNMP
# walk on the edge or a web scrape folded in by `_merge_web_optics`. Optics
# stay one path that never learns where a number came from, so the guard that
# decides "is this a measurement at all" has to sit on that path rather than on
# one of its feeders. weboptics imports these for its own richer check.
#
# An ONU cannot RECEIVE more power than the OLT emits (~+2..+5 dBm at the port,
# before any fibre or splitter loss), so a non-negative Rx is not a reading.
RX_MAX_DBM = 0.0
# The DDM log-floor sentinel. Real ONU sensitivity bottoms out around -28 dBm —
# nothing stays ranged and online at -40, so this is "unreadable", not "dying".
RX_FLOOR_DBM = -40.0
# An ONU TRANSMITS at roughly 0..+5 dBm, so a 0.0 Tx is an ordinary launch power
# and must NOT be rejected the way a 0.0 Rx is. The asymmetry is physics, not an
# oversight: only the high rail is unambiguous for a transmitter. Catching a
# dark ONU's railed Tx needs the supply-voltage column (see weboptics
# `_sane_optics`), which no SNMP profile maps yet.
TX_MAX_DBM = RX_MAX_DBM + 10.0


def sane_rx(rx: float | None) -> float | None:
    """None out an Rx that is a sensor RAIL rather than a measurement.

    Same rule `_sane_optics` has always applied to scraped readings, now on the
    shared path so an SNMP-fed vendor gets it too. It was missing there, and the
    Syrotech GPON fleet is what exposed it: that firmware reports `0.00` across
    the whole DDM block for a dark ONU, so 114 of badri_fiber's 378 ONUs were
    storing 0.0 dBm — read by `list_org_devices.onus_rx` (which counts
    `rx_dbm IS NOT NULL`) as a MEASURED drop. That makes "nothing is wrong" and
    "nothing is measured" render alike, which is the one thing the optics
    surfaces may not do.

    Deliberately keyed on PHYSICS, not on state. Blanking whenever an ONU is
    offline would also throw away the last good reading the panel legitimately
    shows for a dark drop, and it would miss the real fault this catches: one
    ONLINE ONU on Gpon_08 also reports 0.0, which is not a healthy drop but a
    dead sensor, and it grades `ok` — a false negative nobody goes looking for.
    """
    if rx is None:
        return None
    return None if (rx >= RX_MAX_DBM or rx <= RX_FLOOR_DBM) else rx


def sane_tx(tx: float | None) -> float | None:
    """None out a Tx above any real ONU launch power.

    Weak on purpose, and worth knowing HOW weak: the 0xFFFF rail reads +8.16 dBm
    and this does NOT catch it — `_sane_optics` catches that one on the supply
    VOLTAGE (6.55 V), then blanks the whole block. With no voltage column on the
    SNMP path there is nothing here to discriminate a railed Tx from a hot one,
    so this only rejects the physically impossible. Mapping a voltage column
    into the GPON profile vocabulary is what would close the gap.
    """
    if tx is None:
        return None
    return None if tx >= TX_MAX_DBM else tx


def _severity(rx_dbm: float | None, state: str | None,
              warn_dbm: float, crit_dbm: float) -> str:
    if state != "online" or rx_dbm is None:
        return SEV_OK
    if rx_dbm <= crit_dbm:
        return SEV_CRIT
    if rx_dbm <= warn_dbm:
        return SEV_WARN
    return SEV_OK

def _ack_active(ack_until: str | None, ts: str) -> bool:
    if not ack_until:
        return False
    try:
        return _parse(ack_until) > _parse(ts)
    except (ValueError, TypeError):
        return False

def _next_ref(prior_ref: float | None, prior_ref_at: str | None,
              rx_dbm: float | None, ts: str) -> tuple[float | None, str | None]:
    if rx_dbm is None:
        return prior_ref, prior_ref_at
    if prior_ref is None or not prior_ref_at:
        return rx_dbm, ts
    try:
        if _parse(ts) - _parse(prior_ref_at) > timedelta(days=_REF_MAX_AGE_DAYS):
            return rx_dbm, ts
    except (ValueError, TypeError):
        return rx_dbm, ts
    return prior_ref, prior_ref_at

class CentralOpticsMonitor:

    def __init__(self, store, org_id: str, notifier, cfg: Config = CONFIG) -> None:
        self.store = store
        self.org_id = org_id
        self.notifier = notifier
        self.cfg = cfg

    def _thresholds(self, device_id: int) -> tuple[float, float]:
        dev = self.store.get_org_device(self.org_id, device_id) or {}
        warn = dev.get("optical_warn_dbm")
        crit = dev.get("optical_crit_dbm")
        warn = self.cfg.optical_warn_dbm if warn is None else float(warn)
        crit = self.cfg.optical_crit_dbm if crit is None else float(crit)
        return warn, crit

    def sync_device(self, device_id: int, raw_onus: list[dict], ts: str) -> None:
        warn_dbm, crit_dbm = self._thresholds(device_id)
        prior = {r["onu_key"]: r for r in
                 self.store.list_onu_optics(self.org_id, device_id)}

        total = online = warn_count = crit_count = crit_unacked = 0
        for raw in raw_onus:
            onu_key = str(raw.get("onu_key") or "").strip()
            if not onu_key:
                continue
            total += 1
            rx = sane_rx(_to_float(raw.get("rx_dbm")))
            state = str(raw.get("state") or "unknown")
            if state == "online":
                online += 1
            sev = _severity(rx, state, warn_dbm, crit_dbm)
            if sev == SEV_WARN:
                warn_count += 1
            elif sev == SEV_CRIT:
                crit_count += 1

            prev = prior.get(onu_key)
            ref, ref_at = _next_ref(
                prev["rx_ref_dbm"] if prev else None,
                prev["rx_ref_at"] if prev else None, rx, ts)
            ack_until = prev["ack_until"] if prev else None
            if sev == SEV_CRIT and not _ack_active(ack_until, ts):
                crit_unacked += 1

            self.store.upsert_onu_optics(
                self.org_id, device_id, onu_key,
                pon_port=raw.get("pon_port"), onu_id=_to_int(raw.get("onu_id")),
                name=(raw.get("name") or None), serial=(raw.get("serial") or None),
                state=state, rx_dbm=rx, tx_dbm=sane_tx(_to_float(raw.get("tx_dbm"))),
                olt_rx_dbm=_to_float(raw.get("olt_rx_dbm")),
                distance_m=_to_int(raw.get("distance_m")),
                rx_ref_dbm=ref, rx_ref_at=ref_at, severity=sev, ts=ts)

        self._update_badge(device_id, total, online, warn_count, crit_count,
                           crit_unacked, ts)

    def _update_badge(self, device_id: int, total: int, online: int, warn_count: int,
                      crit_count: int, crit_unacked: int, ts: str) -> None:
        prior = self.store.get_olt_optics(self.org_id, device_id)
        was_alarm = bool(prior["alarm"]) if prior else False
        alarm = crit_unacked > 0
        since = (ts if (alarm and not was_alarm)
                 else (prior["alarm_since"] if (prior and alarm) else None))
        self.store.upsert_olt_optics(
            self.org_id, device_id, onus_total=total, onus_online=online,
            warn_count=warn_count, crit_count=crit_count, alarm=alarm,
            alarm_since=since, ts=ts)
        if alarm and not was_alarm:
            self._page(device_id, "OPTICAL_CRIT",
                       f"\U0001f53b Optical critical · {self._name(device_id)}",
                       f"{self._name(device_id)}: {crit_unacked} ONU(s) below the "
                       f"critical Rx-power floor. Subscribers on those drops are at risk "
                       f"of losing sync. Check the ODN / splitters.", ts)
        elif was_alarm and not alarm and crit_count == 0:
            self._page(device_id, "OPTICAL_RECOVERED",
                       f"✅ Optical recovered · {self._name(device_id)}",
                       f"{self._name(device_id)}: no ONUs remain below the critical "
                       f"Rx-power floor.", ts)

    def _name(self, device_id: int) -> str:
        dev = self.store.get_org_device(self.org_id, device_id)
        return dev["name"] if dev else f"#{device_id}"

    def _page(self, device_id: int, kind: str, title: str, body: str,
              ts: str) -> None:
        """Route through the notification GOVERNOR, like every other shell.

        This was the last paging path still calling the notifier inline, and it
        got away with it only because the metric could not fire on most of the
        fleet: C-Data/DBC EPON exposes no per-ONU Rx in SNMP, so its OLTs never
        produced an optical verdict at all. The web-UI scrape (weboptics.py)
        changed that — the whole DBC fleet became capable of these overnight,
        which is exactly the kind of new SNMP-shaped volume the governor exists
        to keep from drowning real ICMP pages.

        The tier stays PUSH (OPTICAL_* is deliberately not in `_DIGEST_KINDS`):
        an ONU under the critical floor is a subscriber about to lose sync, and
        burying that in an hourly roll-up is a judgement for the operator to
        make explicitly, not a side effect of this refactor. What it gains is
        the per-(device, kind) cooldown backstop, which is the actual fix for a
        threshold flap — PYLON has an ONU sitting 0.01 dB off the crit line —
        plus a clean `alert_log.kind` for the by-type analytics, which this
        path was writing as NULL.
        """
        AlertRouter(self.store, self.org_id, self.notifier, self.cfg).emit(
            kind,
            title=title, body=body, priority=3, ts=ts, device_id=device_id,
            gate=self.cfg.optical_alerts)

def _to_float(raw) -> float | None:
    # Non-finite is None, not a number: float() accepts "inf"/"nan", devices do
    # emit them for an out-of-range sensor, and an infinite dBm is both a false
    # reading and invalid JSON — see weboptics._num for the outage it caused.
    if raw in (None, ""):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None

def _to_int(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    # int(inf) raises OverflowError and int(nan) ValueError, so this guard is
    # load-bearing for more than correctness.
    return int(val) if math.isfinite(val) else None
