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

MAX_REDIRECTS = 5
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
        for k, v in headers:
            if k.lower() != "set-cookie":
                continue
            try:
                jar = SimpleCookie()
                jar.load(v)
            except Exception:
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
            if status in (301, 302, 303):
                method, body = "GET", b""

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post_form(self, path: str, fields: dict[str, str], **kw) -> Response:
        from urllib.parse import urlencode
        body = urlencode(fields).encode()
        hdrs = {"Content-Type": "application/x-www-form-urlencoded", **kw.pop("headers", {})}
        return self.request("POST", path, headers=hdrs, body=body, **kw)


DBC = _profiles.builtin("dbc")
OPM_PATH = DBC.optics_path
OPM_STATIC_FIELDS = dict(DBC.optics_static)
OPM_CHARSET = DBC.charset

_SESSION_KEY_RE = DBC.session_key_re()


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
    return [name for name, rx in _key_shapes_for(field_name) if rx.search(html)]


def session_key(html: str, profile=None) -> str | None:
    rx = profile.session_key_re() if profile is not None else _SESSION_KEY_RE
    m = rx.search(html)
    return m.group(1) if m else None


def optics_headings_seen(html: str, profile=None) -> tuple[list[str], int]:

    prof = profile if profile is not None else DBC
    cols = prof.columns or {}
    low = html.lower()
    return ([h for h in cols.values() if h and h.strip().lower() in low], len(cols))


def diagnose_login(html: str, profile=None) -> str:


    prof = profile if profile is not None else DBC
    key_field = prof.session_key_field
    if not html.strip():
        return "the device returned an empty body"
    low = html.lower()
    login_hint = prof.login_page_path.strip("/").lower().rsplit(".", 1)[0]
    has_password_input = re.search(
        r"""<input[^>]*type\s*=\s*['"]?password""", low) is not None
    looks_like_login = (bool(login_hint) and login_hint in low
                        or has_password_input
                        or ("password" in low and "form" in low))
    if key_field in html:
        seen = key_shapes(html, key_field)
        if re.search(rf"{re.escape(key_field)}\.value\s*=\s*(''|\"\")", html):
            return (f"the page ships an EMPTY {key_field}. This firmware "
                    "blanks the token until a session exists, so we are not "
                    "logged in. Either the stored password was refused, or "
                    "someone else holds this OLT's single web session "
                    f"({page_shape(html)})")
        if "js-single-quote" in seen:
            return (f"the page carries {key_field} in the expected form but no "
                    "value could be read out of it: the token is blank or "
                    f"malformed, so the session never started ({page_shape(html)})")
        shapes = ", ".join(seen) or "no form this parser recognises"
        why = ("; it ALSO looks like the login page, so the likelier reading is "
               "a refused password than a parser gap" if looks_like_login else "")
        return (f"the reply carries {key_field} as [{shapes}] but not as "
                f"[js-single-quote], which is the only form read{why} "
                f"({page_shape(html)})")
    if looks_like_login:
        return "the device served its login page again, so the password was refused"
    headings, wanted = optics_headings_seen(html, prof)
    if wanted and len(headings) == wanted:
        return (f"this IS the optical page — all {wanted} column headings are on "
                f"it — but it carries no {key_field}. This build mints no token "
                f"({page_shape(html)})")
    if len(headings) >= 3:
        return (f"this looks like the optical page but only {len(headings)} of "
                f"{wanted} column headings matched, and there is no {key_field} "
                "on it either, so it was not read: a renamed column is a profile "
                "fault to fix, not a page to guess at. Check this profile's "
                f"column names against this build ({page_shape(html)})")
    if prof.vendor_markers and not any(m in low for m in prof.vendor_markers):
        return ("the reply does not look like this OLT's web UI at all. Check "
                "the address really reaches the OLT and not a router in front.")
    return (f"unrecognised reply, and it is NOT the optical page (none of this "
            f"profile's column headings are on it) · {page_shape(html)}")


_TITLE_RE = re.compile(r"<title[^>]*>(.{0,80}?)</title>", re.I | re.S)
_FRAME_SRC_RE = re.compile(r"<frame[^>]+src\s*=\s*[\"']?([^\"'\s>]+)", re.I)


def page_shape(html: str) -> str:

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

    return "".join(c for c in (text or "").lower() if c.isalnum())


def _column_index(rows: list[list[str]], profile) -> dict[str, int] | None:


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


    prof = profile if profile is not None else DBC
    parser = _TableRows()
    parser.feed(html)
    index = _column_index(parser.rows, prof)
    if index is None:
        if not prof.column_order:
            return []
        index = {f: i for i, f in enumerate(prof.column_order) if f}
    need = max(index.values()) + 1
    anchor_at = index["onu_ref"]
    anchor_re = prof.anchor_re
    expect_voltage = "voltage_v" in index
    out: list[dict] = []
    for cells in parser.rows:
        if len(cells) < need:
            continue
        m = anchor_re.match(cells[anchor_at].strip())
        if not m:
            continue
        if prof.onu_id_shape == "pon-colon-onu":
            pon_label, onu_id = m.group(1), int(m.group(2))
            pon_index = pon_label.rsplit("/", 1)[-1]
        else:
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
        if isinstance(row.get("serial"), str):
            row["serial"] = row["serial"].upper()
        out.append(_sane_optics(row, expect_voltage))
    return out


def parse_opm_diag(html: str, profile=None, pon: int | None = None) -> list[dict]:
    return parse_optics_table(html, profile, pon)


_VOLT_MIN_V, _VOLT_MAX_V = 2.0, 5.0
from wisp.central.optics import (  # noqa: E402
    RX_FLOOR_DBM as _RX_FLOOR_DBM,
    RX_MAX_DBM as _RX_MAX_DBM,
)


def _sane_optics(row: dict, expect_voltage: bool = True) -> dict:


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

    try:
        val = float(raw.strip())
    except (TypeError, ValueError, AttributeError):
        return None
    if not math.isfinite(val):
        return None
    return cast(val)


def opm_form(pon: int, key: str, profile=None) -> dict[str, str]:
    return (profile if profile is not None else DBC).optics_form(pon, key)


LOGIN_PATH = DBC.login_path
LOGIN_PAGE_PATH = DBC.login_page_path
DEFAULT_PONS = DBC.default_pons
MAX_PON_INDEX = 64
_PON_LABEL_RE = re.compile(r"^[A-Za-z]+\d+/(\d+)$")


def pon_indices(labels) -> tuple[int, ...]:

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
    charset = (profile.charset if profile is not None else OPM_CHARSET)
    return resp.body.decode(charset, "replace")


def login(http: TunnelHttp, username: str, password: str, profile) -> str | None:


    entry = http.get(profile.login_page_path)
    if not entry.ok:
        return (f"no login page at {profile.login_page_path} "
                f"({entry.error or entry.status}), so credentials NOT sent; "
                "check the address really reaches the OLT's web UI")
    reply = http.post_form(profile.login_path,
                           profile.login_form(username, password))
    if not reply.ok:
        return f"login failed: {reply.error or reply.status}"
    return None


def scrape_optics(http: TunnelHttp, username: str, password: str,
                  profile=None, pons=None,
                  deadline: float | None = None) -> tuple[list[dict], str | None]:


    prof = profile if profile is not None else DBC
    pons = tuple(pons) if pons else prof.default_pons
    err = login(http, username, password, prof)
    if err:
        return [], err

    opened = http.get(prof.optics_path)
    if opened.status == 404:
        return [], (f"this OLT's firmware has no {prof.optics_path} (404). It "
                    "answers on the vendor's web UI but does not carry that "
                    "optical page; it needs its own capture and profile")
    if not opened.ok:
        return [], f"could not open the optics page: {opened.error or opened.status}"
    keyless = False
    key = session_key(_decode(opened, prof), prof) if prof.rotates_key else None
    if prof.rotates_key and not key:
        page = _decode(opened, prof)
        seen, wanted = optics_headings_seen(page, prof)
        if not (wanted and len(seen) == wanted):
            return [], f"login rejected: {diagnose_login(page, prof)}"
        keyless = True
        log.info("web optics: %s serves the optical page with no %s but all %d"
                 " headings — reading it keyless", prof.optics_path,
                 prof.session_key_field, wanted)

    rows: list[dict] = []
    for done, pon in enumerate(pons):
        if deadline is not None and time.monotonic() >= deadline:
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
            seen, wanted = optics_headings_seen(page, prof)
            if len(seen) != wanted:
                return rows, (f"PON{pon}: the optical page stopped being itself "
                              f"({len(seen)} of {wanted} headings) — session lost"
                              " to another login?")
        else:
            nxt = session_key(page, prof) if prof.rotates_key else key
            if prof.rotates_key and not nxt:
                return rows, f"PON{pon}: session lost (someone else logged in?)"
            key = nxt
        rows.extend(parse_optics_table(page, prof, pon))
    return rows, None


def scrape_opm(http: TunnelHttp, username: str, password: str,
               pons=None, profile=None,
               deadline: float | None = None) -> tuple[list[dict], str | None]:
    return scrape_optics(http, username, password, profile, pons, deadline)


def _match_key(raw: str | None) -> str:


    return "".join(c for c in (raw or "") if c.isalnum()).upper()


_MERGED_FIELDS = ("rx_dbm", "tx_dbm")


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
            continue
        if -_CLOCK_GRACE_S <= age <= max_age_s:
            out.append(row)
    return out


def merge_scraped(raw_onus: list[dict], scraped: list[dict], now: str,
                  max_age_s: float) -> tuple[list[dict], int]:


    fresh = _fresh(scraped, now, max_age_s)
    if not fresh:
        return list(raw_onus), 0

    by_mac: dict[str, list[dict]] = {}
    for row in fresh:
        key = _match_key(row.get("serial"))
        if key:
            by_mac.setdefault(key, []).append(row)

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
                    if val is not None:
                        row[fld] = val
                        took = True
                if took:
                    merged += 1
        out.append(row)
    return out, merged


def _by_slot(cands: list[dict], onu: dict) -> dict | None:

    key = str(onu.get("onu_key") or "")
    hits = [c for c in cands if str(c.get("onu_key") or "") == key]
    return hits[0] if len(hits) == 1 else None


def _redirect_path(current: str, location: str) -> str:

    if location.startswith(("http://", "https://")):
        rest = location.split("://", 1)[1]
        location = "/" + rest.partition("/")[2]
    if location.startswith("/"):
        return location
    base = current.rsplit("/", 1)[0]
    return f"{base}/{location}" if base else f"/{location}"
