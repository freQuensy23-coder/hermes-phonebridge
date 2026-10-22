import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .protocol import (
    FILE_OPERATIONS,
    IDENTIFIER_PATTERN,
    MAX_RESULT_DATA_BYTES,
    MAX_RESULT_STRING_CHARS,
    OPERATIONS,
    SCREEN_OPERATIONS,
    BridgeError,
    digest,
    encode_json,
    fingerprint,
    validate_json,
    validate_screenshot_data,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    version TEXT,
    capabilities TEXT NOT NULL DEFAULT '[]',
    permissions TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    revoked_at INTEGER,
    session_id TEXT,
    hello_ready INTEGER NOT NULL DEFAULT 0,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS pairings (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('pending','claimed','expired','revoked')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    device_id TEXT REFERENCES devices(id)
);
CREATE INDEX IF NOT EXISTS pairings_expiry ON pairings(expires_at);
CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    op TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','sent','succeeded','failed','expired')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    completed_at INTEGER,
    idempotency_key TEXT,
    request_hash TEXT NOT NULL,
    result_hash TEXT,
    data TEXT,
    error TEXT,
    UNIQUE(device_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS commands_delivery ON commands(device_id,status,created_at);
CREATE INDEX IF NOT EXISTS commands_expiry ON commands(status,expires_at);
CREATE INDEX IF NOT EXISTS commands_retention ON commands(completed_at);
CREATE TABLE IF NOT EXISTS events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    time INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    UNIQUE(device_id,id)
);
CREATE INDEX IF NOT EXISTS events_device_cursor ON events(device_id,cursor);
CREATE INDEX IF NOT EXISTS events_retention ON events(received_at);
CREATE TABLE IF NOT EXISTS event_receipts (
    device_id TEXT NOT NULL REFERENCES devices(id),
    id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY(device_id,id)
);
CREATE INDEX IF NOT EXISTS receipts_retention ON event_receipts(received_at);
CREATE TABLE IF NOT EXISTS transfers (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    command_id TEXT NOT NULL UNIQUE REFERENCES commands(id),
    size INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS transfers_device ON transfers(device_id);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, settings, clock):
        self.settings = settings
        self.clock = clock
        self.lock = threading.RLock()
        path = settings.db_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = path.parent.stat()
        if directory.st_uid != os.getuid() or directory.st_mode & 0o022:
            raise ValueError("SQLite directory must be owned by this user and not writable by other users")
        for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if candidate.is_symlink():
                raise ValueError("SQLite files must not be symbolic links")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
                raise ValueError("SQLite file must be an unaliased regular file owned by this process user")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self.connection = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.execute("PRAGMA secure_delete=ON")
            page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
            page_limit = settings.max_database_bytes // page_size
            actual_limit = self.connection.execute(f"PRAGMA max_page_count={page_limit}").fetchone()[0]
            if actual_limit > page_limit or page_limit < 16:
                raise ValueError("SQLite database exceeds the configured storage quota")
            mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise ValueError("SQLite WAL could not be enabled")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported PhoneBridge database version")
            self.connection.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
            self.transfers = path.parent / "transfers"
            if self.transfers.is_symlink():
                raise ValueError("Transfer directory must not be a symbolic link")
            self.transfers.mkdir(mode=0o700, exist_ok=True)
            metadata = self.transfers.stat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise ValueError("Transfer directory must be private to this user")
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        with self.lock:
            self.connection.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    def _bounded(self, value, maximum=None, string_limit=None):
        if string_limit is None:
            string_limit = self.settings.max_json_bytes if maximum is None else 16384
        try:
            validate_json(value, min(maximum or self.settings.max_json_bytes, self.settings.max_json_bytes),
                          string_limit)
        except (ValueError, UnicodeError) as error:
            raise BridgeError("VALIDATION_ERROR", "JSON data limit exceeded", 422) from error

    def _expire(self, db):
        now = int(self.clock())
        db.execute("UPDATE pairings SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
        db.execute(
            "UPDATE commands SET status='expired',completed_at=?,error=? "
            "WHERE status IN ('queued','sent') AND expires_at<=?",
            (now, encode_json({"code": "EXPIRED", "message": "Command TTL elapsed"}), now),
        )

    def _reap(self, db):
        rows = db.execute(
            "SELECT t.id FROM transfers t JOIN commands c ON c.id=t.command_id "
            "WHERE c.status NOT IN ('queued','sent')"
        ).fetchall()
        for row in rows:
            db.execute("DELETE FROM transfers WHERE id=?", (row["id"],))
        return [row["id"] for row in rows]

    def _unlink(self, transfer_ids):
        for transfer_id in transfer_ids:
            (self.transfers / transfer_id).unlink(missing_ok=True)

    def _device(self, db, device_id):
        row = db.execute("SELECT * FROM devices WHERE id=? AND revoked_at IS NULL", (device_id,)).fetchone()
        if row is None:
            raise BridgeError("NOT_FOUND", "Device not found", 404)
        return row

    def _session(self, db, device_id, session_id):
        row = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise BridgeError("UNAUTHORIZED", "Device authorization was revoked", 401)
        if row["session_id"] != session_id:
            raise BridgeError("AUTH_REVOKED", "Device session is no longer authorized", 401)
        return row

    def _device_view(self, row):
        return {
            "id": row["id"], "name": row["name"],
            "online": bool(row["session_id"] and row["hello_ready"] and row["last_seen"] is not None
                           and row["last_seen"] > self.clock() - self.settings.online_timeout),
            "version": row["version"],
            "capabilities": json.loads(row["capabilities"]),
            "permissions": json.loads(row["permissions"]),
        }

    def _command_view(self, row):
        return {
            "id": row["id"], "device_id": row["device_id"], "op": row["op"],
            "status": row["status"], "expires_at": row["expires_at"],
            "data": json.loads(row["data"]) if row["data"] is not None else None,
            "error": json.loads(row["error"]) if row["error"] is not None else None,
        }

    def create_pairing(self, request):
        now = int(self.clock())
        pairing_id, token = str(uuid4()), secrets.token_urlsafe(32)
        expires_at = now + request.ttl_seconds
        with self.transaction() as db:
            self._expire(db)
            if db.execute("SELECT count(*) FROM pairings").fetchone()[0] >= self.settings.max_pairings:
                raise BridgeError("QUEUE_FULL", "Pairing record limit reached", 429)
            db.execute(
                "INSERT INTO pairings(id,name,token_hash,status,created_at,expires_at) VALUES(?,?,?,'pending',?,?)",
                (pairing_id, request.name, digest(token), now, expires_at),
            )
        return {"id": pairing_id, "expires_at": expires_at,
                "qr_payload": {"v": 1, "server": self.settings.public_url, "token": token,
                               "expires_at": expires_at}}

    def pairing(self, pairing_id):
        with self.transaction() as db:
            self._expire(db)
            row = db.execute("SELECT * FROM pairings WHERE id=?", (pairing_id,)).fetchone()
            if row is None:
                raise BridgeError("NOT_FOUND", "Pairing not found", 404)
            return {key: row[key] for key in ("id", "status", "expires_at", "device_id")}

    def claim(self, request):
        now = int(self.clock())
        device_id, token = str(uuid4()), secrets.token_urlsafe(32)
        with self.transaction() as db:
            self._expire(db)
            row = db.execute(
                "SELECT * FROM pairings WHERE token_hash=? AND status='pending' AND expires_at>?",
                (digest(request.token), now),
            ).fetchone()
            if row is None:
                raise BridgeError("PAIRING_INVALID", "Pairing is invalid or unavailable", 401)
            if db.execute("SELECT count(*) FROM devices").fetchone()[0] >= self.settings.max_devices:
                raise BridgeError("QUEUE_FULL", "Device record limit reached", 429)
            db.execute("INSERT INTO devices(id,name,token_hash,created_at) VALUES(?,?,?,?)",
                       (device_id, request.name, digest(token), now))
            db.execute("UPDATE pairings SET status='claimed',device_id=? WHERE id=?", (device_id, row["id"]))
        return {"device_id": device_id, "device_token": token, "ws_url": self.settings.ws_url}

    def _revoke_device(self, db, device_id):
        now = int(self.clock())
        db.execute("UPDATE devices SET revoked_at=?,session_id=NULL,hello_ready=0 WHERE id=?", (now, device_id))
        db.execute(
            "UPDATE commands SET status='failed',completed_at=?,error=? "
            "WHERE device_id=? AND status IN ('queued','sent')",
            (now, encode_json({"code": "DEVICE_REVOKED", "message": "Device authorization revoked"}), device_id),
        )

    def revoke_pending_pairing(self, pairing_id):
        with self.transaction() as db:
            row = db.execute("SELECT device_id FROM pairings WHERE id=?", (pairing_id,)).fetchone()
            if row is None:
                raise BridgeError("NOT_FOUND", "Pairing not found", 404)
            if row["device_id"] is None:
                db.execute("UPDATE pairings SET status='revoked' WHERE id=?", (pairing_id,))
            return row["device_id"]

    def revoke_pairing(self, pairing_id):
        with self.transaction() as db:
            row = db.execute("SELECT device_id FROM pairings WHERE id=?", (pairing_id,)).fetchone()
            if row is None:
                raise BridgeError("NOT_FOUND", "Pairing not found", 404)
            db.execute("UPDATE pairings SET status='revoked' WHERE id=?", (pairing_id,))
            if row["device_id"]:
                self._revoke_device(db, row["device_id"])
            reaped = self._reap(db)
        self._unlink(reaped)
        return row["device_id"]

    def revoke_device(self, device_id):
        with self.transaction() as db:
            row = db.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                raise BridgeError("NOT_FOUND", "Device not found", 404)
            self._revoke_device(db, device_id)
            reaped = self._reap(db)
        self._unlink(reaped)

    def health(self):
        with self.lock:
            self.connection.execute("SELECT 1").fetchone()

    def devices(self):
        with self.lock:
            rows = self.connection.execute("SELECT * FROM devices WHERE revoked_at IS NULL ORDER BY created_at,id").fetchall()
            return {"devices": [self._device_view(row) for row in rows]}

    def device(self, device_id):
        with self.lock:
            return self._device_view(self._device(self.connection, device_id))

    def authorize(self, token):
        with self.lock:
            row = self.connection.execute(
                "SELECT id FROM devices WHERE token_hash=? AND revoked_at IS NULL", (digest(token),),
            ).fetchone()
            if row is None:
                raise BridgeError("UNAUTHORIZED", "Device authorization required", 401)
            return row["id"]

    def check_session(self, device_id, session_id):
        with self.lock:
            self._session(self.connection, device_id, session_id)

    def connect(self, token):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM devices WHERE token_hash=? AND revoked_at IS NULL", (digest(token),)).fetchone()
            if row is None:
                raise BridgeError("UNAUTHORIZED", "Device authorization required", 401)
            active_count = db.execute(
                "SELECT count(*) FROM devices WHERE session_id IS NOT NULL AND last_seen>? AND id<>?",
                (self.clock() - self.settings.online_timeout, row["id"]),
            ).fetchone()[0]
            if active_count >= self.settings.max_connections:
                raise BridgeError("QUEUE_FULL", "Connection limit reached", 429)
            session_id = str(uuid4())
            db.execute("UPDATE devices SET session_id=?,hello_ready=0,last_seen=? WHERE id=?",
                       (session_id, self.clock(), row["id"]))
            return row["id"], session_id

    def disconnect(self, device_id, session_id):
        with self.transaction() as db:
            db.execute("UPDATE devices SET session_id=NULL,hello_ready=0 WHERE id=? AND session_id=?",
                       (device_id, session_id))

    def hello(self, device_id, session_id, frame):
        with self.transaction() as db:
            self._session(db, device_id, session_id)
            db.execute("UPDATE devices SET version=?,capabilities=?,permissions=?,hello_ready=1,last_seen=? WHERE id=?",
                       (frame.version, encode_json(frame.capabilities), encode_json(frame.permissions),
                        self.clock(), device_id))

    def touch(self, device_id, session_id):
        with self.transaction() as db:
            self._session(db, device_id, session_id)
            db.execute("UPDATE devices SET last_seen=? WHERE id=?", (self.clock(), device_id))

    def enqueue(self, device_id, request):
        if request.op in FILE_OPERATIONS:
            raise BridgeError("VALIDATION_ERROR", "File delivery requires the authenticated upload endpoint", 422)
        self._bounded(request.args, 32768)
        if request.op in SCREEN_OPERATIONS:
            debug_session = request.args.get("debug_session")
            if not isinstance(debug_session, str) or not IDENTIFIER_PATTERN.fullmatch(debug_session):
                raise BridgeError("VALIDATION_ERROR", "Screen commands require a bounded debug_session identifier", 422)
        now = int(self.clock())
        request_hash = fingerprint({"op": request.op, "args": request.args, "ttl_seconds": request.ttl_seconds})
        with self.transaction() as db:
            self._expire(db)
            device = self._device(db, device_id)
            if request.idempotency_key is not None:
                existing = db.execute("SELECT * FROM commands WHERE device_id=? AND idempotency_key=?",
                                      (device_id, request.idempotency_key)).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise BridgeError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another request", 409)
                    return {"id": existing["id"], "status": "queued", "expires_at": existing["expires_at"]}
            if request.op not in OPERATIONS or request.op not in json.loads(device["capabilities"]):
                raise BridgeError("UNSUPPORTED", f"Operation {request.op} is not advertised by this device", 422)
            command_id, expires_at = self._insert_command(db, device_id, request.op, request.args,
                                                          request.ttl_seconds, request.idempotency_key,
                                                          request_hash, now)
            return {"id": command_id, "status": "queued", "expires_at": expires_at}

    def _insert_command(self, db, device_id, op, args, ttl_seconds, idempotency_key, request_hash, now):
        pending = db.execute("SELECT count(*) FROM commands WHERE device_id=? AND status IN ('queued','sent')",
                             (device_id,)).fetchone()[0]
        if pending >= self.settings.max_pending_commands:
            raise BridgeError("QUEUE_FULL", "Too many undelivered commands for this device", 429)
        records = db.execute("SELECT count(*) FROM commands WHERE device_id=?", (device_id,)).fetchone()[0]
        if records >= self.settings.max_command_records:
            db.execute(
                "DELETE FROM commands WHERE id IN (SELECT id FROM commands WHERE device_id=? "
                "AND status NOT IN ('queued','sent') AND id NOT IN (SELECT command_id FROM transfers) "
                "ORDER BY completed_at, rowid LIMIT ?)",
                (device_id, records - self.settings.max_command_records + 1),
            )
        command_id = str(uuid4())
        expires_at = now + ttl_seconds
        db.execute(
            "INSERT INTO commands(id,device_id,op,args,status,created_at,expires_at,idempotency_key,request_hash) "
            "VALUES(?,?,?,?,'queued',?,?,?,?)",
            (command_id, device_id, op, encode_json(args), now, expires_at, idempotency_key, request_hash),
        )
        return command_id, expires_at

    def command(self, command_id):
        with self.transaction() as db:
            self._expire(db)
            row = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
            if row is None:
                raise BridgeError("NOT_FOUND", "Command not found", 404)
            return self._command_view(row)

    def pending(self, device_id, session_id):
        with self.transaction() as db:
            self._expire(db)
            self._session(db, device_id, session_id)
            rows = db.execute(
                "SELECT id FROM commands WHERE device_id=? AND status IN ('queued','sent') ORDER BY rowid LIMIT ?",
                (device_id, self.settings.max_pending_commands),
            ).fetchall()
            return [row["id"] for row in rows]

    def dispatch(self, device_id, session_id, command_id):
        with self.transaction() as db:
            self._expire(db)
            device = self._session(db, device_id, session_id)
            if not device["hello_ready"]:
                return None
            row = db.execute("SELECT * FROM commands WHERE id=? AND device_id=? AND status IN ('queued','sent')",
                             (command_id, device_id)).fetchone()
            if row is None:
                return None
            if row["op"] not in json.loads(device["capabilities"]):
                db.execute("UPDATE commands SET status='failed',completed_at=?,error=? WHERE id=?",
                           (int(self.clock()), encode_json({"code": "UNSUPPORTED", "message": "Capability no longer advertised"}), command_id))
                return None
            db.execute("UPDATE commands SET status='sent' WHERE id=?", (command_id,))
            return {"type": "command", "id": row["id"], "op": row["op"],
                    "args": json.loads(row["args"]), "expires_at": row["expires_at"]}

    def result(self, device_id, session_id, frame):
        if frame.data is not None:
            self._bounded(frame.data, MAX_RESULT_DATA_BYTES, MAX_RESULT_STRING_CHARS)
        if (frame.ok and frame.error is not None) or (not frame.ok and (frame.error is None or frame.data is not None)):
            raise BridgeError("INVALID_RESULT", "Result success and error fields disagree", 422)
        data = dict(frame.data) if frame.data is not None else None
        if data is not None and data.get("outcome") == "dispatched":
            data["verified"] = False
        if data is not None:
            self._bounded(data, MAX_RESULT_DATA_BYTES, MAX_RESULT_STRING_CHARS)
        now = int(self.clock())
        with self.transaction() as db:
            self._expire(db)
            self._session(db, device_id, session_id)
            row = db.execute("SELECT * FROM commands WHERE id=?", (frame.id,)).fetchone()
            if row is None or row["device_id"] != device_id:
                raise BridgeError("RESULT_OWNERSHIP", "Result has no command owned by this device", 403)
            if frame.ok and row["op"] == "screen.screenshot":
                try:
                    validate_screenshot_data(data)
                except ValueError as error:
                    raise BridgeError("INVALID_RESULT", "Invalid or oversized JPEG screenshot", 422) from error
            result_hash = fingerprint({"ok": frame.ok, "data": data,
                                       "error": frame.error.model_dump() if frame.error else None})
            if row["status"] in ("succeeded", "failed"):
                if row["result_hash"] != result_hash:
                    raise BridgeError("RESULT_CONFLICT", "Terminal command result cannot be changed", 409)
                return
            if row["status"] == "queued":
                raise BridgeError("RESULT_UNCORRELATED", "Command was never delivered to this device", 409)
            if row["status"] == "expired" and (row["op"] in SCREEN_OPERATIONS or row["op"] == "debug.status"):
                return
            db.execute("UPDATE commands SET status=?,completed_at=?,result_hash=?,data=?,error=? WHERE id=?",
                       ("succeeded" if frame.ok else "failed", now, result_hash,
                        encode_json(data) if data is not None else None,
                        encode_json(frame.error.model_dump()) if frame.error else None, frame.id))
            reaped = self._reap(db)
        self._unlink(reaped)

    def event(self, device_id, session_id, frame):
        self._bounded(frame.data, 16384)
        content_hash = fingerprint({"name": frame.name, "data": frame.data, "time": frame.time})
        now = int(self.clock())
        with self.transaction() as db:
            self._session(db, device_id, session_id)
            existing = db.execute("SELECT content_hash FROM event_receipts WHERE device_id=? AND id=?",
                                  (device_id, frame.id)).fetchone()
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise BridgeError("EVENT_CONFLICT", "Event ID belongs to different data", 409)
                return {"type": "ack", "id": frame.id}
            count = db.execute("SELECT count(*) FROM event_receipts WHERE device_id=?", (device_id,)).fetchone()[0]
            if count >= self.settings.max_event_receipts:
                db.execute("DELETE FROM event_receipts WHERE rowid IN (SELECT rowid FROM event_receipts "
                           "WHERE device_id=? ORDER BY received_at, rowid LIMIT ?)",
                           (device_id, count - self.settings.max_event_receipts + 1))
            db.execute("INSERT INTO event_receipts(device_id,id,content_hash,received_at) VALUES(?,?,?,?)",
                       (device_id, frame.id, content_hash, now))
            db.execute("INSERT INTO events(device_id,id,name,data,time,received_at) VALUES(?,?,?,?,?,?)",
                       (device_id, frame.id, frame.name, encode_json(frame.data), frame.time, now))
            db.execute("DELETE FROM events WHERE device_id=? AND cursor NOT IN "
                       "(SELECT cursor FROM events WHERE device_id=? ORDER BY cursor DESC LIMIT ?)",
                       (device_id, device_id, self.settings.max_events))
            return {"type": "ack", "id": frame.id}

    def events(self, device_id, after, limit):
        with self.lock:
            self._device(self.connection, device_id)
            rows = self.connection.execute(
                "SELECT * FROM events WHERE device_id=? AND cursor>? ORDER BY cursor LIMIT ?",
                (device_id, after, limit),
            ).fetchall()
            values = [{"cursor": row["cursor"], "id": row["id"], "name": row["name"],
                       "data": json.loads(row["data"]), "time": row["time"]} for row in rows]
            return {"events": values, "next_cursor": values[-1]["cursor"] if values else after}

    def maintain(self):
        now = int(self.clock())
        with self.transaction() as db:
            self._expire(db)
            reaped = self._reap(db)
            db.execute("DELETE FROM pairings WHERE status<>'pending' AND expires_at<?", (now - self.settings.retention_seconds,))
            db.execute("DELETE FROM commands WHERE completed_at<? AND id NOT IN (SELECT command_id FROM transfers)",
                       (now - self.settings.receipt_retention_seconds,))
            db.execute("DELETE FROM events WHERE received_at<?", (now - self.settings.retention_seconds,))
            db.execute("DELETE FROM event_receipts WHERE received_at<?", (now - self.settings.receipt_retention_seconds,))
            known = {row["id"] for row in db.execute("SELECT id FROM transfers").fetchall()}
        self._unlink(reaped)
        for path in self.transfers.iterdir():
            staging = path.name.startswith("upload-")
            if path.name not in known and (not staging or time.time() - path.stat().st_mtime > 3600):
                path.unlink(missing_ok=True)

    def create_transfer(self, device_id, upload, staged):
        now = int(self.clock())
        request_hash = fingerprint({"op": "files.receive", "name": upload["name"], "mime": upload["mime"],
                                    "kind": upload["kind"], "size": upload["size"], "sha256": upload["sha256"],
                                    "ttl_seconds": upload["ttl_seconds"]})
        transfer_id = str(uuid4())
        try:
            with self.transaction() as db:
                self._expire(db)
                device = self._device(db, device_id)
                key = upload["idempotency_key"]
                if key is not None:
                    existing = db.execute("SELECT * FROM commands WHERE device_id=? AND idempotency_key=?",
                                          (device_id, key)).fetchone()
                    if existing is not None:
                        if existing["request_hash"] != request_hash:
                            raise BridgeError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another request", 409)
                        return {"id": existing["id"], "status": "queued", "expires_at": existing["expires_at"]}
                if not upload["signature_ok"]:
                    raise BridgeError("VALIDATION_ERROR", "Image bytes do not match the declared image type", 422)
                if "files.receive" not in json.loads(device["capabilities"]):
                    raise BridgeError("UNSUPPORTED", "Device does not advertise files.receive", 422)
                live = "SELECT {} FROM transfers t JOIN commands c ON c.id=t.command_id WHERE c.status IN ('queued','sent')"
                count = db.execute(live.format("count(*)") + " AND t.device_id=?", (device_id,)).fetchone()[0]
                if count >= self.settings.max_device_transfers:
                    raise BridgeError("QUEUE_FULL", "Too many undelivered files for this device", 429)
                total = db.execute(live.format("COALESCE(SUM(t.size),0)")).fetchone()[0]
                if total + upload["size"] > self.settings.max_transfer_bytes:
                    raise BridgeError("QUEUE_FULL", "Pending file storage quota reached", 429)
                args = {"transfer_id": transfer_id, "name": upload["name"], "mime": upload["mime"],
                        "kind": upload["kind"], "size": upload["size"], "sha256": upload["sha256"]}
                command_id, expires_at = self._insert_command(db, device_id, "files.receive", args,
                                                              upload["ttl_seconds"], key, request_hash, now)
                db.execute("INSERT INTO transfers(id,device_id,command_id,size,created_at) VALUES(?,?,?,?,?)",
                           (transfer_id, device_id, command_id, upload["size"], now))
                os.replace(staged, self.transfers / transfer_id)
                return {"id": command_id, "status": "queued", "expires_at": expires_at}
        except BaseException:
            (self.transfers / transfer_id).unlink(missing_ok=True)
            raise
        finally:
            Path(staged).unlink(missing_ok=True)

    def open_transfer(self, token, transfer_id):
        with self.transaction() as db:
            self._expire(db)
            device = db.execute("SELECT id FROM devices WHERE token_hash=? AND revoked_at IS NULL",
                                (digest(token),)).fetchone()
            if device is None:
                raise BridgeError("UNAUTHORIZED", "Device authorization required", 401)
            row = db.execute("SELECT t.id, t.device_id, c.status FROM transfers t JOIN commands c ON c.id=t.command_id "
                             "WHERE t.id=?", (transfer_id,)).fetchone()
            if row is None or row["device_id"] != device["id"]:
                raise BridgeError("NOT_FOUND", "Transfer not found", 404)
            if row["status"] == "queued":
                raise BridgeError("NOT_DISPATCHED", "Transfer has not been delivered to the device yet", 409)
            if row["status"] != "sent":
                raise BridgeError("GONE", "Transfer is no longer available", 410)
            try:
                return open(self.transfers / transfer_id, "rb")
            except FileNotFoundError as error:
                raise BridgeError("GONE", "Transfer is no longer available", 410) from error
