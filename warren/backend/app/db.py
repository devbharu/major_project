"""SQLite access. One connection guarded by a lock keeps :memory: mode safe across worker threads."""
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, github_id INTEGER UNIQUE, login TEXT, name TEXT, avatar_url TEXT,
  token TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS repos(
  id INTEGER PRIMARY KEY, user_id INTEGER, full_name TEXT, owner TEXT, name TEXT,
  default_branch TEXT, private INTEGER DEFAULT 0, enabled INTEGER DEFAULT 0, webhook_id INTEGER,
  settings TEXT DEFAULT '{}', last_synced TEXT, watch_since TEXT, created_at TEXT, UNIQUE(user_id, full_name));
CREATE TABLE IF NOT EXISTS pull_requests(
  id INTEGER PRIMARY KEY, repo_id INTEGER, number INTEGER, title TEXT, body TEXT, author TEXT,
  author_avatar TEXT, is_agent INTEGER DEFAULT 0, state TEXT, draft INTEGER DEFAULT 0,
  head_ref TEXT, head_sha TEXT, base_ref TEXT, head_repo TEXT, additions INTEGER DEFAULT 0,
  deletions INTEGER DEFAULT 0, changed_files INTEGER DEFAULT 0, can_modify INTEGER DEFAULT 0, labels TEXT DEFAULT '[]',
  requested_reviewers TEXT DEFAULT '[]', url TEXT, mergeable TEXT, review_status TEXT DEFAULT 'none',
  reviewed_sha TEXT, triage TEXT DEFAULT '{}', change_stack TEXT DEFAULT '{}', summary TEXT,
  premerge TEXT DEFAULT '[]', created_gh TEXT, updated_gh TEXT, updated_at TEXT,
  UNIQUE(repo_id, number));
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY, pr_id INTEGER, repo_id INTEGER, source TEXT, severity TEXT, category TEXT,
  title TEXT, body TEXT, path TEXT, line INTEGER, end_line INTEGER, suggestion TEXT,
  status TEXT DEFAULT 'open', commit_sha TEXT, comment_id INTEGER, meta TEXT DEFAULT '{}', created_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, pr_id INTEGER, actor TEXT, actor_kind TEXT, kind TEXT, body TEXT,
  meta TEXT DEFAULT '{}', created_at TEXT, incident_id INTEGER);
CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY, repo_id INTEGER, pr_id INTEGER, run_id INTEGER, run_url TEXT, workflow TEXT,
  job_name TEXT, step_name TEXT, branch TEXT, head_sha TEXT, kind TEXT DEFAULT 'ci', category TEXT,
  status TEXT DEFAULT 'detected', root_cause TEXT, solution TEXT, confidence REAL DEFAULT 0,
  attempts INTEGER DEFAULT 0, fix_commit TEXT, fix_pr_url TEXT, log_excerpt TEXT, thread_id TEXT, fingerprint TEXT, parent_id INTEGER,
  error TEXT, meta TEXT DEFAULT '{}', created_at TEXT, updated_at TEXT, UNIQUE(repo_id, run_id));
CREATE TABLE IF NOT EXISTS learnings(
  id INTEGER PRIMARY KEY, repo_id INTEGER, text TEXT, path_hint TEXT, source_pr INTEGER,
  created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT, repo_id INTEGER, pr_id INTEGER, incident_id INTEGER, status TEXT, step TEXT,
  error TEXT, seconds INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS scans(
  id INTEGER PRIMARY KEY, repo_id INTEGER, kind TEXT, counts TEXT, created_at TEXT);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_pr_repo_state ON pull_requests(repo_id, state);
CREATE INDEX IF NOT EXISTS idx_findings_pr ON findings(pr_id, status);
CREATE INDEX IF NOT EXISTS idx_findings_repo ON findings(repo_id, source, status);
CREATE INDEX IF NOT EXISTS idx_events_pr ON events(pr_id);
CREATE INDEX IF NOT EXISTS idx_events_incident ON events(incident_id);
CREATE INDEX IF NOT EXISTS idx_incidents_repo ON incidents(repo_id, status);
CREATE INDEX IF NOT EXISTS idx_incidents_pr ON incidents(pr_id);
CREATE INDEX IF NOT EXISTS idx_incidents_fp ON incidents(repo_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_target ON jobs(pr_id, incident_id, status);
CREATE INDEX IF NOT EXISTS idx_scans_repo ON scans(repo_id);
"""
TABLES = {"users", "repos", "pull_requests", "findings", "events", "learnings", "jobs", "scans", "incidents"}
IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> str:
    """Table/column names are interpolated into SQL, so they must be plain identifiers (values always use ? placeholders)."""
    if not IDENT.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


JSON_COLUMNS = {"settings", "labels", "requested_reviewers", "triage", "change_stack", "premerge", "meta", "counts"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
                self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(events)")}
            if "incident_id" not in cols:   # migrate databases created before incidents existed
                self._conn.execute("ALTER TABLE events ADD COLUMN incident_id INTEGER")
            pr_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(pull_requests)")}
            if "can_modify" not in pr_cols:
                self._conn.execute("ALTER TABLE pull_requests ADD COLUMN can_modify INTEGER DEFAULT 0")
            repo_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(repos)")}
            if "watch_since" not in repo_cols:
                self._conn.execute("ALTER TABLE repos ADD COLUMN watch_since TEXT")
            inc_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(incidents)")}
            for col, typ in (("fingerprint", "TEXT"), ("parent_id", "INTEGER")):
                if col not in inc_cols:
                    self._conn.execute(f"ALTER TABLE incidents ADD COLUMN {col} {typ}")
            self._conn.executescript(INDEXES)

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        out = dict(row)
        for key in JSON_COLUMNS & out.keys():
            try:
                out[key] = json.loads(out[key]) if out[key] else ({} if key in ("settings", "triage", "change_stack", "meta") else [])
            except (TypeError, ValueError):
                out[key] = {}
        return out

    @staticmethod
    def _encode(values: dict) -> dict:
        return {k: (json.dumps(v) if k in JSON_COLUMNS and not isinstance(v, str) else v) for k, v in values.items()}

    def query(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [self._decode(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args=()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid

    def insert(self, table: str, values: dict) -> int:
        values = self._encode(values)
        _ident(table)
        cols = ", ".join(_ident(k) for k in values)
        marks = ", ".join("?" for _ in values)
        return self.execute(f"INSERT INTO {table}({cols}) VALUES({marks})", tuple(values.values()))  # nosec B608 - identifiers validated

    def update(self, table: str, row_id: int, values: dict) -> None:
        values = self._encode(values)
        _ident(table)
        sets = ", ".join(f"{_ident(k)}=?" for k in values)
        self.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*values.values(), row_id))  # nosec B608 - identifiers validated

    def get(self, table: str, row_id: int) -> dict | None:
        return self.one(f"SELECT * FROM {_ident(table)} WHERE id=?", (row_id,))  # nosec B608 - identifier validated

    def upsert(self, table: str, keys: dict, values: dict) -> int:
        """Insert or update the row matching `keys`; returns its id."""
        where = " AND ".join(f"{_ident(k)}=?" for k in keys)
        row = self.one(f"SELECT id FROM {_ident(table)} WHERE {where}", tuple(keys.values()))  # nosec B608 - identifiers validated
        if row:
            self.update(table, row["id"], values)
            return row["id"]
        return self.insert(table, {**keys, **values})
