/* photo-viewer.js
   عارض تكبير الصورة الشخصية — يعمل باللمس (الهاتف) وبالفأرة (الكمبيوتر).

   • فتح: بالضغط على الصورة (نقرة أو لمسة)
   • تكبير: قرص إصبعين · ضغط مزدوج · عجلة الفأرة
   • تحريك: سحب بإصبع واحد
   • إغلاق: زر × · الضغط خارج الصورة · مفتاح Esc · القرص لأسفل
*/
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.PhotoViewer = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var CSS = [
    ".pv-backdrop{position:fixed;inset:0;z-index:90;background:rgba(0,0,0,.92);display:none;align-items:center;justify-content:center;touch-action:none;overscroll-behavior:contain}",
    ".pv-backdrop.pv-open{display:flex}",
    ".pv-stage{position:relative;width:100%;height:100%;display:flex;align-items:center;justify-content:center;touch-action:none;overflow:hidden}",
    ".pv-img{max-width:100vw;max-height:100vh;object-fit:contain;transform-origin:center center;will-change:transform;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}",
    ".pv-close{position:absolute;top:calc(env(safe-area-inset-top,0px) + 12px);left:12px;width:44px;height:44px;border-radius:9999px;border:0;background:rgba(255,255,255,.16);color:#fff;font-size:24px;line-height:1;display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:2}",
    ".pv-close:hover{background:rgba(255,255,255,.26)}",
    ".pv-close:focus{outline:none}",
    ".pv-hint{position:absolute;bottom:calc(env(safe-area-inset-bottom,0px) + 18px);left:50%;transform:translateX(-50%);color:rgba(255,255,255,.72);font-size:12px;font-family:Cairo,sans-serif;white-space:nowrap;pointer-events:none}",
  ].join("");

  var MIN_SCALE = 1;
  var MAX_SCALE = 6;
  var DOUBLE_TAP_SCALE = 2.5;
  var DOUBLE_TAP_MS = 320;

  var dom = null;
  var scale = 1;
  var offsetX = 0;
  var offsetY = 0;
  var pointers = new Map();
  var pinchStartDist = 0;
  var pinchStartScale = 1;
  var lastTapTime = 0;
  var lastTapX = 0;
  var lastTapY = 0;
  var tapStart = null;

  function ensureDom() {
    if (dom) return dom;
    if (!document.getElementById("pvStyles")) {
      var style = document.createElement("style");
      style.id = "pvStyles";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    var host = document.createElement("div");
    host.id = "pvHost";
    host.innerHTML =
      '<div class="pv-backdrop" id="pvBackdrop" role="dialog" aria-modal="true" aria-label="عرض الصورة">' +
        '<div class="pv-stage" id="pvStage">' +
          '<img alt="صورة الطالب" class="pv-img" id="pvImg">' +
        '</div>' +
        '<button aria-label="إغلاق" class="pv-close" id="pvClose" type="button">' +
          '<span class="material-symbols-outlined">close</span>' +
        '</button>' +
        '<p class="pv-hint">اضغط مرتين للتكبير • اسحب للتحريك • قرص بإصبعين</p>' +
      '</div>';
    document.body.appendChild(host);

    dom = {
      backdrop: document.getElementById("pvBackdrop"),
      stage: document.getElementById("pvStage"),
      img: document.getElementById("pvImg"),
      close: document.getElementById("pvClose"),
    };

    dom.close.addEventListener("click", function (ev) {
      ev.stopPropagation();
      close();
    });
    dom.backdrop.addEventListener("pointerdown", onPointerDown);
    dom.backdrop.addEventListener("pointermove", onPointerMove);
    dom.backdrop.addEventListener("pointerup", onPointerUp);
    dom.backdrop.addEventListener("pointercancel", onPointerUp);
    dom.backdrop.addEventListener("wheel", onWheel, { passive: false });
    document.addEventListener("keydown", onKeyDown);
    return dom;
  }

  /* ─────────── العرض ─────────── */
  function applyTransform() {
    if (!dom) return;
    dom.img.style.transform =
      "translate(" + offsetX + "px," + offsetY + "px) scale(" + scale + ")";
  }

  function resetTransform() {
    scale = 1;
    offsetX = 0;
    offsetY = 0;
    applyTransform();
  }

  function open(src, alt) {
    if (!src) return;
    ensureDom();
    dom.img.src = src;
    if (alt) dom.img.alt = alt;
    resetTransform();
    dom.backdrop.classList.add("pv-open");
    document.body.style.overflow = "hidden";
  }

  function close() {
    if (!dom) return;
    dom.backdrop.classList.remove("pv-open");
    document.body.style.overflow = "";
    pointers.clear();
    resetTransform();
  }

  function isOpen() {
    return !!(dom && dom.backdrop.classList.contains("pv-open"));
  }

  /* ─────────── التكبير حول نقطة ─────────── */
  function zoomAt(nextScale, cx, cy) {
    var clamped = Math.min(MAX_SCALE, Math.max(MIN_SCALE, nextScale));
    if (clamped === scale) return;
    var rect = dom.stage.getBoundingClientRect();
    var ox = cx - rect.left - rect.width / 2;
    var oy = cy - rect.top - rect.height / 2;
    // نقطة التركيز تبقى ثابتة أثناء التكبير
    var ratio = clamped / scale;
    offsetX = ox - (ox - offsetX) * ratio;
    offsetY = oy - (oy - offsetY) * ratio;
    scale = clamped;
    applyTransform();
  }

  /* ─────────── أحداث المؤشر (لمس وفأرة) ─────────── */
  function onPointerDown(event) {
    if (dom.close.contains(event.target)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    tapStart = {
      x: event.clientX,
      y: event.clientY,
      moved: false,
      onImage: event.target === dom.img,
    };

    if (pointers.size === 2) {
      var pts = Array.from(pointers.values());
      pinchStartDist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      pinchStartScale = scale;
    } else if (pointers.size === 1) {
      // نقرة/لمسة مفردة: نقرة مزدوجة للتكبير
      var now = Date.now();
      var near = Math.abs(event.clientX - lastTapX) < 30 && Math.abs(event.clientY - lastTapY) < 30;
      if (now - lastTapTime < DOUBLE_TAP_MS && near) {
        lastTapTime = 0;
        if (scale > 1.05) {
          resetTransform();
        } else {
          zoomAt(DOUBLE_TAP_SCALE, event.clientX, event.clientY);
        }
        event.preventDefault();
        return;
      }
      lastTapTime = now;
      lastTapX = event.clientX;
      lastTapY = event.clientY;
    }
    event.preventDefault();
  }

  function onPointerMove(event) {
    if (!pointers.has(event.pointerId)) return;
    var prev = pointers.get(event.pointerId);
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (tapStart && !tapStart.moved &&
        Math.abs(event.clientX - tapStart.x) + Math.abs(event.clientY - tapStart.y) > 10) {
      tapStart.moved = true;
    }

    if (pointers.size === 2) {
      // قرص إصبعين: تكبير + تحريك
      var pts = Array.from(pointers.values());
      var dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      var mid = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
      var nextScale = pinchStartScale * (dist / pinchStartDist);
      var target = Math.min(MAX_SCALE, Math.max(MIN_SCALE, nextScale));
      var ratio = target / scale;
      var rect = dom.stage.getBoundingClientRect();
      var ox = mid.x - rect.left - rect.width / 2;
      var oy = mid.y - rect.top - rect.height / 2;
      offsetX = ox - (ox - offsetX) * ratio;
      offsetY = oy - (oy - offsetY) * ratio;
      scale = target;
      applyTransform();
      if (scale <= 1.02) {
        // القرص لأسفل ⇒ إغلاق
        close();
        return;
      }
    } else if (pointers.size === 1 && scale > 1) {
      offsetX += event.clientX - prev.x;
      offsetY += event.clientY - prev.y;
      applyTransform();
    }
    event.preventDefault();
  }

  function onPointerUp(event) {
    var wasTap = tapStart && !tapStart.moved && !tapStart.onImage && scale <= 1.05;
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinchStartDist = 0;
    if (pointers.size === 0) {
      tapStart = null;
      // لمسة/نقرة خارج الصورة ⇒ إغلاق
      if (wasTap) close();
    }
  }

  /* ─────────── عجلة الفأرة (كمبيوتر) ─────────── */
  function onWheel(event) {
    event.preventDefault();
    var factor = Math.exp(-event.deltaY * 0.0015);
    zoomAt(scale * factor, event.clientX, event.clientY);
    if (scale <= 1.02) resetTransform();
  }

  function onKeyDown(event) {
    if (event.key === "Escape" && isOpen()) close();
  }

  /* ─────────── الضغط خارج الصورة = إغلاق ─────────── */
  /* (يتم عبر onPointerUp: لمسة قصيرة على الخلفية خارج الصورة) */

  /* ─────────── الربط: الضغط على أي صورة يفتح العارض ─────────── */
  function bind(element, getSrc) {
    if (!element) return;
    element.style.cursor = "zoom-in";
    element.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var src = typeof getSrc === "function" ? getSrc() : (element.getAttribute("src") || "");
      if (src && src.indexOf("data:image/svg") !== 0) open(src, element.alt);
    });
  }

  function autoBind(ids) {
    ids.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) bind(el, function () { return el.getAttribute("src") || ""; });
    });
  }

  return {
    open: open,
    close: close,
    bind: bind,
    autoBind: autoBind,
    isOpen: isOpen,
  };
});
