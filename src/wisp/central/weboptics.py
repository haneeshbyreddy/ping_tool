"""Per-ONU optical readings scraped from the OLT's own web UI.

WHY THIS EXISTS. On C-Data/DBC EPON firmware the per-ONU DDM values (Rx/Tx/
temperature/supply voltage/bias) are in no SNMP OID at all. Proven 2026-07-22 on
PYLON-OLT by sweeping the whole vendor ONU area (.27 through .33 of
1.3.6.1.4.1.37950.1.1.5.12.1) against 17 known-good on-screen values: `.29`/`.31`/
`.32` are inventory (MAC, model, ranging, timestamps), `.30`/`.33` are scalars,
and the ONE optical column — `.28.1.3` — held -14.29..-14.79 dBm while the very
ONUs it indexes measured -10.96..-28.24 on the OLT's own page (R^2 = 0.039). That
column is the OLT's burst-receiver level, not per-ONU Rx. The `.28` table also
covered exactly the one PON that had been opened in a browser, which is the tell:
the OLT queries each ONU live over EPON-OAM when you open its OPM Diag page and
never stores the answer.

So central asks the page. The edge is already the hands for this — the web-proxy
tunnel relays an arbitrary HTTP request to a device and hands back the body — so
this subsystem is CENTRAL-ONLY: no edge code, no rollout, it works on the fleet
exactly as deployed. Interpretation stays on central, the same discipline the
diagnostic walker states for itself.

This module is the TRANSPORT half, and it is deliberately vendor-neutral. The
per-vendor recipe (login shape, page path, table layout) is DATA — it lives in
`weboptics_profiles.py`'s closed vocabulary and, since 2026-07-23, in a
dashboard row (Settings -> Monitoring -> Web-UI optics vendors). Writing a
parser before seeing one real response is how you get a fabricated dBm, which is
the one failure mode this whole subsystem exists to avoid, so onboarding an OLT
is still a capture first and a profile second — it is just no longer a deploy.

Every vendor-specific name below (`OPM_PATH`, `login_form`, `opm_form`, …) is
now the DBC built-in's value, kept as the default argument so the one
field-verified path stays byte-identical.
"""

from __future__ import annotations

import base64
import logging
import math
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookies import SimpleCookie

from wisp.central import weboptics_profiles as _profiles
from wisp.central.proxy import ProxySession

log = logging.getLogger("wisp.central.weboptics")

# A device login flow is a couple of hops (POST -> 302 -> landing). More than
# this is a redirect loop, not a login.
MAX_REDIRECTS = 5
# Per-request ceiling. The tunnel's own timeout is the real bound; this just
# stops one wedged OLT from holding a sweep thread for minutes.
DEFAULT_TIMEOUT_S = 20.0


@dataclass(slots=True)
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400

    def header(self, name: str) -> str | None:
        low = name.lower()
        for k, v in self.headers:
            if k.lower() == low:
                return v
        return None

    def text(self, limit: int = 0) -> str:
        raw = self.body[:limit] if limit else self.body
        return raw.decode("utf-8", "replace")


@dataclass(slots=True)
class TunnelHttp:
    """An HTTP client whose transport is the edge's web-proxy tunnel.

    The tunnel is stateless per request: it forwards one method/path/headers/body
    and returns one response. A browser supplied its own cookie jar and redirect
    handling; a headless caller has to bring both, which is all this class is.

    It builds an ad-hoc ProxySession rather than registering one through
    ProxyHub.open_session — the same thing the connect preflight does. A
    registered session is a BROWSING session: it shows up in the sessions panel,
    carries a TTL a human is expected to renew, and gets audit-logged per hop. A
    background sweep is neither, and it must not look like an operator poking at
    a device.
    """

    hub: object
    org_id: str
    node_id: str
    device_id: int
    ip: str
    port: int
    scheme: str = "http"
    timeout_s: float = DEFAULT_TIMEOUT_S
    _cookies: dict[str, str] = field(default_factory=dict)

    def _session(self) -> ProxySession:
        now = time.time()
        return ProxySession(
            sid="weboptics", org_id=self.org_id, device_id=self.device_id,
            node_id=self.node_id, device_ip=self.ip, device_port=self.port,
            scheme=self.scheme, created_by=0, created_at=now,
            expires_at=now + self.timeout_s)

    def _absorb_cookies(self, headers: list[tuple[str, str]]) -> None:
        # Edge sends headers as pairs precisely so repeated Set-Cookie survives;
        # collapsing them into a dict upstream would drop all but the last, and a
        # session id is as often the first as the last.
        for k, v in headers:
            if k.lower() != "set-cookie":
                continue
            try:
                jar = SimpleCookie()
                jar.load(v)
            except Exception:                      # malformed cookie: skip it
                continue
            for name, morsel in jar.items():
                self._cookies[name] = morsel.value

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def request(self, method: str, path: str, *, headers: dict | None = None,
                body: bytes = b"", follow_redirects: bool = True) -> Response:
        hdrs = dict(headers or {})
        seen = 0
        while True:
            if self._cookies:
                hdrs["Cookie"] = self._cookie_header()
            resp = self.hub.submit(
                self._session(), method=method, path=path, headers=hdrs,
                body=body, timeout=self.timeout_s)
            if resp is None:
                # No reply within the window. Nearly always a dormant tunnel:
                # the edge only long-polls for orgs with web_proxy granted.
                return Response(0, [], b"", error="tunnel timeout")
            if resp.get("error"):
                return Response(0, [], b"", error=str(resp["error"])[:200])
            pairs = [(str(k), str(v)) for k, v in (resp.get("headers") or [])]
            self._absorb_cookies(pairs)
            try:
                raw = base64.b64decode(resp.get("body_b64") or "")
            except (ValueError, TypeError):
                raw = b""
            status = int(resp.get("status") or 502)
            out = Response(status, pairs, raw)

            if not (follow_redirects and status in (301, 302, 303, 307, 308)):
                return out
            loc = out.header("Location")
            if not loc:
                return out
            seen += 1
            if seen > MAX_REDIRECTS:
                return Response(status, pairs, raw,
                                error=f"more than {MAX_REDIRECTS} redirects")
            path = _redirect_path(path, loc)
            # 303, and universally-in-practice 302, continue as GET with no body.
            if status in (301, 302, 303):
                method, body = "GET", b""

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post_form(self, path: str, fields: dict[str, str], **kw) -> Response:
        from urllib.parse import urlencode
        body = urlencode(fields).encode()
        hdrs = {"Content-Type": "application/x-www-form-urlencoded", **kw.pop("headers", {})}
        return self.request("POST", path, headers=hdrs, body=body, **kw)


# --- the DBC / C-Data OPM Diag recipe, as the default profile -----------------
# Captured from PYLON-OLT 2026-07-22. The page is one POST per PON:
#     POST /action/onuopmdiag.html
#     select=<pon>&port_refresh=Refresh&searchMac=&searchDescription=
#         &onuid=0/&who=100&SessionKey=<key>
# It now lives as `weboptics_profiles.BUILTIN_SPECS['dbc']`, with the constants
# here derived from it so there is ONE copy of the recipe. They stay exported
# because callers (and the field-capture tests) name them.
DBC = _profiles.builtin("dbc")
OPM_PATH = DBC.optics_path
OPM_STATIC_FIELDS = dict(DBC.optics_static)
OPM_CHARSET = DBC.charset

# SessionKey is NOT a cookie — this firmware has none. It is appended to every
# form by inline JS at the foot of each page, and it ROTATES: the reply to a
# request made with 'dmswx' carried 'kmwex'. So each response hands us the key
# for the next request, which makes the whole scrape strictly sequential. That
# happens to be what we wanted anyway for a weak agent.
_SESSION_KEY_RE = DBC.session_key_re()


# The markup variants a session token could plausibly arrive in. This is a
# DIAGNOSTIC vocabulary, not a parsing one: `session_key` still reads only the
# one form captured from a box known to work. Widening the reader on a guess is
# how a scrape starts "succeeding" against a login page — it would lift a
# placeholder key, POST with it, get the login page back, parse zero rows and
# report no error, which is the silent false negative this whole subsystem
# exists to prevent. So: name the shape, restart, then widen deliberately.
_KEY_SHAPE_SOURCES = (
    ("js-single-quote", r"{f}\.value\s*=\s*'", 0),
    ("js-double-quote", r"{f}\.value\s*=\s*\"", 0),
    ("js-unquoted", r"{f}\.value\s*=\s*[A-Za-z0-9_$]", 0),
    ("js-var", r"(?:var|let)\s+{f}\s*=", 0),
    ("hidden-input", r"name\s*=\s*[\"']?{f}", re.I),
    ("query-param", r"[?&]{f}=", 0),
)
_KEY_SHAPE_CACHE: dict[str, tuple[tuple[str, re.Pattern], ...]] = {}


def _key_shapes_for(field_name: str):
    cached = _KEY_SHAPE_CACHE.get(field_name)
    if cached is None:
        esc = re.escape(field_name)
        cached = tuple(
            (name, re.compile(src.replace("{f}", esc), flags))
            for name, src, flags in _KEY_SHAPE_SOURCES)
        _KEY_SHAPE_CACHE[field_name] = cached
    return cached


def key_shapes(html: str, field_name: str = "SessionKey") -> list[str]:
    """Which session-token markup variants a page carries. SHAPE, never content."""
    return [name for name, rx in _key_shapes_for(field_name) if rx.search(html)]


def session_key(html: str, profile=None) -> str | None:
    """The session token this page hands us for the NEXT request."""
    rx = profile.session_key_re() if profile is not None else _SESSION_KEY_RE
    m = rx.search(html)
    return m.group(1) if m else None


def optics_headings_seen(html: str, profile=None) -> tuple[list[str], int]:
    """Which of the profile's own column headings this page prints, and how many
    it maps. ONE definition, because two callers ask the same question for
    opposite purposes — `diagnose_login` to word a refusal, `scrape_optics` to
    decide whether a tokenless page is safe to read anyway — and a page they
    graded differently would mean a build that reports "not the optical page"
    and then gets scraped, or the reverse.

    HEADINGS ONLY, never a row. This answers "am I looking at the right page",
    which is exactly the question a guessed `optics_path` gets wrong; it must
    never become a way to lift a reading.
    """
    prof = profile if profile is not None else DBC
    cols = prof.columns or {}
    low = html.lower()
    return ([h for h in cols.values() if h and h.strip().lower() in low], len(cols))


def diagnose_login(html: str, profile=None) -> str:
    """Why a login reply carried no SessionKey, in terms an operator can act on.

    "login rejected — check credentials" is a guess dressed as a diagnosis, and
    it is the same opaque dead end the session-open preflight was written to
    kill: three very different faults (wrong password, a page shape this parser
    does not know, a device that isn't the OLT at all) all present as one
    sentence, so the first thing anyone does is re-type a password that was
    never wrong.

    Deliberately reports SHAPE, never content — no body snippet reaches the log.
    A login page can echo the username back, and this runs unattended on a
    schedule, so anything it prints is printed forever.
    """
    prof = profile if profile is not None else DBC
    key_field = prof.session_key_field
    if not html.strip():
        return "the device returned an empty body"
    low = html.lower()
    # The path WITHOUT its extension ("/action/login.html" -> "action/login"), so
    # a reply that links the login page under any of its several spellings (this
    # firmware also serves login_first.html) still reads as one.
    login_hint = prof.login_page_path.strip("/").lower().rsplit(".", 1)[0]
    # A PASSWORD INPUT is the build-agnostic tell, and it is needed: this
    # firmware renders its login text from /i18N/login_en_US.properties through
    # jQuery, so the word "password" can be absent from a page that is plainly
    # the login form — which made a bounced login read as "unrecognised reply".
    # The input's own type attribute is markup, not copy, so no bundle can hide
    # it. Quoted either way, and `type = "password"` with spaces is legal HTML.
    has_password_input = re.search(
        r"""<input[^>]*type\s*=\s*['"]?password""", low) is not None
    looks_like_login = (bool(login_hint) and login_hint in low
                        or has_password_input
                        or ("password" in low and "form" in low))
    if key_field in html:
        seen = key_shapes(html, key_field)
        # An EMPTY token is its own answer, and until 2026-07-23 it produced a
        # SELF-CONTRADICTION: `key_shapes` matches the opening quote while the
        # reader needs a non-empty value, so a page shipping `SessionKey.value
        # = ''` reported "carries SessionKey as [js-single-quote] but not as
        # [js-single-quote]" — on 8 of 12 fleet OLTs at once, which is a
        # diagnosis nobody can act on. The blank token is this firmware's
        # pre-session placeholder: the page rendered, we are simply not logged
        # in to it.
        if re.search(rf"{re.escape(key_field)}\.value\s*=\s*(''|\"\")", html):
            return (f"the page ships an EMPTY {key_field}. This firmware "
                    "blanks the token until a session exists, so we are not "
                    "logged in. Either the stored password was refused, or "
                    "someone else holds this OLT's single web session "
                    f"({page_shape(html)})")
        # The token IS in the form we read, yet nothing could be lifted from
        # it: a truncated or oddly-quoted value. Say that rather than blaming
        # the markup shape, which is demonstrably the one we understand.
        if "js-single-quote" in seen:
            return (f"the page carries {key_field} in the expected form but no "
                    "value could be read out of it: the token is blank or "
                    f"malformed, so the session never started ({page_shape(html)})")
        # Report BOTH facts rather than picking one. This branch used to win
        # outright over the login-page check and say only "markup differs",
        # which on first fleet-wide contact was indistinguishable from a
        # refused password on 8 OLTs at once — and those are opposite fixes
        # (write a parser vs. correct a stored credential). Naming the shape is
        # what turns the next restart into an answer instead of another guess.
        shapes = ", ".join(seen) or "no form this parser recognises"
        why = ("; it ALSO looks like the login page, so the likelier reading is "
               "a refused password than a parser gap" if looks_like_login else "")
        return (f"the reply carries {key_field} as [{shapes}] but not as "
                f"[js-single-quote], which is the only form read{why} "
                f"({page_shape(html)})")
    if looks_like_login:
        return "the device served its login page again, so the password was refused"
    # Is this the OPTICAL PAGE with no token, or some other page entirely? Those
    # are opposite fixes — a `session` strategy on a profile row vs. freeing the
    # OLT's single web session — and until 2026-08-07 they gave the identical
    # "unrecognised reply · N chars", which cost a whole session of guessing on
    # chandana-network's MAIN_OLT. The profile already carries the page's own
    # headings, so ASK THE PAGE: nothing else on this firmware prints "RX Power"
    # next to "MAC Address". Headings only, never a row — this decides which
    # question to ask next, it never lifts a reading.
    headings, wanted = optics_headings_seen(html, prof)
    if wanted and len(headings) == wanted:
        # scrape_optics reads this case keyless rather than refusing, so this
        # wording is only reachable through another caller. Keep the two apart:
        # "all headings" and "most headings" are a working build and a profile
        # fault, and one sentence covering both said neither.
        return (f"this IS the optical page — all {wanted} column headings are on "
                f"it — but it carries no {key_field}. This build mints no token "
                f"({page_shape(html)})")
    if len(headings) >= 3:
        return (f"this looks like the optical page but only {len(headings)} of "
                f"{wanted} column headings matched, and there is no {key_field} "
                "on it either, so it was not read: a renamed column is a profile "
                "fault to fix, not a page to guess at. Check this profile's "
                f"column names against this build ({page_shape(html)})")
    # The vendor's own words, from the profile. An empty marker list means the
    # operator gave us nothing to recognise the box by, so we say nothing about
    # it rather than inventing a verdict.
    if prof.vendor_markers and not any(m in low for m in prof.vendor_markers):
        return ("the reply does not look like this OLT's web UI at all. Check "
                "the address really reaches the OLT and not a router in front.")
    return (f"unrecognised reply, and it is NOT the optical page (none of this "
            f"profile's column headings are on it) · {page_shape(html)}")


_TITLE_RE = re.compile(r"<title[^>]*>(.{0,80}?)</title>", re.I | re.S)
_FRAME_SRC_RE = re.compile(r"<frame[^>]+src\s*=\s*[\"']?([^\"'\s>]+)", re.I)


def page_shape(html: str) -> str:
    """A page's STRUCTURE, for a log line — never its content.

    When the parser meets a page it doesn't understand, "unrecognised reply
    (55509 chars)" is a dead end: it cost a deploy cycle to learn only that the
    reply was big. Title and frame targets are what actually tell you where the
    real page went, and unlike the body they are safe to print — they are the
    OLT's own fixed markup, not subscriber data or anything a login echoes back.
    Both are bounded so a hostile or broken page cannot flood the log.
    """
    bits = [f"{len(html)} chars"]
    m = _TITLE_RE.search(html)
    if m:
        title = " ".join(m.group(1).split())[:60]
        if title:
            bits.append(f"title={title!r}")
    frames = _FRAME_SRC_RE.findall(html)[:6]
    if frames:
        bits.append("frames=" + ",".join(f[:40] for f in frames))
    return "; ".join(bits)


class _TableRows(HTMLParser):
    """Every <tr> in the document as a list of its cells' text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "td" and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _norm_head(text: str) -> str:
    """A heading reduced to its letters and digits, for matching.

    The DBC page splits its headings across <font> tags and decorates them with
    units — "Distance" + "(m)", "Temperature" + "(&deg;C)", "TX Bias Current" —
    so an exact string match against what an operator types would fail on
    perfectly correct input.
    """
    return "".join(c for c in (text or "").lower() if c.isalnum())


def _column_index(rows: list[list[str]], profile) -> dict[str, int] | None:
    """field -> cell index, read from the table's own HEADER ROW.

    BY NAME, NEVER BY POSITION — the rule this whole profile mechanism exists
    for. A vendor that orders its table differently (or inserts a column in a
    firmware update) must not silently start reading Tx power into the Rx
    column: that is a plausible-looking lie, and it is the one class of bug the
    reader cannot catch downstream.

    Returns None when no row matches every declared heading, which is a real
    answer and not a failure — the caller falls back to the profile's declared
    `column_order` for a table that genuinely has no header.
    """
    wanted = {f: _norm_head(h) for f, h in (profile.columns or {}).items()}
    if not wanted:
        return None
    for cells in rows:
        heads = [_norm_head(c) for c in cells]
        found: dict[str, int] = {}
        used: set[int] = set()
        for fld, head in wanted.items():
            hit = next((i for i, h in enumerate(heads)
                        if i not in used and h and h.startswith(head)), None)
            if hit is None:
                break
            found[fld] = hit
            used.add(hit)
        if len(found) == len(wanted):
            return found
    return None


def parse_optics_table(html: str, profile=None, pon: int | None = None) -> list[dict]:
    """ONU optical readings from one vendor optics page.

    Only ONLINE ONUs appear on such a page, and that is correct rather than a
    gap: the readings come from querying the ONU over the fibre, so a dark one
    has no value to report. Anything unparseable is DROPPED, never defaulted — a
    fabricated dBm pages a splicing crew, a blank one doesn't.

    Data rows are identified by their ANCHOR CELL, not by class or position:
    on the DBC page the ONU-ID cell carries class='hd' exactly like the header
    cells do, so class cannot tell them apart, and the ONU id is the only
    unambiguous mark of a row that describes a subscriber.
    """
    prof = profile if profile is not None else DBC
    parser = _TableRows()
    parser.feed(html)
    index = _column_index(parser.rows, prof)
    if index is None:
        if not prof.column_order:
            # A profile that maps columns only by heading, meeting a page with
            # no heading it recognises, reads NOTHING. Deliberately: guessing at
            # positions here is how the wrong column becomes an Rx figure.
            return []
        index = {f: i for i, f in enumerate(prof.column_order) if f}
    need = max(index.values()) + 1
    anchor_at = index["onu_ref"]
    anchor_re = prof.anchor_re
    # Whether this vendor publishes a supply rail at all — the DDM sanity check
    # is only meaningful when it does. See _sane_optics.
    expect_voltage = "voltage_v" in index
    out: list[dict] = []
    for cells in parser.rows:
        if len(cells) < need:
            continue
        m = anchor_re.match(cells[anchor_at].strip())
        if not m:
            continue                                   # header, or a layout row
        if prof.onu_id_shape == "pon-colon-onu":
            pon_label, onu_id = m.group(1), int(m.group(2))
            # The PON's own index, for onu_key — "EPON0/3" -> 3.
            pon_index = pon_label.rsplit("/", 1)[-1]
        else:
            # The page names only the ONU; the PON is the one we asked for.
            # Without that the reading has no slot to merge onto, so the row is
            # dropped rather than filed under a guessed port.
            if pon is None:
                continue
            onu_id = int(m.group(1))
            pon_index = str(pon)
            pon_label = (prof.pon_label.replace("{pon}", pon_index)
                         if prof.pon_label else pon_index)
        row = {"onu_key": f"{pon_index}.{onu_id}", "pon_port": pon_label,
               "onu_id": onu_id}
        for fld, at in index.items():
            if fld == "onu_ref":
                continue
            row[fld] = prof.cast(fld, cells[at])
        # MACs are compared upper-case throughout (onuroster._norm_mac and
        # weboptics._match_key both fold case); normalise once, at the source.
        if isinstance(row.get("serial"), str):
            row["serial"] = row["serial"].upper()
        out.append(_sane_optics(row, expect_voltage))
    return out


# The name this parser shipped under while DBC was the only vendor. Kept because
# the field-capture tests are written against it and they are the record of what
# a real page looks like.
def parse_opm_diag(html: str, profile=None, pon: int | None = None) -> list[dict]:
    return parse_optics_table(html, profile, pon)


# An ONU's supply rail is 3.3 V BY DESIGN — every ONU ever built. That makes it
# the one field on this page whose correct value is known a priori, so it is the
# canary for "is this DDM block a measurement at all". Generous bounds: we are
# rejecting rails, not judging a marginal PSU.
_VOLT_MIN_V, _VOLT_MAX_V = 2.0, 5.0
# The Rx/Tx rails moved to `optics.py` when the SNMP path needed the same check:
# that module owns the one path every reading crosses, so the bounds live beside
# it and both feeders share ONE definition. Imported rather than re-stated —
# two copies of a physical constant is how the scrape and the walk end up
# disagreeing about whether the same ONU was measured.
from wisp.central.optics import (  # noqa: E402  (kept beside the rule it serves)
    RX_FLOOR_DBM as _RX_FLOOR_DBM,
    RX_MAX_DBM as _RX_MAX_DBM,
)


def _sane_optics(row: dict, expect_voltage: bool = True) -> dict:
    """Blank readings that are sensor RAILS rather than measurements.

    Same rule as `_num`'s non-finite guard, one level up: a value that cannot
    physically exist is not a reading, and keeping it is worse than having none.
    Found on first fleet-wide contact (2026-07-23, HILL-OLT-1) — an ONU whose
    diagnostics are dead prints the raw rail on every DDM field at once:
    0xFFFF reads 6.55 V / 131.07 mA / +8.16 dBm, 0x0000 reads 0.0 V / -40 dBm.

    Both directions were live faults, and they fail OPPOSITE ways, which is why
    a range check on Rx alone would not have done:
      * +8.16 dBm grades comfortably ABOVE the warn floor, so an ONU with dead
        optics reads as the healthiest drop on the PON — a false negative that
        no operator would ever go looking for.
      * -40.0 dBm sits below the crit floor and pages OPTICAL_CRIT, sending a
        crew after an ONU that may be perfectly well lit.

    Voltage is the discriminator rather than any optical threshold precisely
    because it needs no judgement about what dBm is plausible on this plant: the
    rail is 3.3 V or the block is not being read. The whole optical block goes
    when it fails, because these fields rail TOGETHER — trusting a Tx that came
    out of the same dead register would just move the lie one column over.

    `expect_voltage` is False when the VENDOR's page has no supply-voltage
    column at all (a profile that doesn't map one). Then this check cannot run,
    and running it anyway would blank every reading that vendor ever produces —
    throwing away all the data to protect against a fault we have no way to
    detect. The physical bounds on Rx below still apply, since those need no
    second column. The distinction matters: an ABSENT column is a fact about the
    firmware, a MISSING value in a column that exists is a fact about this ONU,
    and only the second is evidence of a dead sensor.
    """
    volt = row.get("voltage_v")
    if expect_voltage and (volt is None or not (_VOLT_MIN_V <= volt <= _VOLT_MAX_V)):
        for fld in ("temp_c", "voltage_v", "tx_bias_ma", "tx_dbm", "rx_dbm"):
            row[fld] = None
        return row
    rx = row.get("rx_dbm")
    if rx is not None and (rx >= _RX_MAX_DBM or rx <= _RX_FLOOR_DBM):
        row["rx_dbm"] = None
    tx = row.get("tx_dbm")
    if tx is not None and tx >= _RX_MAX_DBM + 10.0:
        row["tx_dbm"] = None
    return row


def _num(raw: str, cast):
    """A cell's number, or None. NON-FINITE IS NONE, not a number.

    `float()` happily accepts "inf", "-inf" and "nan", and this page really does
    print them: an ONU whose transmitter reads below the meter's range came back
    with Tx = -inf. Kept, that value is a lie with infinite confidence — and it
    also serialises as bare `-Infinity`, which is not valid JSON, so ONE such
    cell took out the whole Optical tab for that OLT with a JSON.parse error.
    Same rule as every other unparseable cell here: drop it, never default it.
    """
    try:
        val = float(raw.strip())
    except (TypeError, ValueError, AttributeError):
        return None
    if not math.isfinite(val):
        return None
    return cast(val)


def opm_form(pon: int, key: str, profile=None) -> dict[str, str]:
    """Form body for one PON's refresh."""
    return (profile if profile is not None else DBC).optics_form(pon, key)


# Login is a plain form POST that lands on the main page; it carries NO
# SessionKey because it is what issues the first one. (Both paths are the DBC
# built-in's, exported for callers that predate the profile mechanism.)
LOGIN_PATH = DBC.login_path
# The page the form lives on. A browser reaches it via GET / -> 302; we ask for
# it directly, since the tunnel resolves a path and the redirect just costs a hop.
LOGIN_PAGE_PATH = DBC.login_page_path
# Fallback only, for an OLT whose roster carries no parseable PON label. It is
# PYLON's port count, and treating it as the fleet's was the second reason this
# subsystem only ever worked on PYLON: the same OLTs run 3 to 8 PONs with GAPS
# in the numbering (HILL-OLT-1 has 1,3,4,5,6,7,8), so a fixed 1..4 silently
# skipped just over half the fleet's online ONUs while reporting success.
# The real list comes from the SNMP roster — see `pon_indices`.
DEFAULT_PONS = DBC.default_pons
# A PON port index the vendor could plausibly have. Bounds a roster typo; it is
# not a claim about any particular chassis.
MAX_PON_INDEX = 64
# Roster PON labels look like "EPON0/3" / "GPON0/12" — slot, then port. A label
# without that shape is junk from a partial walk (the fleet really carries an
# empty one and a bare "60"), and a junk index costs a POST that makes a weak
# OLT interrogate a port that isn't there.
_PON_LABEL_RE = re.compile(r"^[A-Za-z]+\d+/(\d+)$")


def pon_indices(labels) -> tuple[int, ...]:
    """The PON ports an OLT actually has, from its SNMP roster's labels.

    The scrape is one POST per PON, so it has to know how many there are, and
    the OLT's own roster is the only honest answer available — asking a fixed
    1..4 everywhere reads as success while missing every ONU on port 5 and up.
    Unparseable labels are dropped rather than guessed at, and an empty result
    lets the caller fall back to DEFAULT_PONS.
    """
    out: set[int] = set()
    for raw in labels or ():
        m = _PON_LABEL_RE.match(str(raw or "").strip())
        if not m:
            continue
        idx = int(m.group(1))
        if 1 <= idx <= MAX_PON_INDEX:
            out.add(idx)
    return tuple(sorted(out))


def login_form(username: str, password: str, profile=None) -> dict[str, str]:
    return (profile if profile is not None else DBC).login_form(username, password)


def _decode(resp: Response, profile=None) -> str:
    # Charset is per-vendor and getting it wrong presents as "this OLT reports
    # no optics" — a Description with any non-ASCII byte mojibakes or raises.
    charset = (profile.charset if profile is not None else OPM_CHARSET)
    return resp.body.decode(charset, "replace")


def scrape_optics(http: TunnelHttp, username: str, password: str,
                  profile=None, pons=None,
                  deadline: float | None = None) -> tuple[list[dict], str | None]:
    """Log in and read every PON's OPM Diag table. Returns (rows, error).

    Sequential by necessity, not by choice: SessionKey rotates with every
    response, so each request needs the key the previous one handed back.

    PARTIAL RESULTS ARE KEPT. If PON3 fails after PON1 and PON2 succeeded, those
    readings are real and worth having — they are merged into the roster by MAC,
    never used to replace it, so a missing PON leaves those ONUs' previous values
    alone rather than blanking them. The error is still returned so the caller
    can log it.

    `deadline` is a `time.monotonic()` stamp after which no further PON is
    asked for. Per-request timeouts bound one hop; this bounds the DEVICE, which
    is what matters once the sweep is fleet-wide rather than one OLT — an
    8-PON box that has gone slow would otherwise hold the single sweep thread
    for minutes while every other OLT's readings go stale behind it.

    Never raises. A scrape is a best-effort enrichment; it must not be able to
    disturb the ICMP path that actually pages people.
    """
    prof = profile if profile is not None else DBC
    pons = tuple(pons) if pons else prof.default_pons
    # Fetch the login page before posting to it, exactly as a browser does.
    # Taken from proxy_audit of a session that demonstrably worked on PYLON:
    # GET / -> 302 -> /action/login_first.html -> /action/login.html -> only
    # THEN POST /action/main.html. We are replicating an observed good client
    # rather than theorising: this firmware keeps no cookie, so whatever state
    # the preamble establishes (if any) is not something we can reason about
    # from the outside. It costs one round trip and cannot do harm.
    entry = http.get(prof.login_page_path)
    # ...and its ANSWER is the gate on sending the password. The hop was already
    # being paid for and then discarded, so the login POST went out no matter
    # what came back — fine while the only target was one hand-verified OLT,
    # not fine once eligibility is inferred fleet-wide. If this vendor's login
    # page isn't there, the thing on the other end is not this vendor's web UI,
    # and an admin credential should not be the way we find that out.
    if not entry.ok:
        return [], (f"no login page at {prof.login_page_path} "
                    f"({entry.error or entry.status}), so credentials NOT sent; "
                    "check the address really reaches the OLT's web UI")

    login = http.post_form(prof.login_path, prof.login_form(username, password))
    if not login.ok:
        return [], f"login failed: {login.error or login.status}"

    # The login reply is a FRAMESET, not a page that carries a SessionKey. That
    # was the first live failure: 55 KB of the OLT's own UI with no key in it,
    # which read as "login rejected" for a password that was demonstrably
    # correct. proxy_audit shows a browser loading /action/main.html and
    # immediately pulling configsave/loginout/systeminfo — frames — and it
    # fetches the vendor's ONU pages by GET (`onuauthinfo.html?select=1`)
    # before any POST. So the FIRST key comes from GETting the OPM page itself,
    # exactly as clicking the menu item does; the Refresh POST then rotates it.
    opened = http.get(prof.optics_path)
    if opened.status == 404:
        # Not a fault to retry: this firmware build has no such page. The
        # C-Data GPON boxes are the live example — same vendor, same login flow,
        # same /action/ UI, but their menu is T-CONT/GEM/DBA and the optical
        # page is somewhere else under another name. Guessing at that name is
        # how a fabricated dBm gets shipped; onboarding it means a capture (and
        # then a profile row, which is what this vocabulary is for).
        return [], (f"this OLT's firmware has no {prof.optics_path} (404). It "
                    "answers on the vendor's web UI but does not carry that "
                    "optical page; it needs its own capture and profile")
    if not opened.ok:
        return [], f"could not open the optics page: {opened.error or opened.status}"
    keyless = False
    key = session_key(_decode(opened, prof), prof) if prof.rotates_key else None
    if prof.rotates_key and not key:
        page = _decode(opened, prof)
        # "No key" USUALLY means "not logged in" — but not always, and the page
        # itself settles it. chandana-network's MAIN_OLT (2026-08-07) serves this
        # very page with ALL NINE of the profile's headings on it and no token
        # anywhere: the login plainly worked and this build simply does not mint
        # a SessionKey, where its sibling on the next IP does. Treating that as a
        # refusal cost a fleet's Rx column and read as "check the password",
        # which was never wrong.
        #
        # The bar is EVERY mapped heading, not a majority: the page has to prove
        # it is the table before an admin session goes on to POST at it. A login
        # page, a session-limit notice or a menu clears none of them. If a build
        # renames one column this refuses and SAYS the count, which is the fault
        # being reported rather than a scrape running on a page we misread.
        #
        # Scoping this per device via a profile row is NOT the alternative it
        # looks like: `name` is deliberately the same token as gpon_profiles.name
        # / org_devices.gpon_vendor, so a row for this OLT would shadow the
        # recipe for every OLT in the org — including the one on .102 that DOES
        # rotate a key and works today.
        seen, wanted = optics_headings_seen(page, prof)
        if not (wanted and len(seen) == wanted):
            # Say WHICH of the several very different reasons it was (see
            # diagnose_login) — never let any of them read as "this OLT reports
            # no optics".
            return [], f"login rejected: {diagnose_login(page, prof)}"
        # `optics_form` already omits the field when key is None, so the POSTs go
        # out shaped exactly as this build's own page would send them.
        keyless = True
        log.info("web optics: %s serves the optical page with no %s but all %d"
                 " headings — reading it keyless", prof.optics_path,
                 prof.session_key_field, wanted)

    rows: list[dict] = []
    for done, pon in enumerate(pons):
        if deadline is not None and time.monotonic() >= deadline:
            # Partial, and said so. The PONs already read are real readings and
            # merge normally; the rest keep whatever the SNMP walk last said.
            return rows, (f"time budget spent after {done} of {len(pons)} "
                          "PON(s); this OLT is answering slowly")
        form = prof.optics_form(pon, key)
        if prof.optics_method == "GET":
            from urllib.parse import urlencode
            resp = http.get(f"{prof.optics_path}?{urlencode(form)}")
        else:
            resp = http.post_form(prof.optics_path, form)
        if not resp.ok:
            return rows, f"PON{pon}: {resp.error or resp.status}"
        page = _decode(resp, prof)
        if keyless:
            # A keyless build has no token to go missing, so the mid-scrape check
            # below cannot run — but the thing it protects against (our session
            # displaced by a human logging in, since this firmware keeps no
            # cookie) still happens. The page proves itself the same way it did
            # on entry: bounced to the login form, the headings go with it.
            seen, wanted = optics_headings_seen(page, prof)
            if len(seen) != wanted:
                return rows, (f"PON{pon}: the optical page stopped being itself "
                              f"({len(seen)} of {wanted} headings) — session lost"
                              " to another login?")
        else:
            nxt = session_key(page, prof) if prof.rotates_key else key
            if prof.rotates_key and not nxt:
                # Session died mid-scrape. The likeliest cause is benign and worth
                # knowing: this firmware keeps no cookie, so a human logging into
                # the OLT can displace our session (and we can displace theirs).
                return rows, f"PON{pon}: session lost (someone else logged in?)"
            key = nxt
        rows.extend(parse_optics_table(page, prof, pon))
    return rows, None


# The name this scraper shipped under while DBC was the only vendor. Kept as the
# entry point the tests drive, since they are the record of one real exchange.
def scrape_opm(http: TunnelHttp, username: str, password: str,
               pons=None, profile=None,
               deadline: float | None = None) -> tuple[list[dict], str | None]:
    return scrape_optics(http, username, password, profile, pons, deadline)


# --- merging a scrape into the SNMP roster ------------------------------------

def _match_key(raw: str | None) -> str:
    """Punctuation-blind MAC key, for matching a scraped row to a roster slot.

    A third normalizer beside onuroster's `_norm_mac` (identity) and
    `search_key` (substring search), and deliberately so. Both views here come
    from ONE box, but they come from two different subsystems of its firmware —
    the SNMP registration table and an HTML page — and nothing guarantees they
    punctuate a MAC the same way. Separator-exact matching would then merge
    NOTHING while looking perfectly healthy, which is the worst failure this
    feature has: a blank Rx column reads as "this vendor doesn't report Rx",
    the exact false negative the whole subsystem exists to kill.

    This does NOT weaken the identity invariant `_norm_mac` protects. That rule
    exists because collapsing punctuation across roster rows fabricates
    duplicate-MAC PAGES; nothing here writes identity or pages anyone — it only
    decides which existing roster slot a reading belongs to, and any ambiguity
    is dropped rather than guessed (see merge_scraped).
    """
    return "".join(c for c in (raw or "") if c.isalnum()).upper()


# Only these come from the scrape. state/name/membership stay SNMP's — the walk
# sees every ONU, the page only ever sees the ones currently online.
#
# `distance_m` is deliberately NOT here, though the page has it and in REAL
# METRES while the dbc SNMP profile's value is EPON time quanta (~39% short).
# Merging it would make onu_optics.distance_m mixed-unit — metres for the online
# ONUs the page returns, quanta for the dark ones it cannot — and ponfault.py
# builds its fibre-cut bracket from BOTH ends of that set (max online short of
# dark, min dark]. Two units in one interval is worse than one wrong unit: the
# survivors would measure FARTHER out than the dark ONUs and invert the bracket
# a splicing crew quotes drum off. The scraped metres are stored in
# onu_web_optics regardless, so the fix is unblocked — but it belongs with the
# UNIT fix (scales.distance = 1.6393 on the dbc gpon_profile, a dashboard row,
# which moves every ONU to metres at once), not ahead of it.
_MERGED_FIELDS = ("rx_dbm", "tx_dbm")


# Tolerance for central's own clock stepping backwards (NTP) between writing a
# scraped_at and reading it. Small: this compares one clock against itself, so
# anything larger would be hiding a real problem rather than absorbing jitter.
_CLOCK_GRACE_S = 60.0


def _fresh(scraped: list[dict], now: str, max_age_s: float) -> list[dict]:
    from wisp.core.analytics import _parse
    try:
        ref = _parse(now)
    except (ValueError, TypeError, AttributeError):
        return []
    out = []
    for row in scraped:
        try:
            age = (ref - _parse(row.get("scraped_at") or "")).total_seconds()
        except (ValueError, TypeError, AttributeError):
            continue                      # unparseable stamp: treat as unusable
        if -_CLOCK_GRACE_S <= age <= max_age_s:
            out.append(row)
    return out


def merge_scraped(raw_onus: list[dict], scraped: list[dict], now: str,
                  max_age_s: float) -> tuple[list[dict], int]:
    """Fold scraped optical readings into the SNMP roster. Returns (rows, merged).

    SNMP stays authoritative for the roster itself — which ONUs exist, their
    state and their names. The scrape contributes only the numbers its firmware
    hides from SNMP, and only ONTO a slot the walk already reported, so this can
    never invent an ONU. That is what makes a partial scrape safe: a PON the
    scrape missed keeps whatever the walk said, instead of going blank.

    Merging happens by MAC, restricted to ONLINE slots. Both halves matter. The
    MAC is the ONU's physical identity, so it survives the PON/slot numbering of
    the two firmware subsystems disagreeing; the online restriction is what
    disposes of the C-Data zombie problem for free, since that reg table keeps
    every slot an ONU ever occupied (the byreddy fleet's 178 "duplicates") while
    the page only lists ONUs it just queried over the fibre. A MAC that is still
    ambiguous after that — a genuine clone or loop, the 2 real ones in that
    fleet — is SKIPPED, because we cannot tell which of two live ONUs answered
    and a reading attributed to the wrong drop sends a tech to the wrong house.

    A stale scrape is dropped wholesale rather than aged in: past max_age_s the
    numbers stop being evidence about now, and a badge is a claim about now.

    `now` is CENTRAL's clock, deliberately not the report's `ts`. That timestamp
    rides in from the edge's envelope, and `scraped_at` is written by central —
    judging one against the other would make a probe with a slow clock quietly
    discard perfectly fresh readings as future-dated. Freshness of a scrape is a
    fact about the clock that took it.
    """
    fresh = _fresh(scraped, now, max_age_s)
    if not fresh:
        return list(raw_onus), 0

    by_mac: dict[str, list[dict]] = {}
    for row in fresh:
        key = _match_key(row.get("serial"))
        if key:
            by_mac.setdefault(key, []).append(row)

    # Online roster slots per MAC, so a clone can be recognised as ambiguous
    # from the ROSTER side too, not just the scrape's.
    online_slots: dict[str, int] = {}
    for onu in raw_onus:
        if str(onu.get("state") or "") == "online":
            key = _match_key(onu.get("serial"))
            if key:
                online_slots[key] = online_slots.get(key, 0) + 1

    out: list[dict] = []
    merged = 0
    for onu in raw_onus:
        row = dict(onu)
        key = _match_key(row.get("serial"))
        cands = by_mac.get(key, []) if key else []
        if (cands and str(row.get("state") or "") == "online"
                and online_slots.get(key, 0) == 1):
            pick = cands[0] if len(cands) == 1 else _by_slot(cands, row)
            if pick is not None:
                took = False
                for fld in _MERGED_FIELDS:
                    val = pick.get(fld)
                    # None never overwrites: a column the page left blank is a
                    # gap in the scrape, not a claim that the walk's value is
                    # wrong. Same reason the scrape can't remove an ONU.
                    if val is not None:
                        row[fld] = val
                        took = True
                if took:
                    merged += 1
        out.append(row)
    return out, merged


def _by_slot(cands: list[dict], onu: dict) -> dict | None:
    """Disambiguate same-MAC scraped rows by the slot they came from, or give up.

    Reached when one MAC appears on several PONs of the same OLT. The roster's
    own identity is the slot (`onu_key`), so if exactly one candidate sits in
    this row's slot it is unambiguously the right reading.
    """
    key = str(onu.get("onu_key") or "")
    hits = [c for c in cands if str(c.get("onu_key") or "") == key]
    return hits[0] if len(hits) == 1 else None


def _redirect_path(current: str, location: str) -> str:
    """Resolve a Location against the request path, keeping it a PATH.

    The tunnel addresses a device by (ip, port, scheme) and takes a path — it has
    no notion of host — so an absolute redirect has to be reduced to its path.
    That is also the safety property: a device redirecting us to another host
    cannot make the edge fetch that host.
    """
    if location.startswith(("http://", "https://")):
        rest = location.split("://", 1)[1]
        location = "/" + rest.partition("/")[2]
    if location.startswith("/"):
        return location
    base = current.rsplit("/", 1)[0]
    return f"{base}/{location}" if base else f"/{location}"
