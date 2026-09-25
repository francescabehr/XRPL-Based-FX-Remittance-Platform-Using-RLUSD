/* UI helpers for the design system (UI_REDESIGN.md §4).
 *
 * Plain JS, no dependencies, loaded with `defer`. Exposes `window.UI` and wires
 * `data-` attributes on DOMContentLoaded. Nothing here delays an action: forms
 * submit immediately and animations run alongside navigation. Every animation
 * respects reduced motion (the OS setting or `html.ds-reduce-motion`).
 */
(function () {
  "use strict";

  var doc = document;
  var root = doc.documentElement;
  var motionQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  function reducedMotion() {
    return (motionQuery && motionQuery.matches) || root.classList.contains("ds-reduce-motion");
  }

  function nextFrame(fn) {
    requestAnimationFrame(function () { requestAnimationFrame(fn); });
  }

  // ── Numbers ────────────────────────────────────────────────────────────────

  // Same output as the server's "{:,.Nf}".format(x): 1,000.00
  function formatNumber(n, decimals) {
    return Number(n).toLocaleString("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  }

  function parseNumber(text) {
    var match = String(text).replace(/,/g, "").match(/-?\d+(\.\d+)?/);
    return match ? parseFloat(match[0]) : 0;
  }

  function decimalsIn(text) {
    var match = String(text).replace(/,/g, "").match(/\d+\.(\d+)/);
    return match ? match[1].length : 0;
  }

  var running = new WeakMap();

  /* Animate el's text to `to`. Writes prefix + number + suffix as ONE text
   * node so it always matches the amount() macro (R1,000.00, 50.874404 UCTUSD). */
  function countUp(el, to, opts) {
    opts = opts || {};
    var ds = el.dataset;
    var decimals = opts.decimals != null ? opts.decimals
      : ds.decimals != null ? parseInt(ds.decimals, 10)
      : decimalsIn(el.textContent);
    var prefix = opts.prefix != null ? opts.prefix : (ds.prefix || "");
    var suffix = opts.suffix != null ? opts.suffix : (ds.suffix || "");
    var from = opts.from != null ? opts.from : parseNumber(el.textContent.replace(prefix, ""));
    var duration = opts.duration != null ? opts.duration : 800;
    var target = Number(to);

    if (running.has(el)) cancelAnimationFrame(running.get(el));

    function write(value) {
      el.textContent = prefix + formatNumber(value, decimals) + suffix;
    }

    if (reducedMotion() || duration <= 0 || from === target) {
      write(target);
      running.delete(el);
      return;
    }

    var start = null;
    function step(now) {
      if (start === null) start = now;
      var t = Math.min((now - start) / duration, 1);
      var eased = 1 - Math.pow(1 - t, 3);
      write(from + (target - from) * eased);
      if (t < 1) {
        running.set(el, requestAnimationFrame(step));
      } else {
        write(target);
        running.delete(el);
      }
    }
    running.set(el, requestAnimationFrame(step));
  }

  // ── Progress ───────────────────────────────────────────────────────────────

  function setProgress(el, pct) {
    var clamped = Math.max(0, Math.min(100, Number(pct) || 0));
    el.style.setProperty("--ds-progress", String(clamped / 100));
    var bar = el.closest("[role=progressbar]") || el;
    if (bar.getAttribute("role") === "progressbar") bar.setAttribute("aria-valuenow", String(Math.round(clamped)));
  }

  // ── Replay a one-shot animation ────────────────────────────────────────────

  function replay(el, cls) {
    el.classList.remove(cls);
    void el.offsetWidth; // force reflow so the animation restarts
    el.classList.add(cls);
  }

  // ── Copy to clipboard ──────────────────────────────────────────────────────

  var liveRegion = null;
  function announce(message) {
    if (!liveRegion) {
      liveRegion = doc.createElement("div");
      liveRegion.className = "visually-hidden";
      liveRegion.setAttribute("aria-live", "polite");
      doc.body.appendChild(liveRegion);
    }
    liveRegion.textContent = "";
    setTimeout(function () { liveRegion.textContent = message; }, 50);
  }

  function fallbackCopy(text) {
    var area = doc.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    doc.body.appendChild(area);
    area.select();
    try { doc.execCommand("copy"); } finally { doc.body.removeChild(area); }
  }

  function copyToClipboard(btn) {
    var text = btn.dataset.copy || "";
    var label = btn.querySelector("[data-copy-label]") || btn;
    function done() {
      if (!btn.dataset.copyOriginal) btn.dataset.copyOriginal = label.textContent;
      label.textContent = "Copied";
      announce("Copied to clipboard");
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(function () {
        label.textContent = btn.dataset.copyOriginal;
      }, 1500);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else {
      fallbackCopy(text);
      done();
    }
  }

  // ── Drawer ─────────────────────────────────────────────────────────────────

  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  var openState = null; // { root, opener, onKey }

  function focusables(container) {
    return Array.prototype.filter.call(container.querySelectorAll(FOCUSABLE), function (el) {
      return el.offsetParent !== null || el === doc.activeElement;
    });
  }

  function openDrawer(id, opener) {
    var drawerRoot = doc.getElementById(id);
    if (!drawerRoot) return false;
    if (openState) closeDrawer(openState.root.id, true);

    var panel = drawerRoot.querySelector(".ds-drawer");
    drawerRoot.hidden = false;
    doc.body.classList.add("ds-scroll-lock");
    nextFrame(function () {
      drawerRoot.classList.add("is-open");
      var first = panel.querySelector("[data-drawer-autofocus]") || focusables(panel)[0] || panel;
      first.focus({ preventScroll: true });
    });

    function onKey(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDrawer(id);
      } else if (event.key === "Tab") {
        var items = focusables(panel);
        if (!items.length) { event.preventDefault(); return; }
        var first = items[0];
        var last = items[items.length - 1];
        if (event.shiftKey && doc.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && doc.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
    doc.addEventListener("keydown", onKey);
    openState = { root: drawerRoot, opener: opener || doc.activeElement, onKey: onKey };
    return true;
  }

  function closeDrawer(id, immediate) {
    if (!openState || (id && openState.root.id !== id)) return;
    var state = openState;
    openState = null;
    doc.removeEventListener("keydown", state.onKey);
    state.root.classList.remove("is-open");
    doc.body.classList.remove("ds-scroll-lock");

    var panel = state.root.querySelector(".ds-drawer");
    var finished = false;
    function finish() {
      if (finished) return;
      finished = true;
      state.root.hidden = true;
    }
    if (immediate || reducedMotion()) {
      finish();
    } else {
      panel.addEventListener("transitionend", finish, { once: true });
      setTimeout(finish, 500); // in case transitionend never fires
    }
    if (state.opener && state.opener.focus) state.opener.focus({ preventScroll: true });
  }

  // ── Flash messages ─────────────────────────────────────────────────────────

  var FLASH_MS = 5000;

  function dismissFlash(flash) {
    if (flash.classList.contains("is-leaving")) return;
    clearTimeout(flash._flashTimer);
    flash.classList.add("is-leaving");
    var remove = function () { if (flash.parentNode) flash.parentNode.removeChild(flash); };
    if (reducedMotion()) remove();
    else setTimeout(remove, 250);
  }

  function initFlash(flash) {
    if (flash._flashReady) return;
    flash._flashReady = true;
    var close = flash.querySelector(".btn-close");
    if (close) {
      close.removeAttribute("data-bs-dismiss"); // we animate the exit ourselves
      close.addEventListener("click", function () { dismissFlash(flash); });
    }
    if (!flash.hasAttribute("data-autodismiss")) return; // errors stay until closed

    var remaining = FLASH_MS;
    var startedAt = 0;
    function start() {
      startedAt = Date.now();
      flash._flashTimer = setTimeout(function () { dismissFlash(flash); }, remaining);
    }
    function pause() {
      clearTimeout(flash._flashTimer);
      remaining = Math.max(1000, remaining - (Date.now() - startedAt));
    }
    // Pause while the reader is on it, so the message can be read in full.
    flash.addEventListener("mouseenter", pause);
    flash.addEventListener("mouseleave", start);
    flash.addEventListener("focusin", pause);
    flash.addEventListener("focusout", start);
    start();
  }

  // ── Polling ────────────────────────────────────────────────────────────────

  /* Poll a JSON endpoint until isDone(data), the timeout, or stop().
   * Pauses while the tab is hidden. `fetcher` may be injected (styleguide demo). */
  function poll(url, opts) {
    opts = opts || {};
    var interval = opts.interval || 2500;
    var timeout = opts.timeout || 120000;
    var fetcher = opts.fetcher || function (u) {
      return fetch(u, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        });
    };
    var startedAt = Date.now();
    var timer = null;
    var stopped = false;

    function stop() {
      stopped = true;
      clearTimeout(timer);
      doc.removeEventListener("visibilitychange", onVisibility);
    }
    function schedule() {
      if (stopped) return;
      if (Date.now() - startedAt >= timeout) {
        stop();
        if (opts.onTimeout) opts.onTimeout();
        return;
      }
      timer = setTimeout(tick, interval);
    }
    function tick() {
      if (stopped) return;
      if (doc.hidden) return; // resumed by onVisibility
      Promise.resolve(fetcher(url)).then(function (data) {
        if (stopped) return;
        if (opts.onUpdate) opts.onUpdate(data);
        if (opts.isDone && opts.isDone(data)) stop();
        else schedule();
      }, function (err) {
        if (opts.onError) opts.onError(err);
        schedule();
      });
    }
    function onVisibility() {
      if (!doc.hidden && !stopped) {
        clearTimeout(timer);
        tick();
      }
    }
    doc.addEventListener("visibilitychange", onVisibility);
    tick();
    return { stop: stop };
  }

  // ── Confirmation for irreversible actions ──────────────────────────────────

  /* form[data-confirm="message"] asks before submitting, in the shared
   * #ds-confirm <dialog> (base.html). Optional: data-confirm-title,
   * data-confirm-label (button text), data-confirm-tone="danger|primary".
   * Without JS the form simply submits. */
  function confirmSubmit(form, submitter) {
    var dialog = doc.getElementById("ds-confirm");
    if (!dialog || typeof dialog.showModal !== "function") return true;

    var ds = form.dataset;
    dialog.querySelector("#ds-confirm-title").textContent = ds.confirmTitle || "Are you sure?";
    dialog.querySelector("#ds-confirm-message").textContent = ds.confirm;
    var ok = dialog.querySelector("[data-confirm-ok]");
    ok.textContent = ds.confirmLabel || "Confirm";
    ok.className = "btn " + (ds.confirmTone === "primary" ? "btn-primary" : "btn-danger");

    dialog.returnValue = "";
    dialog.addEventListener("close", function onClose() {
      dialog.removeEventListener("close", onClose);
      if (dialog.returnValue === "confirm") {
        form._confirmed = true;
        if (form.requestSubmit) form.requestSubmit(submitter || undefined);
        else form.submit();
      } else if (submitter && submitter.focus) {
        submitter.focus();
      }
    });
    dialog.showModal();
    return false;
  }

  doc.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("form[data-confirm]")) return;
    if (form._confirmed) {
      form._confirmed = false; // one confirmation per submit
      return;
    }
    if (!confirmSubmit(form, event.submitter)) event.preventDefault();
  });

  // ── Auto-wiring ────────────────────────────────────────────────────────────

  function wire(scope) {
    scope = scope || doc;

    scope.querySelectorAll("[data-count-up]").forEach(function (el) {
      if (el._countReady) return;
      el._countReady = true;
      var target = el.dataset.countTo != null ? parseFloat(el.dataset.countTo)
        : parseNumber(el.textContent.replace(el.dataset.prefix || "", ""));
      countUp(el, target, { from: 0 });
    });

    scope.querySelectorAll("[data-progress]").forEach(function (el) {
      if (el._progressReady) return;
      el._progressReady = true;
      var pct = el.dataset.progress;
      el.style.setProperty("--ds-progress", "0");
      nextFrame(function () { setProgress(el, pct); });
    });

    scope.querySelectorAll(".ds-flash").forEach(initFlash);
  }

  doc.addEventListener("click", function (event) {
    var copyBtn = event.target.closest("[data-copy]");
    if (copyBtn) {
      event.preventDefault();
      copyToClipboard(copyBtn);
      return;
    }
    var opener = event.target.closest("[data-drawer-open]");
    if (opener) {
      // A real link underneath: without JS (or if the drawer is missing) it navigates.
      if (openDrawer(opener.dataset.drawerOpen, opener)) event.preventDefault();
      return;
    }
    var closer = event.target.closest("[data-drawer-close]");
    if (closer) {
      event.preventDefault();
      var drawerRoot = closer.closest(".ds-drawer-root");
      closeDrawer(drawerRoot ? drawerRoot.id : null);
    }
  });

  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", function () { wire(); });
  } else {
    wire();
  }

  window.UI = {
    reducedMotion: reducedMotion,
    formatNumber: formatNumber,
    countUp: countUp,
    setProgress: setProgress,
    replay: replay,
    copyToClipboard: copyToClipboard,
    openDrawer: openDrawer,
    closeDrawer: closeDrawer,
    dismissFlash: dismissFlash,
    poll: poll,
    wire: wire,
  };
})();
