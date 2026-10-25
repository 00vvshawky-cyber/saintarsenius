from flask import Blueprint, request, session

from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.audit import log_action
from backend.utils.authz import current_school_year
from backend.utils.points import wheel_summary
from backend.utils.timeutil import now_utc

wheel_bp = Blueprint("wheel", __name__)


@wheel_bp.route("/<int:student_id>", methods=["GET"])
@require_role("admin")
def wheel_status(student_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (student_id,))
    if not cursor.fetchone():
        return fail("NOT_FOUND", "الطالب غير موجود", 404)
    year = current_school_year(cursor)
    return ok(wheel_summary(cursor, student_id, year))


@wheel_bp.route("/<int:student_id>/use", methods=["POST"])
@require_role("admin")
def use_spin(student_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, full_name FROM users WHERE id = ?", (student_id,))
    student = cursor.fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)

    year = current_school_year(cursor)
    summary = wheel_summary(cursor, student_id, year)
    if summary["spins_available"] <= 0:
        return fail(
            "VALIDATION_FAILED",
            "لا توجد لفات متاحة — لفات مستحقة = (الأختام ÷ 4) − المستخدمة",
            409,
        )

    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            """
            INSERT INTO wheel_spins_used (student_id, school_year, used_at, actor_id)
            VALUES (?, ?, ?, ?)
            """,
            (student_id, year, now_utc().isoformat(), session["user_id"]),
        )
    log_action(
        "wheel_spin",
        actor_id=session["user_id"],
        target_type="student",
        target_id=student_id,
        details={
            "student_name": student["full_name"],
            "spins_used": summary["spins_used"] + 1,
            "spins_available": summary["spins_available"] - 1,
        },
    )
    return ok({
        "student_id": student_id,
        "spins_used": summary["spins_used"] + 1,
        "spins_available": summary["spins_available"] - 1,
    })
