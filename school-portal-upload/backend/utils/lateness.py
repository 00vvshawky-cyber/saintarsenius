"""قواعد حساب التأخير والخصم (FR-ATT-05 / FR-ATT-06).

الحدود الافتراضية (قابلة للتعديل من إعدادات المدير — FR-ADM-03):
  • 0..9   دقائق ⇒ في الموعد، لا خصم، +10 نقاط حضور
  • 10..19 دقيقة ⇒ خصم «penalty_late_10» (افتراضيًا 1)
  • 20..29 دقيقة ⇒ خصم «penalty_late_20» (افتراضيًا 3)
  • 30      دقيقة ⇒ خصم «penalty_late_30» (افتراضيًا 5)
  • 31+     دقيقة  ⇒ لا نقاط حضور
"""
from datetime import datetime

DEFAULT_PENALTIES = (1, 3, 5)
ATTENDANCE_POINTS = 10


def late_minutes(start_dt: datetime, scan_dt: datetime) -> int:
    diff_seconds = (scan_dt - start_dt).total_seconds()
    if diff_seconds <= 0:
        return 0
    return int(diff_seconds // 60)


def penalties_from_cursor(cursor):
    """يقرأ قواعد الخصم المحفوظة في الإعدادات (مع القيم الافتراضية)."""
    try:
        cursor.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('penalty_late_10', 'penalty_late_20', 'penalty_late_30')"
        )
        values = {r["key"]: r["value"] for r in cursor.fetchall()}
    except Exception:
        values = {}
    out = []
    for key, default in zip(
        ("penalty_late_10", "penalty_late_20", "penalty_late_30"), DEFAULT_PENALTIES
    ):
        try:
            out.append(max(0, int(values.get(key, default))))
        except (TypeError, ValueError):
            out.append(default)
    return tuple(out)


def penalty_for(late_min: int, penalties=None):
    """(awarded, penalty, on_time) — awarded = تُمنح نقاط الحضور."""
    p10, p20, p30 = penalties or DEFAULT_PENALTIES
    if late_min <= 0:
        return (True, 0, True)
    elif late_min <= 9:
        return (True, 0, True)
    elif late_min <= 19:
        return (True, p10, False)
    elif late_min <= 29:
        return (True, p20, False)
    elif late_min == 30:
        return (True, p30, False)
    else:
        return (False, 0, False)
