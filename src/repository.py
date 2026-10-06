import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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
                    item_id INTEGER NOT NULL,
                    command_ref TEXT NOT NULL UNIQUE,
                    primary_object_id TEXT NOT NULL,
                    secondary_object_id TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_status TEXT NOT NULL DEFAULT 'pending',
                    receipt_reason TEXT,
                    receipt_by TEXT,
                    receipt_at TEXT,
                    approved_level TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE INDEX IF NOT EXISTS idx_commands_window
                    ON commands(primary_object_id, status, window_start, window_end);
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

    def _window_clash(self, conn, primary_object_id, window_end, window_start, exclude_id=None):
        query = (
            "SELECT id, command_ref FROM commands "
            "WHERE primary_object_id=? AND status='in_transit' "
            "AND window_start < ? AND window_end > ?"
        )
        params = [primary_object_id, window_end, window_start]
        if exclude_id is not None:
            query += " AND id != ?"
            params.append(exclude_id)
        query += " LIMIT 1"
        return conn.execute(query, params).fetchone()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None, effects=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            effects = effects or {}
            if effects.get("command"):
                cmd = effects["command"]
                clash = self._window_clash(conn, cmd["primary_object_id"], cmd["window_end"], cmd["window_start"])
                if clash:
                    raise ConflictError(
                        "window_conflict",
                        "同一物体在该窗口已有在途指令，落选指令退回协调重排: %s" % clash["command_ref"],
                    )
                conn.execute(
                    "INSERT INTO commands(item_id,command_ref,primary_object_id,secondary_object_id,window_start,window_end,status,receipt_status,receipt_reason,receipt_by,receipt_at,approved_level,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        cmd["command_ref"],
                        cmd["primary_object_id"],
                        cmd["secondary_object_id"],
                        cmd["window_start"],
                        cmd["window_end"],
                        cmd["status"],
                        cmd["receipt_status"],
                        cmd["receipt_reason"],
                        None,
                        None,
                        cmd["approved_level"],
                        now_iso(),
                        now_iso(),
                    ),
                )
            if effects.get("receipt"):
                receipt = effects["receipt"]
                cur = conn.execute(
                    "SELECT id FROM commands WHERE item_id=? AND command_ref=?",
                    (item_id, receipt["command_ref"]),
                ).fetchone()
                if cur is None:
                    raise NotFoundError("command_not_found", "回执对应的指令不存在")
                if receipt["status"] == "executed":
                    conn.execute(
                        "UPDATE commands SET status='executed',receipt_status='executed',receipt_reason=NULL,receipt_by=?,receipt_at=?,updated_at=? WHERE id=?",
                        (actor, now_iso(), now_iso(), cur["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE commands SET status='failed',receipt_status='failed',receipt_reason=?,receipt_by=?,receipt_at=?,updated_at=? WHERE id=?",
                        (receipt["reason"], actor, now_iso(), now_iso(), cur["id"]),
                    )
            if effects.get("retry"):
                retry = effects["retry"]
                cur = conn.execute(
                    "SELECT * FROM commands WHERE item_id=? AND command_ref=?",
                    (item_id, retry["command_ref"]),
                ).fetchone()
                if cur is None:
                    raise NotFoundError("command_not_found", "指令不存在")
                if cur["status"] != "failed":
                    raise ConflictError("command_not_failed", "只有失败退回的指令才能重试")
                clash = self._window_clash(
                    conn, cur["primary_object_id"], cur["window_end"], cur["window_start"], exclude_id=cur["id"]
                )
                if clash:
                    raise ConflictError(
                        "window_conflict",
                        "重试窗口与在途指令冲突，退回协调重排: %s" % clash["command_ref"],
                    )
                conn.execute(
                    "UPDATE commands SET status='in_transit',receipt_status='pending',receipt_reason=NULL,receipt_by=NULL,receipt_at=NULL,updated_at=? WHERE id=?",
                    (now_iso(), cur["id"]),
                )
            if effects.get("void_in_transit"):
                conn.execute(
                    "UPDATE commands SET status='voided',receipt_reason=?,updated_at=? WHERE item_id=? AND status='in_transit'",
                    (effects.get("void_reason"), now_iso(), item_id),
                )
            if action == "resolve":
                done = conn.execute(
                    "SELECT id FROM commands WHERE item_id=? AND status='executed' LIMIT 1",
                    (item_id,),
                ).fetchone()
                if done is None:
                    raise DomainError("receipt_required", "运营方尚未回执确认指令执行结果", 409)
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

    def list_commands(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM commands WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def occupied_windows(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT item_id,command_ref,primary_object_id,window_start,window_end "
                "FROM commands WHERE status='in_transit' ORDER BY window_start"
            ).fetchall()
            return [dict(row) for row in rows]
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

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items(), "occupied_windows": self.occupied_windows()}
        finally:
            conn.close()
