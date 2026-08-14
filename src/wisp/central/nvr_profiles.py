from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from wisp.central.inventory import InventoryError
from wisp.central.weboptics_profiles import CHARSETS

LOGIN_FLOWS = ("digest",)
FORMATS = ("kv",)
STATE_FORMATS = ("camerastate", "event-indexes", "rpc2-camerastate")
STATES = ("online", "offline", "unknown")

_QUERY_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?\[\]{}-]{0,300}$")


def _clean_query_path(raw, field_name: str, *, required: bool = True) -> str:
    path = str(raw or "").strip()
    if not path:
        if required:
            raise InventoryError(f"{field_name} is required")
        return ""
    if "://" in path or path.startswith("//"):
        raise InventoryError(
            f"{field_name} must be a path on the NVR (like /cgi-bin/...), "
            "not a full URL. The tunnel supplies the address itself.")
    if "\\" in path or ".." in path:
        raise InventoryError(f"{field_name} must not contain '..' or backslashes")
    if not _QUERY_PATH_RE.match(path):
        raise InventoryError(
            f"{field_name} must start with '/' and be a plain URL path")
    return path


def _clean_state_map(raw) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise InventoryError(
            "state_map must map the NVR's own state words to online/offline/unknown")
    out: dict[str, str] = {}
    for key, val in raw.items():
        word = str(key or "").strip().lower()
        state = str(val or "").strip().lower()
        if not word:
            continue
        if len(word) > 40:
            raise InventoryError("state_map keys must be 40 characters or fewer")
        if state not in STATES:
            raise InventoryError(
                f"state_map.{word} must be one of: {', '.join(STATES)}")
        out[word] = state
    return out


def clean_nvr_profile_payload(data: dict) -> dict:
    name = str(data.get("name") or "").strip().lower()
    if not name:
        raise InventoryError("profile name is required")
    if len(name) > 64:
        raise InventoryError("profile name must be 64 characters or fewer")
    if not re.match(r"^[a-z0-9][a-z0-9_.-]*$", name):
        raise InventoryError(
            "profile name must be lowercase letters, digits, '.', '_' or '-'")

    flow = str(data.get("login_flow") or "digest").strip().lower()
    if flow not in LOGIN_FLOWS:
        raise InventoryError(
            f"login_flow must be one of: {', '.join(LOGIN_FLOWS)}")

    fmt = str(data.get("format") or "kv").strip().lower()
    if fmt not in FORMATS:
        raise InventoryError(f"format must be one of: {', '.join(FORMATS)}")

    charset = str(data.get("charset") or "utf-8").strip().lower()
    if charset not in CHARSETS:
        raise InventoryError(f"charset must be one of: {', '.join(CHARSETS)}")

    state_format = str(data.get("state_format") or "camerastate").strip().lower()
    if state_format not in STATE_FORMATS:
        raise InventoryError(
            f"state_format must be one of: {', '.join(STATE_FORMATS)}")

    snapshot_path = _clean_query_path(
        data.get("snapshot_path"), "snapshot_path", required=False)
    if snapshot_path and "{channel}" not in snapshot_path:
        raise InventoryError(
            "snapshot_path must carry a {channel} placeholder")
    try:
        snapshot_base = int(data.get("snapshot_channel_base", 1))
    except (TypeError, ValueError):
        raise InventoryError("snapshot_channel_base must be 0 or 1")
    if snapshot_base not in (0, 1):
        raise InventoryError("snapshot_channel_base must be 0 or 1")

    spec = {
        "login_flow": flow,
        "format": fmt,
        "charset": charset,
        "state_format": state_format,
        "channels_path": _clean_query_path(
            data.get("channels_path"), "channels_path"),
        "names_path": _clean_query_path(
            data.get("names_path"), "names_path", required=False),
        "state_path": _clean_query_path(
            data.get("state_path"), "state_path", required=False),
        "state_map": _clean_state_map(data.get("state_map")),
        "snapshot_path": snapshot_path,
        "snapshot_channel_base": snapshot_base,
    }
    enabled = str(data.get("enabled", 1)) not in ("0", "false", "False", "", "None")
    return {"name": name, "spec": spec, "enabled": enabled}


@dataclass(frozen=True, slots=True)
class NvrProfile:
    name: str
    login_flow: str
    format: str
    charset: str
    channels_path: str
    names_path: str
    state_path: str
    state_format: str
    state_map: dict
    snapshot_path: str = ""
    snapshot_channel_base: int = 1

    def snapshot_url(self, channel_no: int) -> str | None:
        if not self.snapshot_path:
            return None
        return self.snapshot_path.replace(
            "{channel}", str(int(channel_no) + self.snapshot_channel_base))

    def state_of(self, raw: str) -> str:
        word = str(raw or "").strip().lower()
        if not word:
            return "unknown"
        return self.state_map.get(word, "unknown")


def profile_from_spec(name: str, spec: dict) -> NvrProfile:
    clean = clean_nvr_profile_payload({"name": name, **(spec or {})})
    s = clean["spec"]
    return NvrProfile(
        name=clean["name"], login_flow=s["login_flow"], format=s["format"],
        charset=s["charset"], channels_path=s["channels_path"],
        names_path=s["names_path"], state_path=s["state_path"],
        state_format=s["state_format"], state_map=dict(s["state_map"]),
        snapshot_path=s["snapshot_path"],
        snapshot_channel_base=s["snapshot_channel_base"])


BUILTIN_SPECS: dict[str, dict] = {
    "cpplus": {
        "login_flow": "digest",
        "format": "kv",
        "charset": "utf-8",
        "channels_path":
            "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice",
        "names_path":
            "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle",
        "state_path": "",
        "state_format": "rpc2-camerastate",
        "state_map": {"connected": "online", "unconnected": "offline",
                      "unconnect": "offline"},
        "snapshot_path": "/cgi-bin/snapshot.cgi?channel={channel}",
        "snapshot_channel_base": 1,
    },
}
BUILTIN_SPECS["dahua"] = dict(BUILTIN_SPECS["cpplus"])


def builtin(name: str) -> NvrProfile | None:
    spec = BUILTIN_SPECS.get(str(name or "").strip().lower())
    return profile_from_spec(name, spec) if spec else None


def builtin_names() -> tuple[str, ...]:
    return tuple(BUILTIN_SPECS)


@dataclass(slots=True)
class ProfileSet:

    _by_org: dict[tuple[str | None, str], NvrProfile] = field(default_factory=dict)
    _disabled: set[tuple[str | None, str]] = field(default_factory=set)

    @classmethod
    def build(cls, rows) -> "ProfileSet":
        out = cls()
        for row in rows or ():
            name = str(row.get("name") or "").strip().lower()
            if not name:
                continue
            org = row.get("org_id") or None
            if not row.get("enabled", True):
                out._disabled.add((org, name))
                continue
            spec = row.get("spec")
            if isinstance(spec, str):
                try:
                    spec = json.loads(spec)
                except (TypeError, ValueError):
                    continue
            try:
                out._by_org[(org, name)] = profile_from_spec(name, spec)
            except InventoryError:
                continue
        return out

    def resolve(self, org_id: str | None, name: str) -> NvrProfile | None:
        key = str(name or "").strip().lower()
        if not key:
            return None
        for scope in (org_id, None):
            if (scope, key) in self._disabled:
                return None
            hit = self._by_org.get((scope, key))
            if hit is not None:
                return hit
        return builtin(key)

    def names(self) -> tuple[str, ...]:
        seen = {n for _, n in self._by_org}
        seen.update(builtin_names())
        seen.difference_update(
            n for org, n in self._disabled
            if org is None and (None, n) not in self._by_org)
        return tuple(sorted(seen))
