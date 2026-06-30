/**
 * Chart.js utilities — create/update alert trend charts without destroy+recreate
 */
var ChartUtils = (function() {
  // Pre-compute 24h labels once, refreshed each hour
  var _cachedHours = null;
  var _cachedHour = -1;

  // rAF-deferred chart creation cache
  var _charts = {};
  var _pending = {};

  function _getHours() {
    var now = new Date();
    var h = now.getHours();
    if (_cachedHours && _cachedHour === h) return _cachedHours;
    _cachedHour = h;
    _cachedHours = [];
    for (var i = 23; i >= 0; i--) {
      var d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h - i);
      _cachedHours.push(('0' + d.getHours()).slice(-2) + ':00');
    }
    return _cachedHours;
  }

  function buildAlertChart(canvasId, existingChart, hourly, compact) {
    var canvas = document.getElementById(canvasId);
    if (!canvas || canvas.offsetParent === null) return existingChart || null;

    var hours = _getHours();

    // Build a quick lookup map: "HH:00" → index
    var idxMap = {};
    for (var i = 0; i < hours.length; i++) idxMap[hours[i]] = i;

    var warnData = new Array(24).fill(0),
        sevData = new Array(24).fill(0),
        critData = new Array(24).fill(0);

    (hourly || []).forEach(function(h) {
      var key = h.hour.slice(11, 13) + ':00';
      var idx = idxMap[key];
      if (idx !== undefined) {
        if (h.level === 'warning') warnData[idx] = h.count;
        else if (h.level === 'severe') sevData[idx] = h.count;
        else if (h.level === 'critical') critData[idx] = h.count;
      }
    });

    // Use rAF-created chart if available
    var chart = existingChart || _charts[canvasId] || null;

    if (chart) {
      chart.data.labels = hours;
      chart.data.datasets[0].data = warnData;
      chart.data.datasets[1].data = sevData;
      chart.data.datasets[2].data = critData;
      chart.update('none');
      return chart;
    }

    // Defer creation via rAF to ensure layout is settled before Chart.js reads dimensions
    if (!_pending[canvasId]) {
      _pending[canvasId] = true;
      var ctx = canvas.getContext('2d');
      var compactFlag = compact;
      requestAnimationFrame(function() {
        _pending[canvasId] = false;
        _charts[canvasId] = new Chart(ctx, {
          type: 'line',
          data: {
            labels: hours,
            datasets: [
              { label: '警告', data: warnData, borderColor: '#ca8a04', backgroundColor: 'rgba(202,138,4,.08)', fill: true, tension: 0, pointRadius: 0, borderWidth: 1.5 },
              { label: '严重', data: sevData, borderColor: '#ea580c', backgroundColor: 'rgba(234,88,12,.08)', fill: true, tension: 0, pointRadius: 0, borderWidth: 1.5 },
              { label: '危险', data: critData, borderColor: '#dc262e', backgroundColor: 'rgba(220,38,38,.08)', fill: true, tension: 0, pointRadius: 0, borderWidth: 2 }
            ]
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            animation: false,
            interaction: { intersect: false, mode: 'index' },
            plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, padding: 10, font: { size: 9 }, usePointStyle: true } } },
            scales: {
              x: { ticks: { font: { size: 8 }, maxTicksLimit: compactFlag ? 4 : 6, autoSkip: true }, grid: { display: false } },
              y: { beginAtZero: true, ticks: { font: { size: 8 }, stepSize: 1 }, grid: { color: '#f0f0f0' } }
            }
          }
        });
      });
    }
    return null;
  }

  function buildModelBars(containerId, models) {
    var div = document.getElementById(containerId);
    if (!div) return;
    if (!models || !models.length) { div.innerHTML = '<div class="empty-state">暂无数据</div>'; return; }
    var esc = window.UI ? UI.escapeHtml : function(v) { return String(v == null ? '' : v); };
    var attr = window.UI ? UI.escapeAttr : esc;
    var max = models[0].count;
    var html = '';
    for (var i = 0; i < models.length; i++) {
      var m = models[i];
      var pct = Math.max(4, Math.round(m.count / max * 100));
      html += '<div class="model-bar"><span class="m-name" title="' + attr(m.name) + '">' + esc(m.name) + '</span><div class="m-track"><div class="m-fill" style="width:' + pct + '%"></div></div><span class="m-cnt">' + esc(m.count) + '</span></div>';
    }
    div.innerHTML = html;
  }

  return {
    buildAlertChart: buildAlertChart,
    buildModelBars: buildModelBars
  };
})();

window.ChartUtils = ChartUtils;
