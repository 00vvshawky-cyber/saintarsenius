/* coptic-date.js
   تحويل التاريخ الميلادي إلى التقويم القبطي — دوال نقية بلا DOM.
   قابلة للاختبار بـ node --test.

   السنة القبطية: 12 شهرًا × 30 يومًا + شهر أخير قصير (5 أيام، 6 في
   السنة الكبيسة) = 365 / 366 يومًا. وتبدأ في 11 سبتمبر من كل سنة
   ميلادية (12 سبتمبر في السنة السابقة لسنوة كبيسة ميلادية).

   • من 11 سبتمبر حتى 31 ديسمبر:  قبطي = ميلادي − 283
   • من 1 يناير حتى 10 سبتمبر:   قبطي = ميلادي − 284
*/

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.CopticDate = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* أسماء الشهور القبطية بالتقاليد المصرية */
  var MONTHS = [
    "توت",    // 1
    "بابه",    // 2
    "هاتور",  // 3
    "كيهيك",  // 4
    "طوبة",   // 5
    "مسرى",   // 6
    "برمودة", // 7
    "بشنس",   // 8
    "بؤونة",  // 9
    "أبيب",   // 10
    "مسي",    // 11
    "برمهة",  // 12
    "أولي",   // 13 (شهر قصير: 5 أيام، 6 في السنة الكبيسة)
  ];

  /* أرقام لاتينية (0-9) */
  function toDigits(value) {
    return String(value == null ? '' : value);
  }

  /* عدد الأيام بين تاريخين (بالتقويم المحلي) */
  function daysBetween(from, to) {
    var uf = Date.UTC(from.getFullYear(), from.getMonth(), from.getDate());
    var ut = Date.UTC(to.getFullYear(), to.getMonth(), to.getDate());
    return Math.round((ut - uf) / 86400000);
  }

  /* هل السنة الميلادية كبيسة (فبراير 29)؟ */
  function isGregorianLeap(year) {
    return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  }

  /* 1 توت من السنة القبطية cy: 11 سبتمبر (12 إذا كانت السنة التالية كبيسة) */
  function copticNewYear(cy) {
    var gregorianYear = cy + 283;
    var day = isGregorianLeap(gregorianYear + 1) ? 12 : 11;
    return new Date(gregorianYear, 8, day);
  }

  /* السنة القبطية لتاريخ ميلادي */
  function copticYear(date) {
    var y = date.getFullYear();
    var beforeNewYear =
      date.getMonth() < 8 || (date.getMonth() === 8 && date.getDate() < 11);
    return beforeNewYear ? y - 284 : y - 283;
  }

  /* السنة القبطية الكبيسة: كل 4 سنوات (النسبة 3 من 4) */
  function isCopticLeap(cy) {
    return (cy + 1) % 4 === 0;
  }

  /* أطوال شهور السنة القبطية */
  function monthLengths(cy) {
    var lengths = MONTHS.map(function () { return 30; });
    lengths[MONTHS.length - 1] = isCopticLeap(cy) ? 6 : 5;
    return lengths;
  }

  /* تحويل تاريخ ميلادي ⇒ { year, month, day, monthName, text } */
  function copticDate(input) {
    var date = input instanceof Date ? input : new Date(input);
    if (isNaN(date.getTime())) return null;

    var cy = copticYear(date);
    var start = copticNewYear(cy);
    var dayOfYear = daysBetween(start, date) + 1; // 1 … 365/366
    var lengths = monthLengths(cy);

    var month = 0;
    while (month < lengths.length && dayOfYear > lengths[month]) {
      dayOfYear -= lengths[month];
      month++;
    }
    if (month >= MONTHS.length) {
      month = MONTHS.length - 1;
      dayOfYear = lengths[month];
    }

    return {
      year: cy,
      month: month + 1,
      day: dayOfYear,
      monthName: MONTHS[month],
      // مثال: «14 بابه 1742 ش»
      text: toDigits(dayOfYear) + " " + MONTHS[month] + " " + toDigits(cy) + " ش",
    };
  }

  /* أسماء الشهور القبطية */
  function monthNames() {
    return MONTHS.slice();
  }

  return {
    copticDate: copticDate,
    copticYear: copticYear,
    isCopticLeap: isCopticLeap,
    copticNewYear: copticNewYear,
    toDigits: toDigits,
    monthNames: monthNames,
  };
});
