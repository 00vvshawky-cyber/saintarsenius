"""الإيقاف الشهري التلقائي + الإيقاف عند انتهاء فترة الاشتراك (FR-AUTH-04 · FR-ADM-02).

«أول كل شهر (يوم 1، 00:00 توقيت القاهرة) تتحول كل الحسابات `active` غير
الدائمة إلى `suspended`؛ المدير يعيد التفعيل يدويًا. الاشتراك الدائم
(`is_permanent = 1`) يُستثنى.»

وحسب FR-ADM-02: **عند انتهاء فترة الاشتراك الموحدة** (من `subscription_to`)
تُوقَف حسابات المدرسة كلها — عدا:
  • الحاصلين على اشتراك دائم،
  • الخدام والإداريين (حتى لا تتعطّل الخدمة).

كل حالة تُنفَّذ مرة واحدة فقط (علامة الشهر / علامة الفترة في الإعدادات)،
حتى يبقى بإمكان المدير إعادة التفعيل يدويًا دون أن يُوقَف فورًا.

الاستخدام:
    python -m backend.jobs.monthly_suspend            # تنفيذ إن كان مستحقًا
    python -m backend.jobs.monthly_suspend --force    # تنفيذ قسري (تجاهل الشرط)
"""
import sys
from datetime import date, datetime, timezone

from flask import has_app_context

from backend.db import db_transaction, get_db
from backend.utils.audit import log_action
from backend.utils.timeutil import to_cairo

MONTH_KEY = "last_monthly_suspend"
PERIOD_KEY = "last_period_suspend"


def _today(now=None):
    dt = now or datetime.now(timezone.utc)
    return to_cairo(dt).date()


def _setting(cursor, key):
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row["value"] if row else None


def _close(conn):
    """يغلق الاتصال فقط إذا لم يكن مشتركًا في سياق الطلب."""
    if not has_app_context():
        conn.close()


def is_due(now=None):
    """هل حان دور الإيقاف؟

    1) بداية شهر جديد (FR-AUTH-04) — مرة واحدة في الشهر.
    2) انتهاء فترة الاشتراك الموحدة ⇒ **حالة تُفرض في كل مرة** (ما دامت
       الفترة منتهية، فالحسابات غير المسدّدة تبقى موقوفة).
    """
    today = _today(now)
    conn = get_db()
    cursor = conn.cursor()
    last_month = _setting(cursor, MONTH_KEY)
    period_to = _setting(cursor, "subscription_to")
    _close(conn)

    # 1) بداية شهر جديد (FR-AUTH-04)
    if not last_month or last_month < today.strftime("%Y-%m"):
        return True

    # 2) انتهت فترة الاشتراك الموحدة ⇒ تُفرض في كل مرة (FR-ADM-02)
    if period_to and today.isoformat() > period_to:
        return True

    return False


def suspend_expired(now=None, force=False):
    """يوقف حسابات الطلاب غير الدائمين — يُرجع عدد الموقوفين."""
    today = _today(now)
    month_key = today.strftime("%Y-%m")

    conn = get_db()
    cursor = conn.cursor()
    last_month = _setting(cursor, MONTH_KEY)
    period_to = _setting(cursor, "subscription_to")
    _close(conn)

    due_monthly = force or (not last_month) or (last_month < month_key)
    due_period = bool(period_to) and today.isoformat() > period_to

    if not (due_monthly or due_period):
        return 0

    with db_transaction() as conn:
        cur = conn.cursor()
        # تُوقَف حسابات الطلاب فقط (من لا يملك دور خادم أو مدير) —
        # أما الدائمون (is_permanent) فهم مستثنون.
        cur.execute(
            """
            UPDATE users SET status = 'suspended'
            WHERE status = 'active' AND COALESCE(is_permanent, 0) = 0
              AND id IN (SELECT user_id FROM user_roles WHERE role = 'student')
              AND id NOT IN (SELECT user_id FROM user_roles WHERE role IN ('servant', 'admin'))
            """
        )
        suspended = cur.rowcount
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MONTH_KEY, month_key),
        )
        if due_period and period_to:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (PERIOD_KEY, period_to),
            )

    if suspended:
        log_action("monthly_suspend", actor_id=None, target_type="user",
                   details={"count": suspended, "month": month_key,
                            "period_ended": bool(due_period)})
    return suspended


def maybe_suspend(now=None):
    """تشغيل آمن (idempotent) — يُستدعى عند الإقلاع وعند فتح شاشة الاشتراكات."""
    try:
        return suspend_expired(now=now)
    except Exception:  # pragma: no cover - لا يجب أن يُعطّل الخدمة
        return 0


if __name__ == "__main__":
    force = "--force" in sys.argv
    count = suspend_expired(force=force)
    if count:
        print(f"Suspended {count} non-permanent student account(s).")
    else:
        print("Nothing to suspend.")
