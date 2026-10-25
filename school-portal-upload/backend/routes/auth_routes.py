from flask import Blueprint, request, session
from backend.db import db_transaction, get_db
from backend.errors import ok, fail
from backend.utils import ratelimit
from backend.utils.deacon import (
    MOTHERS_CLASS_CODES,
    resolve_deacon_rank,
)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime
import functools

auth_bp = Blueprint("auth", __name__)

def require_role(*roles):
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            if "user_id" not in session or session.get("role") not in roles:
                return fail("UNAUTHORIZED", "غير مصرح لك بالوصول", 403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    # Basic validation
    required_fields = ['full_name', 'phone', 'class_code', 'password', 'gender']
    if not all(field in data for field in required_fields):
        return fail("VALIDATION_FAILED", "بيانات غير مكتملة", 422)

    # SRS §3: phones are exactly 11 digits
    phone = data['phone']
    if not (isinstance(phone, str) and len(phone) == 11 and phone.isdigit()):
        return fail("VALIDATION_FAILED", "رقم الموبايل يجب أن يتكون من 11 رقمًا", 422)
    for opt_field in ('whatsapp', 'parent_phone'):
        val = data.get(opt_field)
        if val and not (isinstance(val, str) and len(val) == 11 and val.isdigit()):
            return fail("VALIDATION_FAILED", "رقم الواتساب أو ولي الأمر غير صحيح (11 رقمًا)", 422)

    # رقم ولي الأمر يجب ألا يطابق رقم الطالب (استثناء شباب/أمهات من
    # الإلزام فقط — أما التوأم الرقمي فمحظور على الجميع)
    if data.get('parent_phone') and data['parent_phone'] == phone:
        return fail("VALIDATION_FAILED", "رقم ولي الأمر يجب أن يختلف عن رقم الطالب", 422)

    # صورة الطالب الشخصية (إلزامية في نموذج الانضمام) — data:image مضغوطة
    photo = data.get('photo')
    if photo is not None and not (
        isinstance(photo, str)
        and photo.startswith("data:image/")
        and len(photo) <= 1_200_000
    ):
        return fail("VALIDATION_FAILED", "صورة الطالب غير صالحة", 422)

    # تاريخ الميلاد: YYYY-MM-DD صالح، تاريخ حقيقي، وغير مستقبلي
    birth = data.get('birth_date')
    if birth:
        if not (isinstance(birth, str) and len(birth) == 10
                and birth[4] == '-' and birth[7] == '-'
                and birth.replace('-', '').isdigit()):
            return fail("VALIDATION_FAILED",
                        "تاريخ الميلاد يجب أن يكون بصيغة YYYY-MM-DD", 422)
        try:
            birth_date = datetime.strptime(birth, "%Y-%m-%d").date()
        except ValueError:
            return fail("VALIDATION_FAILED", "تاريخ الميلاد غير صالح", 422)
        if birth_date > date.today():
            return fail("VALIDATION_FAILED",
                        "تاريخ الميلاد لا يمكن أن يكون في المستقبل", 422)

    # SRS FR-A5: names capped at 40 chars (auto-truncated)
    full_name = data['full_name'][:40]

    # الرتبة الشماسية: ذكور فقط، وفصل الحضانة مقفل على «لم أرسم بعد»
    class_code = data['class_code']
    deacon_rank, rank_error = resolve_deacon_rank(
        class_code, data['gender'], data.get('deacon_rank')
    )
    if rank_error:
        return fail("VALIDATION_FAILED", rank_error, 422)

    # فصل الأمهات مخصص للإناث فقط
    if class_code in MOTHERS_CLASS_CODES and data['gender'] != 'female':
        return fail("VALIDATION_FAILED", "فصل الأمهات مخصص للسيدات فقط", 422)

    with db_transaction() as conn:
        cursor = conn.cursor()

        # SRS FR-A4 / risk #5: accounts sharing one phone must have
        # DIFFERENT passwords (multi-role households).
        cursor.execute("SELECT password_hash FROM users WHERE phone = ?", (phone,))
        for row in cursor.fetchall():
            if check_password_hash(row['password_hash'], data['password']):
                return fail(
                    "VALIDATION_FAILED",
                    "كلمة المرور مكررة بين حسابات بنفس الرقم — يلزم أن تكون مختلفة",
                    422,
                )

        # Get class_id
        cursor.execute("SELECT id FROM classes WHERE code = ?", (class_code,))
        row = cursor.fetchone()
        if not row:
            return fail("VALIDATION_FAILED", "فصل غير موجود", 422)
        class_id = row['id']

        password_hash = generate_password_hash(data['password'])

        cursor.execute("""
            INSERT INTO users (full_name, phone, whatsapp, parent_phone, birth_date, gender, deacon_rank, class_id, password_hash, login_key, photo_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            full_name, phone, data.get('whatsapp'), data.get('parent_phone'),
            data.get('birth_date'), data['gender'],
            deacon_rank, class_id, password_hash, phone, photo
        ))

        user_id = cursor.lastrowid
        cursor.execute("INSERT INTO user_roles (user_id, role) VALUES (?, 'student')", (user_id,))

    return ok({"status": "pending"}, 201)

@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    if 'login_key' not in data or 'password' not in data:
        return fail("VALIDATION_FAILED", "يرجى إدخال الهاتف وكلمة المرور", 422)

    login_key = data['login_key']

    if ratelimit.is_blocked(login_key):
        return fail(
            "TOO_MANY_ATTEMPTS",
            "تم تجاوز عدد محاولات الدخول، حاول مرة أخرى بعد 10 دقائق",
            429,
        )

    conn = get_db()
    cursor = conn.cursor()
    # SRS FR-A4: one phone may host several accounts; the account is
    # identified by matching phone + password.
    cursor.execute("SELECT * FROM users WHERE login_key = ?", (login_key,))
    candidates = cursor.fetchall()
    user = None
    for candidate in candidates:
        if check_password_hash(candidate['password_hash'], data['password']):
            user = candidate
            break

    if user is None:
        ratelimit.record_failure(login_key)
        return fail("BAD_CREDENTIALS", "بيانات الدخول غير صحيحة", 401)

    ratelimit.clear(login_key)

    if user['status'] != 'active':
        # api.md: distinct code per status + admin contact phone in details
        cursor.execute("SELECT value FROM settings WHERE key = 'admin_contact_phone'")
        s_row = cursor.fetchone()
        details = {"admin_contact_phone": s_row["value"] if s_row else None}
        if user['status'] == 'suspended':
            # سبب الإيقاف (أدخله المدير) يظهر للطالب
            details["suspension_reason"] = user['suspension_reason']
        status_codes = {
            "pending": ("ACCOUNT_PENDING", "حسابك قيد المراجعة من قِبل المدير"),
            "rejected": ("ACCOUNT_REJECTED", "تم رفض طلب انضمامك للمدرسة"),
            "suspended": ("ACCOUNT_SUSPENDED", "حسابك موقوف حاليًا"),
        }
        code, message = status_codes.get(
            user['status'], ("ACCOUNT_PENDING", "حسابك غير مفعّل")
        )
        return fail(code, message, 403, details=details)

    session["user_id"] = user['id']

    # Get roles
    cursor.execute("SELECT role FROM user_roles WHERE user_id = ?", (user['id'],))
    roles = [r['role'] for r in cursor.fetchall()]
    session["role"] = roles[0] # Default to first

    return ok({
        "user": {"id": user['id'], "full_name": user['full_name'], "status": user['status']},
        "roles": roles,
        "active_role": session["role"],
    })

@auth_bp.route("/switch-role", methods=["POST"])
@require_role("student", "servant", "admin")
def switch_role():
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if not role:
        return fail("VALIDATION_FAILED", "يرجى تحديد الدور المطلوب", 422)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM user_roles WHERE user_id = ? AND role = ?",
        (session["user_id"], role),
    )
    if not cursor.fetchone():
        return fail("FORBIDDEN", "لا تملك هذا الدور", 403)

    session["role"] = role
    return ok({"active_role": role})


@auth_bp.route("/change-password", methods=["POST"])
@require_role("student", "servant", "admin")
def change_password():
    data = request.get_json(silent=True) or {}
    old_password = data.get("old_password")
    new_password = data.get("new_password")
    if not old_password or not new_password:
        return fail("VALIDATION_FAILED", "يرجى إدخال كلمة المرور القديمة والجديدة", 422)
    if len(new_password) < 8:
        return fail("VALIDATION_FAILED", "كلمة المرور الجديدة يجب ألا تقل عن 8 أحرف", 422)

    user_id = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if not row or not check_password_hash(row["password_hash"], old_password):
        return fail("BAD_CREDENTIALS", "كلمة المرور الحالية غير صحيحة", 401)

    new_hash = generate_password_hash(new_password)
    with db_transaction() as conn_tx:
        conn_tx.cursor().execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user_id)
        )
    return ok()


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return ok()

@auth_bp.route("/me", methods=["GET"])
def me():
    if "user_id" not in session:
        return fail("UNAUTHENTICATED", "غير مسجل دخول", 401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, full_name, status, phone, whatsapp FROM users WHERE id = ?",
        (session["user_id"],),
    )
    user = cursor.fetchone()

    cursor.execute("SELECT role FROM user_roles WHERE user_id = ?", (session["user_id"],))
    roles = [r['role'] for r in cursor.fetchall()]

    # api.md §1: /auth/me ⇒ {user, roles, active_role, admin_contact_phone}
    cursor.execute("SELECT value FROM settings WHERE key = 'admin_contact_phone'")
    s_row = cursor.fetchone()

    return ok({
        "user": dict(user),
        "roles": roles,
        "active_role": session["role"],
        "admin_contact_phone": s_row["value"] if s_row else None,
    })
