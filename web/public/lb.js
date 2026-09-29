/* lbtrack tracker — first-party, no deps. Posts to a same-origin endpoint. */
(function () {
  if (window.__lbt) return; window.__lbt = 1;
  var CFG = window.LB_CFG || {};
  var EP = CFG.endpoint || '/e';
  var SITE = location.hostname;
  var LS = { get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
             set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };

  function uid() {
    try { return crypto.randomUUID().replace(/-/g, '').slice(0, 20); }
    catch (e) { return (Date.now().toString(36) + Math.random().toString(36).slice(2, 12)); }
  }

  // ---- identity: persistent visitor, 30-min sliding session ----
  var vid = LS.get('_lbv'); if (!vid) { vid = uid(); LS.set('_lbv', vid); LS.set('_lbfirst', String(Date.now())); }
  var now = Date.now(), last = parseInt(LS.get('_lbseen') || '0', 10), sid = LS.get('_lbs');
  var newSession = !sid || (now - last) > 30 * 60 * 1000;
  if (newSession) {
    sid = uid(); LS.set('_lbs', sid);
    LS.set('_lbvisits', String((parseInt(LS.get('_lbvisits') || '0', 10)) + 1));
    LS.set('_lbseq', '0');
  }
  LS.set('_lbseen', String(now));
  var visits = parseInt(LS.get('_lbvisits') || '1', 10);

  // ---- campaign / outreach tags: captured once, sticky for the session ----
  var qs = new URLSearchParams(location.search);
  var TAGKEYS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'];
  var camp = {};
  if (newSession) {
    TAGKEYS.forEach(function (k) { if (qs.get(k)) camp[k] = qs.get(k); });
    var rt = qs.get('ref') || qs.get('lead') || qs.get('src') || qs.get('c') || '';
    if (rt) camp.reftag = rt;
    LS.set('_lbcamp', JSON.stringify(camp));
  } else {
    try { camp = JSON.parse(LS.get('_lbcamp') || '{}'); } catch (e) { camp = {}; }
  }

  // ---- environment ----
  var env = {
    sw: screen.width, sh: screen.height,
    vw: innerWidth, vh: innerHeight,
    dpr: devicePixelRatio || 1,
    cores: navigator.hardwareConcurrency || 0,
    mem: navigator.deviceMemory || 0,
    touch: navigator.maxTouchPoints || 0,
    lang: (navigator.languages || [navigator.language]).join(','),
    tz: (function () { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return ''; } })(),
    ua: navigator.userAgent
  };
  if (navigator.userAgentData) {
    env.uaMobile = navigator.userAgentData.mobile ? 1 : 0;
    env.uaPlatform = navigator.userAgentData.platform || '';
    try {
      env.uaBrands = (navigator.userAgentData.brands || []).map(function (b) { return b.brand + ' ' + b.version; }).join('; ');
      navigator.userAgentData.getHighEntropyValues(['model', 'platformVersion', 'architecture'])
        .then(function (h) { env.uaModel = [h.model, h.platformVersion, h.architecture].filter(Boolean).join('/'); })
        .catch(function () {});
    } catch (e) {}
  }

  // ---- engagement state (per page view) ----
  var pageStart = Date.now(), engagedMs = 0, lastTick = Date.now(), maxScroll = 0, interacted = 0;
  function scrollPct() {
    var h = document.documentElement.scrollHeight - innerHeight;
    if (h <= 0) return 100;
    return Math.min(100, Math.round((scrollY / h) * 100));
  }
  addEventListener('scroll', function () { var p = scrollPct(); if (p > maxScroll) maxScroll = p; }, { passive: true });
  ['click', 'keydown', 'pointerdown'].forEach(function (t) {
    addEventListener(t, function () { interacted = 1; }, { passive: true, capture: true });
  });

  // ---- queue + transport ----
  var q = [];
  function seqNext() { var n = parseInt(LS.get('_lbseq') || '0', 10) + 1; LS.set('_lbseq', String(n)); return n; }

  function push(type, extra) {
    var e = {
      t: type, ts: Date.now(), site: SITE, vid: vid, sid: sid, visits: visits, seq: seqNext(),
      path: location.pathname, query: location.search, title: document.title,
      ref: document.referrer || '',
      dwell: Date.now() - pageStart, engaged: engagedMs, scroll: maxScroll
    };
    for (var k in env) e[k] = env[k];
    for (var k2 in camp) e[k2] = camp[k2];
    if (extra) for (var k3 in extra) e[k3] = extra[k3];
    q.push(e);
    if (type !== 'ping') schedule();
  }

  var timer = null;
  function schedule() { if (timer) return; timer = setTimeout(function () { timer = null; flush(false); }, 900); }

  function flush(sync) {
    if (!q.length) return;
    var body = JSON.stringify({ events: q.splice(0, q.length) });
    try {
      if (sync && navigator.sendBeacon) {
        navigator.sendBeacon(EP, new Blob([body], { type: 'application/json' }));
        return;
      }
      fetch(EP, { method: 'POST', body: body, keepalive: true, headers: { 'Content-Type': 'application/json' }, credentials: 'omit' })
        .catch(function () {});
    } catch (e) {}
  }

  // ---- pageview ----
  function pageview() {
    pageStart = Date.now(); engagedMs = 0; lastTick = Date.now(); maxScroll = scrollPct(); interacted = 0;
    var nav = null;
    try { nav = performance.getEntriesByType('navigation')[0]; } catch (e) {}
    push('pageview', nav ? { ttfb: Math.round(nav.responseStart), load: Math.round(nav.duration) } : null);
  }

  // ---- heartbeat: real engaged time, only while the tab is visible ----
  setInterval(function () {
    var t = Date.now();
    if (document.visibilityState === 'visible') engagedMs += Math.min(t - lastTick, 15000);
    lastTick = t;
    if (engagedMs > 0 && engagedMs % 1 === 0) push('ping');
    if (q.length > 8) flush(false);
  }, 10000);
  document.addEventListener('visibilitychange', function () {
    lastTick = Date.now();
    if (document.visibilityState === 'hidden') { push('hide'); flush(true); }
  });

  // ---- clicks: outbound, mail, tel, downloads, tagged elements ----
  document.addEventListener('click', function (ev) {
    var el = ev.target && ev.target.closest ? ev.target.closest('a,[data-track],button') : null;
    if (!el) return;
    var label = (el.getAttribute('data-track') || (el.innerText || '').trim().slice(0, 120));
    var href = el.getAttribute('href') || '';
    var type = 'click';
    if (href) {
      if (/^mailto:/i.test(href)) type = 'mail';
      else if (/^tel:/i.test(href)) type = 'tel';
      else if (/\.(pdf|zip|docx?|pptx?|csv|xlsx?)($|\?)/i.test(href)) type = 'download';
      else if (/^https?:\/\//i.test(href) && href.indexOf(location.origin) !== 0) type = 'outbound';
    }
    push(type, { target: href.slice(0, 400), label: label });
  }, { capture: true, passive: true });

  // ---- form engagement (which fields they touched, never the values) ----
  document.addEventListener('focusin', function (ev) {
    var el = ev.target;
    if (!el || !el.tagName) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) {
      if (/password|card|cvv|ssn/i.test(el.name + el.id + el.type)) return;
      push('field', { label: (el.name || el.id || el.type || '').slice(0, 80) });
    }
  }, { capture: true, passive: true });
  document.addEventListener('submit', function (ev) {
    var f = ev.target;
    push('submit', { label: (f && (f.getAttribute('name') || f.getAttribute('id') || f.action) || '').slice(0, 200) });
    flush(true);
  }, { capture: true, passive: true });

  // ---- exit ----
  addEventListener('pagehide', function () { push('exit'); flush(true); });
  addEventListener('beforeunload', function () { flush(true); });

  // ---- SPA route changes ----
  ['pushState', 'replaceState'].forEach(function (m) {
    var orig = history[m];
    history[m] = function () { var r = orig.apply(this, arguments); setTimeout(pageview, 0); return r; };
  });
  addEventListener('popstate', function () { setTimeout(pageview, 0); });

  // ---- public API for custom events ----
  window.lbtrack = function (name, data) { push('custom', { label: name, meta: data || null }); };

  pageview();
})();
