/* photo-crop.js
   دوال نقية لمحرر الصورة الشخصية (قص مربع داخل دائرة).
   لا تعتمد على DOM ولا على المتصفح — قابلة للاختبار بـ node --test.

   ── نموذج الإحداثيات (موحّد) ──────────────────────────────────────────
   • الصورة المصدر: عرضها (natW) وارتفاعها (natH) بالبكسل، بعد تطبيق
     اتجاه EXIF.
   • مساحة القص (view): مربع طول ضلعه viewSize في وحدات الشاشة، ومركزه
     نقطة الأصل (0,0).
   • الحالة state = { rotation, scale, offset }:
       - rotation: زاوية التدوير بالدرجات (أي قيمة، مضاعفات 90 عمليًا).
       - scale: وحدات الشاشة لكل بكسل صورة.
       - offset: {x, y} إزاحة *مركز الصورة* عن مركز القص، بوحدات الشاشة.
   • نقطة داخل الصورة p_src تُحوّل إلى الشاشة بـ:
       p_screen = R(rotation) · (p_src · scale) + offset
     حيث R تدوير في مستوى الشاشة (اتجاه عقارب الساعة مع محور y لأسفل).

   ── القاعدة الذهبية ───────────────────────────────────────────────────
   مستطيل القص (المربع) يجب أن يبقى مغطّى بالصورة بالكامل دائمًا.
   لذلك نُبقي تدوير الصورة *حول مركزها*، ويبقى الضمان: بعد التدوير
   الأبعاد الظاهرة (rotatedSize · scale) ≥ view على المحورين، ثم نحصر
   الإزاحة في الحد الأقصى المسموح.
*/

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.PhotoCrop = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MAX_ZOOM_FACTOR = 5; // الحد الأقصى = 5× حجم التغطية

  function toRadians(deg) {
    return (deg * Math.PI) / 180;
  }

  function normDeg(deg) {
    return ((deg % 360) + 360) % 360;
  }

  /* الأبعاد الظاهرة لصورة (natW×natH) بعد تدوير بمقدار rotation. */
  function rotatedSize(natW, natH, rotation) {
    var r = normDeg(rotation);
    if (r === 90 || r === 270) return { w: natH, h: natW };
    return { w: natW, h: natH };
  }

  /* أصغر مقياس يجعل الصورة (بعد التدوير) تغطي *مربّع الحفظ* بالكامل.
     المقاس = وحدات الشاشة لكل بكسل صورة. نستخدم مربّع الحفظ (= مربّع
     الدائرة المحيط) لا المساحة كلها، لأن المحفوظ هو هذا المربّع فقط. */
  function minScale(natW, natH, rotation, viewSize) {
    var d = rotatedSize(natW, natH, rotation);
    var side = cropSize(viewSize);
    return Math.max(side / d.w, side / d.h);
  }

  function maxScale(natW, natH, rotation, viewSize) {
    return minScale(natW, natH, rotation, viewSize) * MAX_ZOOM_FACTOR;
  }

  /* حالة ابتدائية: الصورة تغطي القص، متمركزة، بلا تدوير. */
  function initialTransform(natW, natH, viewSize) {
    return {
      rotation: 0,
      scale: minScale(natW, natH, 0, viewSize),
      offset: { x: 0, y: 0 },
    };
  }

  /* الأبعاد الظاهرة على الشاشة (وحدات الشاشة) لحالة معيّنة. */
  function displayedSize(state, natW, natH) {
    var d = rotatedSize(natW, natH, state.rotation);
    return { w: d.w * state.scale, h: d.h * state.scale };
  }

  /* طول ضلع *مربّع الحفظ* بوحدات الشاشة، انطلاقًا من نصف قطر الدائرة.
     الدائرة نصف قطرها CIRCLE_R_FRAC × view، والمربّع الذي يُحفظ هو
     المربّع المحيط بها (الذي تلمس الدائرة أضلاعه الأربعة)، حتى يطابق
     المحفوظ تمامًا ما يراه المستخدم داخل الدائرة. */
  var CIRCLE_R_FRAC = 0.4;
  function cropSize(viewSize) {
    return 2 * CIRCLE_R_FRAC * viewSize; // = 0.8 × view
  }

  /* أقصى إزاحة مسموحة (قيمة موجبة) على كل محور بحيث تبقى
     الصورة مغطية لمربع القص. */
  function maxOffset(displayW, displayH, viewSize) {
    return {
      x: Math.max(0, (displayW - viewSize) / 2),
      y: Math.max(0, (displayH - viewSize) / 2),
    };
  }

  /* حصر الإزاحة داخل الحدود (منع الفراغات عند الحواف) بالنسبة لمربّع
     الحفظ. نمرّر هنا *مربّع الحفظ* لا المساحة كلها. */
  function clampOffset(offset, displayW, displayH, viewSize) {
    var lim = maxOffset(displayW, displayH, viewSize);
    var x = Number.isFinite(offset.x) ? offset.x : 0;
    var y = Number.isFinite(offset.y) ? offset.y : 0;
    return {
      x: Math.min(lim.x, Math.max(-lim.x, x)),
      y: Math.min(lim.y, Math.max(-lim.y, y)),
    };
  }

  /* تحويل نقطة من إحداثيات القص (نسبةً لمركز القص) إلى موضعها داخل
     الصورة المصدر (بالبكسل، نسبةً لمركز الصورة)، مع عكس التدوير. */
  function screenToImage(pt, state) {
    var rad = toRadians(state.rotation);
    var cos = Math.cos(rad);
    var sin = Math.sin(rad);
    var dx = pt.x - state.offset.x;
    var dy = pt.y - state.offset.y;
    // عكس R ثم عكس المقياس
    var ix = (dx * cos + dy * sin) / state.scale;
    var iy = (-dx * sin + dy * cos) / state.scale;
    return { x: ix, y: iy };
  }

  /* تحويل نقطة من إحداثيات الصورة (نسبةً للمركز، بالبكسل) إلى الشاشة. */
  function imageToScreen(pt, state) {
    var rad = toRadians(state.rotation);
    var cos = Math.cos(rad);
    var sin = Math.sin(rad);
    var sx = pt.x * state.scale;
    var sy = pt.y * state.scale;
    return {
      x: sx * cos - sy * sin + state.offset.x,
      y: sx * sin + sy * cos + state.offset.y,
    };
  }

  /* تكبير حول نقطة تركيز: النقطة تحت الإصبع/المؤشر تبقى ثابتة على
     الشاشة، ثم نحصر الإزاحة لمنع الفراغات (لا قفزات: الإزاحة تتبع
     النقطة طبيعيًا، والحصر يحدث فقط عند الحافة الحقيقية).
     focus إحداثيات نسبةً إلى مركز القص (وحدات الشاشة). */
  function zoomAtPoint(state, factor, focus, natW, natH, viewSize) {
    var lo = minScale(natW, natH, state.rotation, viewSize);
    var hi = maxScale(natW, natH, state.rotation, viewSize);
    var nextScale = Math.min(hi, Math.max(lo, state.scale * factor));
    var applied = nextScale / state.scale; // معامل فعلي بعد الحصر

    // الموضع الجديد = focus - (focus - offset) * applied
    var nextOffset = {
      x: focus.x - (focus.x - state.offset.x) * applied,
      y: focus.y - (focus.y - state.offset.y) * applied,
    };
    var d = rotatedSize(natW, natH, state.rotation);
    var side = cropSize(viewSize);
    return {
      rotation: state.rotation,
      scale: nextScale,
      offset: clampOffset(
        nextOffset,
        d.w * nextScale,
        d.h * nextScale,
        side
      ),
    };
  }

  /* تكبير حول *مركز القص* — المستخدم بزرّي +/− وشريط التمرير.
     تثبيت المركز يمنع أي إزاحة مفاجئة أثناء التكبير. */
  function zoomCentered(state, factor, natW, natH, viewSize) {
    return zoomAtPoint(state, factor, { x: 0, y: 0 }, natW, natH, viewSize);
  }

  /* تدوير حول مركز العرض (مركز القص) مع إبقاء القص مغطّى:
     1) نُدوّر إزاحة *مركز الصورة* بنفس الزاوية (تبقى البقعة نفسها
        قرب المركز، وهو ما يتوقعه المستخدم).
     2) نرفع المقياس للحد الأدنى الجديد إن لزم (بعد تبديل الأبعاد).
     3) نحصر الإزاحة على الأبعاد الجديدة. */
  function rotate(state, deltaDeg, natW, natH, viewSize) {
    var nextRotation = normDeg(state.rotation + deltaDeg);
    var rad = toRadians(deltaDeg);
    var cos = Math.cos(rad);
    var sin = Math.sin(rad);
    // تدوير متجه الإزاحة (اتجاه عقارب الساعة في إحداثيات الشاشة)
    var rx = state.offset.x * cos - state.offset.y * sin;
    var ry = state.offset.x * sin + state.offset.y * cos;
    var lo = minScale(natW, natH, nextRotation, viewSize);
    var nextScale = Math.max(lo, state.scale);
    var d = rotatedSize(natW, natH, nextRotation);
    var side = cropSize(viewSize);
    return {
      rotation: nextRotation,
      scale: nextScale,
      offset: clampOffset(
        { x: rx, y: ry },
        d.w * nextScale,
        d.h * nextScale,
        side
      ),
    };
  }

  /* إعادة التمركز الكامل (زر إعادة الضبط): نفس التدوير والمقياس
     مع تصفير الإزاحة. */
  function recenter(state, natW, natH, viewSize) {
    return {
      rotation: state.rotation,
      scale: Math.max(
        state.scale,
        minScale(natW, natH, state.rotation, viewSize)
      ),
      offset: { x: 0, y: 0 },
    };
  }

  /* مستطيل القص في إحداثيات الصورة المصدر (لأخذ 512×512).
     يعيد {sx, sy, size, rotation} بالبكسل الأصلي، أو null إن تعذّر.
     الضلع = cropSize/scale، أي مربّع الدائرة المحيط تمامًا (ما يراه
     المستخدم داخل الدائرة فقط). */
  function cropRect(state, natW, natH, viewSize) {
    var scale = state.scale;
    if (!Number.isFinite(scale) || scale <= 0) return null;
    var side = cropSize(viewSize) / scale; // طول ضلع القص بالبكسل الأصلي

    // مركز القص في إحداثيات الصورة (نسبةً لمركز الصورة، بالبكسل)
    var c = screenToImage({ x: 0, y: 0 }, state);

    var usable = Math.min(side, Math.min(natW, natH));
    var sx = natW / 2 + c.x - usable / 2;
    var sy = natH / 2 + c.y - usable / 2;

    // حصر داخل حدود الصورة (حماية رياضية؛ التغطية تمنعه عادةً)
    sx = Math.max(0, Math.min(natW - usable, sx));
    sy = Math.max(0, Math.min(natH - usable, sy));

    return {
      sx: sx,
      sy: sy,
      size: usable,
      rotation: normDeg(state.rotation),
    };
  }

  return {
    MAX_ZOOM_FACTOR: MAX_ZOOM_FACTOR,
    CIRCLE_R_FRAC: CIRCLE_R_FRAC,
    cropSize: cropSize,
    normDeg: normDeg,
    rotatedSize: rotatedSize,
    minScale: minScale,
    maxScale: maxScale,
    initialTransform: initialTransform,
    displayedSize: displayedSize,
    maxOffset: maxOffset,
    clampOffset: clampOffset,
    screenToImage: screenToImage,
    imageToScreen: imageToScreen,
    zoomAtPoint: zoomAtPoint,
    zoomCentered: zoomCentered,
    rotate: rotate,
    recenter: recenter,
    cropRect: cropRect,
  };
});
