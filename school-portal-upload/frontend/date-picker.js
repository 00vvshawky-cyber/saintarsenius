/* date-picker.js
   منتقي تاريخ مخصص (سنة ← شهر ← يوم) — مكوّن مشترك بلا اعتماديات.
   مستخدم في: نموذج الانضمام (تاريخ الميلاد) + شاشة الاشتراكات (فترة الاشتراك).

   الاستخدام:
     DatePicker.open({
       value: 'YYYY-MM-DD' | null,   // القيمة الحالية
       title: 'اختيار التاريخ',       // عنوان الورقة
       minYear: 1940,                  // أصغر سنة في القائمة
       maxYear: <سنة حالية + 5>,       // أكبر سنة
       allowFuture: false,             // منع التواريخ المستقبلية
       onPick: function (iso) { ... }, // التاريخ المختار (YYYY-MM-DD)
       onClear: function () { ... }    // اختياري
     });

   أدوات:
     DatePicker.parse('DD/MM/YYYY' | 'YYYY-MM-DD')  → 'YYYY-MM-DD' | null
     DatePicker.format('YYYY-MM-DD')                → 'DD/MM/YYYY'
*/

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.DatePicker = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"];
  var DAY_INITIALS = ["س", "ح", "ن", "ث", "ر", "خ", "ج"];
  var DAY_NAMES = ["السبت", "الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة"];

  var state = {
    step: 0, year: null, month: null, day: null,
    minYear: 1940, maxYear: 0, allowFuture: false,
    onPick: null, onClear: null, title: "اختيار التاريخ",
  };
  var els = null;

  function pad(n) { return String(n).padStart(2, "0"); }

  /* ---------- أدوات التاريخ ---------- */

  function parse(text) {
    var raw = String(text == null ? "" : text).trim();
    if (!raw) return null;
    var m = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
    if (m) {
      var iso = m[1] + "-" + pad(Number(m[2])) + "-" + pad(Number(m[3]));
      return isValid(iso) ? iso : null;
    }
    var d = raw.match(/^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$/);
    if (!d) return null;
    var day = Number(d[1]), month = Number(d[2]), year = Number(d[3]);
    if (month < 1 || month > 12 || day < 1 || year < 1900) return null;
    var dt = new Date(year, month - 1, day);
    if (dt.getFullYear() !== year || dt.getMonth() !== month - 1 || dt.getDate() !== day) return null;
    return year + "-" + pad(month) + "-" + pad(day);
  }

  function isValid(iso) {
    var p = String(iso).split("-");
    if (p.length !== 3) return false;
    var year = Number(p[0]), month = Number(p[1]), day = Number(p[2]);
    if (month < 1 || month > 12 || day < 1) return false;
    var dt = new Date(Date.UTC(year, month - 1, day));
    return dt.getUTCFullYear() === year && dt.getUTCMonth() === month - 1 && dt.getUTCDate() === day;
  }

  function format(iso) {
    var p = String(iso).split("-");
    if (p.length !== 3) return "";
    return pad(Number(p[2])) + "/" + pad(Number(p[1])) + "/" + p[0];
  }

  /* ---------- بناء الواجهة ---------- */

  function build() {
    if (els) return;
    var overlay = document.createElement("div");
    overlay.className = "fixed inset-0 z-50 bg-black/50 backdrop-blur-xs flex items-end justify-center hidden";
    overlay.id = "datePickerOverlay";
    overlay.addEventListener("click", function () { close(); });

    var sheet = document.createElement("div");
    sheet.className = "bg-surface rounded-t-2xl w-full max-w-sm p-space-md shadow-xl";
    sheet.addEventListener("click", function (e) { e.stopPropagation(); });
    sheet.innerHTML =
      '<div class="flex items-center justify-between mb-space-md">' +
        '<button class="p-1.5 rounded-full text-text-secondary hover:text-text-primary focus:outline-none" id="datePickerBack" type="button" style="visibility:hidden">' +
          '<span class="material-symbols-outlined text-xl">arrow_forward</span>' +
        '</button>' +
        '<h3 class="font-title-lg text-title-lg text-text-primary" id="datePickerTitle">اختيار التاريخ</h3>' +
        '<button aria-label="إغلاق" class="p-1.5 rounded-full text-text-secondary hover:text-text-primary focus:outline-none" id="datePickerClose" type="button">' +
          '<span class="material-symbols-outlined text-xl">close</span>' +
        '</button>' +
      '</div>' +
      '<div class="flex items-center gap-1.5 mb-space-md" id="datePickerSteps"></div>' +
      '<div class="min-h-56 max-h-72 overflow-y-auto overscroll-contain" id="datePickerBody"></div>';

    overlay.appendChild(sheet);
    document.body.appendChild(overlay);

    els = {
      overlay: overlay,
      title: sheet.querySelector("#datePickerTitle"),
      steps: sheet.querySelector("#datePickerSteps"),
      body: sheet.querySelector("#datePickerBody"),
      back: sheet.querySelector("#datePickerBack"),
      close: sheet.querySelector("#datePickerClose"),
    };
    els.back.addEventListener("click", back);
    els.close.addEventListener("click", function () { close(); });
  }

  /* ---------- المنطق ---------- */

  function open(options) {
    var opts = options || {};
    build();
    var now = new Date();
    state.title = opts.title || "اختيار التاريخ";
    state.minYear = opts.minYear || 1940;
    state.maxYear = opts.maxYear || (now.getFullYear() + (opts.allowFuture ? 5 : 0));
    state.allowFuture = !!opts.allowFuture;
    state.onPick = opts.onPick || null;
    state.onClear = opts.onClear || null;

    var current = parse(opts.value);
    if (current) {
      var p = current.split("-");
      state = {
        step: 2, year: Number(p[0]), month: Number(p[1]), day: Number(p[2]),
        minYear: state.minYear, maxYear: state.maxYear, allowFuture: state.allowFuture,
        onPick: state.onPick, onClear: state.onClear, title: state.title,
      };
    } else {
      state.step = 0; state.year = null; state.month = null; state.day = null;
    }

    els.title.textContent = state.title;
    els.overlay.classList.remove("hidden");
    render();
  }

  function close() {
    if (els) els.overlay.classList.add("hidden");
  }
  function back() {
    if (state.step === 0) return;
    state.step -= 1;
    render();
  }

  function cellBase() { return "py-2.5 rounded-lg font-body-md text-body-md transition-all"; }

  function cellCls(active, disabled) {
    if (disabled) return "bg-surface-subtle text-semantic-disabled cursor-not-allowed";
    if (active) return "bg-primary-container text-on-primary shadow-sm";
    return "bg-surface-bright text-text-primary hover:bg-surface-subtle";
  }

  function render() {
    if (!els) return;
    var now = new Date();
    var currentYear = now.getFullYear();

    els.steps.innerHTML = ["السنة", "الشهر", "اليوم"].map(function (label, i) {
      var cls = i === state.step ? "bg-primary-container text-on-primary"
        : i < state.step ? "bg-secondary-fixed text-secondary"
        : "bg-surface-subtle text-text-secondary";
      return '<span class="flex-1 text-center py-1.5 rounded-lg font-label-sm text-label-sm ' + cls + '">' + label + "</span>";
    }).join("");
    els.back.style.visibility = state.step === 0 ? "hidden" : "visible";

    var body = els.body;
    var html = "";

    if (state.step === 0) {
      html = '<div class="grid grid-cols-3 gap-2">';
      for (var y = state.maxYear; y >= state.minYear; y--) {
        html += '<button type="button" class="' + cellBase() + " " + cellCls(y === state.year, false) +
          '" onclick="DatePicker.pickYear(' + y + ')">' + y + "</button>";
      }
      body.innerHTML = html + "</div>";
      return;
    }

    if (state.step === 1) {
      html = '<div class="grid grid-cols-3 gap-2">';
      MONTHS.forEach(function (name, i) {
        var m = i + 1;
        var disabled = !state.allowFuture && state.year === currentYear && m > now.getMonth() + 1;
        html += '<button type="button" ' + (disabled ? "disabled" : "") + ' class="' + cellBase() + " " +
          cellCls(m === state.month, disabled) + '" onclick="DatePicker.pickMonth(' + m + ')">' + name + "</button>";
      });
      body.innerHTML = html + "</div>";
      return;
    }

    var daysInMonth = new Date(state.year, state.month, 0).getDate();
    var firstDow = (new Date(state.year, state.month - 1, 1).getDay() + 1) % 7;
    var isCurrentMonth = state.year === currentYear && state.month === now.getMonth() + 1;
    html = '<div class="grid grid-cols-7 gap-1 mb-1">';
    DAY_INITIALS.forEach(function (d, i) {
      html += '<span title="' + DAY_NAMES[i] + '" class="text-center font-label-sm text-label-sm text-text-secondary py-1">' + d + "</span>";
    });
    html += '</div><div class="grid grid-cols-7 gap-1">';
    for (var i2 = 0; i2 < firstDow; i2++) html += "<span></span>";
    for (var day = 1; day <= daysInMonth; day++) {
      var disabled = !state.allowFuture && isCurrentMonth && day > now.getDate();
      html += '<button type="button" ' + (disabled ? "disabled" : "") + ' class="' + cellBase() + " " +
        cellCls(day === state.day, disabled) + '" onclick="DatePicker.pickDay(' + day + ')">' + day + "</button>";
    }
    body.innerHTML = html + "</div>";
  }

  function pickYear(y) {
    state.year = y; state.month = null; state.day = null;
    state.step = 1; render();
  }

  function pickMonth(m) {
    state.month = m; state.day = null;
    state.step = 2; render();
  }

  function pickDay(d) {
    var iso = state.year + "-" + pad(state.month) + "-" + pad(d);
    close();
    if (state.onPick) state.onPick(iso);
  }

  return {
    open: open,
    close: close,
    parse: parse,
    format: format,
    pickYear: pickYear,
    pickMonth: pickMonth,
    pickDay: pickDay,
  };
});
