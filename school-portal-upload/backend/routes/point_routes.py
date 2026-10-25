from flask import Blueprint, request, session

from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.audit import log_action
from backend.utils.authz import current_school_year, ensure_school_week, has_assignment
from backend.utils.points import points_total, student_points_payload
from backend.utils.timeutil import now_utc, week_key

point_bp = Blueprint("points", __name__)

BONUS_BUDGET_TOTAL = 10  # per (servant, subject, week) — data-model §2.5


def _positive_int(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _student_class(cursor, student_id):
    cursor.execute("SELECT id, class_id, full_name FROM users WHERE id = ?", (student_id,))
    return cursor.fetchone()


@point_bp.route("/bonus/budget", methods=["GET"])
@require_role("servant")
def bonus_budget():
    """Per-subject weekly bonus budget of the logged-in servant.

    `?class_code=` يقصر الرصيد على مواد ذلك الفصل (ما يهمّ الخادم في سياق
    الفصل الحالي) — وإلّا فكل موادّه المسندة.
    """
    wk = week_key()
    servant_id = session["user_id"]
    class_code = request.args.get("class_code")
    conn = get_db()
    cursor = conn.cursor()

    query = """
        SELECT DISTINCT s.id, s.code
        FROM servant_assignments sa
        JOIN subjects s ON sa.subject_id = s.id
        JOIN classes c ON c.id = sa.class_id
        WHERE sa.user_id = ?
    """
    params = [servant_id]
    if class_code:
        query += " AND c.code = ?"
        params.append(class_code)
    cursor.execute(query, params)
    subjects = cursor.fetchall()
    year = current_school_year(cursor)

    result = []
    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        # ensure_school_week: صف رصيد البونص يشير إلى school_weeks (مفتاح أجنبي)
        ensure_school_week(cur, wk)
        for s in subjects:
            cur.execute(
                "SELECT total FROM bonus_budget WHERE servant_id = ? AND subject_id = ? AND week_key = ?",
                (servant_id, s["id"], wk),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO bonus_budget (servant_id, subject_id, week_key, total) VALUES (?, ?, ?, ?)",
                    (servant_id, s["id"], wk, BONUS_BUDGET_TOTAL),
                )
                total = BONUS_BUDGET_TOTAL
            else:
                total = row["total"]
            cur.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS used FROM point_ledger
                WHERE actor_id = ? AND subject_id = ? AND week_key = ? AND kind = 'bonus'
                """,
                (servant_id, s["id"], wk),
            )
            used = cur.fetchone()["used"]
            result.append({
                "subject": s["code"],
                "week_key": wk,
                "total": total,
                "used": used,
                "remaining": total - used,
            })
    return ok(result)


@point_bp.route("/ledger", methods=["GET"])
@require_role("servant", "admin")
def list_ledger():
    """سجل قيود الخادم (المكافآت/الخصومات) — لشاشة «البونص والخصم»."""
    class_code = request.args.get("class_code")
    kind = request.args.get("kind")
    limit = request.args.get("limit") or 20
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20

    conn = get_db()
    cursor = conn.cursor()

    query = """
        SELECT pl.id, pl.student_id, u.full_name, pl.kind, pl.amount, pl.reason,
               pl.week_key, pl.created_at, pl.actor_id,
               actor.full_name AS actor_name,
               s.name_ar AS subject_name, c.code AS class_code, c.name_ar AS class_name
        FROM point_ledger pl
        JOIN users u ON u.id = pl.student_id
        LEFT JOIN users actor ON actor.id = pl.actor_id
        LEFT JOIN subjects s ON s.id = pl.subject_id
        LEFT JOIN classes c ON c.id = u.class_id
        WHERE 1=1
    """
    params = []

    if session["role"] == "servant":
        # الخادم يرى قيوده هو فقط
        query += " AND pl.actor_id = ?"
        params.append(session["user_id"])
        cursor.execute(
            "SELECT DISTINCT class_id FROM servant_assignments WHERE user_id = ?",
            (session["user_id"],),
        )
        allowed = [r["class_id"] for r in cursor.fetchall()]
        if not allowed:
            return ok([])
        placeholders = ",".join("?" * len(allowed))
        query += f" AND u.class_id IN ({placeholders})"
        params.extend(allowed)

    if class_code:
        query += " AND c.code = ?"
        params.append(class_code)
    if kind:
        query += " AND pl.kind = ?"
        params.append(kind)

    query += " ORDER BY pl.created_at DESC, pl.id DESC LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    return ok([dict(r) for r in cursor.fetchall()])


@point_bp.route("/bonus", methods=["POST"])
@require_role("servant")
def add_bonus():
    data = request.get_json(silent=True) or {}
    if not all(k in data for k in ("student_id", "subject", "amount")):
        return fail("VALIDATION_FAILED", "بيانات غير مكتملة", 422)

    amount = _positive_int(data["amount"])
    if amount is None:
        return fail("VALIDATION_FAILED", "القيمة يجب أن تكون رقمًا موجبًا", 422)
    # العقد api.md §8: السبب اختياري للمكافأة (`reason?`) — وإلزامي للخصم فقط.
    reason = (data.get("reason") or "").strip() or None

    servant_id = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()

    student = _student_class(cursor, data["student_id"])
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)

    cursor.execute("SELECT id, name_ar FROM subjects WHERE code = ?", (data["subject"],))
    s_row = cursor.fetchone()
    if not s_row:
        return fail("VALIDATION_FAILED", "المادة غير موجودة", 422)
    subject_id = s_row["id"]

    # The servant may only bonus students of classes he serves in this subject.
    if not has_assignment(cursor, servant_id, student["class_id"], subject_id):
        return fail("FORBIDDEN", "غير مسند لك هذا الفصل والمادة", 403)

    wk = week_key()
    year = current_school_year(cursor)
    now_iso = now_utc().isoformat()

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        ensure_school_week(cur, wk)
        cur.execute(
            "SELECT total FROM bonus_budget WHERE servant_id = ? AND subject_id = ? AND week_key = ?",
            (servant_id, subject_id, wk),
        )
        row = cur.fetchone()
        total = row["total"] if row else BONUS_BUDGET_TOTAL
        if not row:
            cur.execute(
                "INSERT INTO bonus_budget (servant_id, subject_id, week_key, total) VALUES (?, ?, ?, ?)",
                (servant_id, subject_id, wk, BONUS_BUDGET_TOTAL),
            )
        cur.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS used FROM point_ledger
            WHERE actor_id = ? AND subject_id = ? AND week_key = ? AND kind = 'bonus'
            """,
            (servant_id, subject_id, wk),
        )
        used = cur.fetchone()["used"]
        if amount > total - used:
            return fail("BONUS_EXCEEDED", "تجاوزت الحد الأقصى للبونص هذا الأسبوع", 422)

        cur.execute(
            """
            INSERT INTO point_ledger (student_id, week_key, kind, amount, subject_id,
                                      reason, actor_id, school_year, created_at)
            VALUES (?, ?, 'bonus', ?, ?, ?, ?, ?, ?)
            """,
            (student["id"], wk, amount, subject_id, reason, servant_id, year, now_iso),
        )
        ledger_id = cur.lastrowid

    log_action(
        "bonus_add",
        actor_id=servant_id,
        target_type="student",
        target_id=student["id"],
        details={
            "student_name": student["full_name"],
            "subject_name": s_row["name_ar"],
            "amount": amount,
            "reason": reason,
            "week_key": wk,
        },
    )
    return ok({"id": ledger_id, "student_id": student["id"], "amount": amount}, 201)


@point_bp.route("/deduction", methods=["POST"])
@require_role("servant", "admin")
def add_deduction():
    data = request.get_json(silent=True) or {}
    if not all(k in data for k in ("student_id", "amount")):
        return fail("VALIDATION_FAILED", "بيانات غير مكتملة", 422)

    amount = _positive_int(data["amount"])
    if amount is None:
        return fail("VALIDATION_FAILED", "القيمة يجب أن تكون رقمًا موجبًا", 422)
    reason = (data.get("reason") or "").strip()
    if not reason:
        return fail("VALIDATION_FAILED", "السبب إلزامي", 422)

    conn = get_db()
    cursor = conn.cursor()
    student = _student_class(cursor, data["student_id"])
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)

    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], student["class_id"]
    ):
        return fail("FORBIDDEN", "هذا الطالب ليس في فصل مسند لك", 403)

    wk = week_key()
    year = current_school_year(cursor)
    now_iso = now_utc().isoformat()

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        ensure_school_week(cur, wk)
        cur.execute(
            """
            INSERT INTO point_ledger (student_id, week_key, kind, amount, reason,
                                      actor_id, school_year, created_at)
            VALUES (?, ?, 'deduction', ?, ?, ?, ?, ?)
            """,
            (student["id"], wk, -amount, reason, session["user_id"], year, now_iso),
        )
        ledger_id = cur.lastrowid
    log_action(
        "deduction_add",
        actor_id=session["user_id"],
        target_type="student",
        target_id=student["id"],
        details={
            "student_name": student["full_name"],
            "amount": amount,
            "reason": reason,
            "week_key": wk,
        },
    )
    return ok({"id": ledger_id, "student_id": student["id"], "amount": -amount}, 201)


@point_bp.route("/ledger/<int:ledger_id>", methods=["PUT"])
@require_role("admin")
def update_ledger(ledger_id):
    data = request.get_json(silent=True) or {}

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT pl.id, pl.kind, pl.amount, pl.reason, pl.student_id,
               u.full_name AS student_name
        FROM point_ledger pl JOIN users u ON u.id = pl.student_id
        WHERE pl.id = ?
        """,
        (ledger_id,),
    )
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "القيد غير موجود", 404)

    kind = row["kind"]
    if kind not in ("bonus", "deduction"):
        return fail(
            "VALIDATION_FAILED",
            "هذا القيد محسوب تلقائيًا — عدّل مصدره (الدرجة/الحضور) بدلًا من تعديله مباشرة",
            422,
        )

    updates, params = [], []
    new_amount = row["amount"]
    new_reason = row["reason"]
    if "amount" in data:
        amount = _positive_int(data["amount"])
        if amount is None:
            return fail("VALIDATION_FAILED", "القيمة يجب أن تكون رقمًا موجبًا", 422)
        # الخصم يُخزَّن سالبًا — الحفاظ على الإشارة حتى لا ينقلب نوع القيد
        new_amount = -amount if kind == "deduction" else amount
        updates.append("amount = ?")
        params.append(new_amount)
    if "reason" in data:
        reason = (data["reason"] or "").strip()
        if not reason:
            return fail("VALIDATION_FAILED", "السبب لا يمكن أن يكون فارغًا", 422)
        new_reason = reason
        updates.append("reason = ?")
        params.append(reason)
    if not updates:
        return fail("VALIDATION_FAILED", "لا توجد بيانات صالحة للتعديل", 422)

    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            f"UPDATE point_ledger SET {', '.join(updates)} WHERE id = ?",
            (*params, ledger_id),
        )
    log_action(
        "ledger_update",
        actor_id=session["user_id"],
        target_type="student",
        target_id=row["student_id"],
        details={
            "student_name": row["student_name"],
            "kind": kind,
            "old_amount": row["amount"],
            "new_amount": new_amount,
            "old_reason": row["reason"],
            "new_reason": new_reason,
        },
    )
    return ok({"id": ledger_id, "status": "updated",
               "kind": kind, "amount": new_amount})


@point_bp.route("/ledger/<int:ledger_id>", methods=["DELETE"])
@require_role("admin")
def delete_ledger(ledger_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT pl.id, pl.kind, pl.amount, pl.reason, pl.student_id,
               u.full_name AS student_name
        FROM point_ledger pl JOIN users u ON u.id = pl.student_id
        WHERE pl.id = ?
        """,
        (ledger_id,),
    )
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "القيد غير موجود", 404)
    with db_transaction() as conn_tx:
        conn_tx.cursor().execute("DELETE FROM point_ledger WHERE id = ?", (ledger_id,))
    log_action(
        "ledger_delete",
        actor_id=session["user_id"],
        target_type="student",
        target_id=row["student_id"],
        details={
            "student_name": row["student_name"],
            "kind": row["kind"],
            "amount": row["amount"],
            "reason": row["reason"],
        },
    )
    return ok({"id": ledger_id, "status": "deleted"})


@point_bp.route("/student/<int:student_id>", methods=["GET"])
@require_role("servant", "admin")
def student_points(student_id):
    conn = get_db()
    cursor = conn.cursor()
    student = _student_class(cursor, student_id)
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)
    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], student["class_id"]
    ):
        return fail("FORBIDDEN", "هذا الطالب ليس في فصل مسند لك", 403)

    year = current_school_year(cursor)
    payload = student_points_payload(cursor, student_id, year)
    payload["student"] = {"id": student["id"], "full_name": student["full_name"]}
    return ok(payload)


@point_bp.route("/ranking", methods=["GET"])
@require_role("servant", "admin")
def ranking():
    class_code = request.args.get("class_code")
    conn = get_db()
    cursor = conn.cursor()
    year = current_school_year(cursor)

    query = """
        SELECT u.id AS student_id, u.full_name, c.code AS class_code,
               COALESCE(SUM(pl.amount), 0) AS total
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
        LEFT JOIN classes c ON u.class_id = c.id
        LEFT JOIN point_ledger pl ON pl.student_id = u.id AND pl.school_year = ?
        WHERE u.status = 'active'
    """
    params = [year]

    if session["role"] == "servant":
        cursor.execute(
            "SELECT DISTINCT c.code FROM servant_assignments sa "
            "JOIN classes c ON sa.class_id = c.id WHERE sa.user_id = ?",
            (session["user_id"],),
        )
        allowed = [r["code"] for r in cursor.fetchall()]
        if not allowed:
            return ok([])
        placeholders = ",".join("?" * len(allowed))
        query += f" AND c.code IN ({placeholders})"
        params.extend(allowed)

    if class_code:
        query += " AND c.code = ?"
        params.append(class_code)

    query += " GROUP BY u.id ORDER BY total DESC, u.full_name"
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return ok(rows)
