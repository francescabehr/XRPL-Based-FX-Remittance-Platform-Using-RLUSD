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

  /* One shared drawer, many rows: the opener names a <template> whose content
   * (server-rendered detail) is cloned into the drawer's [data-drawer-content]. */
  function fillDrawer(opener) {
    var drawerRoot = doc.getElementById(opener.dataset.drawerOpen);
    var tpl = doc.getElementById(opener.dataset.drawerTemplate);
    if (!drawerRoot || !tpl) return;
    var slot = drawerRoot.querySelector("[data-drawer-content]");
    if (!slot) return;
    slot.replaceChildren(tpl.content.cloneNode(true));
    var title = drawerRoot.querySelector(".ds-drawer__title");
    if (title && opener.dataset.drawerTitle) title.textContent = opener.dataset.drawerTitle;
    wire(slot);
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

  // ── Live settlement status (GET /status) ───────────────────────────────────

  /* [data-status-poll] marks a page region to keep current. Inside it (or on it),
   * each [data-status-item] is one transfer (data-kind="t") or cash-out ("c")
   * with data-id, data-final and data-hash-value. Every copy of an item — the
   * row, its drawer <template>, the open drawer — carries the same
   * data-status-key and is patched together. Badge and hash markup come from
   * the server's own macros; this only decides what to animate. */

  var TIMELINE_LABELS = { done: "done", current: "in progress", failed: "failed", upcoming: "not started" };
  var TIMELINE_MARKERS = {
    done: '<i class="bi bi-check-lg"></i>',
    failed: '<i class="bi bi-x-lg"></i>',
    current: '<span class="ds-pulse-dot"></span>',
    upcoming: "",
  };
  // Same wording as sender/transaction_detail.html.
  var SETTLING_DESC = {
    queued: "Queued for the XRPL Testnet",
    processing: "Submitting to the XRPL Testnet…",
    completed: "Validated on-ledger",
    failed: "Not delivered — no UCTUSD moved",
  };
  var CASHOUT_NOTES = {
    requested: '<i class="bi bi-hourglass-split" aria-hidden="true"></i><span><strong>Waiting for approval.</strong> Your UCTUSD has not been reserved yet.</span>',
    awaiting: '<i class="bi bi-hourglass-split" aria-hidden="true"></i><span><strong>Awaiting ledger confirmation.</strong> Your UCTUSD stays reserved until the burn is verified.</span>',
    approved: '<span class="ds-pulse-dot" aria-hidden="true"></span><span><strong>Burning on XRPL…</strong> You can close this; the status updates by itself.</span>',
  };

  function parseHTML(html) {
    var tpl = doc.createElement("template");
    tpl.innerHTML = html;
    return tpl.content.firstElementChild;
  }

  function statusCopies(key) {
    var selector = '[data-status-key="' + key + '"]';
    var copies = Array.prototype.slice.call(doc.querySelectorAll(selector));
    doc.querySelectorAll("template").forEach(function (tpl) {
      var el = tpl.content.querySelector(selector);
      if (el) copies.push(el);
    });
    return copies;
  }

  // Change a badge in place so its colour transitions instead of popping.
  function morphBadge(slot, html) {
    var next = parseHTML(html);
    var current = slot.querySelector(".ds-status");
    if (!next) return;
    if (!current) { slot.replaceChildren(next); return; }
    current.className = next.className;
    current.setAttribute("data-status", next.getAttribute("data-status") || "");
    current.replaceChildren.apply(current, Array.prototype.slice.call(next.childNodes));
  }

  function setTimelineStep(li, state, desc) {
    if (!li) return;
    li.className = li.className.replace(/\bis-(done|current|failed|upcoming)\b/, "is-" + state);
    if (state === "current") li.setAttribute("aria-current", "step");
    else li.removeAttribute("aria-current");
    var marker = li.querySelector(".ds-timeline__marker");
    if (marker) marker.innerHTML = TIMELINE_MARKERS[state];
    var hidden = li.querySelector(".ds-timeline__label .visually-hidden");
    if (hidden) hidden.textContent = " — " + TIMELINE_LABELS[state];
    if (desc) {
      var d = li.querySelector(".ds-timeline__desc");
      if (d) d.textContent = desc;
    }
  }

  // Mirrors the Jinja in sender/transaction_detail.html (display only).
  function updateTimeline(copy, s) {
    if (!copy.querySelector(".ds-timeline")) return;
    var cashin = s.cashin_status;
    var settlement = s.settlement_status;
    setTimelineStep(copy.querySelector('[data-step="cashin"]'),
      { received: "done", failed: "failed" }[cashin] || "current");
    setTimelineStep(copy.querySelector('[data-step="settling"]'),
      cashin !== "received" ? "upcoming" : ({ completed: "done", failed: "failed" }[settlement] || "current"),
      cashin === "received" ? SETTLING_DESC[settlement] : null);
    setTimelineStep(copy.querySelector('[data-step="settled"]'), settlement === "completed" ? "done" : "upcoming",
      settlement === "completed" ? "Just now" : null);
    var note = copy.querySelector("[data-progress-note]");
    if (note) {
      var text = cashin === "pending"
        ? "Waiting for the card payment to be confirmed. The transfer starts automatically afterwards."
        : (cashin === "received" && ["not_queued", "queued", "processing"].indexOf(settlement) !== -1
          ? "Sending on the XRPL Testnet. This usually takes under a minute." : "");
      note.textContent = text;
      note.hidden = !text;
    }
  }

  function outcome(kind, s) {
    if (kind === "t") {
      if (s.cashin_status === "failed" || s.settlement_status === "failed") return "failed";
      return s.settlement_status === "completed" ? "success" : null;
    }
    if (s.status === "failed") return "failed";
    return s.status === "completed" ? "success" : null;
  }

  function applyStatus(item, s) {
    var kind = item.dataset.kind;
    var key = item.dataset.statusKey;
    var copies = statusCopies(key);
    var result = s.final ? outcome(kind, s) : null;
    var hashChanged = (s.hash || "") !== (item.dataset.hashValue || "");

    copies.forEach(function (copy) {
      var live = doc.contains(copy); // false for <template> content
      var animate = live && !reducedMotion();

      var badgeSlot = copy.querySelector("[data-badge]");
      if (badgeSlot) {
        var before = badgeSlot.textContent.trim();
        morphBadge(badgeSlot, s.badge_html);
        if (live && badgeSlot.textContent.trim() !== before) {
          var changed = copy.querySelector("[data-status-changed]");
          if (changed) changed.hidden = false;
          var stale = copy.querySelector("[data-stale-on-change]");
          if (stale) stale.hidden = true;
        }
      }

      if (hashChanged && s.hash) {
        var hashSlot = copy.querySelector("[data-hash]");
        if (hashSlot) {
          hashSlot.innerHTML = s.hash_html;
          if (animate) replay(hashSlot, "ds-fade-in");
        }
        var shortSlot = copy.querySelector("[data-hash-short]");
        if (shortSlot) {
          shortSlot.textContent = " · " + s.hash.slice(0, 8) + "…";
          shortSlot.hidden = false;
          if (animate) replay(shortSlot, "ds-fade-in");
        }
      }

      if (kind === "t") updateTimeline(copy, s);

      var processing = copy.querySelector("[data-processing]");
      if (processing) {
        if (s.final) processing.hidden = true;
        else if (kind === "c") processing.innerHTML = CASHOUT_NOTES[s.awaiting_ledger ? "awaiting" : s.status] || processing.innerHTML;
      }

      if (result === "success") {
        var check = copy.querySelector("[data-check]");
        var svg = check && check.querySelector(".ds-check-draw");
        if (check && svg) {
          check.hidden = false;
          svg.classList.remove("is-static");
          if (!live) svg.classList.add("is-drawn", "is-static");
          else replay(svg, "is-drawn");
          // On a list row the check stands in for the icon briefly, then steps aside.
          if (live && check.classList.contains("ds-activity__check")) {
            setTimeout(function () { check.classList.add("is-leaving"); }, reducedMotion() ? 0 : 1800);
          }
        }
        var hero = copy.querySelector(".ds-hero");
        if (hero && animate) replay(hero, "ds-slide-up");
      } else if (result === "failed") {
        var amt = copy.querySelector("[data-amount]");
        if (amt && !amt.querySelector(".ds-strike")) {
          var strike = doc.createElement("span");
          strike.className = "ds-strike";
          while (amt.firstChild) strike.appendChild(amt.firstChild);
          amt.appendChild(strike);
        }
        var icon = copy.querySelector("[data-icon]");
        if (icon) icon.classList.add("is-failed");
        var failure = copy.querySelector("[data-failure]");
        if (failure) {
          failure.textContent = s.failure_reason || "";
          failure.hidden = !s.failure_reason;
        }
      }
    });

    item.dataset.hashValue = s.hash || "";
    if (s.final) item.dataset.final = "true";
  }

  function updateBalance(value) {
    if (value == null) return;
    var el = doc.querySelector("[data-wallet-balance] .ds-amount");
    if (!el) return;
    var target = parseFloat(value);
    var shown = el.dataset.countTo != null ? parseFloat(el.dataset.countTo) : NaN;
    if (shown === target) return;
    el.dataset.countTo = String(target);
    countUp(el, target, { duration: 900 });
  }

  function watchStatus(root) {
    if (root._statusWatch) return root._statusWatch;
    var items = root.matches("[data-status-item]") ? [root]
      : Array.prototype.slice.call(root.querySelectorAll("[data-status-item]"));
    function pending() {
      return items.filter(function (el) { return el.dataset.final !== "true"; });
    }
    if (!pending().length) return null;

    var base = (root.dataset.statusPoll || "/status").split("?")[0];
    var watcher = poll(base, {
      interval: 2500,
      timeout: 120000,
      fetcher: function () {
        var open = pending();
        if (!open.length) return Promise.resolve({ transactions: {}, cashouts: {}, wallet_balance: null, all_final: true });
        var query = open.map(function (el) {
          return encodeURIComponent(el.dataset.kind) + "=" + encodeURIComponent(el.dataset.id);
        }).join("&");
        return fetch(base + "?" + query, { credentials: "same-origin", headers: { Accept: "application/json" } })
          .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            return r.json();
          });
      },
      onUpdate: function (data) {
        pending().forEach(function (el) {
          var group = el.dataset.kind === "t" ? data.transactions : data.cashouts;
          var s = group && group[el.dataset.id];
          if (s) applyStatus(el, s);
        });
        updateBalance(data.wallet_balance);
      },
      isDone: function (data) { return data.all_final || !pending().length; },
      onTimeout: function () {
        var note = root.querySelector("[data-poll-note]");
        if (note) note.hidden = false;
      },
    });
    root._statusWatch = watcher;
    return watcher;
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

    if (scope === doc) doc.querySelectorAll("[data-status-poll]").forEach(watchStatus);
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
      // A real link underneath: without JS (or if the drawer is missing) it
      // navigates, and so do modified clicks (new tab / window).
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (opener.dataset.drawerTemplate) fillDrawer(opener);
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
    watchStatus: watchStatus,
    wire: wire,
  };
})();
