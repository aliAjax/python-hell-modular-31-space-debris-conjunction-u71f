import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError
from . import rules


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# 仍占用机动窗口的指令状态。
HOLD_STATUSES = rules.WINDOW_HOLD_STATUSES


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_ref TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL,
                    target_object_id TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issued_level TEXT NOT NULL,
                    fuel_cost_m_s REAL NOT NULL,
                    issued_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE INDEX IF NOT EXISTS idx_commands_target_window
                    ON commands(target_object_id, window_start, window_end);
                CREATE INDEX IF NOT EXISTS idx_commands_item ON commands(item_id);
                CREATE TABLE IF NOT EXISTS command_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(command_id) REFERENCES commands(id)
                );
                CREATE INDEX IF NOT EXISTS idx_receipts_command ON command_receipts(command_id, id);
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    # ---- 规避指令链 ----

    def _new_command_ref(self, conn, item_id):
        for attempt in range(100):
            total = conn.execute(
                "SELECT COUNT(*) AS total FROM commands WHERE item_id=?", (item_id,)
            ).fetchone()["total"]
            candidate = "CMD-%d-%d" % (item_id, total + attempt + 1)
            exists = conn.execute("SELECT 1 FROM commands WHERE command_ref=?", (candidate,)).fetchone()
            if not exists:
                return candidate
        raise ConflictError("command_ref_unavailable", "指令编号生成失败，请重试")

    def _command_summary(self, conn, command_row):
        row = command_row
        receipts = conn.execute(
            "SELECT * FROM command_receipts WHERE command_id=? ORDER BY id", (row["id"],)
        ).fetchall()
        receipt_list = [
            {
                "kind": r["kind"],
                "status": r["status"],
                "reason": r["reason"],
                "actor": r["actor"],
                "role": r["role"],
                "created_at": r["created_at"],
            }
            for r in receipts
        ]
        latest = receipt_list[-1] if receipt_list else None
        return {
            "id": row["id"],
            "command_ref": row["command_ref"],
            "item_id": row["item_id"],
            "target_object_id": row["target_object_id"],
            "maneuver_window": "%s/%s" % (row["window_start"], row["window_end"]),
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "status": row["status"],
            "issued_level": row["issued_level"],
            "fuel_cost_m_s": row["fuel_cost_m_s"],
            "issued_by": row["issued_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "receipt_status": latest["status"] if latest else None,
            "latest_receipt": latest,
            "failure_reason": latest["reason"] if latest and latest["status"] == "failed" else "",
            "receipts": receipt_list,
            "attempts": sum(1 for r in receipt_list if r["kind"] == "retry") + 1,
        }

    def list_commands(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM commands WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            return [self._command_summary(conn, row) for row in rows]
        finally:
            conn.close()

    def occupied_windows(self, item_id):
        """返回同物体上当前占用窗口的指令（含其他事件的指令）。"""
        conn = self.connect()
        try:
            item = conn.execute("SELECT payload FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            payload = json.loads(item["payload"])
            command = payload.get("maneuver_command")
            if command and command.get("status") in HOLD_STATUSES:
                target = command["target_object_id"]
            else:
                target = payload.get("primary_object_id")
            rows = conn.execute(
                """
                SELECT c.* FROM commands c
                WHERE c.target_object_id=? AND c.status IN (?, ?)
                ORDER BY c.window_start
                """,
                (target, HOLD_STATUSES[0], HOLD_STATUSES[1]),
            ).fetchall()
            return [self._command_summary(conn, row) for row in rows]
        finally:
            conn.close()

    def _overlap_holders(self, conn, target, start, end, exclude_item=None):
        """同物体上时间窗重叠、且仍占用窗口的指令。"""
        rows = conn.execute(
            """
            SELECT * FROM commands
            WHERE target_object_id=? AND status IN (?, ?)
              AND window_start < ? AND ? < window_end
            ORDER BY id
            """,
            (target, HOLD_STATUSES[0], HOLD_STATUSES[1], end, start),
        ).fetchall()
        if exclude_item is not None:
            rows = [r for r in rows if r["item_id"] != exclude_item]
        return rows

    def _add_receipt(self, conn, command_id, kind, status, reason, actor, role):
        conn.execute(
            "INSERT INTO command_receipts(command_id,kind,status,reason,actor,role,created_at) VALUES(?,?,?,?,?,?,?)",
            (command_id, kind, status, reason, actor, role, now_iso()),
        )

    def submit_command(self, item_id, actor, role, new_payload, draft, expected_version=None):
        """批准并下达规避指令（事务 + 窗口仲裁）。

        同一物体同一时间只允许一条在途指令：与占用窗口重叠时本指令落选，
        事件退回协调重排，冲突信息持久化后抛出 409。
        """
        conn = self.connect()
        pending_error = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            current_payload = json.loads(row["payload"])
            if current_payload.get("maneuver_command", {}).get("status") in HOLD_STATUSES:
                raise ConflictError("command_in_flight", "该事件已有在途指令，同一时间不能再次下达")

            target = draft["target_object_id"]
            start = draft["window_start"]
            end = draft["window_end"]
            holders = self._overlap_holders(conn, target, start, end, exclude_item=item_id)
            timestamp = now_iso()
            version = int(row["version"]) + 1

            if holders:
                winner = self._command_summary(conn, holders[0])
                command_ref = self._new_command_ref(conn, item_id)
                conn.execute(
                    """
                    INSERT INTO commands(command_ref,item_id,target_object_id,window_start,window_end,
                        status,issued_level,fuel_cost_m_s,issued_by,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (command_ref, item_id, target, start, end, "voided", draft["issued_level"],
                     draft["fuel_cost_m_s"], actor, timestamp, timestamp),
                )
                command_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                self._add_receipt(conn, command_id, "void", "voided",
                                  "窗口与在途指令重叠，落选并退回协调重排", actor, role)
                loser = self._command_summary(
                    conn, conn.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
                )
                new_payload["maneuver_command"] = loser
                conn.execute(
                    "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                    ("returned", version, canonical_json(new_payload), timestamp, item_id),
                )
                event_payload = {
                    "command_ref": command_ref,
                    "window": draft["maneuver_window"],
                    "winner_command_ref": winner["command_ref"],
                    "winner_item_id": winner["item_id"],
                    "winner_window": winner["maneuver_window"],
                    "reason": "窗口与在途指令重叠，落选并退回协调重排",
                }
                conn.execute(
                    "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, "approve", actor, role, canonical_json(event_payload), timestamp),
                )
                self.append_audit(conn, item_id, "command_returned", actor, role, event_payload)
                conn.execute("COMMIT")
                pending_error = ConflictError(
                    "window_overlap",
                    "机动窗口与在途指令 %s 重叠，本指令落选，已退回协调重排" % winner["command_ref"],
                )
                pending_error.winner = winner
                pending_error.command = loser
                return None

            command_ref = self._new_command_ref(conn, item_id)
            conn.execute(
                """
                INSERT INTO commands(command_ref,item_id,target_object_id,window_start,window_end,
                    status,issued_level,fuel_cost_m_s,issued_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (command_ref, item_id, target, start, end, "in_flight", draft["issued_level"],
                 draft["fuel_cost_m_s"], actor, timestamp, timestamp),
            )
            command_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            summary = self._command_summary(
                conn, conn.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
            )
            new_payload["maneuver_command"] = summary
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                ("in_flight", version, canonical_json(new_payload), timestamp, item_id),
            )
            event_payload = {"command_ref": command_ref, "window": draft["maneuver_window"], "target_object_id": target}
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "approve", actor, role, canonical_json(event_payload), timestamp),
            )
            self.append_audit(conn, item_id, "command_issued", actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
            if pending_error is not None:
                raise pending_error

    def _apply_command_directive(self, item_id, action, actor, role, new_payload, event_payload,
                                 expected_version, directive):
        """登记回执 / 重试 / 作废指令的统一事务。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")

            old_payload = json.loads(row["payload"])
            command_info = old_payload.get("maneuver_command") or {}
            command_ref = directive.get("command_ref") or command_info.get("command_ref")
            command_row = conn.execute(
                "SELECT * FROM commands WHERE item_id=? AND command_ref=?",
                (item_id, command_ref),
            ).fetchone()
            if command_row is None:
                raise DomainError("command_not_found", "指令不存在", 404)

            timestamp = now_iso()
            kind = directive["kind"]
            if kind == "receipt":
                if command_row["status"] != "in_flight":
                    raise DomainError("invalid_state", "指令当前状态 %s，不能登记回执" % command_row["status"], 409)
                receipt = directive["receipt"]
                self._add_receipt(conn, command_row["id"], "receipt", receipt["status"],
                                  receipt["reason"], actor, role)
                command_status = "acked" if receipt["status"] == "acked" else "failed"
                item_status = "resolved" if receipt["status"] == "acked" else "failed"
            elif kind == "retry":
                if command_row["status"] != "failed":
                    raise DomainError("invalid_state", "只有回执失败的指令可以重试", 409)
                self._add_receipt(conn, command_row["id"], "retry", "retry",
                                  directive.get("note", ""), actor, role)
                command_status = "in_flight"
                item_status = "in_flight"
            elif kind == "void":
                if command_row["status"] not in HOLD_STATUSES:
                    raise DomainError("invalid_state", "指令当前状态 %s，不能作废" % command_row["status"], 409)
                self._add_receipt(conn, command_row["id"], "void", "voided",
                                  directive.get("reason", ""), actor, role)
                command_status = "voided"
                item_status = "returned"
            else:
                raise DomainError("unknown_directive", "不支持的指令操作")

            conn.execute(
                "UPDATE commands SET status=?,updated_at=? WHERE id=?",
                (command_status, timestamp, command_row["id"]),
            )
            summary = self._command_summary(
                conn, conn.execute("SELECT * FROM commands WHERE id=?", (command_row["id"],)).fetchone()
            )
            new_payload["maneuver_command"] = summary
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (item_status, version, canonical_json(new_payload), timestamp, item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), timestamp),
            )
            audit_type = {
                "receipt": "receipt_recorded",
                "retry": "command_retried",
                "void": "command_voided",
            }[kind]
            self.append_audit(conn, item_id, audit_type, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def void_item_command(self, item_id, actor, role, new_payload, event_payload, expected_version, reason):
        """新观测导致指令不再占优时，在同一事务内作废指令并释放窗口。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            command_info = new_payload.get("maneuver_command") or {}
            command_ref = command_info.get("command_ref")
            command_row = conn.execute(
                "SELECT * FROM commands WHERE item_id=? AND command_ref=?",
                (item_id, command_ref),
            ).fetchone()
            if command_row is None:
                raise DomainError("command_not_found", "指令不存在", 404)
            timestamp = now_iso()
            if command_row["status"] in HOLD_STATUSES:
                self._add_receipt(conn, command_row["id"], "void", "voided", reason, actor, role)
                conn.execute(
                    "UPDATE commands SET status=?,updated_at=? WHERE id=?",
                    ("voided", timestamp, command_row["id"]),
                )
            summary = self._command_summary(
                conn, conn.execute("SELECT * FROM commands WHERE id=?", (command_row["id"],)).fetchone()
            )
            new_payload["maneuver_command"] = summary
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                ("returned", version, canonical_json(new_payload), timestamp, item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "report_revision", actor, role,
                 canonical_json({"reason": reason, "command_ref": command_ref}), timestamp),
            )
            self.append_audit(conn, item_id, "command_voided", actor, role,
                              {"command_ref": command_ref, "reason": reason})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
