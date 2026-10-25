"""Points, stamps, and wheel calculations.

Rules (spec §5 / data-model):
- point_ledger is the single source of truth for totals (SUM(amount) per
  school year).
- One stamp per week when ALL six requirements are met (60/60):
  r1 on-time attendance, r2..r5 top weekly grade = 10 in each of the four
  subjects, r6 quiet Friday liturgy. Bonus/deduction never affect stamps.
- Spins earned = floor(stamps / 4); spins available = earned - used.
"""
from backend.utils.timeutil import week_key

SUBJECT_CODES = ["alhan", "coptic", "taks", "mahfouzat"]


def points_total(cursor, student_id, school_year):
    row = cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM point_ledger "
        "WHERE student_id = ? AND school_year = ?",
        (student_id, school_year),
    ).fetchone()
    return row["total"]


def weekly_requirements(cursor, student_id, wk):
    """{r1..r6} booleans for the given week."""
    reqs = {"r1": False, "r2": False, "r3": False, "r4": False, "r5": False, "r6": False}

    row = cursor.execute(
        "SELECT late_min FROM attendance WHERE user_id = ? AND week_key = ?",
        (student_id, wk),
    ).fetchone()
    reqs["r1"] = bool(row and row["late_min"] is not None and row["late_min"] < 10)

    cursor.execute(
        """
        SELECT s.code, MAX(g.score) AS top
        FROM grades g
        JOIN assessments a ON g.assessment_id = a.id
        JOIN subjects s ON a.subject_id = s.id
        WHERE g.student_id = ? AND a.week_key = ?
        GROUP BY s.code
        """,
        (student_id, wk),
    )
    tops = {r["code"]: r["top"] for r in cursor.fetchall()}
    for i, code in enumerate(SUBJECT_CODES, start=2):
        reqs[f"r{i}"] = tops.get(code) == 10

    row = cursor.execute(
        "SELECT quiet FROM liturgy_attendance WHERE student_id = ? AND week_key = ?",
        (student_id, wk),
    ).fetchone()
    reqs["r6"] = bool(row and row["quiet"] == 1)
    return reqs


def count_stamps(cursor, student_id, school_year=None):
    """Count fully-completed weeks (optionally scoped to one school year)."""
    query = "SELECT week_key FROM school_weeks WHERE is_holiday = 0"
    params = []
    if school_year:
        query += " AND school_year = ?"
        params.append(school_year)
    query += " ORDER BY week_key"
    weeks = cursor.execute(query, params).fetchall()
    stamps = 0
    for w in weeks:
        if all(weekly_requirements(cursor, student_id, w["week_key"]).values()):
            stamps += 1
    return stamps


def stamped_weeks(cursor, student_id, school_year=None):
    """قائمة أسابيع المدرسة التي تحققت فيها المتطلبات الستة (أختام)."""
    query = "SELECT week_key FROM school_weeks WHERE is_holiday = 0"
    params = []
    if school_year:
        query += " AND school_year = ?"
        params.append(school_year)
    query += " ORDER BY week_key"
    weeks = cursor.execute(query, params).fetchall()
    return [
        w["week_key"] for w in weeks
        if all(weekly_requirements(cursor, student_id, w["week_key"]).values())
    ]


def all_weeks(cursor, school_year=None):
    """كل أسابيع المدرسة (غير الأعياد) — لعرض خانات الأختام."""
    query = "SELECT week_key FROM school_weeks WHERE is_holiday = 0"
    params = []
    if school_year:
        query += " AND school_year = ?"
        params.append(school_year)
    query += " ORDER BY week_key"
    return [r["week_key"] for r in cursor.execute(query, params).fetchall()]


def wheel_summary(cursor, student_id, school_year):
    stamps = count_stamps(cursor, student_id, school_year)
    spins_earned = stamps // 4
    row = cursor.execute(
        "SELECT COALESCE(COUNT(*), 0) AS used FROM wheel_spins_used "
        "WHERE student_id = ? AND school_year = ?",
        (student_id, school_year),
    ).fetchone()
    spins_used = row["used"]
    return {
        "stamps": stamps,
        "spins_earned": spins_earned,
        "spins_used": spins_used,
        "spins_available": max(0, spins_earned - spins_used),
    }


def student_points_payload(cursor, student_id, school_year, wk=None, with_history=True):
    wk = wk or week_key()
    total = points_total(cursor, student_id, school_year)
    reqs = weekly_requirements(cursor, student_id, wk)
    stamps = count_stamps(cursor, student_id, school_year)
    row = cursor.execute(
        "SELECT COALESCE(COUNT(*), 0) AS used FROM wheel_spins_used "
        "WHERE student_id = ? AND school_year = ?",
        (student_id, school_year),
    ).fetchone()
    spins_used = row["used"]
    spins_earned = stamps // 4
    spins_available = max(0, spins_earned - spins_used)

    payload = {
        "total": total,
        "current_week": {"week_key": wk, "requirements": reqs},
        "stamps": stamps,
        "spins_available": spins_available,
        # تفاصيل إضافية لعرضها في شاشة الطالب
        "spins_earned": spins_earned,
        "spins_used": spins_used,
        "school_year": school_year,
        "stamped_weeks": stamped_weeks(cursor, student_id, school_year),
        "all_weeks": all_weeks(cursor, school_year),
    }
    row = cursor.execute(
        "SELECT full_name FROM users WHERE id = ?", (student_id,)
    ).fetchone()
    payload["full_name"] = row["full_name"] if row else ""
    if with_history:
        cursor.execute(
            """
            SELECT pl.id, pl.kind, pl.amount, pl.reason, pl.subject_id, pl.week_key,
                   pl.created_at, pl.actor_id, u.full_name AS actor_name,
                   s.code AS subject_code, s.name_ar AS subject_name
            FROM point_ledger pl
            LEFT JOIN users u ON pl.actor_id = u.id
            LEFT JOIN subjects s ON pl.subject_id = s.id
            WHERE pl.student_id = ?
            ORDER BY pl.created_at DESC, pl.id DESC
            """,
            (student_id,),
        )
        payload["history"] = [dict(r) for r in cursor.fetchall()]
    return payload
