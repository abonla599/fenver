/* 官网交互：主题、目录、滚动渐显、导航高亮、阅读进度、复制链接、回到顶部。
   零依赖、零外部请求；语法保守写（var / 函数），Android WebView 也能跑。 */
(function () {
  "use strict";

  var root = document.documentElement;
  root.classList.add("js");

  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var raf = window.requestAnimationFrame || function (fn) { return setTimeout(fn, 16); };
  var cancel = window.cancelAnimationFrame || clearTimeout;

  function storeTheme(v) {
    try { localStorage.setItem("site-theme", v); } catch (e) { /* 隐私模式：不记就不记 */ }
  }

  /* ---------- 主题 ---------- */
  var themeBtn = document.getElementById("themeBtn");
  function syncThemeLabel() {
    if (!themeBtn) return;
    var dark = root.getAttribute("data-theme") === "dark";
    themeBtn.setAttribute("aria-label", dark ? "切换到浅色" : "切换到深色");
  }
  if (themeBtn) {
    syncThemeLabel();
    themeBtn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute("content", next === "dark" ? "#0a0b0e" : "#f3f0e7");
      storeTheme(next);
      syncThemeLabel();
    });
  }

  /* ---------- 目录 ---------- */
  var menuBtn = document.getElementById("menuBtn");
  var nav = document.getElementById("navlinks");
  function setMenu(open) {
    if (!menuBtn || !nav) return;
    nav.classList.toggle("is-open", open);
    menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (menuBtn && nav) {
    menuBtn.addEventListener("click", function () {
      setMenu(!nav.classList.contains("is-open"));
    });
    nav.addEventListener("click", function (ev) {
      if (ev.target && ev.target.closest("a")) setMenu(false);
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") setMenu(false);
    });
    window.addEventListener("resize", function () {
      if (window.innerWidth > 860) setMenu(false);
    }, { passive: true });
  }

  /* ---------- 滚动渐显 ---------- */
  var reveals = [].slice.call(document.querySelectorAll(".reveal"));
  function showAll() { reveals.forEach(function (el) { el.classList.add("in"); }); }
  if (reveals.length) {
    if (!("IntersectionObserver" in window) || reduceMotion) {
      showAll();
    } else {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
        });
      }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
      reveals.forEach(function (el) { io.observe(el); });
      setTimeout(showAll, 1500);
      window.addEventListener("pageshow", showAll);
    }
  }

  /* ---------- 导航高亮 ---------- */
  var links = [].slice.call(document.querySelectorAll(".navlinks a"));
  if (links.length && "IntersectionObserver" in window) {
    var byId = {};
    links.forEach(function (a) {
      var href = a.getAttribute("href") || "";
      if (href.charAt(0) !== "#") return;
      var id = href.slice(1);
      var sec = document.getElementById(id);
      if (sec) byId[id] = a;
    });
    var navIo = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        links.forEach(function (a) { a.removeAttribute("aria-current"); });
        var a = byId[en.target.id];
        if (a) a.setAttribute("aria-current", "true");
      });
    }, { rootMargin: "-45% 0px -50% 0px" });
    Object.keys(byId).forEach(function (id) { navIo.observe(document.getElementById(id)); });
  }

  /* ---------- 复制链接 ---------- */
  var copyBtn = document.getElementById("copyBtn");
  var hint = document.getElementById("copyHint");
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      var url = location.href;
      function done(ok) {
        if (!hint) return;
        hint.textContent = ok ? "已复制，发给朋友就能打开。" : "复制没成，长按地址栏手动复制。";
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(function () { done(true); }, function () { done(false); });
      } else {
        done(false);
      }
    });
  }

  /* ---------- 吸顶、进度、回到顶部 ---------- */
  var topbar = document.querySelector(".topbar");
  var topBtn = document.getElementById("topBtn");
  var progressBar = document.getElementById("progressBar");
  if (topbar || topBtn || progressBar) {
    var frame = 0;
    var paint = function () {
      frame = 0;
      var box = document.documentElement;
      var y = window.pageYOffset || box.scrollTop || 0;
      if (topbar) topbar.classList.toggle("is-stuck", y > 8);
      if (topBtn) topBtn.hidden = y < 600;
      if (progressBar) {
        var max = box.scrollHeight - window.innerHeight;
        var ratio = max > 0 ? Math.min(1, Math.max(0, y / max)) : 0;
        progressBar.style.transform = "scaleX(" + ratio.toFixed(4) + ")";
      }
    };
    var onScroll = function () { if (!frame) frame = raf(paint); };
    paint();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll, { passive: true });
    window.addEventListener("pagehide", function () { if (frame) { cancel(frame); frame = 0; } });
    if (topBtn) {
      topBtn.addEventListener("click", function () {
        window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
      });
    }
  }
})();
