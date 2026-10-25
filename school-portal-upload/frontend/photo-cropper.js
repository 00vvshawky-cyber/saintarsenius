/* photo-cropper.js
   محرّر الصور الشخصية — مكوّن مشترك بين شاشة الانضمام وصفحة «بياناتي».

   يستخدم نفس الدوال النقية في photo-crop.js، ونفس واجهة المستخدم بالضبط
   (دائرة قص، سحب بإصبع واحد، تكبير بإصبعين، حفظ 512×512 WebP).

   الاستخدام:
     PhotoCropper.open({ file, onSaved(dataUrl), onCancel() })  // فتح المحرّر بملف
     PhotoCropper.pick({ onSaved, onCancel })                   // اختيار ملف ثم فتح المحرّر
*/

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./photo-crop.js"));
  } else {
    root.PhotoCropper = factory(root.PhotoCrop);
  }
})(typeof self !== "undefined" ? self : this, function (PhotoCrop) {
  "use strict";

  /* ─────────── الأنماط (نفس القيم المحسوبة لشاشة الانضمام) ─────────── */
  var CSS = [
    ".pc-backdrop{position:fixed;inset:0;z-index:50;background:rgba(0,0,0,.6);display:none;flex-direction:column}",
    ".pc-backdrop.pc-open{display:flex}",
    ".pc-panel{width:100%;max-width:384px;margin:auto;background:#fff;border-radius:12px;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 20px 25px -5px rgba(0,0,0,.1),0 8px 10px -6px rgba(0,0,0,.1)}",
    ".pc-head{display:flex;align-items:center;justify-content:space-between;padding:8px;background:#f5f3ef}",
    ".pc-title{font-family:Cairo,sans-serif;font-size:16px;font-weight:600;line-height:24px;color:#2B1E1A;margin:0}",
    ".pc-close{width:44px;height:44px;border:0;background:transparent;border-radius:9999px;color:#6E6058;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0}",
    ".pc-close:hover{color:#2B1E1A}",
    ".pc-close:focus{outline:none}",
    ".pc-close .material-symbols-outlined{font-size:24px;line-height:24px}",
    ".pc-stage{position:relative;width:100%;background:#000;touch-action:none;user-select:none;-webkit-user-select:none;overflow:hidden;aspect-ratio:1/1}",
    ".pc-img{position:absolute;pointer-events:none;user-select:none;-webkit-user-select:none}",
    ".pc-mask{position:absolute;inset:0;pointer-events:none}",
    ".pc-svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}",
    ".pc-body{padding:8px}",
    ".pc-hint{font-family:Cairo,sans-serif;font-size:11px;font-weight:400;line-height:14px;color:#6E6058;text-align:center;margin:0 0 8px}",
    ".pc-actions{display:flex;align-items:center;gap:8px}",
    ".pc-btn{flex:1;height:44px;border:0;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;font-family:Cairo,sans-serif;font-size:16px;font-weight:600;line-height:24px}",
    ".pc-btn:focus{outline:none}",
    ".pc-btn .material-symbols-outlined{font-size:18px;line-height:18px}",
    ".pc-btn--ghost{background:#F4EFEA;color:#2B1E1A}",
    ".pc-btn--primary{background:#491402;color:#fff}",
  ].join("");

  /* ─────────── بنية الواجهة (مطابقة لشاشة الانضمام) ─────────── */
  var HTML = [
    '<div class="pc-backdrop" id="pcBackdrop" role="dialog" aria-modal="true" aria-label="تعديل الصورة الشخصية">',
    '  <div class="pc-panel">',
    '    <div class="pc-head">',
    '      <h3 class="pc-title">تعديل الصورة</h3>',
    '      <button aria-label="إلغاء" class="pc-close" id="pcClose" type="button">',
    '        <span class="material-symbols-outlined">close</span>',
    '      </button>',
    '    </div>',
    '    <div class="pc-stage" id="pcStage">',
    '      <img alt="معاينة القص" class="pc-img" draggable="false" id="pcImage">',
    '      <div class="pc-mask" id="pcMask"></div>',
    '      <svg class="pc-svg" preserveAspectRatio="none" viewBox="0 0 100 100">',
    '        <circle cx="50" cy="50" fill="none" r="40" stroke="#fbf9f5" stroke-width="1" vector-effect="non-scaling-stroke"></circle>',
    '      </svg>',
    '    </div>',
    '    <div class="pc-body">',
    '      <p class="pc-hint">اسحب الصورة بإصبعك أو بالماوس • قرّب إصبعين للتكبير</p>',
    '      <div class="pc-actions">',
    '        <button class="pc-btn pc-btn--ghost" id="pcChange" type="button">',
    '          <span class="material-symbols-outlined">add_photo_alternate</span>',
    '          <span>تغيير الصورة</span>',
    '        </button>',
    '        <button class="pc-btn pc-btn--primary" id="pcSave" type="button">',
    '          <span class="material-symbols-outlined">check</span>',
    '          <span>حفظ</span>',
    '        </button>',
    '      </div>',
    '    </div>',
    '  </div>',
    '</div>',
  ].join("\n");

  /* ─────────── الحالة ─────────── */
  var st = {
    bitmap: null,
    natW: 0,
    natH: 0,
    view: 0,
    state: null,
    pointers: new Map(),
    startDist: 0,
    dragStart: null,
    zoomFocus: null,
    zoomBase: null,
  };

  var els = null;
  var onSaved = null;
  var onCancel = null;
  var fileInput = null;

  /* ─────────── بناء عناصر DOM مرة واحدة ─────────── */
  function ensureDom() {
    if (els) return els;
    if (!document.getElementById("pcStyles")) {
      var style = document.createElement("style");
      style.id = "pcStyles";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    var host = document.createElement("div");
    host.id = "pcHost";
    host.innerHTML = HTML;
    document.body.appendChild(host);

    els = {
      backdrop: document.getElementById("pcBackdrop"),
      stage: document.getElementById("pcStage"),
      image: document.getElementById("pcImage"),
      mask: document.getElementById("pcMask"),
      close: document.getElementById("pcClose"),
      save: document.getElementById("pcSave"),
      change: document.getElementById("pcChange"),
    };

    els.close.addEventListener("click", function () { cancel(); });
    els.save.addEventListener("click", function () { save(); });
    els.change.addEventListener("click", function () { pickAgain(); });
    els.stage.addEventListener("pointerdown", pointerDown);
    els.stage.addEventListener("pointermove", pointerMove);
    els.stage.addEventListener("pointerup", pointerUp);
    els.stage.addEventListener("pointercancel", pointerUp);
    els.stage.addEventListener("wheel", wheel, { passive: false });

    fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = "image/*";
    fileInput.className = "hidden";
    fileInput.addEventListener("change", function () {
      var f = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (f) loadFile(f);
    });
    document.body.appendChild(fileInput);

    return els;
  }

  /* ─────────── قراءة الملف كصورة نقطية مع احترام EXIF ─────────── */
  function decodeImage(file) {
    if (window.createImageBitmap) {
      return createImageBitmap(file, { imageOrientation: "from-image" }).catch(function () {
        return createImageBitmap(file).catch(function () { return decodeViaImg(file); });
      });
    }
    return decodeViaImg(file);
  }

  function decodeViaImg(file) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      var url = URL.createObjectURL(file);
      img.onload = function () { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("تعذر قراءة الصورة")); };
      img.src = url;
    });
  }

  /* ─────────── الفتح والإغلاق ─────────── */
  function open(options) {
    options = options || {};
    ensureDom();
    onSaved = options.onSaved || null;
    onCancel = options.onCancel || null;
    if (options.file) {
      return loadFile(options.file);
    }
    fileInput.click();
    return Promise.resolve(null);
  }

  function pick(options) {
    return open(options);
  }

  function loadFile(file) {
    if (!file.type || !file.type.startsWith("image/")) {
      return Promise.reject(new Error("يرجى اختيار ملف صورة صالح"));
    }
    return decodeImage(file).then(function (bmp) {
      st.bitmap = bmp;
      st.natW = bmp.width;
      st.natH = bmp.height;
      showModal();
      return null;
    });
  }

  function pickAgain() {
    fileInput.click();
  }

  function showModal() {
    ensureDom();
    var canvas = document.createElement("canvas");
    canvas.width = st.natW;
    canvas.height = st.natH;
    canvas.getContext("2d").drawImage(st.bitmap, 0, 0);
    els.image.src = canvas.toDataURL("image/png");
    els.backdrop.classList.add("pc-open");
    els.view = 0;
    st.view = els.stage.clientWidth || 300;
    st.state = PhotoCrop.initialTransform(st.natW, st.natH, st.view);
    st.pointers.clear();
    render();
    document.addEventListener("keydown", keyDown);
    window.addEventListener("resize", onResize);
  }

  function hideModal() {
    if (!els) return;
    els.backdrop.classList.remove("pc-open");
    st.pointers.clear();
    document.removeEventListener("keydown", keyDown);
    window.removeEventListener("resize", onResize);
  }

  function cancel() {
    hideModal();
    if (onCancel) onCancel();
  }

  function save() {
    var dataUrl = renderOutput();
    hideModal();
    if (onSaved) onSaved(dataUrl);
  }

  /* ─────────── العرض ─────────── */
  function render() {
    var s = st.state;
    var view = st.view;
    var img = els.image;
    // العنصر يحتفظ دائمًا بأبعاد الصورة *الأصلية*، والتدوير والمقياس
    // يُطبَّقان معًا عبر transform واحد حول مركز العنصر.
    var baseW = st.natW;
    var baseH = st.natH;
    img.style.width = baseW + "px";
    img.style.height = baseH + "px";
    img.style.maxWidth = "none";
    img.style.maxHeight = "none";
    img.style.left = view / 2 + s.offset.x - baseW / 2 + "px";
    img.style.top = view / 2 + s.offset.y - baseH / 2 + "px";
    img.style.transformOrigin = "center center";
    img.style.transform = "rotate(" + s.rotation + "deg) scale(" + s.scale + ")";
    // قناع التعتيم: نُعتّم كل شيء إلا الدائرة
    var r = view * 0.4;
    els.mask.style.background =
      "radial-gradient(circle at 50% 50%, rgba(0,0,0,0) " + (r - 1) + "px, rgba(0,0,0,0.55) " + r + "px)";
  }

  function onResize() {
    if (!st.state) return;
    var prevView = st.view;
    var nextView = els.stage.clientWidth || prevView;
    if (nextView && prevView) {
      var ratio = nextView / prevView;
      st.state.scale *= ratio;
      st.state.offset.x *= ratio;
      st.state.offset.y *= ratio;
    }
    st.view = nextView;
    render();
  }

  /* ─────────── السحب والتكبير ─────────── */
  function localPoint(event) {
    var rect = els.stage.getBoundingClientRect();
    return {
      x: event.clientX - rect.left - rect.width / 2,
      y: event.clientY - rect.top - rect.height / 2,
    };
  }

  function pointerDown(event) {
    st.pointers.set(event.pointerId, localPoint(event));
    try {
      if (els.stage.setPointerCapture) els.stage.setPointerCapture(event.pointerId);
    } catch (e) { /* تجاهل */ }
    if (st.pointers.size === 1) {
      st.dragStart = { point: localPoint(event), offset: { x: st.state.offset.x, y: st.state.offset.y } };
    } else if (st.pointers.size === 2) {
      var pts = Array.from(st.pointers.values());
      st.startDist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      st.zoomFocus = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      st.zoomBase = { rotation: st.state.rotation, scale: st.state.scale, offset: { x: st.state.offset.x, y: st.state.offset.y } };
    }
    event.preventDefault();
  }

  function pointerMove(event) {
    if (!st.pointers.has(event.pointerId)) return;
    var next = localPoint(event);
    st.pointers.set(event.pointerId, next);

    if (st.pointers.size === 1 && st.dragStart) {
      var dx = next.x - st.dragStart.point.x;
      var dy = next.y - st.dragStart.point.y;
      var d = PhotoCrop.displayedSize(st.state, st.natW, st.natH);
      st.state.offset = PhotoCrop.clampOffset(
        { x: st.dragStart.offset.x + dx, y: st.dragStart.offset.y + dy },
        d.w, d.h, PhotoCrop.cropSize(st.view)
      );
      render();
    } else if (st.pointers.size === 2) {
      var pts = Array.from(st.pointers.values());
      var dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      var factor = dist / st.startDist;
      var mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      st.state = PhotoCrop.zoomAtPoint(
        st.zoomBase, factor, st.zoomFocus, st.natW, st.natH, st.view
      );
      var panX = mid.x - st.zoomFocus.x;
      var panY = mid.y - st.zoomFocus.y;
      var dd = PhotoCrop.displayedSize(st.state, st.natW, st.natH);
      st.state.offset = PhotoCrop.clampOffset(
        { x: st.state.offset.x + panX, y: st.state.offset.y + panY },
        dd.w, dd.h, PhotoCrop.cropSize(st.view)
      );
      render();
    }
    event.preventDefault();
  }

  function pointerUp(event) {
    st.pointers.delete(event.pointerId);
    if (st.pointers.size === 1) {
      var remaining = Array.from(st.pointers.values())[0];
      st.dragStart = { point: remaining, offset: { x: st.state.offset.x, y: st.state.offset.y } };
    } else if (st.pointers.size === 0) {
      st.dragStart = null;
    }
  }

  function wheel(event) {
    event.preventDefault();
    if (!st.state) return;
    var factor = Math.exp(-event.deltaY * 0.0015);
    st.state = PhotoCrop.zoomAtPoint(
      st.state, factor, localPoint(event), st.natW, st.natH, st.view
    );
    render();
  }

  function keyDown(event) {
    if (!els || !els.backdrop.classList.contains("pc-open")) return;
    if (event.key === "Escape") {
      cancel();
    }
  }

  /* ─────────── الإخراج: قص 512×512 بصيغة WebP مع JPEG بديل ─────────── */
  function outputMime() {
    var c = document.createElement("canvas");
    c.width = 1;
    c.height = 1;
    return c.toDataURL("image/webp").indexOf("data:image/webp") === 0 ? "image/webp" : "image/jpeg";
  }

  function renderOutput() {
    var size = 512;
    var view = st.view;
    var s = st.state;
    var canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";

    // q_out = (size/cropSize)·( R(rot)·(p_img·scale) + offset )
    var cropSide = PhotoCrop.cropSize(view);
    var k = (size * s.scale) / cropSide;
    var bx = (size / cropSide) * s.offset.x;
    var by = (size / cropSide) * s.offset.y;

    ctx.save();
    ctx.translate(size / 2 + bx, size / 2 + by);
    ctx.scale(k, k);
    ctx.rotate((PhotoCrop.normDeg(s.rotation) * Math.PI) / 180);
    ctx.translate(-st.natW / 2, -st.natH / 2);
    ctx.drawImage(st.bitmap, 0, 0);
    ctx.restore();
    return canvas.toDataURL(outputMime(), 0.9);
  }

  return {
    open: open,
    pick: pick,
    outputMime: outputMime,
    CSS: CSS,
  };
});
