import os
import sqlite3
from contextlib import contextmanager

from flask import g, has_app_context

from backend.config import DB_PATH

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def _connect():
    # Load DB_PATH from config, but allow override if set in environment
    db_file = os.getenv("DB_PATH", DB_PATH)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def get_db():
    # Inside a Flask request/app context: cache one connection per context in
    # ``g`` so teardown_appcontext can close it (no more leaked connections).
    # Outside any context (seed/init scripts, tests): return a plain
    # connection the caller owns and must close itself.
    if has_app_context():
        if "db_conn" not in g:
            g.db_conn = _connect()
        return g.db_conn
    return _connect()


def run_schema():
    conn = _connect()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()
    conn.executescript(schema_sql)
    ensure_columns(conn)
    conn.close()


# أعمدة أُضيفت بعد أول إصدار للمخطط (تُضاف تلقائيًا لقواعد البيانات القائمة)
_MIGRATIONS = (
    ("users", "suspension_reason", "TEXT"),
    ("school_weeks", "is_open", "INTEGER NOT NULL DEFAULT 0"),
    ("classes", "google_account", "TEXT"),
)

# جداول أُضيفت بعد أول إصدار للمخطط
_TABLES = (
    """CREATE TABLE IF NOT EXISTS followup_notes (
         id INTEGER PRIMARY KEY,
         student_id INTEGER NOT NULL REFERENCES users(id),
         kind TEXT NOT NULL DEFAULT 'call' CHECK (kind IN ('call','visit','chat')),
         note TEXT NOT NULL,
         created_by INTEGER REFERENCES users(id),
         created_at TEXT NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS idx_followup_notes_student ON followup_notes(student_id, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS assessment_requests (
         id INTEGER PRIMARY KEY,
         assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
         student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
         requested_at TEXT NOT NULL,
         status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','graded')),
         UNIQUE (assessment_id, student_id)
       )""",
    "CREATE INDEX IF NOT EXISTS idx_assessment_requests_assessment ON assessment_requests(assessment_id, status)",
    """CREATE TABLE IF NOT EXISTS followup_assignments (
         id INTEGER PRIMARY KEY,
         student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
         servant_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
         week_key TEXT NOT NULL,
         created_at TEXT NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS idx_followup_assignments_student ON followup_assignments(student_id, created_at DESC)",
)


def ensure_columns(conn=None):
    """يضيف الأعمدة/الجداول الجديدة للقواعد المُنشأة قبلها (ترحيل آمن)."""
    own = conn is None
    if own:
        conn = _connect()
    try:
        for table, column, coltype in _MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        for statement in _TABLES:
            conn.execute(statement)
        conn.commit()
    finally:
        if own:
            conn.close()


def close_db(exception=None):
    # Called from app.teardown_appcontext; closes the per-context connection.
    if has_app_context():
        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()


@contextmanager
def db_transaction():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
