from datetime import datetime, timedelta

from flask import Blueprint, request, session

from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils.audit import log_action
from backend.utils.authz import has_assignment
from backend.utils.timeutil import week_key

followup_bp = Blueprint("followups", __name__)

ACTIONS = {0: "لا يوجد غياب", 1: "الاتصال بالطالب", 2: "الاتصال بولي الأمر", 3: "زيارة"}

# مستوى الافتقاد حسب العقد (api.md §10): call | parent | visit
LEVELS = {0: "none", 1: "call", 2: "parent", 3: "visit"}


def _level(streak):
    return min(streak, 3)


def _action_label(streak):
    return ACTIONS[_level(streak)]


def _level_code(streak):
    return LEVELS[_level(streak)]


def _consecutive_absences(cursor, student_id, current_wk):
    """Weeks with no attendance, counted backwards over COMPLETED weeks only
    (the in-progress current week never counts as an absence)."""
    cursor.execute(
        "SELECT week_key FROM school_weeks WHERE is_holiday = 0 AND week_key < ? "
        "ORDER BY week_key DESC",
        (current_wk,),
    )
    streak = 0
    for row in cursor.fetchall():
        cursor.execute(
            "SELECT 1 FROM attendance WHERE user_id = ? AND week_key = ?",
            (student_id, row["week_key"]),
        )
        if cursor.fetchone():
            break
        streak += 1
    return streak


def _wa_me_link(phone, text=""):
    from urllib.parse import quote
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits.startswith("0"):
        intl = "20" + digits[1:]
    elif digits.startswith("20"):
        intl = digits
    else:
        intl = digits
    return f"https://wa.me/{intl}" + (f"?text={quote(text)}" if text else "")


@followup_bp.route("", methods=["GET"])
@require_role("servant", "admin")
def list_followups():
    """`GET /followup?class_code=&tab=present|absent` (العقد api.md §10)."""
    class_code = request.args.get("class_code")
    tab = request.args.get("tab") or "absent"
    if tab not in ("present", "absent"):
        return fail("VALIDATION_FAILED", "tab يجب أن يكون present أو absent", 422)

    current_wk = week_key()
    conn = get_db()
    cursor = conn.cursor()

    query = """
        SELECT u.id AS student_id, u.full_name, u.phone, u.whatsapp, u.parent_phone,
               u.class_id, c.code AS class_code, c.name_ar AS class_name
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
        LEFT JOIN classes c ON u.class_id = c.id
        WHERE u.status = 'active'
    """
    params = []

    if session["role"] == "servant":
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

    query += " ORDER BY u.full_name"
    cursor.execute(query, params)
    students = [dict(r) for r in cursor.fetchall()]

    items = []
    for student in students:
        cursor.execute(
            """
            SELECT check_in_at, check_out_at, late_min
            FROM attendance WHERE user_id = ? AND week_key = ?
            """,
            (student["student_id"], current_wk),
        )
        att = cursor.fetchone()
        attended = bool(att and att["check_in_at"])

        # FR-FLW-03: تبويبان — حاضرون / غائبون (الأسبوع الحالي)
        if tab == "present" and not attended:
            continue
        if tab == "absent" and attended:
            continue

        # FR-FLW-13: توزيع عشوائي/متوازن على خدام الفصل (بدون تكرار قبل استكمال الدورة)
        if tab == "absent" and session["role"] == "servant" and student.get("class_id"):
            assigned = _next_servant_for_student(cursor, student["student_id"], student["class_id"])
            if assigned is not None and assigned != session["user_id"]:
                continue

        # FR-FLW-01: الغياب المتتالي (الأسابيع المكتملة) + غياب الأسبوع الحالي
        streak = _consecutive_absences(cursor, student["student_id"], current_wk)
        if tab == "absent":
            streak += 1  # الأسبوع الحالي بلا حضور = غياب أيضًا

        cursor.execute(
            "SELECT status FROM followups WHERE student_id = ?",
            (student["student_id"],),
        )
        row = cursor.fetchone()

        # FR-FLW-07: بعد «تم الافتقاد» يُخفى الطالب من قائمة المهام المعلقة
        # (البيانات والسجلات تبقى — الإخفاء من قائمة المعلق فقط).
        if tab == "absent" and row and row["status"] == "done" and request.args.get("include_done") != "1":
            continue

        item = {
            "student_id": student["student_id"],
            "full_name": student["full_name"],
            "phone": student["phone"],
            "whatsapp": _wa_me_link(student["whatsapp"] or student["phone"]),
            "consecutive_absences": streak,
            "level": _level_code(streak),
            "status": row["status"] if row else None,
            # إضافات مساعدة للواجهة
            "action": _action_label(streak),
            "class_code": student["class_code"],
            "class_name": student["class_name"],
            "check_in_at": att["check_in_at"] if att else None,
            "check_out_at": att["check_out_at"] if att else None,
            "late_min": att["late_min"] if att else None,
        }
        # «parent_phone» فقط حين ≥ 2 (العقد api.md §10 / FR-FLW-02)
        if streak >= 2 and student["parent_phone"]:
            item["parent_phone"] = student["parent_phone"]
            item["parent_whatsapp_url"] = _wa_me_link(student["parent_phone"])
        # «آخر غياب» + العدد الإجمالي (مثل سجل الافتقاد للمدير)
        item["last_absence_week"] = _last_absence(cursor, student["student_id"], current_wk)
        item["total_absences"] = _total_absences(cursor, student["student_id"], current_wk)
        # أحدث ملاحظة افتقاد (سبب/نتيجة) — لعرضها في بطاقة الافتقاد
        cursor.execute(
            "SELECT note, kind, created_at FROM followup_notes WHERE student_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (student["student_id"],),
        )
        nrow = cursor.fetchone()
        item["latest_note"] = nrow["note"] if nrow else None
        item["latest_note_kind"] = nrow["kind"] if nrow else None
        item["latest_note_at"] = nrow["created_at"] if nrow else None
        items.append(item)

    # الأشد أولًا (الغائبون) / الأحدث وصولًا (الحاضرون)
    if tab == "absent":
        items.sort(key=lambda i: (-i["consecutive_absences"], i["full_name"]))
    else:
        items.sort(key=lambda i: (i["check_in_at"] or "", i["full_name"]), reverse=True)
    return ok(items)


@followup_bp.route("/<int:student_id>", methods=["PUT"])
@require_role("servant", "admin")
def update_followup_status(student_id):
    """`PUT /followup/{student_id}` ← `{status:"in_progress"|"done"}` (العقد api.md §10)."""
    return _set_status(student_id)


def _last_absence(cursor, student_id, current_wk):
    """أحدث أسبوع (خميس) بلا حضور — «آخر غياب» (FR-ADM-04)."""
    cursor.execute(
        "SELECT week_key FROM school_weeks WHERE is_holiday = 0 AND week_key <= ? "
        "ORDER BY week_key DESC",
        (current_wk,),
    )
    weeks = cursor.fetchall()
    for row in weeks:
        cursor.execute(
            "SELECT 1 FROM attendance WHERE user_id = ? AND week_key = ?",
            (student_id, row["week_key"]),
        )
        if not cursor.fetchone():
            return row["week_key"]
    return None


def _total_absences(cursor, student_id, current_wk):
    """إجمالي أسابيع الغياب (وليس المتتالي فقط) — «العدد» في سجل الافتقاد."""
    cursor.execute(
        "SELECT COUNT(*) AS n FROM school_weeks sw "
        "WHERE sw.is_holiday = 0 AND sw.week_key <= ? "
        "AND NOT EXISTS (SELECT 1 FROM attendance a "
        "WHERE a.user_id = ? AND a.week_key = sw.week_key)",
        (current_wk, student_id),
    )
    return cursor.fetchone()["n"]


def _class_servants(cursor, class_id):
    cursor.execute(
        "SELECT DISTINCT user_id FROM servant_assignments WHERE class_id = ?",
        (class_id,),
    )
    return [r["user_id"] for r in cursor.fetchall()]


def _next_servant_for_student(cursor, student_id, class_id):
    """يُرجع servant_id الذي يجب أن يتابع الطالب في الدورة الحالية،
    أو None إذا لم يكن هناك توزيع (خادم واحد/لا خدام) → يظهر للجميع."""
    servants = _class_servants(cursor, class_id)
    if len(servants) <= 1:
        return None
    # servants who already followed this student in the current cycle
    cursor.execute(
        "SELECT servant_id FROM followup_assignments WHERE student_id = ?",
        (student_id,),
    )
    used = {r["servant_id"] for r in cursor.fetchall()}
    # reset cycle if all servants used
    if used >= set(servants):
        cursor.execute("DELETE FROM followup_assignments WHERE student_id = ?", (student_id,))
        conn = None
        try:
            from flask import g
            if "db_conn" in g:
                g.db_conn.commit()
        except Exception:
            pass
        used = set()
    for s in sorted(servants):
        if s not in used:
            return s
    return servants[0]
    """إجمالي أسابيع الغياب (وليس المتتالي فقط) — «العدد» في سجل الافتقاد."""
    cursor.execute(
        "SELECT COUNT(*) AS n FROM school_weeks sw "
        "WHERE sw.is_holiday = 0 AND sw.week_key <= ? "
        "AND NOT EXISTS (SELECT 1 FROM attendance a "
        "WHERE a.user_id = ? AND a.week_key = sw.week_key)",
        (current_wk, student_id),
    )
    return cursor.fetchone()["n"]


@followup_bp.route("/log", methods=["GET"])
@require_role("admin")
def followup_log():
    """`GET /followup/log` — سجل الافتقاد للمدير (FR-FLW-06 · FR-ADM-04).

    الحقول: الاسم · آخر غياب · العدد · الخادم المسؤول · الحالة.
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.id AS student_id, u.full_name, u.phone, u.whatsapp, u.parent_phone,
               c.code AS class_code, c.name_ar AS class_name,
               f.status, f.updated_at, f.handled_by,
               h.full_name AS servant_name
        FROM followups f
        JOIN users u ON u.id = f.student_id
        LEFT JOIN classes c ON u.class_id = c.id
        LEFT JOIN users h ON h.id = f.handled_by
        ORDER BY f.updated_at DESC
        """
    )
    current_wk = week_key()
    items = []
    for row in cursor.fetchall():
        item = dict(row)
        item["consecutive_absences"] = _consecutive_absences(
            cursor, item["student_id"], current_wk
        )
        item["level"] = _level_code(item["consecutive_absences"])
        item["last_absence_week"] = _last_absence(cursor, item["student_id"], current_wk)
        item["total_absences"] = _total_absences(cursor, item["student_id"], current_wk)
        item["whatsapp_link"] = _wa_me_link(item.get("whatsapp") or item.get("phone"))
        items.append(item)
    return ok(items)


@followup_bp.route("/student/<int:student_id>/notes", methods=["GET"])
@require_role("servant", "admin")
def list_followup_notes(student_id):
    """سجل ملاحظات الافتقاد (تاريخ التواصل) — للمدير والخادم المسؤول."""
    conn = get_db()
    cursor = conn.cursor()
    student = cursor.execute(
        "SELECT id, class_id FROM users WHERE id = ?", (student_id,)
    ).fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)
    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], student["class_id"]
    ):
        return fail("FORBIDDEN", "هذا الطالب ليس في فصل مسند لك", 403)

    cursor.execute(
        """
        SELECT n.id, n.kind, n.note, n.created_at, n.created_by,
               u.full_name AS author_name
        FROM followup_notes n
        LEFT JOIN users u ON u.id = n.created_by
        WHERE n.student_id = ?
        ORDER BY n.created_at DESC, n.id DESC
        """,
        (student_id,),
    )
    return ok([dict(r) for r in cursor.fetchall()])


@followup_bp.route("/student/<int:student_id>/notes", methods=["POST"])
@require_role("servant", "admin")
def add_followup_note(student_id):
    """تسجيل ملاحظة افتقاد (نوع التواصل + النص) — تظهر في سجل الزيارات."""
    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()
    kind = data.get("kind") or "call"
    if not note:
        return fail("VALIDATION_FAILED", "نص الملاحظة مطلوب", 422)
    if kind not in ("call", "visit", "chat"):
        return fail("VALIDATION_FAILED", "نوع التواصل يجب أن يكون call أو visit أو chat", 422)

    conn = get_db()
    cursor = conn.cursor()
    student = cursor.execute(
        "SELECT id, class_id, full_name FROM users WHERE id = ?", (student_id,)
    ).fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)
    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], student["class_id"]
    ):
        return fail("FORBIDDEN", "هذا الطالب ليس في فصل مسند لك", 403)

    from backend.utils.timeutil import now_utc
    now_iso = now_utc().isoformat()
    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        cur.execute(
            "INSERT INTO followup_notes (student_id, kind, note, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (student_id, kind, note, session["user_id"], now_iso),
        )
        note_id = cur.lastrowid
        # أول ملاحظة ⇒ تبدأ المتابعة تلقائيًا
        cur.execute(
            "SELECT 1 FROM followups WHERE student_id = ?", (student_id,)
        )
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO followups (student_id, status, handled_by, updated_at) "
                "VALUES (?, 'in_progress', ?, ?)",
                (student_id, session["user_id"], now_iso),
            )

    log_action(
        "followup_note",
        actor_id=session["user_id"],
        target_type="student",
        target_id=student_id,
        details={
            "kind": kind,
            "note": note[:200],
            "student_name": student["full_name"],
        },
    )
    return ok({"id": note_id, "student_id": student_id, "kind": kind,
               "note": note, "created_at": now_iso}, 201)


@followup_bp.route("/student/<int:student_id>", methods=["GET"])
@require_role("servant", "admin")
def followup_detail(student_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.id, u.full_name, u.class_id, u.parent_phone, c.code AS class_code
        FROM users u LEFT JOIN classes c ON u.class_id = c.id
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

    current_wk = week_key()
    streak = _consecutive_absences(cursor, student_id, current_wk)
    cursor.execute(
        "SELECT status, updated_at FROM followups WHERE student_id = ?",
        (student_id,),
    )
    row = cursor.fetchone()

    return ok({
        "student": {
            "id": student["id"],
            "full_name": student["full_name"],
            "class_code": student["class_code"],
        },
        "absence_weeks": streak,
        "level": _level(streak),
        "action": _action_label(streak),
        "status": row["status"] if row else None,
        "updated_at": row["updated_at"] if row else None,
        # SRS: parent phone is exposed ONLY inside the followup screen.
        "parent_phone": student["parent_phone"],
        "parent_whatsapp_url": _wa_me_link(student["parent_phone"]),
    })


def _set_status(student_id):
    """منطق مشترك لتحديث حالة الافتقاد (in_progress | done | none)."""
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status is None and "in_progress" in data:
        status = "in_progress" if data["in_progress"] else "none"
    if status not in ("in_progress", "done", "none"):
        return fail("VALIDATION_FAILED", "الحالة يجب أن تكون in_progress أو done أو none", 422)

    note = (data.get("note") or "").strip() or None
    kind = data.get("kind") or "call"
    if kind not in ("call", "visit", "chat"):
        kind = "call"

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, class_id, full_name FROM users WHERE id = ?", (student_id,)
    )
    student = cursor.fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)
    if session["role"] == "servant" and not has_assignment(
        cursor, session["user_id"], student["class_id"]
    ):
        return fail("FORBIDDEN", "هذا الطالب ليس في فصل مسند لك", 403)

    # «تم الافتقاد» يتطلب إدخال نتيجة/سبب (إلزامي)
    if status == "done":
        cursor.execute(
            "SELECT 1 FROM followup_notes WHERE student_id = ?", (student_id,)
        )
        has_note = cursor.fetchone() is not None
        if not (note or has_note):
            return fail("NOTE_REQUIRED", "يرجى إدخال نتيجة المتابعة أو سبب الغياب قبل الإتمام", 422)

    from backend.utils.timeutil import now_utc
    now_iso = now_utc().isoformat()

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        if status == "none":
            cur.execute("DELETE FROM followups WHERE student_id = ?", (student_id,))
        else:
            cur.execute(
                """
                INSERT INTO followups (student_id, status, handled_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(student_id) DO UPDATE SET
                    status = excluded.status,
                    handled_by = excluded.handled_by,
                    updated_at = excluded.updated_at
                """,
                (student_id, status, session["user_id"], now_iso),
            )
        if note:
            cur.execute(
                "INSERT INTO followup_notes (student_id, kind, note, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (student_id, kind, note, session["user_id"], now_iso),
            )
        if status == "done":
            cur.execute(
                "INSERT INTO followup_assignments (student_id, servant_id, week_key, created_at) "
                "VALUES (?, ?, ?, ?)",
                (student_id, session["user_id"], week_key(), now_iso),
            )

    log_action(
        "followup_update",
        actor_id=session["user_id"],
        target_type="student",
        target_id=student_id,
        details={"status": status, "student_name": student["full_name"]},
    )
    return ok({"student_id": student_id, "status": status if status != "none" else None})


@followup_bp.route("/student/<int:student_id>", methods=["PUT"])
@require_role("servant", "admin")
def update_followup(student_id):
    """اسم مختصر مطابق لـ `PUT /followup/{student_id}`."""
    return _set_status(student_id)
