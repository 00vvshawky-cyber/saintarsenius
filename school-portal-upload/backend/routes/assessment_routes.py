from urllib.parse import urlparse

from flask import Blueprint, request, session

from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.audit import log_action
from backend.utils.authz import ensure_school_week, has_assignment
from backend.utils.timeutil import closes_at_for, now_utc, week_key

assessment_bp = Blueprint("assessments", __name__)

TITLE_MAX = 80


def _https_url(value):
    """رابط اختبار آمن: https فقط (T051) — يرفض javascript: وغيرها."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def _servant_pairs(cursor):
    """(class_id, subject_id) pairs the active servant may manage; None for admin."""
    if session.get("role") != "servant":
        return None
    cursor.execute(
        "SELECT class_id, subject_id FROM servant_assignments WHERE user_id = ?",
        (session["user_id"],),
    )
    return {(r["class_id"], r["subject_id"]) for r in cursor.fetchall()}


@assessment_bp.route("", methods=["GET"])
@require_role("servant", "admin")
def list_assessments():
    conn = get_db()
    cursor = conn.cursor()

    query = """
        SELECT a.id, a.title, a.kind, a.external_url, a.whatsapp_number,
               a.week_key, a.closes_at, a.created_by, a.class_id,
               s.code AS subject_code, s.name_ar AS subject_name,
               c.code AS class_code, c.name_ar AS class_name,
               u.full_name AS created_by_name
        FROM assessments a
        JOIN subjects s ON a.subject_id = s.id
        JOIN classes c ON a.class_id = c.id
        LEFT JOIN users u ON u.id = a.created_by
        WHERE 1=1
    """
    params = []
    for arg, column in (("class_code", "c.code"), ("subject", "s.code"), ("week_key", "a.week_key")):
        val = request.args.get(arg)
        if val:
            query += f" AND {column} = ?"
            params.append(val)

    pairs = _servant_pairs(cursor)
    if pairs is not None:
        if not pairs:
            return ok([])
        ors = " OR ".join(["(a.class_id = ? AND a.subject_id = ?)"] * len(pairs))
        query += f" AND ({ors})"
        for class_id, subject_id in pairs:
            params.extend([class_id, subject_id])

    cursor.execute(query, params)
    now_iso = now_utc().isoformat()
    items = []
    for row in cursor.fetchall():
        item = dict(row)
        item["is_open"] = item["closes_at"] > now_iso
        # إغلاق يدوي (الخادم) أم إغلاق تلقائي (نهاية الأسبوع)؟
        item["closed_manually"] = int(item["closes_at"] != closes_at_for(item["week_key"]))
        # عدّاد الرصد (لشاشة «رصد الدرجات»): عدد الطلاب المسجَّل لهم درجة + إجمالي طلاب الفصل
        cursor.execute(
            """
            SELECT COUNT(*) AS n FROM users u
            JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
            WHERE u.class_id = ? AND u.status = 'active'
            """,
            (item["class_id"],),
        )
        item["total_students"] = cursor.fetchone()["n"]
        cursor.execute(
            "SELECT COUNT(*) AS n FROM grades WHERE assessment_id = ?",
            (item["id"],),
        )
        item["graded_count"] = cursor.fetchone()["n"]
        items.append(item)
    return ok(items)


@assessment_bp.route("", methods=["POST"])
@require_role("servant", "admin")
def create_assessment():
    data = request.get_json(silent=True) or {}
    required = ["class_code", "subject", "title", "kind"]
    if not all(k in data for k in required):
        return fail("VALIDATION_FAILED", "بيانات غير مكتملة", 422)

    kind = data["kind"]
    if kind not in ("quiz", "recitation"):
        return fail("VALIDATION_FAILED", "نوع الاختبار أو التسميع غير صحيح", 422)

    title = str(data.get("title") or "").strip()
    if not title:
        return fail("VALIDATION_FAILED", "عنوان الاختبار أو التسميع مطلوب", 422)
    if len(title) > TITLE_MAX:
        return fail("VALIDATION_FAILED", "العنوان طويل جدًا (80 حرفًا كحد أقصى)", 422)

    external_url = None
    if kind == "quiz":
        if not _https_url(data.get("external_url")):
            return fail("VALIDATION_FAILED", "رابط الاختبار يجب أن يبدأ بـ https", 422)
        external_url = str(data["external_url"]).strip()

    whatsapp_number = None
    if kind == "recitation":
        whatsapp_number = str(data.get("whatsapp_number") or "").strip()
        if not (len(whatsapp_number) == 11 and whatsapp_number.isdigit()):
            return fail("VALIDATION_FAILED", "رقم الواتساب غير صحيح (11 رقمًا)", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name_ar FROM classes WHERE code = ?", (data["class_code"],))
    c_row = cursor.fetchone()
    cursor.execute("SELECT id, name_ar FROM subjects WHERE code = ?", (data["subject"],))
    s_row = cursor.fetchone()
    if not c_row or not s_row:
        return fail("VALIDATION_FAILED", "الفصل أو المادة غير موجودة", 422)
    class_id, subject_id = c_row["id"], s_row["id"]

    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], class_id, subject_id
    ):
        return fail("FORBIDDEN", "غير مسند لك هذا الفصل والمادة", 403)

    wk = week_key()
    closes_at = closes_at_for(wk)

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        ensure_school_week(cur, wk)
        cur.execute(
            """
            INSERT INTO assessments (subject_id, class_id, week_key, title, kind,
                                     external_url, whatsapp_number, closes_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject_id, class_id, wk, title, kind,
                external_url, whatsapp_number, closes_at,
                session["user_id"],
            ),
        )
        assessment_id = cur.lastrowid

    log_action(
        "assessment_create",
        actor_id=session["user_id"],
        target_type="assessment",
        target_id=assessment_id,
        details={
            "title": title,
            "kind": kind,
            "class_name": c_row["name_ar"],
            "subject_name": s_row["name_ar"],
            "week_key": wk,
        },
    )
    return ok({"id": assessment_id, "week_key": wk, "closes_at": closes_at}, 201)


@assessment_bp.route("/<int:assessment_id>", methods=["PATCH"])
@require_role("servant", "admin")
def update_assessment(assessment_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM assessments WHERE id = ?", (assessment_id,))
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "التسميع/الاختبار غير موجود", 404)
    if session["role"] != "admin" and row["created_by"] != session["user_id"]:
        return fail("FORBIDDEN", "لا تملك صلاحية التعديل", 403)

    cursor.execute("SELECT name_ar FROM classes WHERE id = ?", (row["class_id"],))
    class_row = cursor.fetchone()
    cursor.execute("SELECT name_ar FROM subjects WHERE id = ?", (row["subject_id"],))
    subject_row = cursor.fetchone()

    data = request.get_json(silent=True) or {}
    updates, params = [], []
    content_changed = False
    closed_action = None
    new_title = row["title"]
    if "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            return fail("VALIDATION_FAILED", "عنوان الاختبار أو التسميع مطلوب", 422)
        if len(title) > TITLE_MAX:
            return fail("VALIDATION_FAILED", "العنوان طويل جدًا (80 حرفًا كحد أقصى)", 422)
        new_title = title
        content_changed = True
        updates.append("title = ?")
        params.append(title)
    if "external_url" in data:
        content_changed = True
        new_url = data.get("external_url")
        if new_url in (None, ""):
            if row["kind"] == "quiz":
                return fail("VALIDATION_FAILED", "رابط الاختبار إلزامي", 422)
            updates.append("external_url = ?")
            params.append(None)
        else:
            if not _https_url(new_url):
                return fail("VALIDATION_FAILED", "رابط الاختبار يجب أن يبدأ بـ https", 422)
            updates.append("external_url = ?")
            params.append(str(new_url).strip())
    if "whatsapp_number" in data:
        content_changed = True
        wa = data.get("whatsapp_number")
        if wa in (None, ""):
            if row["kind"] == "recitation":
                return fail("VALIDATION_FAILED", "رقم واتساب التسميع إلزامي", 422)
            updates.append("whatsapp_number = ?")
            params.append(None)
        else:
            wa = str(wa).strip()
            if not (len(wa) == 11 and wa.isdigit()):
                return fail("VALIDATION_FAILED", "رقم الواتساب غير صحيح (11 رقمًا)", 422)
            updates.append("whatsapp_number = ?")
            params.append(wa)
    # «إغلاق الآن» / «إعادة فتح» (زرّ في شاشة إدارة التسميع والامتحانات)
    new_closes_at = None
    if "is_closed" in data:
        closed_action = "assessment_close" if data["is_closed"] else "assessment_reopen"
        if data["is_closed"]:
            new_closes_at = now_utc().isoformat()
        else:
            # إعادة الفتح: يبقى مفتوحًا حتى آخر أربعاء (أسبوع العنصر، أو الأسبوع الحالي إن كان أقدم)
            current_wk = week_key()
            target_wk = row["week_key"] if row["week_key"] >= current_wk else current_wk
            new_closes_at = closes_at_for(target_wk)
        updates.append("closes_at = ?")
        params.append(new_closes_at)
    if not updates:
        return fail("VALIDATION_FAILED", "لا توجد بيانات صالحة للتعديل", 422)

    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            f"UPDATE assessments SET {', '.join(updates)} WHERE id = ?",
            (*params, assessment_id),
        )

    log_details = {
        "title": new_title,
        "kind": row["kind"],
        "class_name": class_row["name_ar"] if class_row else None,
        "subject_name": subject_row["name_ar"] if subject_row else None,
        "week_key": row["week_key"],
    }
    if content_changed:
        log_action(
            "assessment_update",
            actor_id=session["user_id"],
            target_type="assessment",
            target_id=assessment_id,
            details=log_details,
        )
    if closed_action:
        log_action(
            closed_action,
            actor_id=session["user_id"],
            target_type="assessment",
            target_id=assessment_id,
            details=log_details,
        )

    result = {"id": assessment_id, "status": "updated"}
    if new_closes_at is not None:
        result["closes_at"] = new_closes_at
        result["is_open"] = new_closes_at > now_utc().isoformat()
        result["closed_manually"] = int(new_closes_at != closes_at_for(row["week_key"]))
    return ok(result)


@assessment_bp.route("/<int:assessment_id>", methods=["DELETE"])
@require_role("admin")
def delete_assessment(assessment_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT a.id, a.title, a.kind, a.week_key, a.class_id, a.subject_id,
               c.name_ar AS class_name, s.name_ar AS subject_name
        FROM assessments a
        JOIN classes c ON a.class_id = c.id
        JOIN subjects s ON a.subject_id = s.id
        WHERE a.id = ?
        """,
        (assessment_id,),
    )
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "التسميع/الاختبار غير موجود", 404)

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        # Remove dependent grades + their ledger entries first.
        cur.execute("SELECT id FROM grades WHERE assessment_id = ?", (assessment_id,))
        grade_ids = [r["id"] for r in cur.fetchall()]
        if grade_ids:
            placeholders = ",".join("?" * len(grade_ids))
            cur.execute(
                f"DELETE FROM point_ledger WHERE kind = 'grade' AND ref_id IN ({placeholders})",
                grade_ids,
            )
            cur.execute(
                f"DELETE FROM grades WHERE assessment_id = ?", (assessment_id,)
            )
        cur.execute("DELETE FROM assessments WHERE id = ?", (assessment_id,))
    log_action(
        "assessment_delete",
        actor_id=session["user_id"],
        target_type="assessment",
        target_id=assessment_id,
        details={
            "title": row["title"],
            "kind": row["kind"],
            "class_name": row["class_name"],
            "subject_name": row["subject_name"],
            "week_key": row["week_key"],
        },
    )
    return ok({"id": assessment_id, "status": "deleted"})
