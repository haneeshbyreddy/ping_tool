from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time

from wisp.central import nvr_profiles as _profiles
from wisp.central.weboptics import Response, TunnelHttp, page_shape

log = logging.getLogger("wisp.central.nvr")

MAX_CHANNELS = 512

_KV_KEY_RE = re.compile(r"^([A-Za-z0-9_.]+)\[(\d+)\]\.([A-Za-z0-9_]+)$")
_KV_UUID_RE = re.compile(
    r"^([A-Za-z0-9_.]+)\.uuid:[A-Za-z0-9_-]*_(\d+)\.([A-Za-z0-9_]+)$")
_CHALLENGE_ITEM_RE = re.compile(
    r"([a-zA-Z0-9_-]+)\s*=\s*(?:\"([^\"]*)\"|([^\s,]+))")

_PLACEHOLDER_ADDRS = frozenset({"0.0.0.0", "192.168.0.0"})


def parse_kv(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key:
            out.append((key, val.strip()))
    return out


def _groups(pairs) -> dict[tuple[str, int], dict[str, str]]:
    out: dict[tuple[str, int], dict[str, str]] = {}
    for key, val in pairs:
        m = _KV_KEY_RE.match(key) or _KV_UUID_RE.match(key)
        if not m:
            continue
        prefix, idx, fld = m.group(1), int(m.group(2)), m.group(3).lower()
        out.setdefault((prefix, idx), {})[fld] = val
    return out


def _table(pairs, table_name: str) -> dict[int, dict[str, str]]:
    want = table_name.lower()
    out: dict[int, dict[str, str]] = {}
    for (prefix, idx), fields in _groups(pairs).items():
        tail = prefix.rsplit(".", 1)[-1].lower()
        if tail != want:
            continue
        out.setdefault(idx, {}).update(fields)
    return out


def _int_of(raw) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def parse_channels(text: str) -> list[dict]:
    rows: list[dict] = []
    for idx, fields in sorted(_table(parse_kv(text), "RemoteDevice").items()):
        address = (fields.get("address") or "").strip()
        if not address:
            continue
        enabled = (fields.get("enable") or "true").strip().lower() != "false"
        if not enabled and address in _PLACEHOLDER_ADDRS:
            continue
        if len(rows) >= MAX_CHANNELS:
            break
        kind = (fields.get("devicetype") or fields.get("protocoltype")
                or fields.get("type") or "").strip() or None
        name = ((fields.get("devicename") or "").strip()
                or (fields.get("name") or "").strip() or None)
        rows.append({
            "channel_no": idx,
            "name": name,
            "ip_address": address,
            "port": _int_of(fields.get("port")),
            "camera_kind": kind,
            "enabled": enabled,
            "state": "unknown",
        })
    return rows


def parse_names(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for idx, fields in _table(parse_kv(text), "ChannelTitle").items():
        name = (fields.get("name") or "").strip()
        if name:
            out[idx] = name
    return out


def parse_states(text: str, profile) -> dict[int, str]:
    out: dict[int, str] = {}
    for _, fields in _groups(parse_kv(text)).items():
        chan = _int_of(fields.get("channel"))
        raw = fields.get("connectionstate")
        if chan is None or raw is None:
            continue
        out[chan] = profile.state_of(raw)
    return out


_NO_EVENTS_RE = re.compile(r"^\s*Error:\s*No\s+Events?\s*$", re.I)
_EVENT_KEY_RE = re.compile(r"^events\[\d+\]$")


def parse_event_indexes(text: str) -> set[int] | None:
    if _NO_EVENTS_RE.match((text or "").strip()):
        return set()
    out: set[int] = set()
    seen = False
    found: int | None = None
    for key, val in parse_kv(text):
        if _EVENT_KEY_RE.match(key):
            seen = True
            iv = _int_of(val)
            if iv is not None:
                out.add(iv)
        elif key == "found":
            found = _int_of(val)
    if seen:
        return out
    if found == 0:
        return set()
    return None


def parse_challenge(header: str) -> dict[str, str] | None:
    text = str(header or "").strip()
    if not text.lower().startswith("digest"):
        return None
    out: dict[str, str] = {}
    for m in _CHALLENGE_ITEM_RE.finditer(text[6:]):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out if out.get("nonce") and out.get("realm") is not None else None


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest()


def digest_header(method: str, uri: str, username: str, password: str,
                  challenge: dict, nc: int = 1,
                  cnonce: str | None = None) -> str | None:
    algo = (challenge.get("algorithm") or "MD5").upper()
    if algo != "MD5":
        return None
    realm = challenge.get("realm") or ""
    nonce = challenge.get("nonce") or ""
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    qops = [q.strip().lower() for q in (challenge.get("qop") or "").split(",")]
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"',
             f'uri="{uri}"']
    if "auth" in qops:
        cnonce = cnonce or secrets.token_hex(8)
        ncv = f"{nc:08x}"
        response = _md5(f"{ha1}:{nonce}:{ncv}:{cnonce}:auth:{ha2}")
        parts += [f'response="{response}"', "qop=auth", f"nc={ncv}",
                  f'cnonce="{cnonce}"']
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")
        parts.append(f'response="{response}"')
    if challenge.get("opaque"):
        parts.append(f'opaque="{challenge["opaque"]}"')
    parts.append("algorithm=MD5")
    return "Digest " + ", ".join(parts)


class _DigestAuth:
    __slots__ = ("challenge", "nc")

    def __init__(self) -> None:
        self.challenge: dict | None = None
        self.nc = 0

    def header(self, method: str, uri: str, username: str,
               password: str) -> str | None:
        if not self.challenge:
            return None
        self.nc += 1
        return digest_header(method, uri, username, password,
                             self.challenge, self.nc)


def _digest_challenge(resp: Response) -> tuple[dict | None, str | None]:
    saw_basic = False
    for k, v in resp.headers:
        if k.lower() != "www-authenticate":
            continue
        low = v.strip().lower()
        if low.startswith("digest"):
            ch = parse_challenge(v)
            if ch:
                return ch, None
        elif low.startswith("basic"):
            saw_basic = True
    if saw_basic:
        return None, ("this build asks for HTTP Basic auth, not Digest; it "
                      "needs its own login flow in the profile")
    return None, ("the NVR answered 401 with no digest challenge this "
                  "parser recognises, so the credential was NOT sent")


def _authed_get(http: TunnelHttp, path: str, username: str, password: str,
                auth: _DigestAuth) -> tuple[Response, str | None]:
    hdrs: dict[str, str] = {}
    pre = auth.header("GET", path, username, password)
    if pre:
        hdrs["Authorization"] = pre
    resp = http.get(path, headers=hdrs)
    if resp.status != 401:
        return resp, None
    challenge, err = _digest_challenge(resp)
    if err:
        return resp, err
    if digest_header("GET", path, username, password, challenge) is None:
        algo = (challenge.get("algorithm") or "?")
        return resp, (f"the NVR wants {algo} digest auth, which this reader "
                      "does not speak, so the credential was NOT sent")
    auth.challenge, auth.nc = challenge, 0
    resp = http.get(path, headers={
        "Authorization": auth.header("GET", path, username, password) or ""})
    if resp.status == 401:
        auth.challenge, auth.nc = None, 0
        return resp, ("the NVR refused the stored login twice, so the "
                      "password looks wrong")
    return resp, None


RPC2_LOGIN_PATH = "/RPC2_Login"
RPC2_PATH = "/RPC2"


class Rpc2Error(RuntimeError):
    pass


def rpc2_password_hash(username: str, password: str, realm: str,
                       random: str) -> str:
    ha = _md5(f"{username}:{realm}:{password}").upper()
    return _md5(f"{username}:{random}:{ha}").upper()


class Rpc2:

    def __init__(self, http: TunnelHttp) -> None:
        self.http = http
        self.session: str | int | None = None
        self._id = 0

    def call(self, method: str, params, path: str = RPC2_PATH) -> dict:
        self._id += 1
        payload: dict = {"method": method, "params": params, "id": self._id}
        if self.session is not None:
            payload["session"] = self.session
        resp = self.http.request(
            "POST", path, headers={"Content-Type": "application/json"},
            body=json.dumps(payload).encode())
        if not resp.ok:
            raise Rpc2Error(f"{method}: {resp.error or resp.status}")
        try:
            out = json.loads(resp.body.decode("utf-8", "replace"))
        except (TypeError, ValueError):
            raise Rpc2Error(f"{method}: the reply was not JSON "
                            f"({page_shape(resp.text(2000))})")
        if not isinstance(out, dict):
            raise Rpc2Error(f"{method}: unexpected reply shape")
        if out.get("session") is not None:
            self.session = out["session"]
        return out

    def login(self, username: str, password: str) -> None:
        first = self.call("global.login", {
            "userName": username, "password": "", "clientType": "Web3.0",
            "loginType": "Direct"}, path=RPC2_LOGIN_PATH)
        params = first.get("params") or {}
        realm = params.get("realm")
        random = params.get("random")
        encryption = str(params.get("encryption") or "Default")
        if first.get("result") is True:
            return
        if not realm or not random:
            raise Rpc2Error("the login challenge carried no realm/random, so "
                            "the credential was NOT sent")
        if encryption != "Default":
            raise Rpc2Error(f"this build wants {encryption!r} RPC2 login, "
                            "which this reader does not speak; the credential "
                            "was NOT sent")
        pwd = rpc2_password_hash(username, password, realm, random)
        second = self.call("global.login", {
            "userName": username, "password": pwd, "clientType": "Web3.0",
            "loginType": "Direct", "authorityType": "Default",
            "passwordType": "Default"}, path=RPC2_LOGIN_PATH)
        if second.get("result") is not True:
            err = (second.get("error") or {}).get("message") or "refused"
            raise Rpc2Error(f"the NVR refused the RPC2 login: {err}")

    def logout(self) -> None:
        try:
            self.call("global.logout", None)
        except Exception:
            pass


def parse_rpc2_states(reply: dict, profile
                      ) -> tuple[dict[int, str] | None, set[str]]:
    params = reply.get("params")
    states = params.get("states") if isinstance(params, dict) else None
    if not isinstance(states, list):
        return None, set()
    out: dict[int, str] = {}
    unmapped: set[str] = set()
    for item in states:
        if not isinstance(item, dict):
            continue
        chan = _int_of(item.get("channel"))
        raw = item.get("connectionState")
        if chan is None or raw is None:
            continue
        word = str(raw)
        out[chan] = profile.state_of(word)
        if out[chan] == "unknown" and word.strip():
            unmapped.add(word.strip())
    return (out if out else None), unmapped


def rpc2_camera_states(http: TunnelHttp, username: str, password: str,
                       profile) -> tuple[dict[int, str] | None, str | None]:
    rpc = Rpc2(http)
    try:
        rpc.login(username, password)
    except Rpc2Error as exc:
        return None, f"RPC2 login failed: {exc}"
    try:
        reply = rpc.call("LogicDeviceManager.getCameraState",
                         {"uniqueChannels": [-1]})
        states, unmapped = parse_rpc2_states(reply, profile)
        if states is None:
            err = (reply.get("error") or {}).get("message")
            return None, ("the camera-state reply carried no states"
                          + (f" ({err})" if err else ""))
        err = None
        if unmapped:
            words = ", ".join(repr(w) for w in sorted(unmapped))
            err = (f"the NVR reports state word(s) this profile's map does "
                   f"not know: {words} — those channels show unknown")
        return states, err
    except Rpc2Error as exc:
        return None, str(exc)
    finally:
        rpc.logout()


MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
SNAPSHOT_TIMEOUT_S = 30.0
SNAPSHOT_ATTEMPTS = 3
SNAPSHOT_RETRY_PAUSE_S = 1.5


def fetch_snapshot(http: TunnelHttp, username: str, password: str,
                   channel_no: int, profile=None
                   ) -> tuple[bytes | None, str | None]:
    prof = profile if profile is not None else _profiles.builtin("cpplus")
    path = prof.snapshot_url(channel_no)
    if not path:
        return None, "this NVR's recipe has no snapshot page"
    auth = _DigestAuth()
    tried = 0
    for attempt in range(SNAPSHOT_ATTEMPTS):
        tried += 1
        resp, err = _authed_get(http, path, username, password, auth)
        if err:
            return None, err
        if resp.status == 400:
            return None, ("the NVR refuses a frame for this channel — it is "
                          "dark or empty, so there is no video to grab")
        if resp.status == 0 or resp.error:
            return None, ("the camera did not answer the NVR's frame request "
                          f"within {int(http.timeout_s)}s. Some camera models "
                          "never serve snapshots — the channel can still be "
                          "live")
        if resp.status >= 500:
            if attempt + 1 < SNAPSHOT_ATTEMPTS:
                time.sleep(SNAPSHOT_RETRY_PAUSE_S)
            continue
        if not resp.ok:
            return None, f"the frame request failed: {resp.status}"
        if resp.body[:2] != b"\xff\xd8":
            return None, ("the reply was not an image "
                          f"({page_shape(resp.text(500))})")
        if len(resp.body) > MAX_SNAPSHOT_BYTES:
            return None, "the frame is implausibly large — refused"
        return resp.body, None
    return None, ("the camera did not answer the NVR's frame request "
                  f"({tried} tries). Some camera models never serve "
                  "snapshots — the channel can still be live")


def read_channels(http: TunnelHttp, username: str, password: str,
                  profile=None) -> tuple[list[dict] | None, str | None]:
    prof = profile if profile is not None else _profiles.builtin("cpplus")
    auth = _DigestAuth()

    resp, err = _authed_get(http, prof.channels_path, username, password, auth)
    if err:
        return None, err
    if resp.status == 404:
        return None, (f"this build has no {prof.channels_path.split('?')[0]} "
                      "(404). It answers on a web UI but does not carry that "
                      "channel table; it needs its own capture and profile")
    if not resp.ok:
        return None, (f"could not open the channel table: "
                      f"{resp.error or resp.status}")
    text = resp.body.decode(prof.charset, "replace")
    chans = parse_channels(text)
    if not chans:
        return [], ("the reply carried no channel rows this profile could "
                    "read: either no IP cameras are configured, or this "
                    f"build prints its table differently ({page_shape(text)})")

    if prof.names_path:
        r2, e2 = _authed_get(http, prof.names_path, username, password, auth)
        if e2 is None and r2.ok:
            names = parse_names(r2.body.decode(prof.charset, "replace"))
            for row in chans:
                row["name"] = names.get(row["channel_no"]) or row["name"]

    state_err = None
    if prof.state_format == "rpc2-camerastate":
        states, state_err = rpc2_camera_states(http, username, password, prof)
        if states is not None:
            unnamed = []
            for row in chans:
                row["state"] = states.get(row["channel_no"], "unknown")
                if row["enabled"] and row["channel_no"] not in states:
                    unnamed.append(channel_label(row))
            if unnamed and not state_err:
                state_err = ("the state reply named no state for "
                             + ", ".join(unnamed[:6]) + " — shown unknown")
        else:
            state_err = ("the channel list was read but camera states could "
                         f"not be: {state_err}. States show unknown")
    elif prof.state_path:
        r3, e3 = _authed_get(http, prof.state_path, username, password, auth)
        if e3 is not None or not r3.ok:
            state_err = ("the channel list was read but camera states could "
                         "not be: "
                         f"{e3 or r3.error or r3.status}. States show unknown")
        elif prof.state_format == "event-indexes":
            body = r3.body.decode(prof.charset, "replace")
            lost = parse_event_indexes(body)
            if lost is None:
                state_err = ("the channel list was read but the video-loss "
                             f"reply was not recognised ({page_shape(body)}). "
                             "States show unknown")
            else:
                for row in chans:
                    if not row["enabled"]:
                        continue
                    row["state"] = ("offline" if row["channel_no"] in lost
                                    else "online")
        else:
            states = parse_states(r3.body.decode(prof.charset, "replace"), prof)
            for row in chans:
                row["state"] = states.get(row["channel_no"], "unknown")
    return chans, state_err


def transitions(prior: dict[int, str], rows: list[dict],
                unwatched: frozenset[int] | set[int] = frozenset()
                ) -> dict[str, list[dict]]:
    dark: list[dict] = []
    restored: list[dict] = []
    for row in rows or ():
        if not row.get("enabled") or row["channel_no"] in unwatched:
            continue
        before = prior.get(row["channel_no"])
        now = row.get("state")
        if before == "online" and now == "offline":
            dark.append(row)
        elif before == "offline" and now == "online":
            restored.append(row)
    return {"dark": dark, "restored": restored}


def channel_label(row: dict) -> str:
    label = f"CH{int(row.get('channel_no', 0)) + 1}"
    name = (row.get("name") or "").strip()
    return f"{label} {name}" if name else label


def batch_detail(rows: list[dict], limit: int = 4) -> str:
    labels = [channel_label(r) for r in rows[:limit]]
    extra = len(rows) - len(labels)
    text = ", ".join(labels)
    return f"{text} +{extra}" if extra > 0 else text
