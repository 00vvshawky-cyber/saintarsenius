from backend.db import get_db

CLASSES = [
    ("nursery", "حضانة", 1),
    ("g1_2", "أولى وتانية", 2),
    ("g3_4", "تالتة ورابعة", 3),
    ("g5_6", "خامسة وستة", 4),
    ("prep", "إعدادي", 5),
    ("secondary", "ثانوي", 6),
    ("youth", "شباب", 7),
    ("mothers", "أمهات", 8),
]

SUBJECTS = [
    ("alhan", "الألحان", 1),
    ("coptic", "القبطي", 2),
    ("taks", "الطقس", 3),
    ("mahfouzat", "المحفوظات", 4),
]

SETTINGS = [
    ("qr_interval_sec", "30"),
    ("attendance_start", "17:00"),
    ("attendance_window_min", "60"),
    ("checkout_time", "21:00"),
    ("admin_contact_phone", "01234567890"),
    ("whatsapp_channel_url", "https://whatsapp.com"),
    ("subscription_from", "2026-10-01"),
    ("subscription_to", "2027-07-01"),
    ("current_school_year", "2026-2027"),
    # قواعد الخصم (FR-ATT-05) — قابلة للتعديل من إعدادات المدير (FR-ADM-03)
    ("penalty_late_10", "1"),
    ("penalty_late_20", "3"),
    ("penalty_late_30", "5"),
]

def seed_data():
    conn = get_db()
    cursor = conn.cursor()

    for code, name_ar, order in CLASSES:
        cursor.execute("INSERT OR IGNORE INTO classes (code, name_ar, sort_order) VALUES (?, ?, ?)", (code, name_ar, order))

    for code, name_ar, order in SUBJECTS:
        cursor.execute("INSERT OR IGNORE INTO subjects (code, name_ar, sort_order) VALUES (?, ?, ?)", (code, name_ar, order))

    for key, val in SETTINGS:
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

    conn.commit()
    conn.close()
    print("Default classes, subjects, and settings seeded.")

if __name__ == "__main__":
    seed_data()
