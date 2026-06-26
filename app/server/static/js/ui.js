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
