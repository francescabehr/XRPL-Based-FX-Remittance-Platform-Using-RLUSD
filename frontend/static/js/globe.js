/* Landing-page globe: dotted continents on a slowly rotating sphere, with
   payment routes from Johannesburg drawing on a loop. Plain canvas, no libraries.

   - Land dots come from globe-land.js (window.GLOBE_LAND, flat [lon, lat, ...]).
   - Colours come from the design tokens on :root, so the brand lives in one place.
   - Reduced motion: one static frame centred on Africa, routes fully drawn.
   - Pauses when the tab is hidden or the globe is scrolled out of view.
   - Without JS (or without canvas) the static SVG fallback stays in place.
   The loop is the one sanctioned exception to "no decorative loops"
   (UI_REDESIGN.md §4). */
(function () {
  "use strict";

  var canvas = document.querySelector("[data-globe]");
  if (!canvas || !window.GLOBE_LAND || !canvas.getContext) return;
  var ctx = canvas.getContext("2d");
  if (!ctx) return;

  var fallback = document.querySelector("[data-globe-fallback]");
  if (fallback) fallback.style.display = "none";
  canvas.hidden = false;

  var RAD = Math.PI / 180;
  var TILT = -12 * RAD;                 // view centre latitude: southern Africa sits high
  var START_LON = 20;                   // start centred on Africa
  var DEG_PER_SEC = 8;                  // one turn in 45 s
  var ROUTE_PERIOD = 5.2;               // seconds per route cycle
  var sinT = Math.sin(TILT), cosT = Math.cos(TILT);

  var ORIGIN = [28.05, -26.2];          // Johannesburg
  var DESTINATIONS = [
    [-0.13, 51.5],    // London
    [3.4, 6.5],       // Lagos
    [36.8, -1.3],     // Nairobi
    [-74.0, 40.7],    // New York
    [55.3, 25.2],     // Dubai
    [151.2, -33.9],   // Sydney
  ];

  /* ---- colours from tokens ------------------------------------------------ */
  function token(name, fallbackValue) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallbackValue;
  }
  function rgba(hex, a) {
    var h = hex.replace("#", "");
    if (h.length === 3) h = h.replace(/./g, "$&$&");
    var n = parseInt(h, 16);
    return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }
  // On the berry sphere, sage routes (3.4:1) and chalk land/markers (4.85:1) hold
  // their contrast; the Thulian highlight (1.9:1) would disappear.
  var C = {
    halo: token("--accent-soft", "#F6ECF0"),
    sphere: token("--accent", "#933B5B"),
    sphereDeep: token("--accent-active", "#672940"),
    route: token("--sage", "#AABAAE"),
    land: token("--chalk", "#E3D6BF"),
    marker: token("--chalk", "#E3D6BF"),
  };

  /* ---- geometry ------------------------------------------------------------ */
  var land = window.GLOBE_LAND;
  var nLand = land.length / 2;
  var landLon = new Float32Array(nLand), landSin = new Float32Array(nLand), landCos = new Float32Array(nLand);
  for (var i = 0; i < nLand; i++) {
    var lat = land[2 * i + 1] * RAD;
    landLon[i] = land[2 * i] * RAD;
    landSin[i] = Math.sin(lat);
    landCos[i] = Math.cos(lat);
  }

  function vec(lon, lat) {
    var l = lon * RAD, p = lat * RAD;
    return [Math.cos(p) * Math.cos(l), Math.cos(p) * Math.sin(l), Math.sin(p)];
  }
  // A great-circle arc lifted off the surface: points as [lon, lat, altitude].
  function arc(a, b, n) {
    var va = vec(a[0], a[1]), vb = vec(b[0], b[1]);
    var om = Math.acos(Math.min(1, va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2]));
    var lift = 0.04 + 0.12 * (om / Math.PI);
    var pts = [];
    for (var k = 0; k <= n; k++) {
      var t = k / n, s1 = Math.sin((1 - t) * om) / Math.sin(om), s2 = Math.sin(t * om) / Math.sin(om);
      var x = s1 * va[0] + s2 * vb[0], y = s1 * va[1] + s2 * vb[1], z = s1 * va[2] + s2 * vb[2];
      pts.push([Math.atan2(y, x), Math.atan2(z, Math.sqrt(x * x + y * y)), 1 + lift * Math.sin(Math.PI * t)]);
    }
    return pts;
  }
  var routes = DESTINATIONS.map(function (d) { return { to: d, pts: arc(ORIGIN, d, 64) }; });

  /* ---- sizing -------------------------------------------------------------- */
  var size = 0, dpr = 1, cx = 0, cy = 0, R = 0;
  function resize() {
    var w = canvas.clientWidth;
    if (!w) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    size = w;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(w * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx = cy = w / 2;
    R = w / 2 * 0.89;
  }

  /* ---- projection (orthographic, centre lon0 / TILT) ----------------------- */
  var lon0 = START_LON * RAD;
  // Returns [x, y, depth, visible] for lon/lat in radians at altitude a (1 = surface).
  function project(lon, lat, a) {
    var cl = Math.cos(lat), sl = Math.sin(lat), dl = lon - lon0, cd = Math.cos(dl);
    var cosc = sinT * sl + cosT * cl * cd;
    var x = a * cl * Math.sin(dl);
    var y = a * (cosT * sl - sinT * cl * cd);
    // Only the near side is drawn: a route disappears over the horizon rather than
    // looping outside the globe's outline.
    var visible = cosc > 0;
    return [cx + R * x, cy - R * y, a * cosc, visible];
  }

  /* ---- drawing ------------------------------------------------------------- */
  function drawSphere() {
    ctx.beginPath();
    ctx.arc(cx, cy, R * 1.085, 0, 2 * Math.PI);
    ctx.fillStyle = C.halo;
    ctx.fill();

    var g = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.35, R * 0.1, cx, cy, R);
    g.addColorStop(0, C.sphere);
    g.addColorStop(1, C.sphereDeep);
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, 2 * Math.PI);
    ctx.fillStyle = g;
    ctx.fill();
  }

  function drawGrid() {
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    var lat, lon, k, p, pen;
    for (lat = -60; lat <= 60; lat += 30) {
      ctx.beginPath(); pen = false;
      for (k = 0; k <= 120; k++) {
        p = project((-180 + k * 3) * RAD, lat * RAD, 1);
        if (p[2] > 0) { pen ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); pen = true; } else pen = false;
      }
      ctx.stroke();
    }
    for (lon = -180; lon < 180; lon += 30) {
      ctx.beginPath(); pen = false;
      for (k = 0; k <= 60; k++) {
        p = project(lon * RAD, (-90 + k * 3) * RAD, 1);
        if (p[2] > 0) { pen ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); pen = true; } else pen = false;
      }
      ctx.stroke();
    }
  }

  function drawLand() {
    var base = Math.max(1, R * 0.0105);
    for (var i = 0; i < nLand; i++) {
      var dl = landLon[i] - lon0, cd = Math.cos(dl);
      var cosc = sinT * landSin[i] + cosT * landCos[i] * cd;
      if (cosc <= 0.02) continue;
      var x = cx + R * landCos[i] * Math.sin(dl);
      var y = cy - R * (cosT * landSin[i] - sinT * landCos[i] * cd);
      ctx.globalAlpha = 0.18 + 0.5 * cosc;          // fades towards the limb
      ctx.fillRect(x - base / 2, y - base / 2, base, base);
    }
    ctx.globalAlpha = 1;
  }

  function drawShine() {
    var g = ctx.createRadialGradient(cx - R * 0.35, cy - R * 0.4, 0, cx - R * 0.35, cy - R * 0.4, R * 1.1);
    g.addColorStop(0, "rgba(255,255,255,0.16)");
    g.addColorStop(0.6, "rgba(255,255,255,0)");
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, 2 * Math.PI);
    ctx.fillStyle = g;
    ctx.fill();
    var rim = ctx.createRadialGradient(cx, cy, R * 0.75, cx, cy, R);
    rim.addColorStop(0, "rgba(0,0,0,0)");
    rim.addColorStop(1, "rgba(0,0,0,0.25)");
    ctx.fillStyle = rim;
    ctx.fill();
  }

  function marker(lon, lat, radius, fill, ring) {
    var p = project(lon * RAD, lat * RAD, 1);
    if (p[2] <= 0) return;
    if (ring) {
      ctx.beginPath();
      ctx.arc(p[0], p[1], radius * 2, 0, 2 * Math.PI);
      ctx.strokeStyle = rgba(C.marker, 0.55);
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(p[0], p[1], radius, 0, 2 * Math.PI);
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = ring ? "#fff" : C.sphere;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  // progress: how much of the arc is drawn (0..1); alpha: its opacity.
  function drawRoute(route, progress, alpha) {
    if (alpha <= 0 || progress <= 0) return;
    var pts = route.pts, last = Math.floor(progress * (pts.length - 1));
    ctx.strokeStyle = rgba(C.route, alpha);
    ctx.lineWidth = Math.max(1.5, R * 0.011);
    ctx.lineCap = "round";
    ctx.beginPath();
    var pen = false, head = null;
    for (var k = 0; k <= last; k++) {
      var p = project(pts[k][0], pts[k][1], pts[k][2]);
      if (p[3]) { pen ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); pen = true; head = p; } else { pen = false; head = null; }
    }
    ctx.stroke();
    if (head && progress < 1) {                     // the leading dot while drawing
      ctx.beginPath();
      ctx.arc(head[0], head[1], Math.max(2.5, R * 0.016), 0, 2 * Math.PI);
      ctx.fillStyle = rgba(C.route, alpha);
      ctx.fill();
    }
  }

  function ease(t) { return 1 - Math.pow(1 - t, 3); }

  // One route's state at time t (seconds): draw → hold → fade → rest.
  function routeState(i, t) {
    var phase = ((t / ROUTE_PERIOD) + i / routes.length) % 1;
    if (phase < 0.3) return [ease(phase / 0.3), 1];
    if (phase < 0.6) return [1, 1];
    if (phase < 0.8) return [1, 1 - (phase - 0.6) / 0.2];
    return [0, 0];
  }

  function frame(t, still) {
    if (!size) return;
    ctx.clearRect(0, 0, size, size);
    drawSphere();
    drawGrid();
    ctx.fillStyle = C.land;
    drawLand();
    drawShine();
    routes.forEach(function (r, i) {
      var s = still ? [1, 1] : routeState(i, t);
      drawRoute(r, s[0], s[1]);
      marker(r.to[0], r.to[1], Math.max(2.5, R * 0.018), C.route, false);
    });
    marker(ORIGIN[0], ORIGIN[1], Math.max(3.5, R * 0.026), C.marker, true);
  }

  /* ---- loop ---------------------------------------------------------------- */
  var reduce = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  var onScreen = true, raf = 0, clock = 0, lastTs = 0;

  function setState(state) { canvas.setAttribute("data-globe-state", state); }

  function tick(ts) {
    var dt = lastTs ? Math.min((ts - lastTs) / 1000, 0.1) : 0;  // no jump after a pause
    lastTs = ts;
    clock += dt;
    lon0 = (START_LON - DEG_PER_SEC * clock) * RAD;
    frame(clock, false);
    raf = window.requestAnimationFrame(tick);
  }

  function update() {
    window.cancelAnimationFrame(raf);
    raf = 0;
    lastTs = 0;
    if (reduce && reduce.matches) {
      lon0 = START_LON * RAD;
      frame(0, true);
      setState("static");
    } else if (document.hidden || !onScreen) {
      setState("paused");
    } else {
      setState("running");
      raf = window.requestAnimationFrame(tick);
    }
  }

  resize();
  update();
  if (!raf) frame(clock, !!(reduce && reduce.matches));

  if (window.ResizeObserver) {
    new ResizeObserver(function () { resize(); if (!raf) frame(clock, !!(reduce && reduce.matches)); }).observe(canvas);
  } else {
    window.addEventListener("resize", function () { resize(); });
  }
  if (window.IntersectionObserver) {
    new IntersectionObserver(function (entries) {
      onScreen = entries[0].isIntersecting;
      update();
    }).observe(canvas);
  }
  document.addEventListener("visibilitychange", update);
  if (reduce) {
    if (reduce.addEventListener) reduce.addEventListener("change", update);
    else if (reduce.addListener) reduce.addListener(update);
  }
})();
