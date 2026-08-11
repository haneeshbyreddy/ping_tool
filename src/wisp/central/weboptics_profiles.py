from __future__ import annotations

import re
from dataclasses import dataclass, field

from wisp.central.inventory import InventoryError

FIELDS = ("onu_ref", "serial", "name", "distance_m", "temp_c", "voltage_v",
          "tx_bias_ma", "tx_dbm", "rx_dbm")
REQUIRED_FIELDS = ("onu_ref", "serial")
_INT_FIELDS = ("distance_m",)
_FLOAT_FIELDS = ("temp_c", "voltage_v", "tx_bias_ma", "tx_dbm", "rx_dbm")

ONU_ID_SHAPES = ("pon-colon-onu", "onu-index")
_SHAPE_RE = {
    "pon-colon-onu": re.compile(r"^([A-Za-z]+\d+/\d+):(\d+)$"),
    "onu-index": re.compile(r"^(\d+)$"),
}

SESSION_STRATEGIES = ("rotating-key", "cookie")

OPTICS_METHODS = ("POST", "GET")

CHARSETS = ("utf-8", "gb2312", "gbk", "big5", "latin-1", "iso-8859-1",
            "shift_jis", "euc-kr")

MAX_PONS = 64
_MAX_STATIC_FIELDS = 12
_MAX_MARKERS = 8
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,200}$")
_FORM_FIELD_RE = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,64}$")


def _clean_path(raw, field_name: str) -> str:

    path = str(raw or "").strip()
    if not path:
        raise InventoryError(f"{field_name} is required")
    if "://" in path or path.startswith("//"):
        raise InventoryError(
            f"{field_name} must be a path on the device (like /action/opm.html), "
            "not a full URL. The tunnel supplies the address itself.")
    if "\\" in path or ".." in path:
        raise InventoryError(f"{field_name} must not contain '..' or backslashes")
    if not _PATH_RE.match(path):
        raise InventoryError(
            f"{field_name} must start with '/' and be a plain URL path")
    return path


def _clean_form_field(raw, field_name: str) -> str:
    val = str(raw or "").strip()
    if not _FORM_FIELD_RE.match(val):
        raise InventoryError(
            f"{field_name} must be a form field name (letters, digits, . _ - [ ])")
    return val


def _clean_static(raw, field_name: str) -> dict[str, str]:

    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise InventoryError(f"{field_name} must be an object of form fields")
    if len(raw) > _MAX_STATIC_FIELDS:
        raise InventoryError(
            f"{field_name} may hold at most {_MAX_STATIC_FIELDS} fields")
    out: dict[str, str] = {}
    for key, val in raw.items():
        name = _clean_form_field(key, f"{field_name} field name")
        text = str("" if val is None else val)
        if len(text) > 200:
            raise InventoryError(f"{field_name}.{name} must be 200 characters or fewer")
        out[name] = text
    return out


def _clean_columns(raw) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise InventoryError("columns must map a reading to its table heading")
    out: dict[str, str] = {}
    for key, val in raw.items():
        if key not in FIELDS:
            raise InventoryError(
                f"unknown column {key!r}: must be one of {', '.join(FIELDS)}")
        head = str(val or "").strip()
        if not head:
            continue
        if len(head) > 64:
            raise InventoryError(f"columns.{key} must be 64 characters or fewer")
        out[key] = head
    return out


def _clean_order(raw) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise InventoryError("column_order must be a list of readings, in table order")
    out: list[str] = []
    for item in raw:
        val = str(item or "").strip()
        if val and val not in FIELDS:
            raise InventoryError(
                f"unknown column {val!r} in column_order: must be one of "
                f"{', '.join(FIELDS)} (or blank to skip a column)")
        out.append(val)
    if len(out) > 40:
        raise InventoryError("column_order may list at most 40 columns")
    return tuple(out)


def _clean_pons(raw) -> tuple[int, ...]:

    if raw in (None, ""):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise InventoryError("default_pons must be a list of PON port numbers")
    out: list[int] = []
    for item in raw:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            raise InventoryError("default_pons must contain whole numbers")
        if not 1 <= idx <= MAX_PONS:
            raise InventoryError(f"PON numbers must be between 1 and {MAX_PONS}")
        out.append(idx)
    if not out:
        raise InventoryError("default_pons must list at least one PON")
    return tuple(sorted(set(out)))


def _clean_markers(raw) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, (list, tuple)):
        raise InventoryError("vendor_markers must be a list of words")
    out = []
    for item in raw[:_MAX_MARKERS]:
        val = str(item or "").strip().lower()
        if val:
            out.append(val[:32])
    return tuple(out)


def clean_web_optics_profile_payload(data: dict) -> dict:
    name = str(data.get("name") or "").strip().lower()
    if not name:
        raise InventoryError("profile name is required")
    if len(name) > 64:
        raise InventoryError("profile name must be 64 characters or fewer")
    if not re.match(r"^[a-z0-9][a-z0-9_.-]*$", name):
        raise InventoryError(
            "profile name must be lowercase letters, digits, '.', '_' or '-'")

    session = str(data.get("session") or "rotating-key").strip().lower()
    if session not in SESSION_STRATEGIES:
        raise InventoryError(
            f"session must be one of: {', '.join(SESSION_STRATEGIES)}")
    method = str(data.get("optics_method") or "POST").strip().upper()
    if method not in OPTICS_METHODS:
        raise InventoryError(f"optics_method must be one of: {', '.join(OPTICS_METHODS)}")
    charset = str(data.get("charset") or "utf-8").strip().lower()
    if charset not in CHARSETS:
        raise InventoryError(f"charset must be one of: {', '.join(CHARSETS)}")
    shape = str(data.get("onu_id_shape") or "pon-colon-onu").strip().lower()
    if shape not in ONU_ID_SHAPES:
        raise InventoryError(f"onu_id_shape must be one of: {', '.join(ONU_ID_SHAPES)}")

    columns = _clean_columns(data.get("columns"))
    order = _clean_order(data.get("column_order"))
    if not columns and not order:
        raise InventoryError(
            "the profile must say where the readings are: map columns by "
            "heading, or give the table's column order")
    for required in REQUIRED_FIELDS:
        if required not in columns and required not in order:
            raise InventoryError(
                f"the {required!r} column is required: without it a data row "
                "cannot be told from a layout row, or a reading from a slot")
    if "rx_dbm" not in columns and "rx_dbm" not in order:
        raise InventoryError(
            "the profile must locate 'rx_dbm': per-ONU received power is the "
            "reading this whole subsystem exists to recover")

    pon_label = str(data.get("pon_label") or "").strip()
    if shape == "onu-index":
        if not pon_label or "{pon}" not in pon_label:
            raise InventoryError(
                "with onu_id_shape 'onu-index' the page never names the PON, so "
                "pon_label must supply it (e.g. 'GPON0/{pon}')")
    if len(pon_label) > 32:
        raise InventoryError("pon_label must be 32 characters or fewer")

    spec = {
        "login_page_path": _clean_path(data.get("login_page_path"), "login_page_path"),
        "login_path": _clean_path(data.get("login_path"), "login_path"),
        "optics_path": _clean_path(data.get("optics_path"), "optics_path"),
        "username_field": _clean_form_field(
            data.get("username_field") or "user", "username_field"),
        "password_field": _clean_form_field(
            data.get("password_field") or "pass", "password_field"),
        "login_static": _clean_static(data.get("login_static"), "login_static"),
        "session": session,
        "session_key_field": _clean_form_field(
            data.get("session_key_field") or "SessionKey", "session_key_field"),
        "optics_method": method,
        "pon_field": _clean_form_field(data.get("pon_field") or "select", "pon_field"),
        "optics_static": _clean_static(data.get("optics_static"), "optics_static"),
        "charset": charset,
        "onu_id_shape": shape,
        "pon_label": pon_label,
        "columns": columns,
        "column_order": list(order),
        "default_pons": list(_clean_pons(data.get("default_pons")) or (1, 2, 3, 4)),
        "vendor_markers": list(_clean_markers(data.get("vendor_markers"))),
    }
    enabled = str(data.get("enabled", 1)) not in ("0", "false", "False", "", "None")
    return {"name": name, "spec": spec, "enabled": enabled}


@dataclass(frozen=True, slots=True)
class WebOpticsProfile:
    name: str
    login_page_path: str
    login_path: str
    optics_path: str
    username_field: str
    password_field: str
    login_static: dict
    session: str
    session_key_field: str
    optics_method: str
    pon_field: str
    optics_static: dict
    charset: str
    onu_id_shape: str
    pon_label: str
    columns: dict
    column_order: tuple
    default_pons: tuple
    vendor_markers: tuple = ()

    @property
    def anchor_re(self) -> re.Pattern:
        return _SHAPE_RE[self.onu_id_shape]

    @property
    def rotates_key(self) -> bool:
        return self.session == "rotating-key"

    def login_form(self, username: str, password: str) -> dict[str, str]:
        return {self.username_field: username, self.password_field: password,
                **self.login_static}

    def optics_form(self, pon: int, key: str | None) -> dict[str, str]:
        form = {self.pon_field: str(pon), **self.optics_static}
        if key is not None and self.rotates_key:
            form[self.session_key_field] = key
        return form

    def session_key_re(self) -> re.Pattern:
        return re.compile(
            rf"{re.escape(self.session_key_field)}\.value\s*=\s*'([^']+)'")

    def cast(self, field_name: str, raw: str):
        from wisp.central.weboptics import _num
        if field_name in _INT_FIELDS:
            return _num(raw, int)
        if field_name in _FLOAT_FIELDS:
            return _num(raw, float)
        text = str(raw or "").strip()
        return text or None


def profile_from_spec(name: str, spec: dict) -> WebOpticsProfile:

    clean = clean_web_optics_profile_payload({"name": name, **(spec or {})})
    s = clean["spec"]
    return WebOpticsProfile(
        name=clean["name"], login_page_path=s["login_page_path"],
        login_path=s["login_path"], optics_path=s["optics_path"],
        username_field=s["username_field"], password_field=s["password_field"],
        login_static=dict(s["login_static"]), session=s["session"],
        session_key_field=s["session_key_field"], optics_method=s["optics_method"],
        pon_field=s["pon_field"], optics_static=dict(s["optics_static"]),
        charset=s["charset"], onu_id_shape=s["onu_id_shape"],
        pon_label=s["pon_label"], columns=dict(s["columns"]),
        column_order=tuple(s["column_order"]), default_pons=tuple(s["default_pons"]),
        vendor_markers=tuple(s["vendor_markers"]))


BUILTIN_SPECS: dict[str, dict] = {
    "dbc": {
        "login_page_path": "/action/login.html",
        "login_path": "/action/main.html",
        "optics_path": "/action/onuopmdiag.html",
        "username_field": "user",
        "password_field": "pass",
        "login_static": {"button": "Login", "who": "100"},
        "session": "rotating-key",
        "session_key_field": "SessionKey",
        "optics_method": "POST",
        "pon_field": "select",
        "optics_static": {"port_refresh": "Refresh", "searchMac": "",
                          "searchDescription": "", "onuid": "0/", "who": "100"},
        "charset": "gb2312",
        "onu_id_shape": "pon-colon-onu",
        "pon_label": "",
        "columns": {"onu_ref": "ONU ID", "serial": "MAC Address",
                    "name": "Description", "distance_m": "Distance",
                    "temp_c": "Temperature", "voltage_v": "Supply Voltage",
                    "tx_bias_ma": "TX Bias", "tx_dbm": "TX Power",
                    "rx_dbm": "RX Power"},
        "column_order": ["onu_ref", "serial", "name", "distance_m", "temp_c",
                         "voltage_v", "tx_bias_ma", "tx_dbm", "rx_dbm"],
        "default_pons": [1, 2, 3, 4],
        "vendor_markers": ["dbc", "epon", "olt"],
    },
}


def builtin(name: str) -> WebOpticsProfile | None:
    spec = BUILTIN_SPECS.get(str(name or "").strip().lower())
    return profile_from_spec(name, spec) if spec else None


def builtin_names() -> tuple[str, ...]:
    return tuple(BUILTIN_SPECS)


@dataclass(slots=True)
class ProfileSet:

    _by_org: dict[tuple[str | None, str], WebOpticsProfile] = field(default_factory=dict)

    @classmethod
    def build(cls, rows) -> "ProfileSet":
        out = cls()
        for name in BUILTIN_SPECS:
            prof = builtin(name)
            if prof:
                out._by_org[(None, name)] = prof
        for row in rows or ():
            key = (row.get("org_id"), str(row.get("name") or "").strip().lower())
            if not row.get("enabled", True):
                out._by_org[key] = None
                continue
            try:
                prof = profile_from_spec(str(row.get("name") or ""), row.get("spec"))
            except InventoryError:
                continue
            out._by_org[(row.get("org_id"), prof.name)] = prof
        return out

    def resolve(self, org_id: str | None, name: str) -> WebOpticsProfile | None:
        key = str(name or "").strip().lower()
        if not key:
            return None
        for scope in (org_id, None):
            if (scope, key) in self._by_org:
                return self._by_org[(scope, key)]
        return None

    def names(self) -> set[str]:
        return {name for (_scope, name), prof in self._by_org.items() if prof}
