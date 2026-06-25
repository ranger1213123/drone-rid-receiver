/**
 * Shared UI utilities — toast, notifications, audio alerts, DOM helpers
 */
var UI = (function() {
  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/`/g, '&#96;');
  }

  function jsString(value) {
    return JSON.stringify(String(value == null ? '' : value));
  }

  // ── Toast notification ──
  function toast(msg, type) {
    type = type || 'info';
    var el = document.getElementById('toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toast';
      el.style.cssText = 'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:9999;padding:8px 20px;border-radius:8px;font-size:12px;font-weight:500;pointer-events:none;transition:opacity .3s;opacity:0';
      document.body.appendChild(el);
    }
    var colors = { info: '#2563eb', error: '#dc262e', ok: '#16a34a', warn: '#ea580c' };
    el.style.background = colors[type] || colors.info;
    el.style.color = '#fff';
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(toast._tid);
    toast._tid = setTimeout(function() { el.style.opacity = '0'; }, 2500);
  }

  // ── Desktop notification ──
  function requestNotifyPermission() {
    if (window.Notification && Notification.permission === 'default') {
      Notification.requestPermission();
    }
  }

  function notify(title, body, tag) {
    if (!window.Notification || Notification.permission !== 'granted') return;
    new Notification(title, {
      body: body,
      icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🚁</text></svg>',
      tag: tag
    });
  }

  // ── Audio alert (Web Audio API) ──
  var _audioCtx = null;

  function beep(level) {
    try {
      if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      var osc = _audioCtx.createOscillator();
      var gain = _audioCtx.createGain();
      osc.connect(gain); gain.connect(_audioCtx.destination);
      var freq = level === 'critical' ? 880 : level === 'severe' ? 660 : 440;
      osc.frequency.value = freq; osc.type = 'square';
      gain.gain.setValueAtTime(0.08, _audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + 0.25);
      osc.start(_audioCtx.currentTime); osc.stop(_audioCtx.currentTime + 0.25);
    } catch(e) {}
  }

  // ── Animate a numeric element value ──
  function animateEl(el, target) {
    var cur = parseInt(el.textContent) || 0;
    if (cur === target) return;
    var step = Math.max(1, Math.abs(target - cur) / 8);
    var dir = target > cur ? 1 : -1;
    (function tick() {
      cur += step * dir;
      if (dir > 0 ? cur >= target : cur <= target) { el.textContent = target; return; }
      el.textContent = Math.round(cur);
      requestAnimationFrame(tick);
    })();
  }

  // ── Event delegation helper ──
  // Usage: UI.delegate(container, 'click', '[data-action="delete"]', handler)
  function delegate(container, eventType, selector, handler) {
    container.addEventListener(eventType, function(e) {
      var target = e.target.closest(selector);
      if (target && container.contains(target)) {
        handler.call(target, e, target);
      }
    });
  }

  // ── Simple confirmation dialog ──
  function confirm(msg, onOk) {
    if (window.confirm(msg)) onOk();
  }

  // ── Format time ──
  function fmtTime(isoStr) {
    return (isoStr || '').substring(11, 19);
  }

  return {
    escapeHtml: escapeHtml,
    escapeAttr: escapeAttr,
    jsString: jsString,
    toast: toast,
    requestNotifyPermission: requestNotifyPermission,
    notify: notify,
    beep: beep,
    animateEl: animateEl,
    delegate: delegate,
    confirm: confirm,
    fmtTime: fmtTime
  };
})();

window.UI = UI;
