from __future__ import annotations

import json

from wisp.central.store_util import _now_iso

RADIUS_STATUS_STATES = ("ok", "partial", "skipped", "no_profile", "no_credentials",
                        "unreachable", "login", "forbidden", "error")

_CUSTOMER_COLS = ("org_id, account_id, username, name, mac, mobile, alt_mobile,"
                  " acno, status, expiry, package, branch, area, address, balance,"
                  " first_seen_at, last_seen_at")

_ACCOUNT_COLS = ("id, org_id, label, profile, base_url, username, password_enc,"
                 " enabled, updated_by, updated_at")

_LATEST_SEQ_CTE = ("WITH seq AS (SELECT account_id, MAX(seen_seq) AS account_seq"
                   "  FROM radius_customers WHERE org_id=? GROUP BY account_id) ")


class RadiusStoreMixin:

    def get_radius_account(self, account_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_ACCOUNT_COLS} FROM radius_accounts WHERE id=?",
                (int(account_id),)).fetchone()
        return dict(row) if row else None

    def org_radius_accounts(self, org_id: str, *,
                            enabled_only: bool = False) -> list[dict]:
        q = f"SELECT {_ACCOUNT_COLS} FROM radius_accounts WHERE org_id=?"
        if enabled_only:
            q += " AND enabled=1"
        q += " ORDER BY id"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(q, (org_id,)).fetchall()]

    def list_radius_accounts(self, *, enabled_only: bool = True) -> list[dict]:
        q = f"SELECT {_ACCOUNT_COLS} FROM radius_accounts"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY org_id, id"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(q).fetchall()]

    def set_radius_account(self, org_id: str, *, profile: str, base_url: str,
                           username: str | None, password_enc: str | None,
                           account_id: int | None = None, label: str = "",
                           enabled: bool = True,
                           updated_by: str | None = None) -> int:

        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            if not account_id:
                same = conn.execute(
                    "SELECT id FROM radius_accounts WHERE org_id=? AND base_url=?"
                    " AND IFNULL(username,'')=IFNULL(?,'')",
                    (org_id, base_url, username)).fetchone()
                if same is not None:
                    account_id = int(same["id"])
            if account_id:
                sets = ["profile=?", "base_url=?", "username=?", "label=?",
                        "enabled=?", "updated_by=?", "updated_at=?"]
                args: list = [profile, base_url, username, label,
                              1 if enabled else 0, updated_by, now]
                if password_enc is not None:
                    sets.insert(3, "password_enc=?")
                    args.insert(3, password_enc)
                args.extend([int(account_id), org_id])
                cur = conn.execute(
                    f"UPDATE radius_accounts SET {', '.join(sets)}"
                    " WHERE id=? AND org_id=?", args)
                conn.commit()
                if cur.rowcount:
                    return int(account_id)

            cur = conn.execute(
                "INSERT INTO radius_accounts (org_id, label, profile, base_url,"
                " username, password_enc, enabled, updated_by, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (org_id, label, profile, base_url, username, password_enc,
                 1 if enabled else 0, updated_by, now))
            conn.commit()
            return int(cur.lastrowid)

    def delete_radius_account(self, account_id: int) -> bool:

        with self._write_lock, self._connect() as conn:
            row = conn.execute("SELECT org_id FROM radius_accounts WHERE id=?",
                               (int(account_id),)).fetchone()
            if row is None:
                conn.commit()
                return False
            for table in ("radius_customers", "radius_links", "radius_status"):
                conn.execute(f"DELETE FROM {table} WHERE account_id=?",
                             (int(account_id),))
            conn.execute("DELETE FROM radius_accounts WHERE id=?", (int(account_id),))
            conn.commit()
            return True

    def list_radius_profiles(self, org_id: str | None = None) -> list[dict]:
        q = ("SELECT id, org_id, name, spec, enabled, created_at, updated_at"
             " FROM radius_profiles")
        args: list = []
        if org_id is not None:
            q += " WHERE org_id IS NULL OR org_id=?"
            args.append(org_id)
        q += " ORDER BY IFNULL(org_id,''), name"
        with self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            row["enabled"] = bool(row["enabled"])
            try:
                row["spec"] = json.loads(row["spec"])
            except (TypeError, ValueError):
                row["spec"] = {}
            out.append(row)
        return out

    def set_radius_profile(self, name: str, spec: dict, *, org_id: str | None = None,
                           enabled: bool = True) -> None:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO radius_profiles (org_id, name, spec, enabled,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(IFNULL(org_id,''), name) DO UPDATE SET"
                "   spec=excluded.spec, enabled=excluded.enabled,"
                "   updated_at=excluded.updated_at",
                (org_id, name, json.dumps(spec, sort_keys=True),
                 1 if enabled else 0, now, now))
            conn.commit()

    def delete_radius_profile(self, name: str, *, org_id: str | None = None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM radius_profiles WHERE IFNULL(org_id,'')=? AND name=?",
                (org_id or "", name))
            conn.commit()
            return cur.rowcount > 0

    def upsert_radius_customers(self, org_id: str, account_id: int,
                                customers: list[dict],
                                ts: str | None = None) -> int:

        if not customers:
            return 0
        now = ts or _now_iso()
        account_id = int(account_id)
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seen_seq), 0) + 1 AS n FROM radius_customers"
                " WHERE org_id=? AND account_id=?", (org_id, account_id)).fetchone()
            seq = int(row["n"])
            for c in customers:
                username = str(c.get("username") or "").strip()
                if not username:
                    continue
                conn.execute(
                    "INSERT INTO radius_customers (org_id, account_id, username,"
                    " name, mac, mobile, alt_mobile, acno, status, expiry, package,"
                    " branch, area, address, balance, first_seen_at, last_seen_at,"
                    " seen_seq)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(org_id, account_id, username) DO UPDATE SET"
                    "   name=excluded.name, mac=excluded.mac, mobile=excluded.mobile,"
                    "   alt_mobile=excluded.alt_mobile, acno=excluded.acno,"
                    "   status=excluded.status, expiry=excluded.expiry,"
                    "   package=excluded.package, branch=excluded.branch,"
                    "   area=excluded.area, address=excluded.address,"
                    "   balance=excluded.balance, last_seen_at=excluded.last_seen_at,"
                    "   seen_seq=excluded.seen_seq",
                    (org_id, account_id, username, c.get("name"), c.get("mac"),
                     c.get("mobile"), c.get("alt_mobile"), c.get("acno"),
                     c.get("status") or "unknown", c.get("expiry"), c.get("package"),
                     c.get("branch"), c.get("area"), c.get("address"),
                     c.get("balance"), now, now, seq))
            conn.commit()
        return len(customers)

    def list_radius_customers(self, org_id: str, *, account_id: int | None = None,
                              limit: int | None = None) -> list[dict]:
        q = f"SELECT {_CUSTOMER_COLS} FROM radius_customers WHERE org_id=?"
        args: list = [org_id]
        if account_id is not None:
            q += " AND account_id=?"
            args.append(int(account_id))
        q += " ORDER BY account_id, username"
        if limit:
            q += " LIMIT ?"
            args.append(int(limit))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(q, args).fetchall()]

    def get_radius_customer(self, org_id: str, username: str,
                            account_id: int | None = None) -> dict | None:
        q = (f"SELECT {_CUSTOMER_COLS} FROM radius_customers"
             " WHERE org_id=? AND username=?")
        args: list = [org_id, username]
        if account_id is not None:
            q += " AND account_id=?"
            args.append(int(account_id))
        q += " ORDER BY account_id LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(q, args).fetchone()
        return dict(row) if row else None

    def org_radius_customer_rows(self, org_id: str) -> list[dict]:

        with self._connect() as conn:
            rows = conn.execute(
                _LATEST_SEQ_CTE
                + f"SELECT {', '.join('c.' + col.strip() for col in _CUSTOMER_COLS.split(','))},"
                " c.seen_seq, a.label AS account_label,"
                " a.profile AS account_profile, seq.account_seq"
                " FROM radius_customers c"
                " JOIN radius_accounts a ON a.id = c.account_id"
                " JOIN seq ON seq.account_id = c.account_id"
                " WHERE c.org_id=? AND a.enabled=1"
                " ORDER BY c.account_id, c.username",
                (org_id, org_id)).fetchall()
        return [dict(r) for r in rows]

    def org_radius_links(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device_id, onu_key, account_id, username, match_by,"
                " updated_at FROM radius_links WHERE org_id=?", (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def radius_customers_for_link(self, org_id: str) -> list[dict]:

        with self._connect() as conn:
            rows = conn.execute(
                _LATEST_SEQ_CTE
                + "SELECT c.account_id, c.username, c.mac, c.status"
                " FROM radius_customers c"
                " JOIN radius_accounts a ON a.id = c.account_id"
                " JOIN seq ON seq.account_id = c.account_id"
                " WHERE c.org_id=? AND a.enabled=1"
                "   AND c.seen_seq = seq.account_seq"
                " ORDER BY c.account_id, c.username",
                (org_id, org_id)).fetchall()
        return [dict(r) for r in rows]

    def replace_radius_links(self, org_id: str, links, ts: str | None = None) -> int:

        now = ts or _now_iso()
        rows = [(org_id, int(l.device_id), str(l.onu_key),
                 int(l.account_id or 0), l.username, l.match_by, now)
                for l in links or ()]
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM radius_links WHERE org_id=?", (org_id,))
            conn.executemany(
                "INSERT INTO radius_links (org_id, device_id, onu_key, account_id,"
                " username, match_by, updated_at) VALUES (?,?,?,?,?,?,?)", rows)
            conn.commit()
        return len(rows)

    def radius_link_for(self, org_id: str, device_id: int, onu_key: str) -> dict | None:

        with self._connect() as conn:
            row = conn.execute(
                "SELECT l.username, l.match_by, l.updated_at, l.account_id,"
                " a.label AS account_label, a.base_url AS account_url,"
                " c.name, c.mobile,"
                " c.alt_mobile, c.status, c.expiry, c.package, c.branch, c.area,"
                " c.address, c.balance, c.acno, c.mac"
                " FROM radius_links l"
                " LEFT JOIN radius_customers c"
                "   ON c.org_id = l.org_id AND c.username = l.username"
                "   AND c.account_id = l.account_id"
                " LEFT JOIN radius_accounts a ON a.id = l.account_id"
                " WHERE l.org_id=? AND l.device_id=? AND l.onu_key=?",
                (org_id, device_id, onu_key)).fetchone()
        return dict(row) if row else None

    def radius_customer_count(self, org_id: str,
                              account_id: int | None = None) -> int:
        q = "SELECT COUNT(*) AS n FROM radius_customers WHERE org_id=?"
        args: list = [org_id]
        if account_id is not None:
            q += " AND account_id=?"
            args.append(int(account_id))
        with self._connect() as conn:
            row = conn.execute(q, args).fetchone()
        return int(row["n"] or 0)

    def set_radius_status(self, org_id: str, account_id: int, state: str,
                          detail: str | None = None, *, profile: str = "",
                          customers: int = 0, linked: int = 0) -> None:

        if state not in RADIUS_STATUS_STATES:
            state = "error"
        now = _now_iso()
        ok_now = now if state in ("ok", "partial") else None
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO radius_status (account_id, org_id, profile, state,"
                " detail, customers, linked, updated_at, last_ok_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(account_id) DO UPDATE SET org_id=excluded.org_id,"
                "   profile=excluded.profile,"
                "   state=excluded.state, detail=excluded.detail,"
                "   customers=excluded.customers, linked=excluded.linked,"
                "   updated_at=excluded.updated_at,"
                "   last_ok_at=COALESCE(excluded.last_ok_at, radius_status.last_ok_at)",
                (int(account_id), org_id, profile, state, detail, customers, linked,
                 now, ok_now))
            conn.commit()

    def get_radius_status(self, account_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT account_id, org_id, profile, state, detail, customers,"
                " linked, updated_at, last_ok_at FROM radius_status"
                " WHERE account_id=?", (int(account_id),)).fetchone()
        return dict(row) if row else None

    def org_radius_status(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.account_id, s.org_id, s.profile, s.state, s.detail,"
                " s.customers, s.linked, s.updated_at, s.last_ok_at"
                " FROM radius_status s"
                " JOIN radius_accounts a ON a.id = s.account_id"
                " WHERE s.org_id=? ORDER BY s.account_id", (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def radius_link_inputs(self, org_id: str) -> tuple[list[dict], list[dict]]:

        with self._connect() as conn:
            macs = conn.execute(
                "SELECT m.device_id, m.onu_key, m.mac FROM onu_user_macs m"
                " JOIN org_devices d ON d.id = m.device_id"
                " WHERE m.org_id=? AND d.org_id=? AND d.is_active=1",
                (org_id, org_id)).fetchall()
            onus = conn.execute(
                "SELECT o.device_id, o.onu_key, o.name FROM onu_optics o"
                " JOIN org_devices d ON d.id = o.device_id"
                " WHERE o.org_id=? AND d.org_id=? AND d.is_active=1"
                "   AND COALESCE(o.name,'') <> ''",
                (org_id, org_id)).fetchall()
        return [dict(r) for r in macs], [dict(r) for r in onus]
