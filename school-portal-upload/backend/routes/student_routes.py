from flask import Blueprint, request, session
from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.timeutil import now_utc

student_bp = Blueprint("student", __name__)


@student_bp.route("/subjects", methods=["GET"])
@require_role("student", "servant", "admin")
def list_subjects():
    """المواد الأربع + عدد محتويات كل مادة + المسؤول المسند (إن وُجد)."""
    conn = get_db()
    cursor = conn.cursor()

    # فصل الطالب المستهدف (المعاينة ?as= للمدير)
    target_id = request.args.get("as") if session.get("role") == "admin" else None
    user_id = target_id or session["user_id"]
    cursor.execute("SELECT class_id FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    class_id = row["class_id"] if row else None

    cursor.execute("SELECT id, code, name_ar FROM subjects ORDER BY sort_order")
    subjects = cursor.fetchall()

    items = []
    for s in subjects:
        count = 0
        teacher = None
        if class_id:
            cursor.execute(
                "SELECT COUNT(*) AS c FROM content_links WHERE class_id = ? AND subject_id = ?",
                (class_id, s["id"]),
            )
            count = cursor.fetchone()["c"]
            cursor.execute(
                "SELECT u.full_name FROM servant_assignments sa "
                "JOIN users u ON u.id = sa.user_id "
                "WHERE sa.class_id = ? AND sa.subject_id = ? LIMIT 1",
                (class_id, s["id"]),
            )
            t_row = cursor.fetchone()
            teacher = t_row["full_name"] if t_row else None
        items.append({
            "code": s["code"],
            "name_ar": s["name_ar"],
            "count": count,
            "teacher": teacher,
        })
    return ok(items)


@student_bp.route("/subjects/<subject_code>/content", methods=["GET"])
@require_role("student", "servant", "admin")
def subject_content(subject_code):
    target_id = request.args.get("as") if session.get("role") == "admin" else None
    user_id = target_id or session["user_id"]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM subjects WHERE code = ?", (subject_code,))
    if not cursor.fetchone():
        return fail("NOT_FOUND", "المادة غير موجودة", 404)

    cursor.execute("SELECT class_id FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row or not row["class_id"]:
        return fail("NOT_FOUND", "لا يوجد فصل مرتبط بهذا الحساب", 404)

    cursor.execute(
        """
        SELECT cl.id, cl.title, cl.kind, cl.url, cl.created_at
        FROM content_links cl
        JOIN subjects s ON cl.subject_id = s.id
        WHERE cl.class_id = ? AND s.code = ?
        ORDER BY cl.created_at DESC
        """,
        (row["class_id"], subject_code),
    )
    return ok([dict(r) for r in cursor.fetchall()])

@student_bp.route("/profile", methods=["GET"])
@require_role("student", "servant", "admin")
def get_profile():
    student_id = request.args.get("as")
    if student_id and session.get("role") == "admin":
        # Admin viewing as student
        pass
    else:
        student_id = session.get("user_id")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.*, c.name_ar as class_name
        FROM users u
        LEFT JOIN classes c ON u.class_id = c.id
        WHERE u.id = ?
    """, (student_id,))
    user = cursor.fetchone()

    if not user:
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    user_dict = dict(user)
    user_dict.pop("password_hash", None)
    return ok(user_dict)

@student_bp.route("/profile", methods=["PATCH"])
@require_role("student")
def update_profile():
    """تعديل ملف الطالب — المسموح: الصورة الشخصية فقط.

    بقية البيانات الشخصية (الاسم/الواتساب/ولي الأمر) يعدّلها المدير
    فقط (PATCH /admin/users/{id}). أي محاولة لتعديلها مباشرة تُرفض.
    """
    data = request.get_json(silent=True) or {}
    user_id = session.get("user_id")

    # الطالب يعدّل الصورة فقط
    allowed = ["photo_url"]
    forbidden = [k for k in data.keys() if k not in allowed]
    if forbidden:
        return fail(
            "FORBIDDEN",
            "لا يمكنك تعديل هذه البيانات — التعديل يتم عبر إدارة المدرسة",
            403,
            {"forbidden_fields": forbidden, "allowed_fields": allowed},
        )

    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return fail("VALIDATION_FAILED", "لا توجد بيانات صالحة للتعديل", 422)

    # التحقق من صحة الصورة
    if "photo_url" in updates:
        photo = updates["photo_url"]
        if not (
            isinstance(photo, str)
            and photo.startswith("data:image/")
            and len(photo) <= 1_200_000
        ):
            return fail("VALIDATION_FAILED", "الصورة غير صالحة", 422)

    with db_transaction() as conn:
        cursor = conn.cursor()
        for field, value in updates.items():
            cursor.execute(f"UPDATE users SET {field} = ? WHERE id = ?", (value, user_id))

    return ok(updates)


def _target_user(cursor):
    """Resolve ?as= (admin previewing) or the logged-in user's row."""
    as_id = request.args.get("as") if session.get("role") == "admin" else None
    uid = int(as_id) if as_id else session.get("user_id")
    cursor.execute(
        """
        SELECT u.id, u.full_name, u.photo_url, u.class_id, c.name_ar AS class_name
        FROM users u LEFT JOIN classes c ON u.class_id = c.id
        WHERE u.id = ?
        """,
        (uid,),
    )
    return cursor.fetchone()


@student_bp.route("/home", methods=["GET"])
@require_role("student", "servant", "admin")
def home():
    conn = get_db()
    cursor = conn.cursor()
    user = _target_user(cursor)
    if not user:
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    # عدد الإشعارات غير المقروءة (شارة على البطاقة)
    cursor.execute(
        "SELECT COUNT(*) AS c FROM notifications n "
        "LEFT JOIN notification_reads nr ON nr.notification_id = n.id AND nr.user_id = ? "
        "WHERE (n.class_id IS NULL OR n.class_id = ?) AND nr.user_id IS NULL",
        (user["id"], user["class_id"]),
    )
    unread_notifications = cursor.fetchone()["c"]

    tiles = [
        {"key": "profile", "title": "بياناتي"},
        {"key": "attendance", "title": "الحضور والانصراف"},
        {"key": "subjects", "title": "المواد الدراسية"},
        {"key": "assessments", "title": "التسميع والتقييم"},
        {"key": "points", "title": "سجل النقاط"},
        {"key": "notifications", "title": "الإشعارات", "badge": unread_notifications},
    ]
    return ok({
        "full_name": user["full_name"],
        "photo_url": user["photo_url"],
        "class_name": user["class_name"],
        "tiles": tiles,
    })


@student_bp.route("/points", methods=["GET"])
@require_role("student", "servant", "admin")
def my_points():
    conn = get_db()
    cursor = conn.cursor()
    user = _target_user(cursor)
    if not user:
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    from backend.utils.authz import current_school_year
    from backend.utils.points import student_points_payload

    year = current_school_year(cursor)
    return ok(student_points_payload(cursor, user["id"], year))


def _wa_me_link(phone, text):
    """Egyptian 01XXXXXXXXX → wa.me/201XXXXXXXXX (spec §7-6)."""
    from urllib.parse import quote
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits.startswith("0"):
        intl = "20" + digits[1:]
    elif digits.startswith("20"):
        intl = digits
    else:
        intl = digits
    return f"https://wa.me/{intl}?text={quote(text)}"


@student_bp.route("/assessments", methods=["GET"])
@require_role("student", "servant", "admin")
def my_assessments():
    conn = get_db()
    cursor = conn.cursor()
    user = _target_user(cursor)
    if not user:
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)
    if not user["class_id"]:
        return fail("NOT_FOUND", "لا يوجد فصل مرتبط بهذا الحساب", 404)

    cursor.execute(
        """
        SELECT a.id, a.title, a.kind, a.external_url, a.whatsapp_number,
               a.closes_at, a.week_key, s.code AS subject_code, s.name_ar AS subject_name,
               g.score, ar.requested_at AS started_at
        FROM assessments a
        JOIN subjects s ON a.subject_id = s.id
        LEFT JOIN grades g ON g.assessment_id = a.id AND g.student_id = ?
        LEFT JOIN assessment_requests ar ON ar.assessment_id = a.id AND ar.student_id = ?
        WHERE a.class_id = ?
        ORDER BY a.closes_at DESC
        """,
        (user["id"], user["id"], user["class_id"]),
    )
    # Admin viewing as student via ?as=
    now_iso = now_utc().isoformat()
    items = []
    for row in cursor.fetchall():
        item = dict(row)
        item["is_open"] = item["closes_at"] > now_iso
        item["started"] = bool(item.get("started_at"))
        if item["kind"] == "quiz":
            item["start_url"] = item["external_url"]
        else:
            message = (
                f"أنا {user['full_name']}، فصل {user['class_name']}، "
                f"سأسمّع {item['title']}"
            )
            item["start_url"] = _wa_me_link(item["whatsapp_number"], message)
        del item["external_url"]
        del item["whatsapp_number"]
        items.append(item)
    return ok(items)


@student_bp.route("/assessments/<int:assessment_id>/start", methods=["POST"])
@require_role("student")
def start_assessment(assessment_id):
    user_id = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM assessments WHERE id = ?", (assessment_id,))
    assessment = cursor.fetchone()
    if not assessment:
        return fail("NOT_FOUND", "التقييم غير موجود", 404)
    cursor.execute("SELECT class_id FROM users WHERE id = ?", (user_id,))
    u = cursor.fetchone()
    if not u or u["class_id"] != assessment["class_id"]:
        return fail("FORBIDDEN", "هذا التقييم ليس لفصلك", 403)
    now_iso = now_utc().isoformat()
    if assessment["closes_at"] <= now_iso:
        return fail("ASSESSMENT_CLOSED", "انتهى وقت هذا التقييم", 403)
    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            "INSERT OR IGNORE INTO assessment_requests (assessment_id, student_id, requested_at, status) "
            "VALUES (?, ?, ?, 'pending')",
            (assessment_id, user_id, now_iso),
        )
    return ok({"assessment_id": assessment_id, "started": True, "requested_at": now_iso})


@student_bp.route("/notifications", methods=["GET"])
@require_role("student", "servant", "admin")
def my_notifications():
    conn = get_db()
    cursor = conn.cursor()
    user = _target_user(cursor)
    if not user:
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    cursor.execute("SELECT value FROM settings WHERE key = 'whatsapp_channel_url'")
    row = cursor.fetchone()
    channel_url = row["value"] if row else None

    cursor.execute(
        """
        SELECT n.id, n.title, n.body, n.created_at,
               CASE WHEN nr.user_id IS NULL THEN 1 ELSE 0 END AS unread
        FROM notifications n
        LEFT JOIN notification_reads nr
               ON nr.notification_id = n.id AND nr.user_id = ?
        WHERE n.class_id IS NULL OR n.class_id = ?
        ORDER BY n.created_at DESC
        """,
        (user["id"], user["class_id"]),
    )
    items = [dict(r) for r in cursor.fetchall()]
    unread = sum(1 for i in items if i["unread"])
    return ok({"whatsapp_channel_url": channel_url, "unread_count": unread, "items": items})


@student_bp.route("/notifications/<int:notification_id>/read", methods=["POST"])
@require_role("student", "servant", "admin")
def mark_notification_read(notification_id):
    conn = get_db()
    cursor = conn.cursor()
    user = _target_user(cursor)
    if not user:
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    cursor.execute(
        "SELECT id, class_id FROM notifications WHERE id = ?", (notification_id,)
    )
    notification = cursor.fetchone()
    if not notification or (
        notification["class_id"] is not None
        and notification["class_id"] != user["class_id"]
    ):
        return fail("NOT_FOUND", "الإشعار غير موجود", 404)

    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            """
            INSERT OR IGNORE INTO notification_reads (user_id, notification_id, read_at)
            VALUES (?, ?, ?)
            """,
            (user["id"], notification_id, now_utc().isoformat()),
        )
    return ok({"id": notification_id, "status": "read"})
