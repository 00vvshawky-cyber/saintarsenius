from flask import Flask, jsonify, request, session
from urllib.parse import urlparse
from backend.config import DB_PATH, SECRET_KEY
from backend.db import get_db, close_db
from backend.errors import register_error_handlers, fail
import os

def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = SECRET_KEY

    # Session cookie hardening (CSRF / session-theft defense).
    # SESSION_COOKIE_SECURE=1 in .env once HTTPS is decided (spec Q4).
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

    register_error_handlers(app)

    # CSRF defense-in-depth on top of SameSite=Lax: reject state-changing
    # requests whose Origin does not match this host. Non-browser clients
    # (no Origin header) are not affected.
    @app.before_request
    def csrf_protect():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("Origin")
            if origin:
                origin_host = urlparse(origin).netloc
                if origin_host and origin_host != request.host:
                    return fail("FORBIDDEN", "طلب من مصدر غير موثوق", 403)

    @app.before_request
    def enforce_active_account():
        """حساب موقوف/مرفوض/قيد المراجعة لا يكمل جلسته.

        يُنفَّذ عند كل طلب API: فور إيقاف الطالب من شاشة الاشتراكات (أو
        انتهاء فترة الاشتراك) تُمنع جلسته المفتوحة فورًا (FR-AUTH-04).
        """
        if not request.path.startswith("/api/v1/"):
            return None
        user_id = session.get("user_id")
        if not user_id:
            return None

        from backend.db import get_db
        conn = get_db()
        row = conn.execute(
            "SELECT status, suspension_reason FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            session.clear()
            return fail("UNAUTHENTICATED", "غير مسجّل دخول", 401)
        if row["status"] == "active":
            return None

        # الحساب لم يعد نشطًا ⇒ تُنهى الجلسة ويُرفض الطلب
        admin_phone = None
        try:
            s_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'admin_contact_phone'"
            ).fetchone()
            admin_phone = s_row["value"] if s_row else None
        except Exception:
            pass
        session.clear()
        details = {"admin_contact_phone": admin_phone}
        if row["status"] == "suspended":
            details["suspension_reason"] = row["suspension_reason"]
        status_codes = {
            "pending": ("ACCOUNT_PENDING", "حسابك قيد المراجعة من قِبل المدير"),
            "rejected": ("ACCOUNT_REJECTED", "تم رفض طلب انضمامك للمدرسة"),
            "suspended": ("ACCOUNT_SUSPENDED", "حسابك موقوف حاليًا"),
        }
        code, message = status_codes.get(
            row["status"], ("ACCOUNT_PENDING", "حسابك غير مفعّل")
        )
        return fail(code, message, 403, details=details)

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    @app.teardown_appcontext
    def teardown(exception):
        close_db(exception)

    # Blueprints will be added here
    from backend.routes.auth_routes import auth_bp
    app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")
    from backend.routes.attendance_routes import attendance_bp
    app.register_blueprint(attendance_bp, url_prefix="/api/v1/attendance")
    from backend.routes.student_routes import student_bp
    app.register_blueprint(student_bp, url_prefix="/api/v1/student")
    from backend.routes.content_routes import content_bp
    app.register_blueprint(content_bp, url_prefix="/api/v1/content")
    from backend.routes.admin_routes import admin_bp
    app.register_blueprint(admin_bp, url_prefix="/api/v1/admin")
    from backend.routes.assessment_routes import assessment_bp
    app.register_blueprint(assessment_bp, url_prefix="/api/v1/assessments")
    from backend.routes.grade_routes import grade_bp
    app.register_blueprint(grade_bp, url_prefix="/api/v1/grades")
    from backend.routes.liturgy_routes import liturgy_bp
    app.register_blueprint(liturgy_bp, url_prefix="/api/v1/liturgy")
    from backend.routes.point_routes import point_bp
    app.register_blueprint(point_bp, url_prefix="/api/v1/points")
    from backend.routes.wheel_routes import wheel_bp
    app.register_blueprint(wheel_bp, url_prefix="/api/v1/wheel")
    from backend.routes.servant_routes import servant_bp
    app.register_blueprint(servant_bp, url_prefix="/api/v1/servant")
    from backend.routes.followup_routes import followup_bp
    # المسار التعاقدي: /api/v1/followup — و(/api/v1/followups) اسم مختصر
    app.register_blueprint(followup_bp, url_prefix="/api/v1/followup")
    app.register_blueprint(followup_bp, url_prefix="/api/v1/followups", name="followups_alias")
    from backend.routes.sync_routes import sync_bp
    app.register_blueprint(sync_bp, url_prefix="/api/v1/sync")

    # الإيقاف الشهري التلقائي (FR-AUTH-04) — يُنفَّذ عند الإقلاع إن كان مستحقًا
    try:
        from backend.db import ensure_columns
        ensure_columns()
    except Exception:  # pragma: no cover
        pass
    try:
        from backend.jobs.monthly_suspend import maybe_suspend
        maybe_suspend()
    except Exception:  # pragma: no cover - لا يجب أن يُعطّل الإقلاع
        pass

    # Serving frontend (ملفات الواجهة في frontend/ — تُقدَّم شاشة تلو الأخرى)
    from flask import send_file

    FRONTEND_DIR = os.path.abspath(os.path.join(app.root_path, "..", "frontend"))

    def _frontend_file(rel_path):
        """يعيد المسار الكامل المأمون داخل frontend أو None."""
        if not rel_path:
            return None
        target = os.path.abspath(os.path.join(FRONTEND_DIR, rel_path))
        if target.startswith(FRONTEND_DIR + os.sep) and os.path.isfile(target):
            return target
        return None

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        # مسارات الـ API غير المطابقة تبقى JSON 404 ولا تُقدَّم HTML
        if path.startswith("api/"):
            return fail("NOT_FOUND", "المسار غير موجود", 404)

        target = _frontend_file(path)
        if target is None and not os.path.splitext(path)[1]:
            target = _frontend_file(path + ".html")  # /login ← login.html
        if target is None and not path:
            target = _frontend_file("index.html")
        if target is None:
            # صفحة غير موجودة بعد ← شاشة الدخول (بداية المسار دائمًا)
            target = _frontend_file("index.html")
        if target is None:
            return fail("NOT_FOUND", "لم يتم تجهيز الواجهة بعد", 404)
        return send_file(target)

    return app
