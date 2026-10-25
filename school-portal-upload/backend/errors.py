from flask import jsonify

def ok(data=None, status=200):
    response = {}
    if data is not None:
        response["data"] = data
    return jsonify(response), status

def fail(code, message, status=400, details=None):
    err = {
        "error": {
            "code": code,
            "message": message
        }
    }
    if details is not None:
        err["error"]["details"] = details
    return jsonify(err), status

def register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(e):
        return fail("NOT_FOUND", "الصفحة أو المسار غير موجود", 404)

    @app.errorhandler(405)
    def method_not_allowed(e):
        return fail("METHOD_NOT_ALLOWED", "طريقة الطلب غير مسموح بها", 405)

    @app.errorhandler(500)
    def internal_error(e):
        return fail("INTERNAL_ERROR", "حدث خطأ غير متوقع في الخادم", 500)
