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
      icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" fill="white"><path d="M340.65 809.17a138.26 138.26 0 1 1-114.43-114.43 330 330 0 0 1 40.41-46.06 193.1 193.1 0 0 0-198.82 46.09c-75.21 75.21-75.21 197.59 0 272.81s197.6 75.22 272.83 0a193.1 193.1 0 0 0 46.1-198.75c-14.72 11.99-30.17 25.49-46.09 40.34zM764.81 641.69a330 330 0 0 1 39.8 46.32 138.27 138.27 0 1 1-114.77 114.84c-15.99-14.63-31.47-27.96-46.33-39.76a193.1 193.1 0 0 0 46.33 196.8c75.22 75.22 197.62 75.22 272.83 0s75.22-197.6 0-272.83a193.1 193.1 0 0 0-197.86-46.37zM692.82 227.86a138.27 138.27 0 1 1 114.7 114.67c-15.25 16.52-28.54 31.93-40.05 46.23a193.1 193.1 0 0 0 198.23-46.27c75.22-75.22 75.22-197.6 0-272.83s-197.62-75.22-272.83 0a193.1 193.1 0 0 0-46.24 198.33c13.95-11.26 29.32-24.53 46.19-40.13zM258.29 374.94a330 330 0 0 1-41.12-45.77 138.26 138.26 0 1 1 113.83-113.79c15.65 14.9 31 28.61 45.77 41.12a193.1 193.1 0 0 0-45.69-200.09c-75.18-75.22-197.6-75.22-272.78 0s-75.22 197.6 0 272.83a193.18 193.18 0 0 0 199.99 45.7zM518.34 460.18a56.33 56.33 0 1 0 39.91 16.49 56.01 56.01 0 0 0-39.91-16.49zM787.95 845.34c3.2 3.42 11.06 12.32 12.7 13.95l.82.79a8 8 0 0 0 1.43 1.3c19.2 17.26 46.82 18.59 62.94 2.42 15.13-15.13 14.95-40.39.61-59.32a170 170 0 0 0-12.24-12.17c-1.59-1.34-2.86-2.52-3.48-3-44.13-40.88-188.14-180.73-185.3-262.66 0-3.42 0-17.78 0-21.93-.4-82.2 141.6-220.08 185.35-260.61.54-.49 1.89-1.66 3.48-3.02a167 167 0 0 0 12.24-12.16c14.3-18.92 14.52-44.18-.62-59.32-16.12-16.12-43.74-14.85-62.94 2.42a8 8 0 0 0-1.43 1.3l-.81.77c-1.65 1.64-9.5 10.54-12.7 13.97-43.07 46.13-170.6 175.53-251.23 181.66-6.35.49-28.06.39-33.37.17-82.55-3.25-217.42-142.11-257.35-185.29-.5-.54-1.66-1.89-3.02-3.48a164 164 0 0 0-12.16-12.24c-18.92-14.3-44.18-14.52-59.32.6-16.12 16.13-14.86 43.75 2.42 62.94a8.7 8.7 0 0 0 1.3 1.43l.77.8c1.65 1.66 10.54 9.5 13.96 12.7 46.3 45.98 170.22 168.06 182.27 248.9 1.3 8.7 1.22 44.21-1.12 55.23-17.16 80.73-136 197.73-179.81 238.63-3.42 3.22-12.32 11.06-13.96 12.7l-.77.82a8 8 0 0 0-1.3 1.43c-17.28 19.2-18.59 46.82-2.42 62.94 15.13 15.13 40.39 14.96 59.32.61a167 167 0 0 0 12.16-12.24c1.36-1.59 2.52-2.86 3.03-3.48 46.51-43.97 175.93-177.35 258.85-186.66 7.48-.84 33.96-.82 40.87-.16 80.71 7.45 206.9 135.54 249.7 181.31zM580.25 578.41a87.55 87.55 0 1 1 0-123.81 86.98 86.98 0 0 1 0 123.81z"/></svg>',
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

  // ── Message box (replaces native alert/confirm) ──
  var _msgLayer = null;

  function _ensureMsgLayer() {
    if (_msgLayer) return _msgLayer;
    _msgLayer = document.createElement('div');
    _msgLayer.className = 'message-overlay';
    _msgLayer.innerHTML =
      '<div class="message-box">' +
      '<div class="msg-icon" id="msgIcon"></div>' +
      '<div class="msg-title" id="msgTitle"></div>' +
      '<div class="msg-body" id="msgBody"></div>' +
      '<div class="msg-actions" id="msgActions"></div>' +
      '</div>';
    document.body.appendChild(_msgLayer);
    return _msgLayer;
  }

  function _closeMsgBox() { if (_msgLayer) _msgLayer.classList.remove('show'); }

  function _showMsgBox(iconHtml, title, body, buttons) {
    return new Promise(function(resolve) {
      var layer = _ensureMsgLayer();
      document.getElementById('msgIcon').innerHTML = iconHtml;
      document.getElementById('msgTitle').textContent = title;
      document.getElementById('msgBody').textContent = body;
      var actionsEl = document.getElementById('msgActions');
      actionsEl.innerHTML = '';
      var resolved = false;
      buttons.forEach(function(btn) {
        var el = document.createElement('button');
        el.textContent = btn.label;
        if (btn.primary) el.className = 'primary';
        if (btn.danger) el.className = 'danger';
        el.addEventListener('click', function() {
          if (!resolved) { resolved = true; _closeMsgBox(); resolve(btn.value); }
        });
        actionsEl.appendChild(el);
      });
      layer.classList.add('show');
      var firstBtn = actionsEl.querySelector('button');
      if (firstBtn) setTimeout(function() { firstBtn.focus(); }, 50);
    });
  }

  var _ICONS = {
    success: '<svg width="40" height="40" viewBox="0 0 40 40" fill="none"><circle cx="20" cy="20" r="18" stroke="#16a34a" stroke-width="2.5"/><path d="M13 20l5 5 9-9" stroke="#16a34a" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    error: '<svg width="40" height="40" viewBox="0 0 40 40" fill="none"><circle cx="20" cy="20" r="18" stroke="#dc2626" stroke-width="2.5"/><path d="M14 14l12 12M26 14l-12 12" stroke="#dc2626" stroke-width="2.5" stroke-linecap="round"/></svg>',
    warning: '<svg width="40" height="40" viewBox="0 0 40 40" fill="none"><path d="M20 5L4 35h32L20 5z" stroke="#ca8a04" stroke-width="2.5" stroke-linejoin="round"/><line x1="20" y1="16" x2="20" y2="24" stroke="#ca8a04" stroke-width="2.5" stroke-linecap="round"/><circle cx="20" cy="29" r="1.5" fill="#ca8a04"/></svg>',
    info: '<svg width="40" height="40" viewBox="0 0 40 40" fill="none"><circle cx="20" cy="20" r="18" stroke="#2563eb" stroke-width="2.5"/><line x1="20" y1="14" x2="20" y2="22" stroke="#2563eb" stroke-width="2.5" stroke-linecap="round"/><circle cx="20" cy="27" r="1.5" fill="#2563eb"/></svg>'
  };

  var Message = {
    success: function(msg) {
      return _showMsgBox(_ICONS.success, '', msg, [{ label: '确定', value: true, primary: true }]);
    },
    error: function(msg) {
      return _showMsgBox(_ICONS.error, '', msg, [{ label: '确定', value: true, primary: true }]);
    },
    warning: function(msg) {
      return _showMsgBox(_ICONS.warning, '', msg, [{ label: '确定', value: true, primary: true }]);
    },
    info: function(msg) {
      return _showMsgBox(_ICONS.info, '', msg, [{ label: '确定', value: true, primary: true }]);
    },
    confirm: function(msg) {
      return _showMsgBox(_ICONS.info, '确认操作', msg, [
        { label: '取消', value: false },
        { label: '确定', value: true, primary: true }
      ]);
    }
  };

  // ── Simple confirmation dialog (delegates to Message.confirm) ──
  function confirm(msg, onOk) {
    Message.confirm(msg).then(function(ok) { if (ok && onOk) onOk(); });
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
    Message: Message,
    fmtTime: fmtTime
  };
})();

window.UI = UI;
