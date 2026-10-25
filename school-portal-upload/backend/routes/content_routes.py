from flask import Blueprint, request, session
from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.audit import log_action
from backend.utils.timeutil import now_utc

content_bp = Blueprint("content", __name__)

# أنواع المحتوى (FR-CNT-02)
CONTENT_KINDS = ("audio", "image", "pdf")


def _servant_pairs(cursor):
    """(class_id, subject_id) pairs the active servant may manage; None for admin.

    نفس القاعدة المستخدمة في كل التطبيق: الخادم يدير موادّه المسندة فقط
    (انظر backend/utils/authz.py → assigned_subject_ids).
    """
    if session.get("role") != "servant":
        return None
    cursor.execute(
        "SELECT class_id, subject_id FROM servant_assignments WHERE user_id = ?",
        (session["user_id"],),
    )
    return [(r["class_id"], r["subject_id"]) for r in cursor.fetchall()]


@content_bp.route("", methods=["GET"])
@require_role("servant", "admin")
def list_content():
    conn = get_db()
    cursor = conn.cursor()

    class_code = request.args.get("class_code")
    subject_code = request.args.get("subject")

    if class_code:
        cursor.execute("SELECT id FROM classes WHERE code = ?", (class_code,))
        c_row = cursor.fetchone()
        class_id = c_row["id"] if c_row else None
    else:
        class_id = None

    query = """
        SELECT cl.id, cl.title, cl.kind, cl.url, cl.created_at, cl.created_by,
               s.code AS subject_code, s.name_ar AS subject_name,
               c.code AS class_code, c.name_ar AS class_name,
               u.full_name AS created_by_name
        FROM content_links cl
        JOIN subjects s ON cl.subject_id = s.id
        JOIN classes c ON cl.class_id = c.id
        LEFT JOIN users u ON u.id = cl.created_by
        WHERE 1=1
    """
    params = []
    if class_id:
        query += " AND cl.class_id = ?"
        params.append(class_id)
    if subject_code:
        query += " AND s.code = ?"
        params.append(subject_code)

    # الخادم يرى محتوى فصوله ومواده المسندة له فقط (FR-CNT-03)
    pairs = _servant_pairs(cursor)
    if pairs is not None:
        if class_id is not None:
            pairs = [p for p in pairs if p[0] == class_id]
        if subject_code:
            cursor.execute("SELECT id FROM subjects WHERE code = ?", (subject_code,))
            s_row = cursor.fetchone()
            if not s_row:
                return fail("NOT_FOUND", "المادة غير موجودة", 404)
            pairs = [p for p in pairs if p[1] == s_row["id"]]
        if not pairs:
            return ok([])
        ors = " OR ".join(["(cl.class_id = ? AND cl.subject_id = ?)"] * len(pairs))
        query += f" AND ({ors})"
        for class_id_v, subject_id_v in pairs:
            params.extend([class_id_v, subject_id_v])
    cursor.execute(query, params)
    # الأحدث أولًا (FR-CNT-04)
    items = [dict(row) for row in cursor.fetchall()]
    items.sort(key=lambda x: (x.get("created_at") or "", x.get("id") or 0), reverse=True)
    return ok(items)

@content_bp.route("", methods=["POST"])
@require_role("servant", "admin")
def add_content():
    data = request.get_json(silent=True) or {}
    required = ["class_code", "subject", "title", "kind", "url"]
    if not all(k in data for k in required):
        return fail("VALIDATION_FAILED", "بيانات غير مكتملة", 422)

    conn = get_db()
    cursor = conn.cursor()

    kind = data["kind"]
    if kind not in CONTENT_KINDS:
        return fail("VALIDATION_FAILED", "نوع المحتوى يجب أن يكون audio أو image أو pdf", 422)

    url = str(data["url"]).strip()
    if not url:
        return fail("VALIDATION_FAILED", "رابط المحتوى مطلوب", 422)
    if not (url.startswith("http://") or url.startswith("https://")):
        return fail("VALIDATION_FAILED", "رابط المحتوى يجب أن يبدأ بـ http أو https", 422)

    title = str(data["title"]).strip()
    if not title:
        return fail("VALIDATION_FAILED", "عنوان المحتوى مطلوب", 422)
    if len(title) > 120:
        return fail("VALIDATION_FAILED", "عنوان المحتوى طويل جدًا (120 حرفًا كحد أقصى)", 422)

    cursor.execute("SELECT id, name_ar FROM classes WHERE code = ?", (data["class_code"],))
    c_row = cursor.fetchone()
    cursor.execute("SELECT id, name_ar FROM subjects WHERE code = ?", (data["subject"],))
    s_row = cursor.fetchone()

    if not c_row or not s_row:
        return fail("VALIDATION_FAILED", "الفصل أو المادة غير موجودة", 422)

    class_id = c_row["id"]
    subject_id = s_row["id"]

    # If servant, verify assignment
    if session.get("role") == "servant":
        cursor.execute("""
            SELECT 1 FROM servant_assignments
            WHERE user_id = ? AND class_id = ? AND subject_id = ?
        """, (session["user_id"], class_id, subject_id))
        if not cursor.fetchone():
            return fail("FORBIDDEN", "غير مسند لك هذا الفصل والمادة", 403)

    now_iso = now_utc().isoformat()

    with db_transaction() as conn_tx:
        cursor_tx = conn_tx.cursor()
        cursor_tx.execute("""
            INSERT INTO content_links (subject_id, class_id, title, kind, url, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (subject_id, class_id, title, kind, url, session["user_id"], now_iso))
        content_id = cursor_tx.lastrowid

    log_action(
        "content_add",
        actor_id=session["user_id"],
        target_type="content",
        target_id=content_id,
        details={
            "title": title,
            "kind": kind,
            "class_name": c_row["name_ar"],
            "subject_name": s_row["name_ar"],
        },
    )
    return ok({"id": content_id, "status": "created"}, 201)


@content_bp.route("/<int:content_id>", methods=["DELETE"])
@require_role("servant", "admin")
def delete_content(content_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT cl.id, cl.created_by, cl.title, cl.kind,
               s.name_ar AS subject_name, c.name_ar AS class_name
        FROM content_links cl
        JOIN subjects s ON cl.subject_id = s.id
        JOIN classes c ON cl.class_id = c.id
        WHERE cl.id = ?
        """,
        (content_id,),
    )
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "المحتوى غير موجود", 404)
    if session.get("role") != "admin" and row["created_by"] != session["user_id"]:
        return fail("FORBIDDEN", "لا تملك صلاحية حذف هذا المحتوى", 403)

    with db_transaction() as conn_tx:
        conn_tx.cursor().execute("DELETE FROM content_links WHERE id = ?", (content_id,))
    log_action(
        "content_delete",
        actor_id=session["user_id"],
        target_type="content",
        target_id=content_id,
        details={
            "title": row["title"],
            "kind": row["kind"],
            "class_name": row["class_name"],
            "subject_name": row["subject_name"],
        },
    )
    return ok({"id": content_id, "status": "deleted"})
