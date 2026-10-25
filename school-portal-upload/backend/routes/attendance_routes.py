import base64
import io
import ipaddress
import random
import secrets
from datetime import datetime, timedelta, timezone

import qrcode
from flask import Blueprint, request, session

from backend.config import SERVER_MODE
from backend.db import get_db, db_transaction
from backend.errors import ok, fail
from backend.routes.auth_routes import require_role
from backend.utils import ratelimit
from backend.utils.audit import log_action
from backend.utils.lateness import late_minutes, penalty_for, penalties_from_cursor
from backend.utils.timeutil import now_utc, to_cairo, week_key

attendance_bp = Blueprint("attendance", __name__)


def _get_or_create_qr():
    """Return the current rotating QR payload, minting one if expired."""
    now = now_utc()
    now_iso = now.isoformat()

    with db_transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'qr_interval_sec'")
        interval = int(cursor.fetchone()['value'])

        # Clean up tokens that are past their grace period
        # (FR-ATT-02: كل رمز صالح للفترة الحالية + فترة سماح واحدة)
        grace_cutoff = (now - timedelta(seconds=interval)).isoformat()
        cursor.execute("DELETE FROM qr_tokens WHERE valid_to < ?", (grace_cutoff,))

        cursor.execute(
            "SELECT * FROM qr_tokens WHERE valid_from <= ? AND valid_to >= ?",
            (now_iso, now_iso),
        )
        token_row = cursor.fetchone()

        if token_row:
            return {
                "token": token_row['token'],
                "numeric_code": token_row['numeric_code'],
                "valid_to": token_row['valid_to'],
                "interval_sec": interval,
            }

        token = secrets.token_urlsafe(24)
        numeric_code = f"{random.randint(0, 9999):04d}"
        valid_to = now + timedelta(seconds=interval)
        cursor.execute(
            "INSERT INTO qr_tokens (token, numeric_code, valid_from, valid_to) VALUES (?, ?, ?, ?)",
            (token, numeric_code, now_iso, valid_to.isoformat()),
        )
        return {
            "token": token,
            "numeric_code": numeric_code,
            "valid_to": valid_to.isoformat(),
            "interval_sec": interval,
        }


def _setting(cursor, key, default=None):
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row['value'] if row else default


def _attendance_window_open(now_cairo):
    """Thursday (school day) and within [attendance_start, +attendance_window_min]."""
    conn = get_db()
    cursor = conn.cursor()
    if now_cairo.weekday() != 3:  # Thursday
        return False
    wk = week_key(now_utc())
    cursor.execute(
        "SELECT is_holiday FROM school_weeks WHERE week_key = ?", (wk,)
    )
    row = cursor.fetchone()
    if row and row['is_holiday']:
        return False
    start_str = _setting(cursor, "attendance_start", "17:00")
    window_min = int(_setting(cursor, "attendance_window_min", "60"))
    hour, minute = map(int, start_str.split(':'))
    start = now_cairo.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start <= now_cairo <= start + timedelta(minutes=window_min)


def _school_day_open(wk):
    """True iff the admin has opened the school day for this week_key."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT is_open FROM school_weeks WHERE week_key = ?", (wk,))
    row = cursor.fetchone()
    return bool(row and row["is_open"])


@attendance_bp.route("/qr/current", methods=["GET"])
@require_role("servant", "admin")
def get_qr():
    wk = week_key()
    if not _school_day_open(wk):
        return fail("SCHOOL_DAY_CLOSED", "اليوم الدراسي مغلق", 403)
    return ok(_get_or_create_qr())


@attendance_bp.route("/qr/image", methods=["GET"])
@require_role("servant", "admin")
def get_qr_image():
    wk = week_key()
    if not _school_day_open(wk):
        return fail("SCHOOL_DAY_CLOSED", "اليوم الدراسي مغلق", 403)
    qr_data = _get_or_create_qr()
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(qr_data["token"])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return ok({**qr_data, "image": f"data:image/png;base64,{b64}"})


def _is_local_request():
    """SRS §2.3/§9: attendance is only accepted from the school LAN."""
    addr = request.remote_addr
    if not addr:
        return False
    ip = ipaddress.ip_address(addr)
    return ip.is_private or ip.is_loopback


@attendance_bp.route("/scan", methods=["POST"])
@require_role("student")
def scan():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    code = data.get("numeric_code")
    user_id = session.get("user_id")

    # SRS §2.1/FR-S5: the cloud server never accepts attendance.
    if SERVER_MODE != "school":
        return fail("SERVER_MODE_DISABLED", "تسجيل الحضور متاح على سيرفر المدرسة فقط", 403)

    if not _is_local_request():
        return fail("FORBIDDEN", "المسح متاح من الشبكة المحلية للمدرسة فقط", 403)

    if not token and not code:
        return fail("QR_INVALID", "رمز غير صالح", 400)

    # SRS §2.3: rate-limit code entry attempts per student.
    scan_key = f"scan:{user_id}"
    if ratelimit.is_blocked(
        scan_key,
        max_attempts=ratelimit.SCAN_MAX_ATTEMPTS,
        window_seconds=ratelimit.SCAN_WINDOW_SECONDS,
    ):
        return fail("TOO_MANY_ATTEMPTS", "محاولات كثيرة، انتظر قليلًا ثم أعد المحاولة", 429)

    conn = get_db()
    cursor = conn.cursor()

    now = now_utc()
    now_iso = now.isoformat()

    # فترة السماح (FR-ATT-02): يبقى الرمز مقبولاً حتى نهاية الفترة التالية
    cursor.execute("SELECT value FROM settings WHERE key = 'qr_interval_sec'")
    interval_row = cursor.fetchone()
    interval_sec = int(interval_row['value']) if interval_row else 30
    accept_from = (now - timedelta(seconds=interval_sec)).isoformat()

    if token:
        cursor.execute(
            "SELECT * FROM qr_tokens WHERE token = ? AND valid_to >= ?",
            (token, accept_from),
        )
    else:
        cursor.execute(
            "SELECT * FROM qr_tokens WHERE numeric_code = ? AND valid_to >= ?",
            (code, accept_from),
        )

    if not cursor.fetchone():
        ratelimit.record_failure(scan_key)
        return fail("QR_EXPIRED", "انتهت صلاحية الرمز", 400)

    # 2. Check current school week
    wk = week_key(now)

    # 1b. School day must be explicitly opened by admin
    if not _school_day_open(wk):
        return fail("SCHOOL_DAY_CLOSED", "اليوم الدراسي مغلق", 403)
    cursor.execute(
        "SELECT * FROM school_weeks WHERE week_key = ? AND is_holiday = 0", (wk,)
    )
    if not cursor.fetchone():
        # Auto-create school week if it's Thursday and doesn't exist
        if to_cairo(now).weekday() == 3:  # Thursday
            year = _setting(cursor, "current_school_year", "2026-2027")
            with db_transaction() as conn_tx:
                conn_tx.cursor().execute(
                    "INSERT OR IGNORE INTO school_weeks (week_key, school_year) VALUES (?, ?)",
                    (wk, year),
                )
        else:
            return fail("OUTSIDE_WINDOW", "لا توجد مدرسة اليوم", 403)

    # 3. Check existing attendance
    cursor.execute(
        "SELECT * FROM attendance WHERE user_id = ? AND week_key = ?", (user_id, wk)
    )
    att = cursor.fetchone()

    cursor.execute("SELECT full_name FROM users WHERE id = ?", (user_id,))
    student_row = cursor.fetchone()
    student_name = student_row["full_name"] if student_row else None
    source = "qr" if token else "code"
    result = None
    audit_details = None

    with db_transaction() as conn_tx:
        cursor_tx = conn_tx.cursor()

        if not att:
            # Action: Check-in
            start_str = _setting(cursor_tx, "attendance_start", "17:00")
            cairo_now = to_cairo(now)
            hour, minute = map(int, start_str.split(':'))
            school_start = cairo_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )

            late_min = late_minutes(school_start, cairo_now)
            awarded, penalty, on_time = penalty_for(late_min, penalties_from_cursor(cursor_tx))

            cursor_tx.execute(
                """
                INSERT INTO attendance (user_id, week_key, check_in_at, late_min, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, wk, now_iso, late_min, source),
            )

            year = _setting(cursor_tx, "current_school_year", "2026-2027")

            if awarded:
                cursor_tx.execute(
                    """
                    INSERT INTO point_ledger (student_id, week_key, kind, amount, actor_id, school_year, created_at)
                    VALUES (?, ?, 'attendance', 10, ?, ?, ?)
                    """,
                    (user_id, wk, user_id, year, now_iso),
                )
                if penalty > 0:
                    cursor_tx.execute(
                        """
                        INSERT INTO point_ledger (student_id, week_key, kind, amount, actor_id, school_year, created_at)
                        VALUES (?, ?, 'late_penalty', ?, ?, ?, ?)
                        """,
                        (user_id, wk, -penalty, user_id, year, now_iso),
                    )

            result = ok({"action": "check_in", "at": now_iso, "late_min": late_min, "penalty": penalty})
            audit_details = {
                "action": "check_in",
                "student_name": student_name,
                "week_key": wk,
                "at": now_iso,
                "late_min": late_min,
                "penalty": penalty,
                "points": 10 if awarded else 0,
                "source": source,
            }

        elif att['check_out_at'] is None:
            # Action: Check-out
            cursor_tx.execute(
                "UPDATE attendance SET check_out_at = ? WHERE id = ?", (now_iso, att['id'])
            )
            result = ok({"action": "check_out", "at": now_iso})
            audit_details = {
                "action": "check_out",
                "student_name": student_name,
                "week_key": wk,
                "at": now_iso,
                "late_min": att["late_min"],
                "source": source,
            }
        else:
            return fail("ALREADY_CHECKED_OUT", "لقد قمت بتسجيل الانصراف بالفعل", 400)

    log_action(
        "attendance_scan",
        actor_id=user_id,
        target_type="student",
        target_id=user_id,
        details=audit_details,
    )
    return result


@attendance_bp.route("/today", methods=["GET"])
@require_role("servant", "admin")
def attendance_today():
    """Roster of the current school week; servants only see assigned classes."""
    wk = week_key()
    conn = get_db()
    cursor = conn.cursor()
    class_code = request.args.get("class_code")

    query = """
        SELECT u.id as student_id, u.full_name, c.code as class_code,
               a.check_in_at, a.check_out_at, a.late_min
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id AND ur.role = 'student'
        LEFT JOIN attendance a ON a.user_id = u.id AND a.week_key = ?
        LEFT JOIN classes c ON u.class_id = c.id
        WHERE u.status = 'active'
    """
    params = [wk]

    if session.get("role") == "servant":
        cursor.execute(
            "SELECT DISTINCT c.code FROM servant_assignments sa "
            "JOIN classes c ON sa.class_id = c.id WHERE sa.user_id = ?",
            (session["user_id"],),
        )
        allowed = [r['code'] for r in cursor.fetchall()]
        if not allowed:
            return ok([])
        placeholders = ",".join("?" * len(allowed))
        query += f" AND c.code IN ({placeholders})"
        params.extend(allowed)
        if class_code and class_code not in allowed:
            return fail("FORBIDDEN", "هذا الفصل غير مسند لك", 403)

    if class_code:
        query += " AND c.code = ?"
        params.append(class_code)

    query += " ORDER BY u.full_name"
    cursor.execute(query, params)
    return ok([dict(row) for row in cursor.fetchall()])


@attendance_bp.route("/status", methods=["GET"])
@require_role("student")
def my_status():
    """حالة حضور الطالب لهذا الأسبوع (للشاشة)."""
    user_id = session["user_id"]
    wk = week_key()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT check_in_at, check_out_at, late_min FROM attendance "
        "WHERE user_id = ? AND week_key = ?",
        (user_id, wk),
    )
    att = cursor.fetchone()
    window_open = _attendance_window_open(to_cairo(now_utc()))
    start = _setting(cursor, "attendance_start", "17:00")
    checkout = _setting(cursor, "checkout_time", "21:00")
    wk = week_key()
    return ok({
        "checked_in": bool(att and att['check_in_at']),
        "checked_out": bool(att and att['check_out_at']),
        "window_open": window_open,
        "school_day_open": _school_day_open(wk),
        # تفاصيل إضافية لعرضها في شاشة الطالب
        "check_in_at": att['check_in_at'] if att else None,
        "check_out_at": att['check_out_at'] if att else None,
        "late_min": att['late_min'] if att else None,
        "attendance_start": start,
        "checkout_time": checkout,
        "week_key": wk,
    })


@attendance_bp.route("/lateness-calc", methods=["GET"])
@require_role("admin")
def lateness_calc():
    """حاسبة التأخير (FR-ADM-03): دقائق ⇒ أثرها على النقاط."""
    raw = request.args.get("minutes")
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return fail("VALIDATION_FAILED", "يرجى إدخال عدد الدقائق كرقم صحيح", 422)
    if minutes < 0:
        return fail("VALIDATION_FAILED", "عدد الدقائق لا يمكن أن يكون سالبًا", 422)
    awarded, penalty, on_time = penalty_for(
        minutes, penalties_from_cursor(get_db().cursor())
    )
    if minutes > 30:
        band = "late_30_plus"
    elif minutes >= 30:
        band = "late_30"
    elif minutes >= 20:
        band = "late_20"
    elif minutes >= 10:
        band = "late_10"
    else:
        band = "on_time"
    return ok({
        "penalty": penalty,             # قيمة الخصم (موجبة) — تظهر سالبة في السجل
        "gives_points": awarded,        # هل تُمنح نقاط الحضور (+10)؟
        "late_min": minutes,
        "on_time": on_time,            # «حضور في الموعد» = أقل من 10 دقائق
        "attendance_points": 10 if awarded else 0,
        "band": band,
    })


@attendance_bp.route("/manual", methods=["PUT"])
@require_role("admin")
def manual_attendance():
    data = request.get_json(silent=True) or {}
    student_id = data.get("student_id")
    wk = data.get("week_key")
    check_in_at = data.get("check_in_at")
    check_out_at = data.get("check_out_at")
    reason = (data.get("reason") or "").strip() or None

    if not student_id or not wk:
        return fail("VALIDATION_FAILED", "يرجى تحديد الطالب والأسبوع", 422)
    try:
        datetime.strptime(wk, "%Y-%m-%d")
    except ValueError:
        return fail("VALIDATION_FAILED", "صيغة الأسبوع غير صحيحة (YYYY-MM-DD)", 422)

    late_min = None
    if check_in_at:
        try:
            cin = datetime.fromisoformat(check_in_at)
        except ValueError:
            return fail("VALIDATION_FAILED", "صيغة وقت الحضور غير صحيحة (ISO-8601)", 422)
        conn = get_db()
        cursor = conn.cursor()
        start_str = _setting(cursor, "attendance_start", "17:00")
        cairo_in = to_cairo(cin)
        hour, minute = map(int, start_str.split(':'))
        school_start = cairo_in.replace(hour=hour, minute=minute, second=0, microsecond=0)
        late_min = late_minutes(school_start, cairo_in)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, full_name FROM users WHERE id = ?", (student_id,))
    student = cursor.fetchone()
    if not student:
        return fail("NOT_FOUND", "الطالب غير موجود", 404)

    awarded, penalty, _ = penalty_for(late_min or 0)
    year = _setting(cursor, "current_school_year", "2026-2027")
    now_iso = now_utc().isoformat()

    with db_transaction() as conn_tx:
        cur = conn_tx.cursor()
        # Manual edits may target a week with no school_weeks row yet (FK).
        year = _setting(cursor, "current_school_year", "2026-2027")
        cur.execute(
            "INSERT OR IGNORE INTO school_weeks (week_key, school_year) VALUES (?, ?)",
            (wk, year),
        )
        cur.execute(
            "SELECT id FROM attendance WHERE user_id = ? AND week_key = ?",
            (student_id, wk),
        )
        row = cur.fetchone()
        had_record = row is not None
        if row:
            att_id = row['id']
            cur.execute(
                "UPDATE attendance SET check_in_at = ?, check_out_at = ?, late_min = ?, source = 'manual', edited_by = ? WHERE id = ?",
                (check_in_at, check_out_at, late_min, session["user_id"], att_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO attendance (user_id, week_key, check_in_at, check_out_at, late_min, source, edited_by)
                VALUES (?, ?, ?, ?, ?, 'manual', ?)
                """,
                (student_id, wk, check_in_at, check_out_at, late_min, session["user_id"]),
            )
            att_id = cur.lastrowid

        # Keep the ledger consistent: replace attendance entries for the week.
        cur.execute(
            "DELETE FROM point_ledger WHERE student_id = ? AND week_key = ? AND kind IN ('attendance', 'late_penalty')",
            (student_id, wk),
        )
        if check_in_at and awarded:
            cur.execute(
                """
                INSERT INTO point_ledger (student_id, week_key, kind, amount, actor_id, school_year, created_at)
                VALUES (?, ?, 'attendance', 10, ?, ?, ?)
                """,
                (student_id, wk, session["user_id"], year, now_iso),
            )
            if penalty > 0:
                cur.execute(
                    """
                    INSERT INTO point_ledger (student_id, week_key, kind, amount, actor_id, school_year, created_at)
                    VALUES (?, ?, 'late_penalty', ?, ?, ?, ?)
                    """,
                    (student_id, wk, -penalty, session["user_id"], year, now_iso),
                )

    log_action(
        "attendance_manual",
        actor_id=session["user_id"],
        target_type="student",
        target_id=student_id,
        details={
            "student_name": student["full_name"],
            "week_key": wk,
            "check_in_at": check_in_at,
            "check_out_at": check_out_at,
            "late_min": late_min,
            "reason": reason,
            "updated": had_record,
        },
    )
    return ok({
        "id": att_id,
        "student_id": student_id,
        "week_key": wk,
        "check_in_at": check_in_at,
        "check_out_at": check_out_at,
        "late_min": late_min,
        "source": "manual",
    })
