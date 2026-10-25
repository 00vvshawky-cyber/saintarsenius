from flask import Blueprint, request, session

from backend.db import get_db
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.authz import assigned_class_ids, has_assignment

servant_bp = Blueprint("servant", __name__)


@servant_bp.route("/classes", methods=["GET"])
@require_role("servant", "admin")
def my_classes():
    conn = get_db()
    cursor = conn.cursor()
    if session["role"] == "admin":
        cursor.execute(
            "SELECT id, code, name_ar, google_account FROM classes ORDER BY sort_order"
        )
        classes = [dict(r) for r in cursor.fetchall()]
        for cls in classes:
            cls["class_code"] = cls["code"]
            cursor.execute(
                "SELECT code, name_ar FROM subjects ORDER BY sort_order"
            )
            cls["subjects"] = [dict(r) for r in cursor.fetchall()]
            cursor.execute(
                """
                SELECT COUNT(*) AS n FROM users u
                JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
                WHERE u.class_id = ? AND u.status = 'active'
                """,
                (cls["id"],),
            )
            cls["student_count"] = cursor.fetchone()["n"]
        return ok(classes)

    cursor.execute(
        """
        SELECT DISTINCT c.id, c.code, c.name_ar, c.google_account
        FROM servant_assignments sa
        JOIN classes c ON sa.class_id = c.id
        WHERE sa.user_id = ?
        ORDER BY c.sort_order
        """,
        (session["user_id"],),
    )
    classes = [dict(r) for r in cursor.fetchall()]
    for cls in classes:
        cls["class_code"] = cls["code"]
        cursor.execute(
            """
            SELECT s.code, s.name_ar
            FROM servant_assignments sa
            JOIN subjects s ON sa.subject_id = s.id
            WHERE sa.user_id = ? AND sa.class_id = ?
            ORDER BY s.sort_order
            """,
            (session["user_id"], cls["id"]),
        )
        cls["subjects"] = [dict(r) for r in cursor.fetchall()]
        cursor.execute(
            """
            SELECT COUNT(*) AS n FROM users u
            JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
            WHERE u.class_id = ? AND u.status = 'active'
            """,
            (cls["id"],),
        )
        cls["student_count"] = cursor.fetchone()["n"]
    return ok(classes)


def _class_row(cursor, class_code):
    cursor.execute("SELECT id, code, name_ar FROM classes WHERE code = ?", (class_code,))
    return cursor.fetchone()


def _roster(cursor, class_id):
    """كشف طلاب فصل: الاسم + الهاتف + الرتبة + هاتف ولي الأمر + الصورة."""
    cursor.execute(
        """
        SELECT u.id, u.full_name, u.phone, u.gender, u.birth_date, u.photo_url,
               u.deacon_rank, u.parent_phone
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
        WHERE u.class_id = ? AND u.status = 'active'
        ORDER BY u.full_name
        """,
        (class_id,),
    )
    return [dict(r) for r in cursor.fetchall()]


@servant_bp.route("/students", methods=["GET"])
@require_role("servant", "admin")
def students():
    """المسار التعاقدي: `GET /servant/students?class_code=` — طلاب الفصل (يتحقق الإسناد)."""
    class_code = request.args.get("class_code")
    if not class_code:
        return fail("VALIDATION_FAILED", "class_code مطلوب", 422)

    conn = get_db()
    cursor = conn.cursor()
    c_row = _class_row(cursor, class_code)
    if not c_row:
        return fail("NOT_FOUND", "الفصل غير موجود", 404)

    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], c_row["id"]
    ):
        return fail("FORBIDDEN", "هذا الفصل غير مسند لك", 403)

    return ok(_roster(cursor, c_row["id"]))


@servant_bp.route("/classes/<class_code>/students", methods=["GET"])
@require_role("servant", "admin")
def class_students(class_code):
    """اسم مختصر مطابق لـ `GET /servant/students?class_code=`."""
    conn = get_db()
    cursor = conn.cursor()
    c_row = _class_row(cursor, class_code)
    if not c_row:
        return fail("NOT_FOUND", "الفصل غير موجود", 404)

    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], c_row["id"]
    ):
        return fail("FORBIDDEN", "هذا الفصل غير مسند لك", 403)

    return ok(_roster(cursor, c_row["id"]))


@servant_bp.route("/students/<int:student_id>", methods=["GET"])
@require_role("servant", "admin")
def student_profile(student_id):
    """ملف الطالب (للقراءة) — المسار التعاقدي `GET /servant/students/{id}`."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.id, u.full_name, u.phone, u.whatsapp, u.parent_phone, u.gender,
               u.birth_date, u.photo_url, u.deacon_rank, u.class_id,
               c.code AS class_code, c.name_ar AS class_name
        FROM users u
        LEFT JOIN classes c ON u.class_id = c.id
        WHERE u.id = ?
        """,
        (student_id,),
    )
    student = cursor.fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)

    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], student["class_id"]
    ):
        return fail("FORBIDDEN", "هذا الطالب ليس في فصل مسند لك", 403)

    return ok(dict(student))
