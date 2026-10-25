"""Sync packages (spec §13): JSON export signed with HMAC over SECRET_KEY."""
import hmac
import hashlib
import json

from flask import Blueprint, request, session

from backend.config import SECRET_KEY
from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.audit import log_action
from backend.utils.authz import current_school_year
from backend.utils.timeutil import now_utc

sync_bp = Blueprint("sync", __name__)

SCHEMA_VERSION = 1

# Order matters for inserts (parents first); imports defer FK checks anyway.
SYNC_TABLES = (
    "classes",
    "subjects",
    "users",
    "user_roles",
    "servant_assignments",
    "settings",
    "school_weeks",
    "attendance",
    "assessments",
    "grades",
    "liturgy_attendance",
    "point_ledger",
    "bonus_budget",
    "wheel_spins_used",
    "followups",
    "notifications",
    "notification_reads",
    "audit_log",
)


def _canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign(data):
    return hmac.new(SECRET_KEY.encode("utf-8"), _canonical(data),
                    hashlib.sha256).hexdigest()


def _export_payload(cursor, school_year):
    tables = {}
    for table in SYNC_TABLES:
        cursor.execute(f"SELECT * FROM {table}")
        tables[table] = [dict(r) for r in cursor.fetchall()]
    return {
        "schema_version": SCHEMA_VERSION,
        "school_year": school_year,
        "generated_at": now_utc().isoformat(),
        "tables": tables,
    }


@sync_bp.route("/export", methods=["GET"])
@require_role("admin")
def export_package():
    conn = get_db()
    cursor = conn.cursor()
    payload = _export_payload(cursor, current_school_year(cursor))
    return ok({**payload, "signature": sign(payload)})


@sync_bp.route("/import", methods=["POST"])
@require_role("admin")
def import_package():
    body = request.get_json(silent=True) or {}
    payload = body.get("payload")
    signature = body.get("signature")
    if not isinstance(payload, dict):
        return fail("VALIDATION_FAILED", "الحزمة أو التوقيع مفقود", 422)

    # Tolerate both shapes: {payload, signature} and the full exported package
    # passed as payload (signature inside it too).
    if payload.get("signature") and not signature:
        signature = payload["signature"]
    payload = {k: v for k, v in payload.items() if k != "signature"}
    if not signature:
        return fail("VALIDATION_FAILED", "الحزمة أو التوقيع مفقود", 422)

    if payload.get("schema_version") != SCHEMA_VERSION:
        return fail("VALIDATION_FAILED", "إصدار مخطط غير مدعوم", 422)
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        return fail("VALIDATION_FAILED", "محتوى الحزمة غير صالح", 422)

    expected = sign(payload)
    if not hmac.compare_digest(str(signature), expected):
        # Contract: 409 when the signature fails.
        return fail("SIGNATURE_INVALID", "توقيع الحزمة غير صالح", 409)

    unknown = set(tables) - set(SYNC_TABLES)
    if unknown:
        return fail("VALIDATION_FAILED", f"جداول غير معروفة: {sorted(unknown)}", 422)

    imported = {}
    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute("PRAGMA defer_foreign_keys = ON")
        # Delete children first (reverse order), then insert parents first.
        for table in reversed(SYNC_TABLES):
            if table in tables:
                cur.execute(f"DELETE FROM {table}")
        for table in SYNC_TABLES:
            rows = tables.get(table)
            if not isinstance(rows, list):
                continue
            for row in rows:
                columns = list(row.keys())
                cur.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' * len(columns))})",
                    [row[c] for c in columns],
                )
            imported[table] = len(rows)

    log_action("sync_import", actor_id=session["user_id"],
               details={"tables": imported})
    return ok({"imported": imported, "school_year": payload.get("school_year")})
