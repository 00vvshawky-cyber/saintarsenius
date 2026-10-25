from flask import Blueprint, request, session

from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.audit import log_action
from backend.utils.authz import (
    assigned_subject_ids, current_school_year, has_assignment,
)
from backend.utils.timeutil import now_utc

grade_bp = Blueprint("grades", __name__)


def _upsert_weekly_grade_ledger(cur, student_id, subject_id, wk, actor_id, ref_id, now_iso):
    """Weekly subject grade = highest score among that subject's assessments
    this week; keep exactly one 'grade' ledger entry per (student, week, subject)."""
    cur.execute(
        """
        SELECT MAX(g.score) AS top FROM grades g
        JOIN assessments a ON g.assessment_id = a.id
        WHERE g.student_id = ? AND a.subject_id = ? AND a.week_key = ?
        """,
        (student_id, subject_id, wk),
    )
    top = cur.fetchone()["top"]
    cur.execute(
        """
        UPDATE point_ledger SET amount = ?, ref_id = ?, created_at = ?
        WHERE student_id = ? AND week_key = ? AND kind = 'grade' AND subject_id = ?
        """,
        (top, ref_id, now_iso, student_id, wk, subject_id),
    )
    if cur.rowcount == 0:
        cur.execute(
            """
            INSERT INTO point_ledger (student_id, week_key, kind, amount, subject_id,
                                      ref_id, actor_id, school_year, created_at)
            VALUES (?, ?, 'grade', ?, ?, ?, ?, ?, ?)
            """,
            (student_id, wk, top, subject_id, ref_id, actor_id,
             current_school_year(cur), now_iso),
        )


def _parse_score(raw):
    try:
        score = int(raw)
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 10 else None


@grade_bp.route("", methods=["POST"])
@require_role("servant", "admin")
def create_grade():
    data = request.get_json(silent=True) or {}
    if not all(k in data for k in ("assessment_id", "student_id", "score")):
        return fail("VALIDATION_FAILED", "بيانات غير مكتملة", 422)
    score = _parse_score(data["score"])
    if score is None:
        return fail("VALIDATION_FAILED", "الدرجة يجب أن تكون رقمًا صحيحًا من 0 إلى 10", 422)

    assessment_id = data["assessment_id"]
    student_id = data["student_id"]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM assessments WHERE id = ?", (assessment_id,))
    assessment = cursor.fetchone()
    if not assessment:
        return fail("NOT_FOUND", "التسميع/الاختبار غير موجود", 404)

    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], assessment["class_id"], assessment["subject_id"]
    ):
        return fail("FORBIDDEN", "غير مسند لك هذا الفصل والمادة", 403)

    cursor.execute(
        "SELECT class_id, full_name FROM users WHERE id = ?", (student_id,)
    )
    student = cursor.fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)
    if student["class_id"] != assessment["class_id"]:
        return fail("VALIDATION_FAILED", "الطالب ليس في فصل هذا الاختبار", 422)

    cursor.execute("SELECT name_ar FROM subjects WHERE id = ?", (assessment["subject_id"],))
    subject_row = cursor.fetchone()
    subject_name = subject_row["name_ar"] if subject_row else None

    # 409 must be decided before opening the transaction (it commits on exit).
    cursor.execute(
        "SELECT id FROM grades WHERE assessment_id = ? AND student_id = ?",
        (assessment_id, student_id),
    )
    if cursor.fetchone():
        return fail("GRADE_EXISTS", "درجة الطالب مسجلة بالفعل — يمكن للمدير تعديلها", 409)

    now_iso = now_utc().isoformat()
    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute(
            "INSERT INTO grades (assessment_id, student_id, score, entered_by, entered_at) VALUES (?, ?, ?, ?, ?)",
            (assessment_id, student_id, score, session["user_id"], now_iso),
        )
        grade_id = cur.lastrowid
        _upsert_weekly_grade_ledger(
            cur, student_id, assessment["subject_id"], assessment["week_key"],
            session["user_id"], grade_id, now_iso,
        )
        cur.execute(
            "UPDATE assessment_requests SET status = 'graded' "
            "WHERE assessment_id = ? AND student_id = ?",
            (assessment_id, student_id),
        )
    log_action(
        "grade_add",
        actor_id=session["user_id"],
        target_type="student",
        target_id=student_id,
        details={
            "student_name": student["full_name"],
            "subject_name": subject_name,
            "assessment_title": assessment["title"],
            "kind": assessment["kind"],
            "score": score,
            "week_key": assessment["week_key"],
        },
    )
    return ok({"id": grade_id, "student_id": student_id, "score": score}, 201)


@grade_bp.route("/<int:grade_id>", methods=["PUT"])
@require_role("admin")
def update_grade(grade_id):
    data = request.get_json(silent=True) or {}
    score = _parse_score(data.get("score"))
    if score is None:
        return fail("VALIDATION_FAILED", "الدرجة يجب أن تكون رقمًا صحيحًا من 0 إلى 10", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT g.id, g.student_id, g.score AS old_score, a.subject_id, a.week_key,
               a.title AS assessment_title, a.kind,
               u.full_name AS student_name, s.name_ar AS subject_name
        FROM grades g
        JOIN assessments a ON g.assessment_id = a.id
        JOIN users u ON g.student_id = u.id
        JOIN subjects s ON a.subject_id = s.id
        WHERE g.id = ?
        """,
        (grade_id,),
    )
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "الدرجة غير موجودة", 404)

    now_iso = now_utc().isoformat()
    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute(
            "UPDATE grades SET score = ?, entered_by = ?, entered_at = ? WHERE id = ?",
            (score, session["user_id"], now_iso, grade_id),
        )
        _upsert_weekly_grade_ledger(
            cur, row["student_id"], row["subject_id"], row["week_key"],
            session["user_id"], grade_id, now_iso,
        )
    log_action(
        "grade_update",
        actor_id=session["user_id"],
        target_type="student",
        target_id=row["student_id"],
        details={
            "student_name": row["student_name"],
            "subject_name": row["subject_name"],
            "assessment_title": row["assessment_title"],
            "kind": row["kind"],
            "score": score,
            "old_score": row["old_score"],
            "week_key": row["week_key"],
        },
    )
    return ok({"id": grade_id, "score": score})


@grade_bp.route("", methods=["GET"])
@require_role("servant", "admin")
def list_grades():
    class_code = request.args.get("class_code")
    if not class_code:
        return fail("VALIDATION_FAILED", "يرجى تحديد الفصل", 422)
    subject_code = request.args.get("subject")
    assessment_id = request.args.get("assessment_id")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM classes WHERE code = ?", (class_code,))
    c_row = cursor.fetchone()
    if not c_row:
        return fail("NOT_FOUND", "الفصل غير موجود", 404)
    class_id = c_row["id"]

    if session["role"] == "servant":
        if not has_assignment(cursor, session["user_id"], class_id):
            return fail("FORBIDDEN", "هذا الفصل غير مسند لك", 403)
        # الخادم يقرأ/يرصد درجات موادّه المسندة له فقط (FR-GRD-01/06)
        allowed_subjects = assigned_subject_ids(cursor, session["user_id"], class_id)
        if subject_code:
            cursor.execute("SELECT id FROM subjects WHERE code = ?", (subject_code,))
            s_row = cursor.fetchone()
            if not s_row:
                return fail("NOT_FOUND", "المادة غير موجودة", 404)
            if s_row["id"] not in allowed_subjects:
                return fail("FORBIDDEN", "هذه المادة غير مسندة لك", 403)
        elif not allowed_subjects:
            return ok([])

    cursor.execute(
        """
        SELECT u.id AS student_id, u.full_name
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
        WHERE u.class_id = ? AND u.status = 'active'
        ORDER BY u.full_name
        """,
        (class_id,),
    )
    students = [dict(r) for r in cursor.fetchall()]

    query = """
        SELECT g.id, g.assessment_id, g.score, g.student_id,
               a.title, a.kind, a.week_key, s.code AS subject_code,
               eb.full_name AS entered_by_name
        FROM grades g
        JOIN assessments a ON g.assessment_id = a.id
        JOIN subjects s ON a.subject_id = s.id
        LEFT JOIN users eb ON eb.id = g.entered_by
        WHERE a.class_id = ?
    """
    params = [class_id]
    if subject_code:
        query += " AND s.code = ?"
        params.append(subject_code)
    if assessment_id:
        query += " AND a.id = ?"
        params.append(assessment_id)
    if session["role"] == "servant" and not subject_code:
        # بلا تحديد مادة ⇒ درجات موادّه المسندة فقط
        ors = " OR ".join(["a.subject_id = ?"] * len(allowed_subjects))
        query += f" AND ({ors})"
        params.extend(allowed_subjects)
    cursor.execute(query, params)

    by_student = {}
    for row in cursor.fetchall():
        by_student.setdefault(row["student_id"], []).append(
            {k: row[k] for k in ("id", "assessment_id", "title", "kind", "week_key", "subject_code", "score", "entered_by_name")}
        )

    for student in students:
        student["grades"] = by_student.get(student["student_id"], [])
    return ok(students)


@grade_bp.route("/requests", methods=["GET"])
@require_role("servant", "admin")
def list_grade_requests():
    assessment_id = request.args.get("assessment_id")
    if not assessment_id:
        return fail("VALIDATION_FAILED", "assessment_id مطلوب", 422)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, class_id FROM assessments WHERE id = ?", (assessment_id,))
    a = cursor.fetchone()
    if not a:
        return fail("NOT_FOUND", "التقييم غير موجود", 404)
    if session["role"] == "servant":
        cursor.execute(
            "SELECT 1 FROM servant_assignments WHERE user_id = ? AND class_id = ? AND subject_id = "
            "(SELECT subject_id FROM assessments WHERE id = ?)",
            (session["user_id"], a["class_id"], assessment_id),
        )
        if not cursor.fetchone():
            return fail("FORBIDDEN", "غير مسند لك", 403)
    cursor.execute(
        """
        SELECT ar.student_id, ar.requested_at, ar.status, u.full_name
        FROM assessment_requests ar
        JOIN users u ON u.id = ar.student_id
        WHERE ar.assessment_id = ?
        ORDER BY ar.requested_at
        """,
        (assessment_id,),
    )
    return ok([dict(r) for r in cursor.fetchall()])


@grade_bp.route("/student/<int:student_id>", methods=["GET"])
@require_role("servant", "admin")
def student_grades(student_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, full_name, class_id FROM users WHERE id = ?", (student_id,)
    )
    student = cursor.fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)

    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], student["class_id"]
    ):
        return fail("FORBIDDEN", "هذا الطالب ليس في فصل مسند لك", 403)

    cursor.execute(
        """
        SELECT g.id, g.score, g.assessment_id, a.title, a.kind, a.week_key,
               s.code AS subject_code, s.name_ar AS subject_name,
               eb.full_name AS entered_by_name
        FROM grades g
        JOIN assessments a ON g.assessment_id = a.id
        JOIN subjects s ON a.subject_id = s.id
        LEFT JOIN users eb ON eb.id = g.entered_by
        WHERE g.student_id = ?
        ORDER BY a.week_key DESC
        """,
        (student_id,),
    )
    subjects = {}
    for row in cursor.fetchall():
        entry = subjects.setdefault(
            row["subject_code"],
            {"subject_code": row["subject_code"], "subject_name": row["subject_name"], "grades": []},
        )
        entry["grades"].append(
            {k: row[k] for k in ("id", "assessment_id", "title", "kind", "week_key", "score", "entered_by_name")}
        )
    return ok({"student": {"id": student["id"], "full_name": student["full_name"]},
               "subjects": list(subjects.values())})
