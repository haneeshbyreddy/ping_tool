"""Live ping: a scrolling stream of individual echoes for ONE device.

"I'm fixing it now, show me it come back." A technician standing at a box wants
to watch packets land, one line per packet, and see the moment it answers.

THE CONSTRAINT THIS MODULE EXISTS TO ENFORCE
--------------------------------------------
**A live-ping result may never reach the FSM.** `api/edge.report()` routes
`mode="recheck"` into `central_engine.run_cycle`, which feeds the state machine:
flap streaks, hysteresis counters, outages, pages. If live packets reached that
path, an operator merely WATCHING a device would move its counters, and at
1 packet/second — with no hysteresis budgeted for it — could page a human at
3am about a device that is fine.

The guarantee is STRUCTURAL, not a rule somebody has to remember:

* This module imports nothing from `wisp.central` at all. No store, no
  `EngineRegistry`, no `dispatch`, no notifier. There is no handle here to
  reach the FSM WITH.
* A `LivePingHub` holds no database connection. Samples live in a bounded
  in-memory ring per session and are never persisted — this is not history,
  and a restart is a legitimate end to a live session.
* The wire type is `(seq, rtt_ms | None)`, deliberately NOT `PingResult`.
  `run_cycle` takes a dict of `PingResult`; a live sample is not one and cannot
  be passed to it without somebody writing a conversion, which is the review
  moment this shape exists to create.
* The edge runs live sessions on their own asyncio tasks against their own
  central endpoint (`POST /edge/liveping`). Nothing they produce enters
  `_gather_pings` or the report envelope.

`unit/test_liveping.py:FsmIsolationTest` pins the import ban by parsing this
file, and `integration/test_central_liveping.py` pins the behaviour by running
a full session and asserting device state, events and outages never move.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

# The build that first carried `POST /edge/liveping`. Half the fleet will lack
# it for a while, so the dashboard asks BEFORE offering the button: a probe
# that cannot answer must say "probe needs v0.16.0", never spin forever on a
# request nobody will ever pick up.
MIN_EDGE_VERSION = "0.16.0"

# Room for a full session at the fast cadence (300 s at 1/s) plus slack. A
# session that somehow overruns loses its OLDEST lines, which is the right end
# to lose: the panel scrolls, and the interesting packet is the newest one.
_RING = 600

# How long a finished session stays READABLE after it stops. Without this the
# panel's next poll 404s the instant the five minutes are up, and "the session
# ended" would render as "something broke". The session is no longer live: it
# accepts no samples, holds no slot against the caps, and is gone from the
# edge's set.
_LINGER_S = 90.0

# How long a session may sit waiting for the probe to pick it up. The probe
# dials central, so a session started while the channel is dormant is not seen
# until the next report — and that cadence is clamped 10-120 s at both ends, so
# this covers the worst case with margin. It is NOT the ping budget: the five
# minutes start when the probe actually answers (see `_arrived`), because a
# technician standing at a device asked for five minutes of packets, not five
# minutes minus however long the channel took to wake up.
_ARM_S = 180.0

STOP_OPERATOR = "operator"
STOP_EXPIRED = "expired"
STOP_REFUSED = "refused"


@dataclass
class LiveSession:
    sid: str
    org_id: str
    node_id: str
    device_id: int
    device_ip: str
    interval_ms: int
    infra: bool
    started_by: str
    started_at: float
    expires_at: float
    samples: deque = field(default_factory=lambda: deque(maxlen=_RING))
    high_seq: int = 0
    sent: int = 0
    lost: int = 0
    last_sample_at: float | None = None
    picked_up_at: float | None = None
    stopped_at: float | None = None
    stop_reason: str | None = None
    stop_detail: str | None = None

    def live(self, now: float) -> bool:
        return self.stopped_at is None and now < self.expires_at

    def readable(self, now: float) -> bool:
        end = self.stopped_at if self.stopped_at is not None else self.expires_at
        return now < end + _LINGER_S

    def public(self, now: float) -> dict:
        received = self.sent - self.lost
        return {
            "sid": self.sid,
            "device_id": self.device_id,
            "device_ip": self.device_ip,
            "interval_ms": self.interval_ms,
            "infra": self.infra,
            "started_by": self.started_by,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "remaining_s": max(0.0, round(self.expires_at - now, 1)),
            "live": self.live(now),
            # Whether the PROBE has this session yet. The one fact that lets
            # the panel tell "waiting for the probe to check in" apart from
            # "the device is not answering" — two sentences that must never
            # look alike.
            "picked_up": self.picked_up_at is not None,
            # Seconds since the last packet ARRIVED here. The other half of
            # the same honesty rule as `picked_up`: a probe that dies mid
            # session simply stops sending, and without this the panel would
            # keep showing its last reading and read as "the device is
            # answering". Measured on CENTRAL's clock, because it is a fact
            # about the channel, not about the device.
            # From the last packet, or from FIRST CONTACT if none has landed
            # yet — never from creation. Measuring the wake-up wait as silence
            # made the panel announce "the probe has gone quiet" the instant it
            # picked a session up, which is the opposite of what happened.
            "silent_s": round(
                now - (self.last_sample_at or self.picked_up_at or self.started_at), 1),
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
            "sent": self.sent,
            "received": received,
            "lost": self.lost,
            "high_seq": self.high_seq,
        }

    def edge_view(self, now: float) -> dict:
        """What the probe is told. Only what it needs to send packets."""
        return {
            "sid": self.sid,
            "device_id": self.device_id,
            "device_ip": self.device_ip,
            "interval_ms": self.interval_ms,
            # Seconds the probe may keep going, its own bound on the deadline.
            "remaining_s": max(0.0, round(self.expires_at - now, 1)),
        }


class LivePingHub:
    """In-memory, TTL'd, dies on restart. Holds no store and no engine.

    Every cap the feature promises is enforced here, in one place: the rate
    (as `interval_ms` on the session, floored server-side), the hard stop (as
    `expires_at`), one session per device, and a ceiling per org.
    """

    def __init__(self, *, max_s: int = 300, interval_ms: int = 1000,
                 infra_interval_ms: int = 2000, max_per_org: int = 3) -> None:
        self.max_s = max(5, int(max_s))
        # Floors, not just defaults. A caller cannot ask for a faster rate:
        # there is no knob on the request for it, and these clamps are what
        # makes that true even if one is ever added.
        self.interval_ms = max(200, int(interval_ms))
        self.infra_interval_ms = max(self.interval_ms, int(infra_interval_ms))
        self.max_per_org = max(1, int(max_per_org))
        self._cond = threading.Condition()
        self._sessions: dict[str, LiveSession] = {}
        self._tokens: dict[tuple[str, str], int] = {}

    # -- internals ---------------------------------------------------------

    def _bump(self, org_id: str, node_id: str) -> None:
        key = (org_id, node_id)
        self._tokens[key] = self._tokens.get(key, 0) + 1
        self._cond.notify_all()

    def _reap_locked(self, now: float) -> None:
        for sess in list(self._sessions.values()):
            if sess.stopped_at is None and now >= sess.expires_at:
                sess.stopped_at = sess.expires_at
                sess.stop_reason = STOP_EXPIRED
                self._bump(sess.org_id, sess.node_id)
            if not sess.readable(now):
                del self._sessions[sess.sid]

    def _live_for_org(self, org_id: str, now: float) -> list[LiveSession]:
        return [s for s in self._sessions.values()
                if s.org_id == org_id and s.live(now)]

    # -- dashboard side ----------------------------------------------------

    def start(self, *, org_id: str, node_id: str, device_id: int,
              device_ip: str, infra: bool, started_by: str,
              now: float | None = None) -> tuple[LiveSession | None, str | None]:
        """Open a session, or explain why not. Never raises.

        A second viewer of a device that is already being watched JOINS the
        running session rather than opening a second one. Two operators on one
        outage is the normal case, not a conflict — refusing the second one
        would be baffling ("somebody else is looking at it"), and running two
        streams would double the packet rate at the device, which is the one
        thing every bound here exists to prevent.
        """
        now = time.time() if now is None else now
        with self._cond:
            self._reap_locked(now)
            for sess in self._sessions.values():
                # The org test is belt and braces: `org_devices.id` is a
                # global autoincrement, so a device id already implies its
                # org. It is here anyway because what this branch returns is
                # a whole session object to the caller, and a device-id-only
                # match is one schema change away from being a cross-org leak.
                if (sess.org_id == org_id and sess.device_id == device_id
                        and sess.live(now)):
                    return sess, None
            if len(self._live_for_org(org_id, now)) >= self.max_per_org:
                return None, (f"this organization already has {self.max_per_org} live "
                              f"ping session(s) running")
            interval = self.infra_interval_ms if infra else self.interval_ms
            sess = LiveSession(
                sid=secrets.token_urlsafe(18), org_id=org_id, node_id=node_id,
                device_id=device_id, device_ip=device_ip, interval_ms=interval,
                infra=infra, started_by=started_by, started_at=now,
                expires_at=now + _ARM_S)
            self._sessions[sess.sid] = sess
            self._bump(org_id, node_id)
            return sess, None

    def get(self, sid: str, now: float | None = None) -> LiveSession | None:
        now = time.time() if now is None else now
        with self._cond:
            self._reap_locked(now)
            return self._sessions.get(sid)

    def for_device(self, org_id: str, device_id: int,
                   now: float | None = None) -> LiveSession | None:
        """The session on this device, live or recently finished.

        Readable-not-live is returned on purpose: it is how the panel says
        "the five minutes are up" instead of going blank.
        """
        now = time.time() if now is None else now
        with self._cond:
            self._reap_locked(now)
            best: LiveSession | None = None
            for sess in self._sessions.values():
                if sess.org_id != org_id or sess.device_id != device_id:
                    continue
                if best is None or sess.started_at > best.started_at:
                    best = sess
            return best

    def stop(self, sid: str, org_id: str, *, reason: str = STOP_OPERATOR,
             detail: str | None = None, node_id: str | None = None,
             now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._cond:
            # Reap first, so a session that ran out while the operator was
            # reaching for the button keeps "expired" as its reason rather
            # than being relabelled "operator". The panel says different
            # things for the two, and only one of them is a fact about a
            # person.
            self._reap_locked(now)
            sess = self._sessions.get(sid)
            if sess is None or sess.org_id != org_id:
                return False
            # `node_id` is passed ONLY by the edge refusal path, where the
            # caller is a probe rather than a person. An org's own operator may
            # stop any session on their devices whichever node runs it, but one
            # NODE must not stop another node's session: the refusal writes an
            # attacker-controlled sentence into `stop_detail` that the panel
            # renders as the reason, so a stolen node credential could kill a
            # sibling's stream and explain it in its own words. `ingest`
            # already checks the node; this is the same check on the way out.
            if node_id is not None and sess.node_id != node_id:
                return False
            if sess.stopped_at is not None:
                return False
            sess.stopped_at = now
            sess.stop_reason = reason
            sess.stop_detail = detail
            self._bump(sess.org_id, sess.node_id)
            return True

    def read(self, sess: LiveSession, after: int) -> tuple[list[list], int]:
        """Samples AND the cursor to ask with next time, from one locked read.

        They have to leave together. Read outside the lock, `high_seq` can
        advance between the two statements, and the next poll's `after` then
        skips the sample that landed in the gap — a packet the panel never
        draws and never can, because the cursor already moved past it.
        """
        with self._cond:
            return ([[seq, rtt] for seq, rtt in sess.samples if seq > after],
                    sess.high_seq)

    def org_live_count(self, org_id: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._cond:
            self._reap_locked(now)
            return len(self._live_for_org(org_id, now))

    def node_has_work(self, org_id: str, node_id: str,
                      now: float | None = None) -> bool:
        """Does this probe have a live session waiting? The wake-up flag."""
        now = time.time() if now is None else now
        with self._cond:
            self._reap_locked(now)
            return any(s.org_id == org_id and s.node_id == node_id and s.live(now)
                       for s in self._sessions.values())

    # -- edge side ---------------------------------------------------------

    def ingest(self, org_id: str, node_id: str, sid: str,
               samples: list, now: float | None = None) -> None:
        """Fold a batch of raw echoes into a session's ring.

        Note what is NOT here: no store write, no engine, no notifier, no
        outage. A sample lands in a deque and stops. That is the whole of what
        a live-ping measurement is allowed to do to this system.
        """
        now = time.time() if now is None else now
        with self._cond:
            sess = self._sessions.get(sid)
            if sess is None or sess.org_id != org_id or sess.node_id != node_id:
                return
            self._arrived(sess, now)
            if not sess.live(now):
                return
            for item in samples:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                try:
                    seq = int(item[0])
                except (TypeError, ValueError):
                    continue
                rtt = item[1]
                if rtt is not None:
                    try:
                        rtt = round(float(rtt), 3)
                    except (TypeError, ValueError):
                        rtt = None
                    if rtt is not None and (rtt < 0 or rtt > 60_000):
                        rtt = None
                if seq <= sess.high_seq:
                    continue
                sess.high_seq = seq
                sess.sent += 1
                if rtt is None:
                    sess.lost += 1
                sess.samples.append((seq, rtt))
                sess.last_sample_at = now
            self._cond.notify_all()

    def _arrived(self, sess: LiveSession, now: float) -> None:
        """First contact from the probe: the ping budget starts HERE.

        Until this moment the session was only on the arming clock. Starting
        the five minutes at CREATION charged the operator for the wake-up: on
        a 120 s cadence a third of the session could be gone before the first
        packet was ever sent. Idempotent — only the first contact moves it, so
        a chatty probe cannot extend its own session.
        """
        # `live` first, and it is load-bearing: without it a sample arriving
        # for a session that already blew its arming deadline would push the
        # deadline out and RESURRECT it. A session that timed out waiting for
        # the probe is over; a late packet is not a reason to restart it.
        if sess.picked_up_at is not None or not sess.live(now):
            return
        sess.picked_up_at = now
        sess.expires_at = now + self.max_s

    def mark_picked_up(self, org_id: str, node_id: str,
                       now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._cond:
            for sess in self._sessions.values():
                if (sess.org_id == org_id and sess.node_id == node_id
                        and sess.live(now)):
                    self._arrived(sess, now)

    def exchange(self, org_id: str, node_id: str, *, token: int,
                 hold_s: float, deliver: bool) -> tuple[int, list[dict]]:
        """The probe's long-poll: hand back the CURRENT set of live sessions.

        Declarative on purpose. The reply is the whole truth about what this
        probe should be pinging, so the edge reconciles rather than replaying
        a command log: a session that vanishes from the set is stopped, and
        there is no "stop" message that can be lost. That is what makes both
        halves restart-safe with no stored state anywhere.

        `token` is the caller's view of the set. Central holds the request
        while its own token still matches and the probe had nothing to
        deliver, so an idle channel costs one parked request instead of a
        poll loop, and a start or stop lands within a second.
        """
        deadline = time.monotonic() + max(0.0, hold_s)
        key = (org_id, node_id)
        with self._cond:
            while True:
                now = time.time()
                self._reap_locked(now)
                cur = self._tokens.get(key, 0)
                if deliver or cur != token or time.monotonic() >= deadline:
                    sessions = [s.edge_view(now) for s in self._sessions.values()
                                if s.org_id == org_id and s.node_id == node_id
                                and s.live(now)]
                    return cur, sessions
                # Slices, not one long wait: expiry is a clock fact and fires
                # no notify, so the loop has to come back and look.
                self._cond.wait(min(2.0, max(0.05, deadline - time.monotonic())))


def is_infra(device_id: int, devices: list[dict]) -> bool:
    """Is this device something else's parent, i.e. aggregation gear?

    The same test `_gentle_probe_plan` makes on the edge, made HERE because
    central holds the whole org topology and the probe only ever sees its own
    slice — a device whose children sit on another node would read as a leaf
    edge-side and get the fast cadence it must not have.
    """
    return any(d.get("parent_device_id") == device_id for d in devices)
