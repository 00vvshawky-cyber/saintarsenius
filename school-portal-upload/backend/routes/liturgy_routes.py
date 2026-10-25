from flask import Blueprint, request, session

from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.audit import log_action
from backend.utils.authz import current_school_year, ensure_school_week, has_assignment
from backend.utils.timeutil import now_utc, week_key

liturgy_bp = Blueprint("liturgy", __name__)


def _parse_quiet(raw):
    """SRS: quiet=1 → +10 points (six-requirement stamp); quiet=0 → no points."""
    if raw in (1, True, "1"):
        return 1
    if raw in (0, False, "0"):
        return 0
    return None


def _sync_liturgy_ledger(cur, student_id, wk, quiet, actor_id):
    """Keep one 'liturgy' ledger entry per (student, week): +10 when quiet."""
    cur.execute(
        "SELECT id FROM point_ledger WHERE student_id = ? AND week_key = ? AND kind = 'liturgy'",
        (student_id, wk),
    )
    row = cur.fetchone()
    if quiet == 1:
        now_iso = now_utc().isoformat()
        if row:
            cur.execute(
                "UPDATE point_ledger SET amount = 10, actor_id = ?, created_at = ? WHERE id = ?",
                (actor_id, now_iso, row["id"]),
            )
        else:
            cur.execute(
                """
                INSERT INTO point_ledger (student_id, week_key, kind, amount, actor_id, school_year, created_at)
                VALUES (?, ?, 'liturgy', 10, ?, ?, ?)
                """,
                (student_id, wk, actor_id, current_school_year(cur), now_iso),
            )
    elif row:
        cur.execute("DELETE FROM point_ledger WHERE id = ?", (row["id"],))


def _check_student_class(cursor, student_id):
    """Return the student's row, enforcing servant assignment (403/404)."""
    cursor.execute(
        "SELECT id, class_id, full_name FROM users WHERE id = ?", (student_id,)
    )
    student = cursor.fetchone()
    if not student:
        return None, fail("NOT_FOUND", "الطالب غير موجود", 404)
    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], student["class_id"]
    ):
        return None, fail("FORBIDDEN", "هذا الطالب ليس في فصل مسند لك", 403)
    return student, None


@liturgy_bp.route("", methods=["GET"])
@require_role("servant", "admin")
def list_liturgy():
    class_code = request.args.get("class_code")
    wk = request.args.get("week_key") or week_key()
    if not class_code:
        return fail("VALIDATION_FAILED", "يرجى تحديد الفصل", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM classes WHERE code = ?", (class_code,))
    c_row = cursor.fetchone()
    if not c_row:
        return fail("NOT_FOUND", "الفصل غير موجود", 404)

    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], c_row["id"]
    ):
        return fail("FORBIDDEN", "هذا الفصل غير مسند لك", 403)

    cursor.execute(
        """
        SELECT la.student_id, u.full_name, la.week_key, la.quiet, la.marked_by
        FROM liturgy_attendance la
        JOIN users u ON la.student_id = u.id
        WHERE u.class_id = ? AND la.week_key = ?
        ORDER BY u.full_name
        """,
        (c_row["id"], wk),
    )
    return ok([dict(r) for r in cursor.fetchall()])


@liturgy_bp.route("/candidates", methods=["GET"])
@require_role("servant", "admin")
def candidates():
    class_code = request.args.get("class_code")
    if not class_code:
        return fail("VALIDATION_FAILED", "يرجى تحديد الفصل", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM classes WHERE code = ?", (class_code,))
    c_row = cursor.fetchone()
    if not c_row:
        return fail("NOT_FOUND", "الفصل غير موجود", 404)

    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], c_row["id"]
    ):
        return fail("FORBIDDEN", "هذا الفصل غير مسند لك", 403)

    cursor.execute(
        """
        SELECT u.id, u.full_name
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
        WHERE u.class_id = ? AND u.status = 'active'
        ORDER BY u.full_name
        """,
        (c_row["id"],),
    )
    return ok([dict(r) for r in cursor.fetchall()])


@liturgy_bp.route("", methods=["POST"])
@require_role("servant", "admin")
def add_liturgy():
    data = request.get_json(silent=True) or {}
    if not all(k in data for k in ("student_id", "week_key", "quiet")):
        return fail("VALIDATION_FAILED", "بيانات غير مكتملة", 422)
    quiet = _parse_quiet(data["quiet"])
    if quiet is None:
        return fail("VALIDATION_FAILED", "قيمة الهدوء يجب أن تكون 0 أو 1", 422)
    wk = data["week_key"]
    try:
        from datetime import datetime
        datetime.strptime(wk, "%Y-%m-%d")
    except ValueError:
        return fail("VALIDATION_FAILED", "صيغة الأسبوع غير صحيحة (YYYY-MM-DD)", 422)

    conn = get_db()
    cursor = conn.cursor()
    student, err = _check_student_class(cursor, data["student_id"])
    if err:
        return err

    cursor.execute(
        "SELECT id FROM liturgy_attendance WHERE student_id = ? AND week_key = ?",
        (student["id"], wk),
    )
    if cursor.fetchone():
        return fail("LITURGY_EXISTS", "طالب مسجّل بالفعل لهذا الأسبوع — استخدم التعديل", 409)

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        ensure_school_week(cur, wk)
        cur.execute(
            "INSERT INTO liturgy_attendance (student_id, week_key, quiet, marked_by) VALUES (?, ?, ?, ?)",
            (student["id"], wk, quiet, session["user_id"]),
        )
        _sync_liturgy_ledger(cur, student["id"], wk, quiet, session["user_id"])
    log_action(
        "liturgy_mark",
        actor_id=session["user_id"],
        target_type="student",
        target_id=student["id"],
        details={"student_name": student["full_name"], "week_key": wk, "quiet": quiet},
    )
    return ok({"student_id": student["id"], "week_key": wk, "quiet": quiet}, 201)


@liturgy_bp.route("/<int:student_id>", methods=["PUT"])
@require_role("servant", "admin")
def update_liturgy(student_id):
    data = request.get_json(silent=True) or {}
    quiet = _parse_quiet(data.get("quiet"))
    wk = data.get("week_key") or week_key()
    if quiet is None:
        return fail("VALIDATION_FAILED", "قيمة الهدوء يجب أن تكون 0 أو 1", 422)

    conn = get_db()
    cursor = conn.cursor()
    student, err = _check_student_class(cursor, student_id)
    if err:
        return err

    cursor.execute(
        "SELECT id FROM liturgy_attendance WHERE student_id = ? AND week_key = ?",
        (student_id, wk),
    )
    if not cursor.fetchone():
        return fail("NOT_FOUND", "لا يوجد سجل قداس لهذا الطالب والأسبوع", 404)

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute(
            "UPDATE liturgy_attendance SET quiet = ?, marked_by = ? WHERE student_id = ? AND week_key = ?",
            (quiet, session["user_id"], student_id, wk),
        )
        _sync_liturgy_ledger(cur, student_id, wk, quiet, session["user_id"])
    log_action(
        "liturgy_update",
        actor_id=session["user_id"],
        target_type="student",
        target_id=student_id,
        details={"student_name": student["full_name"], "week_key": wk, "quiet": quiet},
    )
    return ok({"student_id": student_id, "week_key": wk, "quiet": quiet})


@liturgy_bp.route("/<int:student_id>", methods=["DELETE"])
@require_role("servant", "admin")
def delete_liturgy(student_id):
    wk = request.args.get("week_key") or week_key()

    conn = get_db()
    cursor = conn.cursor()
    student, err = _check_student_class(cursor, student_id)
    if err:
        return err

    cursor.execute(
        "SELECT id FROM liturgy_attendance WHERE student_id = ? AND week_key = ?",
        (student_id, wk),
    )
    if not cursor.fetchone():
        return fail("NOT_FOUND", "لا يوجد سجل قداس لهذا الطالب والأسبوع", 404)

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute(
            "DELETE FROM liturgy_attendance WHERE student_id = ? AND week_key = ?",
            (student_id, wk),
        )
        _sync_liturgy_ledger(cur, student_id, wk, 0, session["user_id"])
    log_action(
        "liturgy_delete",
        actor_id=session["user_id"],
        target_type="student",
        target_id=student_id,
        details={"student_name": student["full_name"], "week_key": wk},
    )
    return ok({"student_id": student_id, "week_key": wk, "status": "deleted"})
