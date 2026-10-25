PRAGMA foreign_keys = ON;

-- الفصول الثمانية (بيانات ثابتة تُزرع)
CREATE TABLE classes (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,         -- nursery, g1_2, g3_4, g5_6, prep, secondary, youth, mothers
  name_ar TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  google_account TEXT                -- حساب Google المرتبط بالفصل (بريد)
);

-- المواد الأربع (تُزرع)
CREATE TABLE subjects (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,         -- alhan, coptic, taks, mahfouzat
  name_ar TEXT NOT NULL,
  sort_order INTEGER NOT NULL
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  full_name TEXT NOT NULL,
  phone TEXT,                          -- 11 رقمًا؛ قد يكون NULL لطالب بلا هاتف؛
                                       -- مكرر مسموح لحسابات متعددة بنفس الرقم (SRS FR-A4)
  whatsapp TEXT,
  parent_phone TEXT,
  birth_date TEXT,                    -- YYYY-MM-DD
  gender TEXT CHECK (gender IN ('male','female')),
  deacon_rank TEXT,                   -- ذكور فقط
  class_id INTEGER REFERENCES classes(id),
  photo_url TEXT,                     -- Q6 معلّق: اتركه NULL
  password_hash TEXT NOT NULL,
  login_key TEXT NOT NULL,            -- يُستخدم للدخول: phone؛ الحساب يُحدد بمطابقة الرقم وكلمة المرور
  has_phone INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','active','rejected','suspended')),
  is_permanent INTEGER NOT NULL DEFAULT 0,   -- اشتراك دائم
  suspension_reason TEXT,              -- سبب الإيقاف (يظهر للطالب عند محاولة الدخول)
  created_at TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id)     -- المدير إن أنشأه بالنيابة
);

-- أدوار المستخدم (multi-profile)
CREATE TABLE user_roles (
  user_id INTEGER NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK (role IN ('student','servant','admin')),
  PRIMARY KEY (user_id, role)
);

-- إسناد الخادم: (فصل، مادة)
CREATE TABLE servant_assignments (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  class_id INTEGER NOT NULL REFERENCES classes(id),
  subject_id INTEGER NOT NULL REFERENCES subjects(id),
  UNIQUE (user_id, class_id, subject_id)
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE school_weeks (
  week_key TEXT PRIMARY KEY,          -- تاريخ الخميس YYYY-MM-DD
  is_holiday INTEGER NOT NULL DEFAULT 0,
  is_open INTEGER NOT NULL DEFAULT 0,
  school_year TEXT NOT NULL
);

CREATE TABLE qr_tokens (
  id INTEGER PRIMARY KEY,
  token TEXT UNIQUE NOT NULL,         -- عشوائي 32+ حرفًا
  numeric_code TEXT NOT NULL,         -- 4 أرقام
  valid_from TEXT NOT NULL,
  valid_to TEXT NOT NULL
);

CREATE TABLE attendance (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  week_key TEXT NOT NULL REFERENCES school_weeks(week_key),
  check_in_at TEXT,
  check_out_at TEXT,
  late_min INTEGER,
  source TEXT NOT NULL DEFAULT 'qr' CHECK (source IN ('qr','code','manual')),
  edited_by INTEGER REFERENCES users(id),
  UNIQUE (user_id, week_key)
);

CREATE TABLE content_links (
  id INTEGER PRIMARY KEY,
  subject_id INTEGER NOT NULL REFERENCES subjects(id),
  class_id INTEGER NOT NULL REFERENCES classes(id),
  title TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('audio','image','pdf')),
  url TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE assessments (
  id INTEGER PRIMARY KEY,
  subject_id INTEGER NOT NULL REFERENCES subjects(id),
  class_id INTEGER NOT NULL REFERENCES classes(id),
  week_key TEXT NOT NULL REFERENCES school_weeks(week_key),
  title TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('quiz','recitation')),
  external_url TEXT,                  -- للاختبار
  whatsapp_number TEXT,               -- للتسميع
  closes_at TEXT NOT NULL,            -- الأربعاء 23:59:59 القاهرة (UTC مخزَّن)
  created_by INTEGER NOT NULL REFERENCES users(id),
  CHECK ((kind='quiz' AND external_url IS NOT NULL) OR (kind='recitation' AND whatsapp_number IS NOT NULL))
);

CREATE TABLE grades (
  id INTEGER PRIMARY KEY,
  assessment_id INTEGER NOT NULL REFERENCES assessments(id),
  student_id INTEGER NOT NULL REFERENCES users(id),
  score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10),
  entered_by INTEGER NOT NULL REFERENCES users(id),
  entered_at TEXT NOT NULL,
  UNIQUE (assessment_id, student_id)
);

CREATE TABLE liturgy_attendance (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES users(id),
  week_key TEXT NOT NULL REFERENCES school_weeks(week_key),
  quiet INTEGER NOT NULL CHECK (quiet IN (0,1)),
  marked_by INTEGER NOT NULL REFERENCES users(id),
  UNIQUE (student_id, week_key)
);

-- رصيد مكافآت الخادم (لكل خادم/مادة/أسبوع)
CREATE TABLE bonus_budget (
  servant_id INTEGER NOT NULL REFERENCES users(id),
  subject_id INTEGER NOT NULL REFERENCES subjects(id),
  week_key TEXT NOT NULL REFERENCES school_weeks(week_key),
  total INTEGER NOT NULL DEFAULT 10,
  PRIMARY KEY (servant_id, subject_id, week_key)
);

-- القيد المحاسبي للنقاط — المصدر الوحيد للإجمالي
CREATE TABLE point_ledger (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES users(id),
  week_key TEXT REFERENCES school_weeks(week_key),
  kind TEXT NOT NULL CHECK (kind IN
    ('attendance','late_penalty','grade','liturgy','bonus','deduction','manual')),
  amount INTEGER NOT NULL,            -- موجب أو سالب
  subject_id INTEGER REFERENCES subjects(id),
  ref_id INTEGER,                     -- grades.id أو غيره
  reason TEXT,
  actor_id INTEGER NOT NULL REFERENCES users(id),
  school_year TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_ledger_student ON point_ledger(student_id, school_year);

CREATE TABLE wheel_spins_used (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES users(id),
  school_year TEXT NOT NULL,
  used_at TEXT NOT NULL,
  actor_id INTEGER NOT NULL REFERENCES users(id)
);

CREATE TABLE followups (
  student_id INTEGER PRIMARY KEY REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress','done')),
  handled_by INTEGER REFERENCES users(id),
  updated_at TEXT NOT NULL
);

-- سجل ملاحظات الافتقاد (تاريخ التواصل مع الأسرة)
CREATE TABLE followup_notes (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL DEFAULT 'call' CHECK (kind IN ('call','visit','chat')),
  note TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE INDEX idx_followup_notes_student ON followup_notes(student_id, created_at DESC);

CREATE TABLE notifications (
  id INTEGER PRIMARY KEY,
  class_id INTEGER REFERENCES classes(id),   -- NULL = للجميع
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_by INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL
);

-- حالة قراءة الإشعار لكل مستخدم (عدّاد غير المقروء — SRS FR-S20)
CREATE TABLE notification_reads (
  user_id INTEGER NOT NULL REFERENCES users(id),
  notification_id INTEGER NOT NULL REFERENCES notifications(id),
  read_at TEXT NOT NULL,
  PRIMARY KEY (user_id, notification_id)
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  actor_id INTEGER REFERENCES users(id),
  action TEXT NOT NULL,
  target_type TEXT,
  target_id INTEGER,
  details_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_audit_time ON audit_log(created_at);
