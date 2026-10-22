import json
from pathlib import Path
import sqlite3
import time
import uuid

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "workbench.sqlite3"
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS checkpoints (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL,
                    state TEXT NOT NULL, cancel INTEGER NOT NULL DEFAULT 0,
                    request TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
                    result TEXT, error TEXT, retry_of TEXT, worker TEXT);
            """
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM settings WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value))
            )

    def replace_checkpoints(self, entries):
        with self.connect() as db:
            db.execute("DELETE FROM checkpoints")
            db.executemany(
                "INSERT INTO checkpoints VALUES (?,?)",
                [(x["id"], json.dumps(x)) for x in entries],
            )

    def checkpoints(self):
        with self.connect() as db:
            return [
                json.loads(x[0]) for x in db.execute("SELECT payload FROM checkpoints")
            ]

    def checkpoint(self, identity):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM checkpoints WHERE id=?", (identity,)
            ).fetchone()
        if not row:
            raise ValueError("该权重不在索引中，请刷新权重列表")
        return json.loads(row[0])

    def create(self, request, retry_of=None):
        identity = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,created,updated,state,request,retry_of) VALUES (?,?,?,?,?,?)",
                (identity, now, now, "queued", json.dumps(request), retry_of),
            )
        return self.get(identity)

    @staticmethod
    def decode(row):
        result = dict(row)
        for key in ("request", "progress", "result", "worker"):
            result[key] = json.loads(result[key]) if result[key] else None
        result["cancel"] = bool(result["cancel"])
        return result

    def get(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
        if not row:
            raise KeyError(identity)
        return self.decode(row)

    def jobs(self):
        with self.connect() as db:
            return [
                self.decode(x)
                for x in db.execute("SELECT * FROM jobs ORDER BY created DESC")
            ]

    def update(self, identity, **values):
        allowed = {"state", "cancel", "progress", "result", "error", "worker"}
        if not values.keys() <= allowed:
            raise ValueError("invalid update fields")
        values = {
            k: (
                json.dumps(v)
                if k in ("progress", "result", "worker") and v is not None
                else v
            )
            for k, v in values.items()
        }
        values["updated"] = time.time()
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                [*values.values(), identity],
            )

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM jobs WHERE state='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET state='preparing',updated=? WHERE id=?",
                (time.time(), row[0]),
            )
        return self.get(row[0])

    def cancel(self, identity):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM jobs WHERE id=?", (identity,)
            ).fetchone()
            if not row:
                raise KeyError(identity)
            if row[0] not in TERMINAL:
                db.execute(
                    "UPDATE jobs SET cancel=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,updated=? WHERE id=?",
                    (time.time(), identity),
                )
        return self.get(identity)

    def delete(self, identity):
        with self.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (identity,))
