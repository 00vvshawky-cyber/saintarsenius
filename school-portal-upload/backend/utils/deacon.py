"""قواعد الرتبة الشماسية وفصل الحضانة (SRS FR-AUTH-01).

تُستخدم من مسار التسجيل ومسار المدير حتى لا تتعارض القواعد.
"""

# القيم القانونية للرتبة الشماسية (مطابقة لـ frontend و DB)
DEACON_RANKS = (
    "none",           # لم أرسم بعد
    "epsaltos",      # الإبصالتس
    "anagnostis",    # الأغنسطس
    "epidiakon",     # الإيبودياكون
    "diakon",        # الدياكون
    "archdiakon",    # الأرشيدياكون
)

# فصل الحضانة: لم يُرسم بعد — الرتبة مقفلة على "none"
NURSERY_CLASS_CODES = ("nursery", "kg")

# فصل الأمهات: للإناث فقط
MOTHERS_CLASS_CODES = ("mothers",)


def resolve_deacon_rank(class_code, gender, deacon_rank):
    """تُرجع (rank, error) — rank هي القيمة التي تُخزَّن فعليًا.

    القواعد:
      • الإناث: لا رتبة شماسية (None).
      • فصل الحضانة: الرتبة مقفلة على "none" (لم أرسم بعد).
      • أي قيمة خارج القائمة القانونية ← خطأ.
    """
    if gender == "female":
        # الإناث لا يحملون رتبة شماسية — نتجاهل أي قيمة مرسلة
        return None, None

    rank = deacon_rank if deacon_rank is not None else "none"
    if rank not in DEACON_RANKS:
        return None, "الرتبة الشماسية غير صحيحة"

    if class_code in NURSERY_CLASS_CODES:
        # الحضانة: قفل الرتبة على «لم أرسم بعد»
        return "none", None

    return rank, None
