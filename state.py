import json
import sqlite3
import threading
import time
from contextlib import contextmanager


class State:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, project TEXT NOT NULL, task TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', ready INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL, branch TEXT, commit_sha TEXT,
                    report TEXT NOT NULL DEFAULT '', cancel INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schedules (
                    project TEXT PRIMARY KEY, hours REAL NOT NULL, next_run REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "thread_id" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN thread_id TEXT")

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def pending_requests(self, project):
        with self.db() as db:
            results = []
            for row in db.execute("SELECT key,value FROM settings WHERE key LIKE 'dialog_request:%'"):
                request = json.loads(row[1])
                if request and request.get("project") == project:
                    job = db.execute("SELECT status FROM jobs WHERE id=?", (request["job_id"],)).fetchone()
                    if job and job[0] == "running":
                        results.append((row[0].split(":", 1)[1], request))
            return results

    def mark_dialog_message(self, thread_id, item_id):
        with self.db() as db:
            return db.execute("INSERT OR IGNORE INTO settings VALUES (?, 'true')", (f"seen_dialog:{thread_id}:{item_id}",)).rowcount == 1

    def add(self, project, task, thread_id=None):
        now = time.time()
        with self.db() as db:
            return db.execute("INSERT INTO jobs(project,task,created,updated,thread_id,ready) VALUES(?,?,?,?,?,?)", (project, task, now, now, thread_id, int(thread_id is not None))).lastrowid

    def job(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def jobs(self, project=None):
        with self.db() as db:
            sql = "SELECT * FROM jobs" + (" WHERE project=?" if project else "") + " ORDER BY id DESC LIMIT 20"
            return [dict(row) for row in db.execute(sql, (project,) if project else ())]

    def request(self, project):
        with self.db() as db:
            row = db.execute("SELECT id FROM jobs WHERE project=? AND status='queued' AND ready=0 ORDER BY id LIMIT 1", (project,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET ready=1 WHERE id=?", (row[0],))
            return row[0]

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT j.id,j.status FROM jobs j
                WHERE j.status='publish_queued' OR
                (j.status='queued' AND j.ready=1 AND NOT EXISTS (
                    SELECT 1 FROM jobs other WHERE other.project=j.project
                    AND other.status IN ('running','review','publishing','publish_queued')
                )) ORDER BY j.id LIMIT 1""").fetchone()
            if not row:
                return None
            status = "publishing" if row[1] == "publish_queued" else "running"
            db.execute("UPDATE jobs SET status=?, updated=? WHERE id=?", (status, time.time(), row[0]))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row[0],)).fetchone())

    def update(self, job_id, **fields):
        allowed = {"status", "branch", "commit_sha", "report", "cancel", "ready"}
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid job fields")
        fields["updated"] = time.time()
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), job_id))

    def cancel(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return False
            if row[0] == "queued":
                db.execute("UPDATE jobs SET status='cancelled', cancel=1 WHERE id=?", (job_id,))
                return True
            if row[0] == "running":
                db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job_id,))
                return True
            return False

    def recover(self):
        with self.db() as db:
            rows = db.execute("SELECT id FROM jobs WHERE status IN ('running','publishing')").fetchall()
            for row in rows:
                db.execute("UPDATE jobs SET status='interrupted',report=? WHERE id=?", ("Процесс прерван. Проверь рабочее дерево и ветку перед повторным запуском.", row[0]))
                db.execute("INSERT INTO outbox(text) VALUES (?)", (f"Задача #{row[0]} прервана перезапуском. /job {row[0]}",))
            if rows:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('paused','true')")

    def queue_publish(self, job_id):
        with self.db() as db:
            return db.execute("UPDATE jobs SET status='publish_queued' WHERE id=? AND status='review' AND commit_sha IS NOT NULL", (job_id,)).rowcount == 1

    def discard(self, job_id):
        with self.db() as db:
            return db.execute("UPDATE jobs SET status='rejected' WHERE id=? AND status='review'", (job_id,)).rowcount == 1

    def notify(self, text):
        with self.db() as db:
            db.execute("INSERT INTO outbox(text) VALUES (?)", (text[:4000],))

    def messages(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox ORDER BY id LIMIT 10")]

    def delivered(self, message_id):
        with self.db() as db:
            db.execute("DELETE FROM outbox WHERE id=?", (message_id,))

    def schedule(self, project, hours):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO schedules VALUES(?,?,?,1)", (project, hours, time.time() + hours * 3600))

    def schedules(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM schedules ORDER BY project")]

    def tick(self, now=None):
        now = time.time() if now is None else now
        if self.get("paused", False):
            return
        with self.db() as db:
            for row in db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (now,)).fetchall():
                busy = db.execute("SELECT 1 FROM jobs WHERE project=? AND (status IN ('running','review','publishing','publish_queued') OR (status='queued' AND ready=1))", (row["project"],)).fetchone()
                if not busy:
                    db.execute("UPDATE jobs SET ready=1 WHERE id=(SELECT id FROM jobs WHERE project=? AND status='queued' AND ready=0 AND thread_id IS NULL ORDER BY id LIMIT 1)", (row["project"],))
                db.execute("UPDATE schedules SET next_run=? WHERE project=?", (now + row["hours"] * 3600, row["project"]))

    def accept_update(self, update_id):
        with self.db() as db:
            return db.execute("INSERT OR IGNORE INTO updates VALUES (?)", (update_id,)).rowcount == 1
