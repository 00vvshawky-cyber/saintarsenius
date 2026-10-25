/* ============================================================
   school-portal — الطبقة المشتركة للواجهة (من الشاشة 1 فصاعدًا)
   - api(): غلاف fetch لـ /api/v1 يفهم شكل {data} و {error:{code,message,details}}
   - إدارة جلسة + حارس أدوار
   - رسائل toast بألوان التصميم (Design tokens)
   ============================================================ */

const API_BASE = "/api/v1";

/* ============================================================
   إخفاء زر «إظهار/إخفاء كلمة المرور» المدمج في المتصفح (Edge)
   السبب: زر المتصفح يُرسم بأيقونة عبر background-image/background-color،
   وTailwind (preflight) يصفّرها للأزرار ⇒ تظهر فقط الكلمة «إخفاء كلمة المرور».
   التطبيق يملك زر العين الخاص به ⇒ نخفي زر المتصفح.
   ============================================================ */
(function hideNativePasswordToggle() {
  if (document.getElementById("ui-native-password-style")) return;
  const style = document.createElement("style");
  style.id = "ui-native-password-style";
  style.textContent =
    'input[type="password"]::-ms-reveal,' +
    'input[type="password"]::-ms-clear{display:none !important;}' +
    '.ui-password-toggle{display:none !important;}';
  (document.head || document.documentElement).appendChild(style);
})();

class ApiError extends Error {
  constructor(code, message, status, details) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details || {};
  }
}

async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const init = {
    method,
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }

  let res;
  try {
    res = await fetch(API_BASE + path, init);
  } catch (e) {
    throw new ApiError("NETWORK_ERROR", "تعذر الاتصال بالخادم، تحقق من الشبكة", 0);
  }

  let payload = null;
  try {
    payload = await res.json();
  } catch (e) {
    payload = null;
  }

  if (!res.ok) {
    const err = (payload && payload.error) || {};
    throw new ApiError(
      err.code || "UNKNOWN",
      err.message || "حدث خطأ غير متوقع",
      res.status,
      err.details
    );
  }
  return payload ? payload.data : null;
}

/* ---------- الجلسة (كوكي الخادم هو مصدر الحقيقة؛ هذه نسخة مؤقتة
   لخدمات التوجيه والحارس فقط) ---------- */

const SESSION_KEY = "school_portal_session";

function saveSession(data) {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(data));
  } catch (e) {}
}

function getSession() {
  try {
    return JSON.parse(localStorage.getItem(SESSION_KEY) || "null");
  } catch (e) {
    return null;
  }
}

function clearSession() {
  try {
    localStorage.removeItem(SESSION_KEY);
  } catch (e) {}
}

const ROLE_HOME = {
  student: "student-home.html",
  servant: "servant-classes.html",
  admin: "admin.html",
};

const ROLE_LABEL = {
  student: "طالب",
  servant: "خادم",
  admin: "مدير",
};

/* تحويل بعد نجاح الدخول: متعدد الأدوار ← شاختيار الدور،
   وإلا ← صفحة الدور مباشرة */
function redirectAfterLogin(data) {
  saveSession(data);
  const roles = (data && data.roles) || [];
  if (roles.length > 1) {
    location.href = "roles.html";
    return;
  }
  const role = (data && data.active_role) || roles[0] || "student";
  location.href = ROLE_HOME[role] || ROLE_HOME.student;
}

/* حارس لأي صفحة داخلية: يتحقق من الجلسة عبر /auth/me.
   يعيد بيانات المستخدم أو يحوّل لشاشة الدخول. */
async function requireAuth(allowedRoles) {
  try {
    const me = await api("/auth/me");
    saveSession({ user: me.user, roles: me.roles, active_role: me.active_role });
    if (
      allowedRoles &&
      allowedRoles.length &&
      !allowedRoles.includes(me.active_role)
    ) {
      location.href = ROLE_HOME[me.active_role] || "index.html";
      return null;
    }
    return me;
  } catch (e) {
    /* حساب موقوف/مرفوض/قيد المراجعة ⇒ نُعلم الطالب وله رقم الإدارة */
    if (
      e &&
      (e.code === "ACCOUNT_SUSPENDED" ||
        e.code === "ACCOUNT_REJECTED" ||
        e.code === "ACCOUNT_PENDING")
    ) {
      const phone = e.details && e.details.admin_contact_phone;
      const reason = e.details && e.details.suspension_reason;
      let msg;
      if (e.code === "ACCOUNT_SUSPENDED") {
        msg =
          (reason
            ? "انتهت مدة الاشتراك — السبب: " + reason + "، للتواصل مع إدارة المدرسة:"
            : "انتهت مدة الاشتراك، للتواصل مع إدارة المدرسة:") +
          (phone ? " " + phone : "");
      } else {
        msg =
          (e.message || "حسابك غير مفعّل") + (phone ? " — للإدارة: " + phone : "");
      }
      toast(msg, "error");
    }
    clearSession();
    location.href = "index.html";
    return null;
  }
}

async function logout() {
  try {
    await api("/auth/logout", { method: "POST" });
  } catch (e) {}
  clearSession();
  location.href = "index.html";
}

/* ---------- حالة انتظار أزرار النماذج ---------- */

function setBusy(formId, busy, label) {
  const form = document.getElementById(formId);
  if (!form) return;
  const btn = form.querySelector('button[type="submit"]');
  if (!btn) return;
  if (busy) {
    btn.dataset.prevHtml = btn.innerHTML;
    btn.disabled = true;
    btn.style.opacity = "0.75";
    btn.innerHTML = "";
    const span = document.createElement("span");
    span.textContent = label || "جارٍ الإرسال...";
    btn.appendChild(span);
  } else {
    if (btn.dataset.prevHtml !== undefined) {
      btn.innerHTML = btn.dataset.prevHtml;
      delete btn.dataset.prevHtml;
    }
    btn.disabled = false;
    btn.style.opacity = "";
  }
}

/* ---------- رسائل toast (بألوان التصميم) ----------
   كل رسالة تختفي تلقائيًا بعد 3 ثوانٍ، ويمكن إغلاقها يدويًا أيضًا.
   لكل رسالة مؤقّت خاص بها؛ عند الإغلاق أو الاستبدال يُلغى المؤقّت
   حتى لا تُخفي رسالة جديدة بسبب مؤقّت رسالة سابقة. */

const TOAST_LIFETIME_MS = 3000;

function toast(message, kind, action) {
  kind = kind || "error";
  let host = document.getElementById("appToastHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "appToastHost";
    host.dir = "rtl";
    host.style.cssText =
      "position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:100;" +
      "width:calc(100% - 24px);max-width:366px;display:flex;flex-direction:column;" +
      "gap:8px;pointer-events:none;font-family:Cairo,sans-serif;";
    document.body.appendChild(host);
  }

  const styles = {
    error: "background:#ffdad6;color:#93000a;", // error-container / on-error-container
    success: "background:#ffddb8;color:#2a1700;", // secondary-fixed / on-secondary-fixed
    info: "background:#efeeea;color:#1b1c1a;", // surface-container / on-surface
  };

  const el = document.createElement("div");
  el.dir = "rtl";
  el.style.cssText =
    "pointer-events:auto;border-radius:12px;padding:10px 14px;" +
    "box-shadow:0 6px 20px rgba(0,0,0,.18);font-size:13px;line-height:1.7;" +
    "display:flex;align-items:center;gap:8px;" +
    (styles[kind] || styles.error);

  const txt = document.createElement("span");
  txt.style.flex = "1";
  txt.textContent = message;
  el.appendChild(txt);

  if (action && action.label && action.href) {
    const a = document.createElement("a");
    a.textContent = action.label;
    a.href = action.href;
    a.style.cssText =
      "font-weight:700;color:inherit;text-decoration:underline;white-space:nowrap;";
    el.appendChild(a);
  }

  host.appendChild(el);

  // إغلاق يدوي (لكل الأنواع) + إلغاء المؤقّت
  let timer = null;
  const dismiss = function () {
    if (timer) { clearTimeout(timer); timer = null; }
    if (el.parentNode) el.remove();
  };
  el._dismiss = dismiss;
  el.style.cursor = "pointer";
  el.title = "اضغط للإغلاق";
  el.addEventListener("click", dismiss);

  // إخفاء تلقائي بعد 3 ثوانٍ
  timer = setTimeout(dismiss, TOAST_LIFETIME_MS);
}
