import json
import os
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit

from flask import Blueprint, request, session
from werkzeug.security import generate_password_hash

import backend.config as config
from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.audit import log_action
from backend.utils.deacon import (
    MOTHERS_CLASS_CODES,
    resolve_deacon_rank,
)
from backend.utils.timeutil import CAIRO_TZ, week_key

admin_bp = Blueprint("admin", __name__)

# Registration fields the admin reviews — never expose password_hash.
_REQUEST_COLUMNS = (
    "id, full_name, phone, whatsapp, parent_phone, birth_date, "
    "gender, deacon_rank, class_id, status, created_at, photo_url, "
    "(SELECT code FROM classes WHERE classes.id = users.class_id) AS class_code, "
    "(SELECT name_ar FROM classes WHERE classes.id = users.class_id) AS class_name"
)


@admin_bp.route("/join-requests", methods=["GET"])
@require_role("admin")
def list_join_requests():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM users "
        "WHERE status = 'pending' ORDER BY created_at DESC"
    )
    return ok([dict(row) for row in cursor.fetchall()])


@admin_bp.route("/join-requests/<int:user_id>", methods=["GET"])
@require_role("admin")
def join_request_detail(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM users WHERE id = ?", (user_id,)
    )
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "لا يوجد طلب انضمام بهذا المعرف", 404)
    return ok(dict(row))


@admin_bp.route("/join-requests/<int:user_id>/approve", methods=["POST"])
@require_role("admin")
def approve_join_request(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not cursor.fetchone():
        return fail("NOT_FOUND", "لا يوجد طلب انضمام بهذا المعرف", 404)

    # Idempotent: approving an already-active or previously-rejected account
    # simply (re)activates it.
    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            "UPDATE users SET status = 'active' WHERE id = ?", (user_id,)
        )
    log_action(
        "join_request_approve",
        actor_id=session["user_id"],
        target_type="user",
        target_id=user_id,
    )
    return ok({"id": user_id, "status": "active"})


def _purge_user(cur, user_id, actor_id):
    """حذف نهائي لحساب المستخدم وكل سجلاته، مع الحفاظ على البيانات المشتركة.

    • السجلات التي تخص المستخدم تُحذف (نقاط، حضور، درجات، افتقاد، تكليفات...).
    • أي أثر إنشاء/إدخال في بيانات مشتركة (محتوى، اختبارات، إشعارات) يُنقل
      إلى المدير المنفّذ بدل الحذف، لأن أعمدةها NOT NULL.
    • سجل التدقيق يبقى محفوظًا لكن دون ربط بحساب محذوف.
    """
    # 1) بيانات المستخدم نفسه
    for table, column in (
        ("notification_reads", "user_id"),
        ("wheel_spins_used", "student_id"),
        ("bonus_budget", "servant_id"),
        ("followup_notes", "student_id"),
        ("followups", "student_id"),
        ("liturgy_attendance", "student_id"),
        ("grades", "student_id"),
        ("point_ledger", "student_id"),
        ("attendance", "user_id"),
        ("servant_assignments", "user_id"),
        ("user_roles", "user_id"),
    ):
        cur.execute(f"DELETE FROM {table} WHERE {column} = ?", (user_id,))

    # 2) البيانات المشتركة: نقل الأثر للمدير المنفّذ
    for table, column in (
        ("content_links", "created_by"),
        ("assessments", "created_by"),
        ("grades", "entered_by"),
        ("liturgy_attendance", "marked_by"),
        ("point_ledger", "actor_id"),
        ("wheel_spins_used", "actor_id"),
        ("notifications", "created_by"),
        ("users", "created_by"),
        ("attendance", "edited_by"),
        ("followups", "handled_by"),
        ("followup_notes", "created_by"),
    ):
        cur.execute(
            f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
            (actor_id, user_id),
        )

    # 3) سجل التدقيق يبقى لكن دون ربط بحساب محذوف
    cur.execute("UPDATE audit_log SET actor_id = NULL WHERE actor_id = ?", (user_id,))
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))


@admin_bp.route("/join-requests/<int:user_id>/reject", methods=["POST"])
@require_role("admin")
def reject_join_request(user_id):
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or None
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, full_name FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "لا يوجد طلب انضمام بهذا المعرف", 404)

    full_name = row["full_name"]
    # حذف نهائي: حذف الحساب والسجلات المرتبطة به.
    with db_transaction() as conn_tx:
        _purge_user(conn_tx.cursor(), user_id, session["user_id"])

    log_action(
        "join_request_reject",
        actor_id=session["user_id"],
        target_type="user",
        target_id=user_id,
        details={"reason": reason, "full_name": full_name, "deleted": True},
    )
    return ok({"id": user_id, "status": "deleted", "reason": reason})


# ---------------------------------------------------------------- users ----

_USER_LIST_COLUMNS = """
    SELECT u.id, u.full_name, u.phone, u.login_key, u.status, u.is_permanent,
           u.created_at, u.photo_url, u.deacon_rank, u.gender,
           u.whatsapp, u.parent_phone, u.birth_date, u.has_phone,
           c.code AS class_code, c.name_ar AS class_name
    FROM users u LEFT JOIN classes c ON u.class_id = c.id
"""


@admin_bp.route("/users", methods=["GET"])
@require_role("admin")
def list_users():
    q = request.args.get("q", "").strip()
    class_code = request.args.get("class_code")
    role = request.args.get("role")

    query = _USER_LIST_COLUMNS
    params = []
    if q:
        query += (" WHERE (u.full_name LIKE ? OR u.phone LIKE ? OR u.login_key LIKE ?"
                  " OR u.parent_phone LIKE ? OR u.whatsapp LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    if class_code:
        query += (" AND" if "WHERE" in query else " WHERE") + " c.code = ?"
        params.append(class_code)
    if role:
        if role not in ("student", "servant", "admin"):
            return fail("VALIDATION_FAILED", "دور غير صحيح", 422)
        query += (" AND" if "WHERE" in query else " WHERE")
        query += " EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_id = u.id AND ur.role = ?)"
        params.append(role)
    query += " ORDER BY u.full_name"

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(query, params)
    users = [dict(r) for r in cursor.fetchall()]
    for user in users:
        cursor.execute(
            "SELECT role FROM user_roles WHERE user_id = ? ORDER BY role",
            (user["id"],),
        )
        user["roles"] = [r["role"] for r in cursor.fetchall()]
    return ok(users)


@admin_bp.route("/users/<int:user_id>", methods=["GET"])
@require_role("admin")
def user_detail(user_id):
    """ملف مستخدم كامل (لشاشة «إدارة البيانات») — كل الحقول + الأدوار + التكليفات."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(_USER_LIST_COLUMNS + " WHERE u.id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)
    user = dict(row)
    cursor.execute(
        "SELECT role FROM user_roles WHERE user_id = ? ORDER BY role", (user_id,)
    )
    user["roles"] = [r["role"] for r in cursor.fetchall()]
    user["assignments"] = _user_assignments(cursor, user_id)
    return ok(user)


def _user_assignments(cursor, user_id):
    """تكليفات الخادم: (فصل، مادة) — للعرض في شاشة إدارة البيانات."""
    cursor.execute(
        """
        SELECT c.code AS class_code, c.name_ar AS class_name,
               s.code AS subject, s.name_ar AS subject_name
        FROM servant_assignments sa
        JOIN classes c ON c.id = sa.class_id
        JOIN subjects s ON s.id = sa.subject_id
        WHERE sa.user_id = ?
        ORDER BY c.sort_order, s.sort_order
        """,
        (user_id,),
    )
    return [dict(r) for r in cursor.fetchall()]


@admin_bp.route("/users", methods=["POST"])
@require_role("admin")
def create_user():
    """Admin creates a complete account (spec: student without a phone)."""
    data = request.get_json(silent=True) or {}
    if not data.get("full_name") or not data.get("class_code"):
        return fail("VALIDATION_FAILED", "الاسم والفصل إلزاميان", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM classes WHERE code = ?", (data["class_code"],))
    c_row = cursor.fetchone()
    if not c_row:
        return fail("VALIDATION_FAILED", "الفصل غير موجود", 422)

    gender = data.get("gender")
    if gender not in (None, "male", "female"):
        return fail("VALIDATION_FAILED", "الجنس غير صحيح", 422)

    # نفس قواعد الرتبة الشماسية المستخدمة في التسجيل
    if data["class_code"] in MOTHERS_CLASS_CODES and gender != "female":
        return fail("VALIDATION_FAILED", "فصل الأمهات مخصص للسيدات فقط", 422)
    deacon_rank, rank_error = resolve_deacon_rank(
        data["class_code"], gender, data.get("deacon_rank")
    )
    if rank_error:
        return fail("VALIDATION_FAILED", rank_error, 422)

    phone = data.get("phone")
    if phone is not None and not (
        isinstance(phone, str) and len(phone) == 11 and phone.isdigit() and phone.startswith("01")
    ):
        return fail("VALIDATION_FAILED", "رقم الهاتف غير صحيح (11 رقمًا يبدأ بـ 01)", 422)

    password = data.get("password") or secrets.token_urlsafe(9)
    login_key = phone if phone else "st" + secrets.token_hex(4)
    password_hash = generate_password_hash(password)
    now_iso = datetime.now(timezone.utc).isoformat()

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute(
            """
            INSERT INTO users (full_name, phone, whatsapp, parent_phone, birth_date,
                               gender, deacon_rank, class_id, password_hash, login_key,
                               has_phone, status, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                str(data["full_name"])[:40], phone, data.get("whatsapp"),
                data.get("parent_phone"), data.get("birth_date"), gender,
                deacon_rank, c_row["id"], password_hash, login_key,
                1 if phone else 0, now_iso, session["user_id"],
            ),
        )
        uid = cur.lastrowid
        roles = data.get("roles") or ["student"]
        for role in roles:
            if role in ("student", "servant", "admin"):
                cur.execute(
                    "INSERT OR IGNORE INTO user_roles (user_id, role) VALUES (?, ?)",
                    (uid, role),
                )

    log_action("user_create", actor_id=session["user_id"], target_type="user",
               target_id=uid, details={"full_name": str(data["full_name"])[:40]})
    return ok({"id": uid, "login_key": login_key, "password": password,
               "status": "active"}, 201)


@admin_bp.route("/users/<int:user_id>", methods=["PATCH"])
@require_role("admin")
def update_user(user_id):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT u.id, u.gender, u.deacon_rank, u.class_id, c.code AS class_code "
        "FROM users u LEFT JOIN classes c ON u.class_id = c.id WHERE u.id = ?",
        (user_id,),
    )
    current = cursor.fetchone()
    if not current:
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    updates, params = [], []
    simple = ("full_name", "whatsapp", "parent_phone", "birth_date",
              "gender", "photo_url", "login_key")
    for field in simple:
        if field in data:
            value = data[field]
            if field == "full_name":
                value = str(value)[:40]  # SRS: name auto-capped at 40
            if field == "gender" and value not in (None, "male", "female"):
                return fail("VALIDATION_FAILED", "الجنس غير صحيح", 422)
            if field in ("whatsapp", "parent_phone") and value:
                if not (
                    isinstance(value, str) and len(value) == 11
                    and value.isdigit() and value.startswith("01")
                ):
                    return fail("VALIDATION_FAILED", "رقم الهاتف غير صحيح (11 رقمًا يبدأ بـ 01)", 422)
            if field == "photo_url" and value:
                if not (
                    isinstance(value, str) and value.startswith("data:image/")
                    and len(value) <= 1_200_000
                ):
                    return fail("VALIDATION_FAILED", "صورة غير صالحة", 422)
            updates.append(f"{field} = ?")
            params.append(value)

    if "phone" in data:
        phone = data["phone"]
        if phone and not (
            isinstance(phone, str) and len(phone) == 11
            and phone.isdigit() and phone.startswith("01")
        ):
            return fail("VALIDATION_FAILED", "رقم الهاتف غير صحيح (11 رقمًا يبدأ بـ 01)", 422)
        updates.append("phone = ?")
        params.append(phone)
        # الرقم المعتمد هو مفتاح الدخول ⇒ مزامنته حتى يدخل الطالب برقمه الجديد
        if phone and "login_key" not in data:
            updates.append("login_key = ?")
            params.append(phone)
        updates.append("has_phone = ?")
        params.append(1 if phone else 0)

    if "status" in data:
        if data["status"] not in ("pending", "active", "rejected", "suspended"):
            return fail("VALIDATION_FAILED", "حالة غير صحيحة", 422)
        updates.append("status = ?")
        params.append(data["status"])

    new_class_code = current["class_code"]
    if "class_code" in data:
        new_class_code = data["class_code"]
        cursor.execute("SELECT id FROM classes WHERE code = ?", (new_class_code,))
        c_row = cursor.fetchone()
        if not c_row:
            return fail("VALIDATION_FAILED", "الفصل غير موجود", 422)
        updates.append("class_id = ?")
        params.append(c_row["id"])

    # الرتبة الشماسية: نفس القواعد المستخدمة في التسجيل
    # (ذكور فقط + قفل فصل الحضانة على «لم أرسم بعد»)
    new_gender = data.get("gender", current["gender"])
    if new_class_code in MOTHERS_CLASS_CODES and new_gender != "female":
        return fail("VALIDATION_FAILED", "فصل الأمهات مخصص للسيدات فقط", 422)

    rank_input = data.get("deacon_rank", current["deacon_rank"])
    resolved_rank, rank_error = resolve_deacon_rank(
        new_class_code, new_gender, rank_input
    )
    if rank_error:
        return fail("VALIDATION_FAILED", rank_error, 422)
    if resolved_rank != current["deacon_rank"] or "deacon_rank" in data:
        updates.append("deacon_rank = ?")
        params.append(resolved_rank)

    if "password" in data:
        if not isinstance(data["password"], str) or len(data["password"]) < 8:
            return fail("VALIDATION_FAILED", "كلمة المرور يجب ألا تقل عن 8 أحرف", 422)
        updates.append("password_hash = ?")
        params.append(generate_password_hash(data["password"]))

    # «إعادة تعيين كلمة المرور» — يولّد كلمة مرور جديدة ويعيدها مرة واحدة
    generated_password = None
    if data.get("reset_password"):
        import secrets, string
        def _strong_initial(len_=12):
            alphabet = string.ascii_letters + string.digits + "!@#%^&*"
            return ''.join(secrets.choice(alphabet) for _ in range(len_))
        generated_password = "Arsanious@" + _strong_initial(10)
        updates.append("password_hash = ?")
        params.append(generate_password_hash(generated_password))

    if not updates:
        return fail("VALIDATION_FAILED", "لا توجد بيانات صالحة للتعديل", 422)

    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
            (*params, user_id),
        )
    log_action("user_update", actor_id=session["user_id"], target_type="user",
               target_id=user_id, details={"fields": [u.split(" =")[0] for u in updates]})
    result = {"id": user_id, "status": "updated"}
    if generated_password:
        result["password"] = generated_password
    return ok(result)


@admin_bp.route("/users/<int:user_id>/roles", methods=["PUT"])
@require_role("admin")
def set_roles(user_id):
    data = request.get_json(silent=True) or {}
    roles = data.get("roles")
    if not isinstance(roles, list) or not roles:
        return fail("VALIDATION_FAILED", "يجب تحديد دور واحد على الأقل", 422)
    if any(r not in ("student", "servant", "admin") for r in roles):
        return fail("VALIDATION_FAILED", "دور غير صحيح", 422)
    if user_id == session["user_id"] and "admin" not in roles:
        return fail("VALIDATION_FAILED", "لا يمكنك سحب دور المدير عن نفسك", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not cursor.fetchone():
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
        for role in roles:
            cur.execute(
                "INSERT INTO user_roles (user_id, role) VALUES (?, ?)",
                (user_id, role),
            )
    log_action("user_roles_set", actor_id=session["user_id"], target_type="user",
               target_id=user_id, details={"roles": roles})
    return ok({"id": user_id, "roles": roles})


@admin_bp.route("/users/<int:user_id>/assignments", methods=["PUT"])
@require_role("admin")
def set_assignments(user_id):
    data = request.get_json(silent=True) or {}
    assignments = data.get("assignments")
    if not isinstance(assignments, list):
        return fail("VALIDATION_FAILED", "الإسنادات يجب أن تكون قائمة", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not cursor.fetchone():
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    resolved = []
    for item in assignments:
        cursor.execute(
            "SELECT id FROM classes WHERE code = ?", (item.get("class_code"),)
        )
        c_row = cursor.fetchone()
        cursor.execute(
            "SELECT id FROM subjects WHERE code = ?", (item.get("subject"),)
        )
        s_row = cursor.fetchone()
        if not c_row or not s_row:
            return fail("VALIDATION_FAILED", "فصل أو مادة غير موجودة", 422)
        resolved.append((c_row["id"], s_row["id"]))

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute("DELETE FROM servant_assignments WHERE user_id = ?", (user_id,))
        for class_id, subject_id in resolved:
            cur.execute(
                "INSERT OR IGNORE INTO servant_assignments (user_id, class_id, subject_id) "
                "VALUES (?, ?, ?)",
                (user_id, class_id, subject_id),
            )
    log_action("user_assignments_set", actor_id=session["user_id"],
               target_type="user", target_id=user_id,
               details={"assignments": data.get("assignments")})
    return ok({"id": user_id, "count": len(resolved),
               "assignments": _user_assignments(cursor, user_id)})


@admin_bp.route("/users/<int:user_id>/permanent", methods=["PUT"])
@require_role("admin")
def set_permanent(user_id):
    data = request.get_json(silent=True) or {}
    if "is_permanent" not in data:
        return fail("VALIDATION_FAILED", "يرجى تحديد is_permanent", 422)
    is_permanent = 1 if data["is_permanent"] else 0

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not cursor.fetchone():
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            "UPDATE users SET is_permanent = ? WHERE id = ?",
            (is_permanent, user_id),
        )
    log_action("user_permanent_set", actor_id=session["user_id"],
               target_type="user", target_id=user_id,
               details={"is_permanent": is_permanent})
    return ok({"id": user_id, "is_permanent": bool(is_permanent)})


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@require_role("admin")
def delete_user(user_id):
    """حذف نهائي لأي حساب من التطبيق (FR-ADM-05) — لا يمكن التراجع."""
    if user_id == session["user_id"]:
        return fail("VALIDATION_FAILED", "لا يمكنك حذف حسابك الحالي", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, full_name, status FROM users WHERE id = ?", (user_id,)
    )
    row = cursor.fetchone()
    if not row:
        return fail("NOT_FOUND", "المستخدم غير موجود", 404)

    cursor.execute("SELECT role FROM user_roles WHERE user_id = ?", (user_id,))
    roles = [r["role"] for r in cursor.fetchall()]
    if "admin" in roles:
        cursor.execute("SELECT COUNT(*) AS c FROM user_roles WHERE role = 'admin'")
        if cursor.fetchone()["c"] <= 1:
            return fail("VALIDATION_FAILED", "لا يمكن حذف آخر حساب إداري", 422)

    with db_transaction() as conn_tx:
        _purge_user(conn_tx.cursor(), user_id, session["user_id"])

    log_action(
        "user_delete",
        actor_id=session["user_id"],
        target_type="user",
        target_id=user_id,
        details={
            "full_name": row["full_name"],
            "status": row["status"],
            "roles": roles,
            "deleted": True,
        },
    )
    return ok({"id": user_id, "status": "deleted"})


# ---------------------------------------------------------- subscriptions ---

@admin_bp.route("/subscriptions", methods=["GET"])
@require_role("admin")
def subscriptions():
    # الإيقاف الشهري التلقائي (FR-AUTH-04) — يُتحقّق عند فتح الشاشة
    try:
        from backend.jobs.monthly_suspend import maybe_suspend
        maybe_suspend()
    except Exception:  # pragma: no cover
        pass

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT value FROM settings WHERE key = 'subscription_from'"
    )
    period_from = cursor.fetchone()["value"]
    cursor.execute("SELECT value FROM settings WHERE key = 'subscription_to'")
    period_to = cursor.fetchone()["value"]

    cursor.execute("SELECT id, code, name_ar FROM classes ORDER BY sort_order")
    classes = []
    for c_row in cursor.fetchall():
        cursor.execute(
            """
            SELECT u.id, u.full_name AS name, u.status, u.is_permanent, u.suspension_reason
            FROM users u
            JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
            WHERE u.class_id = ? AND u.status IN ('active', 'suspended')
            ORDER BY u.full_name
            """,
            (c_row["id"],),
        )
        classes.append({
            "class_code": c_row["code"],
            "class_name": c_row["name_ar"],
            "students": [dict(r) for r in cursor.fetchall()],
        })
    return ok({"from": period_from, "to": period_to, "classes": classes})


@admin_bp.route("/subscriptions/period", methods=["PUT"])
@require_role("admin")
def set_subscription_period():
    data = request.get_json(silent=True) or {}
    for key in ("from", "to"):
        if key in data and data[key] is not None:
            try:
                datetime.strptime(data[key], "%Y-%m-%d")
            except ValueError:
                return fail("VALIDATION_FAILED", f"صيغة {key} غير صحيحة (YYYY-MM-DD)", 422)

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        if data.get("from") is not None:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES ('subscription_from', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (data["from"],),
            )
        if data.get("to") is not None:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES ('subscription_to', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (data["to"],),
            )
    log_action("subscription_period_set", actor_id=session["user_id"],
               details={"from": data.get("from"), "to": data.get("to")})
    return ok({"from": data.get("from"), "to": data.get("to")})


@admin_bp.route("/subscriptions/suspend-expired", methods=["POST"])
@require_role("admin")
def suspend_expired_now():
    """إيقاف كل الحسابات غير الدائمة (بأمر المدير) — يُستدعى عند انتهاء المدة."""
    from backend.jobs.monthly_suspend import suspend_expired
    count = suspend_expired(force=True)
    return ok({"suspended": count})


@admin_bp.route("/subscriptions/students/<int:student_id>", methods=["PUT"])
@require_role("admin")
def set_subscription_status(student_id):
    data = request.get_json(silent=True) or {}
    if data.get("status") not in ("active", "suspended"):
        return fail("VALIDATION_FAILED", "الحالة يجب أن تكون active أو suspended", 422)

    # سبب الإيقاف إلزامي (يظهر للطالب عند محاولة الدخول)
    reason = (data.get("reason") or "").strip() or None
    if data["status"] == "suspended" and not reason:
        return fail("VALIDATION_FAILED", "سبب الإيقاف إلزامي", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, is_permanent, status FROM users WHERE id = ?", (student_id,)
    )
    student = cursor.fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)

    # الاشتراك الدائم (والمدير/الخدمة) لا يُوقَفون — حتى لا تُقفل إدارة المدرسة
    if data["status"] == "suspended":
        cursor.execute(
            "SELECT 1 FROM user_roles WHERE user_id = ? AND role IN ('admin', 'servant')",
            (student_id,),
        )
        if student["is_permanent"] or cursor.fetchone():
            return fail(
                "VALIDATION_FAILED",
                "لا يمكن إيقاف حساب دائم أو حساب إدارة/خدمة — ألغِ الاشتراك الدائم أولًا",
                422,
            )

    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            "UPDATE users SET status = ?, suspension_reason = ? WHERE id = ?",
            (data["status"], reason if data["status"] == "suspended" else None, student_id),
        )
    log_action("subscription_status_set", actor_id=session["user_id"],
               target_type="user", target_id=student_id,
               details={"status": data["status"], "reason": reason})
    return ok({"id": student_id, "status": data["status"], "reason": reason})


# -------------------------------------------------------------- settings ----

_SETTING_KEYS = (
    "qr_interval_sec", "attendance_start", "attendance_window_min",
    "checkout_time", "admin_contact_phone", "whatsapp_channel_url",
    "subscription_from", "subscription_to", "current_school_year",
    "penalty_late_10", "penalty_late_20", "penalty_late_30",
)
_NUMERIC_SETTINGS = ("qr_interval_sec", "attendance_window_min",
                     "penalty_late_10", "penalty_late_20", "penalty_late_30")
_TIME_SETTINGS = ("attendance_start", "checkout_time")
_DATE_SETTINGS = ("subscription_from", "subscription_to")
# This value is changed only by POST /admin/new-year. Allowing it through the
# generic settings endpoint would bypass archiving and confirmation.
_PROTECTED_SETTING_KEYS = ("current_school_year",)


def _validate_setting(key, value):
    if key not in _SETTING_KEYS:
        return "مفتاح إعداد غير معروف"
    if key in _PROTECTED_SETTING_KEYS:
        return "العام الدراسي الحالي يُعدَّل من شاشة بدء العام الجديد فقط"
    value = str(value).strip()
    if key in _NUMERIC_SETTINGS:
        try:
            if int(value) <= 0:
                raise ValueError
        except ValueError:
            return "القيمة يجب أن تكون رقمًا موجبًا"
    elif key in _TIME_SETTINGS:
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            return "الصيغة يجب أن تكون HH:MM"
    elif key in _DATE_SETTINGS:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return "الصيغة يجب أن تكون YYYY-MM-DD"
    elif key == "admin_contact_phone":
        if len(value) != 11 or not value.isdigit():
            return "رقم الإدارة يجب أن يتكون من 11 رقمًا"
    elif key == "whatsapp_channel_url" and value:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "رابط القناة يجب أن يبدأ بـ http:// أو https://"
    return None


@admin_bp.route("/settings", methods=["GET"])
@require_role("admin")
def get_settings():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(_SETTING_KEYS))})",
        _SETTING_KEYS,
    )
    return ok({r["key"]: r["value"] for r in cursor.fetchall()})


@admin_bp.route("/settings", methods=["PUT"])
@require_role("admin")
def put_settings():
    data = request.get_json(silent=True) or {}
    if not data:
        return fail("VALIDATION_FAILED", "لا توجد بيانات", 422)
    for key, value in data.items():
        error = _validate_setting(key, value)
        if error:
            return fail("VALIDATION_FAILED", error, 422)

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        for key, value in data.items():
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value).strip()),
            )
    log_action("settings_update", actor_id=session["user_id"],
               details={"keys": list(data.keys())})
    return ok({k: str(v).strip() for k, v in data.items()})


# ----------------------------------------------------------------- audit ----

AUDIT_ACTION_LABELS = {
    "join_request_approve": "قبول طلب انضمام",
    "join_request_reject": "رفض طلب انضمام",
    "user_create": "إنشاء حساب",
    "user_update": "تعديل بيانات حساب",
    "user_delete": "حذف حساب نهائيًا",
    "user_roles_set": "تعديل أدوار",
    "user_assignments_set": "تعديل تكليفات",
    "user_permanent_set": "تعديل اشتراك دائم",
    "subscription_period_set": "تعديل فترة الاشتراك",
    "subscription_status_set": "تعديل حالة اشتراك",
    "settings_update": "تعديل إعدادات",
    "notification_create": "إرسال إشعار",
    "followup_note": "تسجيل افتقاد",
    "followup_update": "تحديث حالة افتقاد",
    "school_week_set": "تعديل أسبوع دراسي",
    "new_year": "بدء عام دراسي",
    "backup_create": "إنشاء نسخة احتياطية",
    "backup_restore": "استعادة نسخة احتياطية",
    "export_db": "تنزيل أرشيف قاعدة البيانات",
    "sync_import": "مزامنة بيانات",
    "monthly_suspend": "إيقاف اشتراك تلقائي",
    "grade_add": "رصد درجة",
    "grade_update": "تعديل درجة",
    "bonus_add": "منح بونص",
    "deduction_add": "تسجيل خصم",
    "ledger_update": "تعديل قيد نقاط",
    "ledger_delete": "حذف قيد نقاط",
    "attendance_scan": "حضور ذاتي",
    "attendance_manual": "حضور يدوي",
    "content_add": "إضافة محتوى",
    "content_delete": "حذف محتوى",
    "assessment_create": "إنشاء تقييم",
    "assessment_update": "تعديل تقييم",
    "assessment_close": "إغلاق تقييم",
    "assessment_reopen": "إعادة فتح تقييم",
    "assessment_delete": "حذف تقييم",
    "liturgy_mark": "تسجيل قداس",
    "liturgy_update": "تعديل قداس",
    "liturgy_delete": "حذف قداس",
    "wheel_spin": "لفة عجلة",
}


def _sql_text(value):
    return "'" + str(value).replace("'", "''") + "'"


_AUDIT_ACTION_LABEL_SQL = (
    "CASE a.action "
    + " ".join(
        f"WHEN {_sql_text(key)} THEN {_sql_text(label)}"
        for key, label in AUDIT_ACTION_LABELS.items()
    )
    + " ELSE a.action END"
)

_AUDIT_TARGET_NAME_SQL = """
    CASE
        WHEN a.target_type IN ('user', 'student')
            THEN (SELECT full_name FROM users t WHERE t.id = a.target_id)
        WHEN a.target_type = 'content'
            THEN (SELECT title FROM content_links t WHERE t.id = a.target_id)
        WHEN a.target_type = 'assessment'
            THEN (SELECT title FROM assessments t WHERE t.id = a.target_id)
        WHEN a.target_type = 'notification'
            THEN (SELECT title FROM notifications t WHERE t.id = a.target_id)
        WHEN a.target_type = 'school_week'
            THEN CAST(a.target_id AS TEXT)
        ELSE NULL
    END
"""

_AUDIT_ACTOR_ROLE_SQL = """
    CASE
        WHEN EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = a.actor_id AND r.role = 'admin') THEN 'admin'
        WHEN EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = a.actor_id AND r.role = 'servant') THEN 'servant'
        WHEN EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = a.actor_id AND r.role = 'student') THEN 'student'
        ELSE NULL
    END
"""

_AUDIT_ROWS_CTE = f"""
    WITH audit_rows AS (
        SELECT a.id, a.actor_id, u.full_name AS actor_name,
               c.name_ar AS actor_class_name,
               a.action, {_AUDIT_ACTION_LABEL_SQL} AS action_label,
               a.target_type, a.target_id,
               {_AUDIT_TARGET_NAME_SQL} AS target_name,
               a.details_json, a.created_at,
               {_AUDIT_ACTOR_ROLE_SQL} AS actor_role,
               (SELECT GROUP_CONCAT(r.role) FROM user_roles r
                WHERE r.user_id = a.actor_id) AS actor_roles
        FROM audit_log a
        LEFT JOIN users u ON a.actor_id = u.id
        LEFT JOIN classes c ON u.class_id = c.id
    )
"""


def _cairo_day_start(raw, next_day=False):
    """Convert a YYYY-MM-DD (Cairo) to a UTC ISO boundary."""
    value = str(raw or "").strip()[:10]
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    if next_day:
        day = day + timedelta(days=1)
    return datetime.combine(day, time.min, tzinfo=CAIRO_TZ).astimezone(
        timezone.utc
    ).isoformat()


@admin_bp.route("/audit", methods=["GET"])
@require_role("admin")
def audit_log_list():
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    actor_id = request.args.get("actor_id")
    role = (request.args.get("role") or "").strip().lower()
    q = (request.args.get("q") or "").strip()[:80]
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        return fail("VALIDATION_FAILED", "رقم الصفحة غير صحيح", 422)
    if role and role not in ("student", "servant", "admin"):
        return fail("VALIDATION_FAILED", "الدور غير صحيح", 422)

    where = " WHERE 1=1"
    params = []

    if date_from:
        start = _cairo_day_start(date_from)
        if not start:
            return fail("VALIDATION_FAILED", "صيغة تاريخ البداية غير صحيحة (YYYY-MM-DD)", 422)
        where += " AND created_at >= ?"
        params.append(start)
    if date_to:
        end = _cairo_day_start(date_to, next_day=True)
        if not end:
            return fail("VALIDATION_FAILED", "صيغة تاريخ النهاية غير صحيحة (YYYY-MM-DD)", 422)
        where += " AND created_at < ?"
        params.append(end)
    if date_from and date_to:
        start_day = str(date_from).strip()[:10]
        end_day = str(date_to).strip()[:10]
        if start_day > end_day:
            return fail("VALIDATION_FAILED", "تاريخ البداية يجب أن يسبق تاريخ النهاية", 422)
    if actor_id:
        where += " AND actor_id = ?"
        params.append(actor_id)
    if q:
        like = f"%{q}%"
        where += (
            " AND (actor_name LIKE ? OR target_name LIKE ? OR action LIKE ?"
            " OR action_label LIKE ? OR details_json LIKE ? OR actor_class_name LIKE ?)"
        )
        params.extend([like] * 6)

    # Counts are independent of the role tab (but respect date/search/actor).
    base_where = where
    base_params = list(params)

    if role:
        # دور أساسي واحد لكل عملية (إداري > خادم > طالب) حتى لا تتكرر
        # العملية في أكثر من شريحة عندما يملك المستخدم أكثر من دور.
        where += " AND actor_role = ?"
        params.append(role)

    conn = get_db()
    cursor = conn.cursor()
    counts_sql = (
        _AUDIT_ROWS_CTE
        + """
        SELECT COUNT(*) AS all_count,
               COALESCE(SUM(CASE WHEN actor_role = 'student' THEN 1 ELSE 0 END), 0) AS student_count,
               COALESCE(SUM(CASE WHEN actor_role = 'servant' THEN 1 ELSE 0 END), 0) AS servant_count,
               COALESCE(SUM(CASE WHEN actor_role = 'admin' THEN 1 ELSE 0 END), 0) AS admin_count
        FROM audit_rows
        """
        + base_where
    )
    cursor.execute(counts_sql, base_params)
    counts_row = cursor.fetchone()

    count_sql = _AUDIT_ROWS_CTE + " SELECT COUNT(*) AS n FROM audit_rows" + where
    cursor.execute(count_sql, params)
    total = cursor.fetchone()["n"]

    per_page = 50
    query = (
        _AUDIT_ROWS_CTE
        + " SELECT * FROM audit_rows"
        + where
        + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    query_params = list(params) + [per_page, (page - 1) * per_page]
    cursor.execute(query, query_params)

    items = []
    for row in cursor.fetchall():
        item = dict(row)
        raw_details = item.get("details_json")
        try:
            item["details"] = json.loads(raw_details) if raw_details else None
        except (TypeError, ValueError):
            item["details"] = None
        roles = item.get("actor_roles")
        item["actor_roles"] = roles.split(",") if roles else []
        items.append(item)

    return ok({
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "counts": {
            "all": counts_row["all_count"],
            "student": counts_row["student_count"],
            "servant": counts_row["servant_count"],
            "admin": counts_row["admin_count"],
        },
    })


# ---------------------------------------------------------- notifications ----

@admin_bp.route("/notifications", methods=["GET"])
@require_role("admin")
def list_notifications():
    """سجل الإشعارات المرسلة، مع تصفية اختيارية حسب الفصل."""
    class_code = (request.args.get("class_code") or "all").strip()
    where = ""
    params = []
    selected_class = None

    conn = get_db()
    cursor = conn.cursor()
    if class_code and class_code != "all":
        cursor.execute(
            "SELECT id, code, name_ar FROM classes WHERE code = ?", (class_code,)
        )
        selected_class = cursor.fetchone()
        if not selected_class:
            return fail("VALIDATION_FAILED", "الفصل غير موجود", 422)
        where = " WHERE n.class_id IS NULL OR n.class_id = ?"
        params.append(selected_class["id"])

    cursor.execute(
        "SELECT n.id, n.title, n.body, n.created_at, "
        "       c.code AS class_code, c.name_ar AS class_name "
        "FROM notifications n LEFT JOIN classes c ON c.id = n.class_id"
        + where
        + " ORDER BY n.created_at DESC, n.id DESC",
        params,
    )
    items = [dict(row) for row in cursor.fetchall()]
    return ok({
        "items": items,
        "total": len(items),
        "class_code": selected_class["code"] if selected_class else "all",
        "class_name": selected_class["name_ar"] if selected_class else None,
    })


@admin_bp.route("/notifications", methods=["POST"])
@require_role("admin")
def create_notification():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    if not title or not body:
        return fail("VALIDATION_FAILED", "العنوان والنص إلزاميان", 422)
    if len(title) > 80:
        return fail("VALIDATION_FAILED", "العنوان طويل جدًا (80 حرفًا كحد أقصى)", 422)
    if len(body) > 240:
        return fail("VALIDATION_FAILED", "نص الإشعار طويل جدًا (240 حرفًا كحد أقصى)", 422)

    class_id = None
    conn = get_db()
    cursor = conn.cursor()
    if data.get("class_code"):
        cursor.execute(
            "SELECT id FROM classes WHERE code = ?", (data["class_code"],)
        )
        c_row = cursor.fetchone()
        if not c_row:
            return fail("VALIDATION_FAILED", "الفصل غير موجود", 422)
        class_id = c_row["id"]

    now_iso = datetime.now(timezone.utc).isoformat()
    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute(
            "INSERT INTO notifications (class_id, title, body, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (class_id, title, body, session["user_id"], now_iso),
        )
        nid = cur.lastrowid
    log_action("notification_create", actor_id=session["user_id"],
               target_type="notification", target_id=nid,
               details={"title": title, "class_code": data.get("class_code")})
    return ok({"id": nid, "class_code": data.get("class_code")}, 201)


# ----------------------------------------------------------- student-view ----

@admin_bp.route("/student-view/<int:student_id>", methods=["GET"])
@require_role("admin")
def student_view(student_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.id, u.full_name, u.status, c.code AS class_code, c.name_ar AS class_name
        FROM users u LEFT JOIN classes c ON u.class_id = c.id
        WHERE u.id = ?
        """,
        (student_id,),
    )
    student = cursor.fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)

    cursor.execute(
        """
        SELECT week_key, check_in_at, check_out_at, late_min, source
        FROM attendance WHERE user_id = ? ORDER BY week_key DESC
        """,
        (student_id,),
    )
    attendance = [dict(r) for r in cursor.fetchall()]

    cursor.execute(
        """
        SELECT week_key, quiet FROM liturgy_attendance
        WHERE student_id = ? ORDER BY week_key DESC
        """,
        (student_id,),
    )
    liturgy = [dict(r) for r in cursor.fetchall()]

    from backend.utils.authz import current_school_year
    from backend.utils.points import wheel_summary

    year = current_school_year(cursor)
    spins = wheel_summary(cursor, student_id, year)
    cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM point_ledger "
        "WHERE student_id = ? AND school_year = ?",
        (student_id, year),
    )
    total = cursor.fetchone()["total"]

    return ok({
        "student": dict(student),
        "attendance": attendance,
        "liturgy": liturgy,
        "stamps": spins["stamps"],
        "spins": spins,
        "total": total,
    })


# ----------------------------------------------------------- school weeks ----

@admin_bp.route("/school-weeks", methods=["GET"])
@require_role("admin")
def list_school_weeks():
    """Weeks of the current school year (used by the holiday toggle screen)."""
    from backend.utils.authz import current_school_year

    conn = get_db()
    cursor = conn.cursor()
    year = current_school_year(cursor)
    cursor.execute(
        "SELECT week_key, is_holiday, school_year FROM school_weeks "
        "WHERE school_year = ? ORDER BY week_key",
        (year,),
    )
    weeks = [
        {
            "week_key": row["week_key"],
            "is_holiday": bool(row["is_holiday"]),
            "school_year": row["school_year"],
        }
        for row in cursor.fetchall()
    ]
    return ok({
        "school_year": year,
        "current_week_key": week_key(),
        "weeks": weeks,
    })


@admin_bp.route("/school-weeks/<week_key>", methods=["PUT"])
@require_role("admin")
def set_school_week(week_key):
    try:
        week_day = datetime.strptime(week_key, "%Y-%m-%d").date()
    except ValueError:
        return fail("VALIDATION_FAILED", "صيغة الأسبوع غير صحيحة (YYYY-MM-DD)", 422)
    if week_day.weekday() != 3:
        return fail("VALIDATION_FAILED", "الأسبوع الدراسي يجب أن يكون يوم خميس", 422)
    data = request.get_json(silent=True) or {}
    if "is_holiday" not in data:
        return fail("VALIDATION_FAILED", "يرجى تحديد is_holiday", 422)
    is_holiday = 1 if data["is_holiday"] else 0

    conn = get_db()
    cursor = conn.cursor()
    from backend.utils.authz import current_school_year, ensure_school_week

    year = current_school_year(cursor)
    try:
        start_year, end_year = (int(part) for part in year.split("-"))
    except (AttributeError, ValueError):
        return fail("VALIDATION_FAILED", "صيغة العام الدراسي الحالية غير صحيحة", 422)
    if week_day < date(start_year, 9, 1) or week_day > date(end_year, 8, 31):
        return fail(
            "VALIDATION_FAILED",
            "الأسبوع يجب أن يقع داخل العام الدراسي الحالي",
            422,
        )

    cursor.execute(
        "SELECT school_year FROM school_weeks WHERE week_key = ?", (week_key,)
    )
    existing = cursor.fetchone()
    if existing and existing["school_year"] != year:
        return fail(
            "VALIDATION_FAILED",
            "لا يمكن تعديل أسبوع تابع لعام دراسي سابق",
            422,
        )

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        ensure_school_week(cur, week_key)
        cur.execute(
            "UPDATE school_weeks SET is_holiday = ? WHERE week_key = ?",
            (is_holiday, week_key),
        )
    log_action("school_week_set", actor_id=session["user_id"],
               target_type="school_week", target_id=week_key,
               details={"is_holiday": is_holiday, "school_year": year})
    return ok({
        "week_key": week_key,
        "is_holiday": bool(is_holiday),
        "school_year": year,
    })


# -------------------------------------------------------------- new year ----

_ARCHIVE_TABLES = (
    "attendance", "assessments", "grades", "liturgy_attendance",
    "point_ledger", "bonus_budget", "wheel_spins_used",
)


@admin_bp.route("/new-year", methods=["POST"])
@require_role("admin")
def new_year():
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return fail("VALIDATION_FAILED", "يتطلب التأكيد(confirm: true)", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT value FROM settings WHERE key = 'current_school_year'"
    )
    old_year = cursor.fetchone()["value"]
    try:
        start, end = old_year.split("-")
        new_year_value = f"{int(start) + 1}-{int(end) + 1}"
    except ValueError:
        return fail("VALIDATION_FAILED", "صيغة العام الدراسي الحالية غير صحيحة", 422)

    suffix = old_year.replace("-", "_")
    archived = []
    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        for table in _ARCHIVE_TABLES:
            archive_name = f"archive_{suffix}_{table}"
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {archive_name} AS SELECT * FROM {table}"
            )
            # Refresh the snapshot with the final year's rows.
            cur.execute(f"DELETE FROM {archive_name}")
            cur.execute(
                f"INSERT INTO {archive_name} SELECT * FROM {table}"
            )
            archived.append(archive_name)
        cur.execute(
            "UPDATE settings SET value = ? WHERE key = 'current_school_year'",
            (new_year_value,),
        )
        # Weekly bonus budgets belong to the old year's weeks.
        cur.execute("DELETE FROM bonus_budget")

    log_action("new_year", actor_id=session["user_id"],
               details={"from": old_year, "to": new_year_value})
    return ok({"archived": archived, "new_school_year": new_year_value})


# ---------------------------------------------------------- school day -------
@admin_bp.route("/school-day/status", methods=["GET"])
@require_role("admin", "servant")
def school_day_status():
    wk = week_key()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_holiday, is_open FROM school_weeks WHERE week_key = ?", (wk,)
    )
    row = cursor.fetchone()
    is_holiday = bool(row and row["is_holiday"])
    is_open = bool(row and row["is_open"])
    return ok({"week_key": wk, "is_open": is_open, "is_holiday": is_holiday})


@admin_bp.route("/school-day/open", methods=["POST"])
@require_role("admin")
def school_day_open():
    wk = week_key()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_holiday FROM school_weeks WHERE week_key = ?", (wk,)
    )
    row = cursor.fetchone()
    if row and row["is_holiday"]:
        return fail("HOLIDAY", "لا يمكن فتح يوم دراسي في أسبوع عطلة", 409)
    year = _setting_value(cursor, "current_school_year", "2026-2027")
    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute(
            "INSERT INTO school_weeks (week_key, school_year, is_open) VALUES (?, ?, 1) "
            "ON CONFLICT(week_key) DO UPDATE SET is_open = 1",
            (wk, year),
        )
    log_action("school_day_open", actor_id=session["user_id"], details={"week_key": wk})
    return ok({"week_key": wk, "is_open": True})


@admin_bp.route("/school-day/close", methods=["POST"])
@require_role("admin")
def school_day_close():
    wk = week_key()
    conn = get_db()
    cursor = conn.cursor()
    year = _setting_value(cursor, "current_school_year", "2026-2027")
    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute(
            "INSERT INTO school_weeks (week_key, school_year, is_open) VALUES (?, ?, 0) "
            "ON CONFLICT(week_key) DO UPDATE SET is_open = 0",
            (wk, year),
        )
        # Invalidate all QR tokens for the day
        cur.execute("DELETE FROM qr_tokens")
    log_action("school_day_close", actor_id=session["user_id"], details={"week_key": wk})
    return ok({"week_key": wk, "is_open": False})


# helper used above
def _setting_value(cursor, key, default=None):
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row["value"] if row else default


# ---------------------------------------------------------- google accounts ----
@admin_bp.route("/classes/<class_code>/google-account", methods=["PUT"])
@require_role("admin")
def set_class_google_account(class_code):
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip() or None
    if email and "@" not in email:
        return fail("VALIDATION_FAILED", "بريد Google غير صحيح", 422)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM classes WHERE code = ?", (class_code,))
    if not cursor.fetchone():
        return fail("NOT_FOUND", "الفصل غير موجود", 404)
    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            "UPDATE classes SET google_account = ? WHERE code = ?",
            (email, class_code),
        )
    log_action("class_google_account_set", actor_id=session["user_id"],
               target_type="class", target_id=class_code,
               details={"email": email})
    return ok({"class_code": class_code, "google_account": email})


# ------------------------------------------------------ export / backup ----

@admin_bp.route("/export-db", methods=["GET"])
@require_role("admin")
def export_db():
    import tempfile
    from flask import send_file

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'current_school_year'")
    row = cursor.fetchone()
    year = row["value"] if row else "unknown"

    source = sqlite3.connect(os.getenv("DB_PATH", config.DB_PATH))
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    target = sqlite3.connect(tmp_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    log_action("export_db", actor_id=session["user_id"],
               details={"school_year": year})
    filename = (
        f"school_export_{year.replace('-', '_')}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    )
    response = send_file(
        tmp_path,
        as_attachment=True,
        download_name=filename,
        mimetype="application/x-sqlite3",
    )
    response.call_on_close(lambda: os.path.exists(tmp_path) and os.remove(tmp_path))
    return response


@admin_bp.route("/backup", methods=["POST"])
@require_role("admin")
def create_backup_route():
    from backend.jobs.backup import create_backup
    filename = create_backup()
    log_action("backup_create", actor_id=session["user_id"],
               details={"filename": filename})
    return ok({"filename": filename}, 201)


@admin_bp.route("/backups", methods=["GET"])
@require_role("admin")
def list_backups_route():
    from backend.jobs.backup import list_backups
    return ok(list_backups())


@admin_bp.route("/restore", methods=["POST"])
@require_role("admin")
def restore_route():
    from backend.jobs.backup import restore_backup
    data = request.get_json(silent=True) or {}
    filename = data.get("filename")
    if not filename:
        return fail("VALIDATION_FAILED", "يرجى تحديد اسم النسخة", 422)
    if data.get("confirm") is not True:
        return fail("VALIDATION_FAILED", "يتطلب التأكيد(confirm: true)", 422)
    if not restore_backup(filename):
        return fail("NOT_FOUND", "النسخة الاحتياطية غير موجودة", 404)
    log_action("backup_restore", actor_id=session["user_id"],
               details={"filename": filename})
    return ok({"filename": filename, "status": "restored"})
