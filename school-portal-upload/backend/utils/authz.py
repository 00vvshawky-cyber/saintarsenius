"""Authorization + small shared helpers for route modules."""


def has_assignment(cursor, user_id, class_id, subject_id=None):
    """True if the servant is assigned to the class (optionally the subject too)."""
    if subject_id is None:
        cursor.execute(
            "SELECT 1 FROM servant_assignments WHERE user_id = ? AND class_id = ?",
            (user_id, class_id),
        )
    else:
        cursor.execute(
            "SELECT 1 FROM servant_assignments WHERE user_id = ? AND class_id = ? AND subject_id = ?",
            (user_id, class_id, subject_id),
        )
    return cursor.fetchone() is not None


def assigned_class_ids(cursor, user_id):
    cursor.execute(
        "SELECT DISTINCT class_id FROM servant_assignments WHERE user_id = ?",
        (user_id,),
    )
    return [row["class_id"] for row in cursor.fetchall()]


def assigned_subject_ids(cursor, user_id, class_id=None):
    """معرّفات المواد المسندة للخادم (ومرتبطة بفصل معيّن إن طُلب).

    قاعدة ثابتة في التطبيق كله: **الخادم يعرض/يرصد موادّه المسندة فقط**
    (FR-CNT-03 · FR-GRD-01/06). دالة واحدة تحفظ القاعدة لكل الشاشات.
    """
    if class_id is None:
        cursor.execute(
            "SELECT DISTINCT subject_id FROM servant_assignments WHERE user_id = ?",
            (user_id,),
        )
    else:
        cursor.execute(
            "SELECT DISTINCT subject_id FROM servant_assignments "
            "WHERE user_id = ? AND class_id = ?",
            (user_id, class_id),
        )
    return [row["subject_id"] for row in cursor.fetchall()]


def ensure_school_week(cursor, week_key_str):
    """Create the school_weeks row if missing (FK target for several tables)."""
    cursor.execute(
        "SELECT value FROM settings WHERE key = 'current_school_year'"
    )
    row = cursor.fetchone()
    year = row["value"] if row else "2026-2027"
    cursor.execute(
        "INSERT OR IGNORE INTO school_weeks (week_key, school_year) VALUES (?, ?)",
        (week_key_str, year),
    )


def current_school_year(cursor):
    cursor.execute("SELECT value FROM settings WHERE key = 'current_school_year'")
    row = cursor.fetchone()
    return row["value"] if row else "2026-2027"
