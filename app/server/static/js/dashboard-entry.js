/**
 * Dashboard (list view) entry point.
 * All page-specific logic extracted from dashboard.html inline script.
 */
import './api.js';

var _lastAlertViewTime = 0;  // 上次查看告警的时间戳，用于未读计数
import './ui.js';
import regionData from './region-data.js';

// Alias shared UI functions
var showToast = UI.toast;
var catchErr = function(msg){
  return function(e){
    console.warn(msg, e);
    UI.toast((msg||'请求失败')+': '+(e.message||'网络错误'), 'error');
  };
};

// ═══════════ State ═══════════
let pollTimer, lastDrones = [], prevAlertLevels = {}, currentUser = {};
let plData = [], stData = [], userData = [], psData = [];
let _allStations = [];
let wlData = [];
let _editingPlId = null;
let _editingStName2 = null;
let _editingUsername2 = null;
let selectedDrone = null;

let pageTitles = {
  drones:'无人机列表', alerts:'告警日志', trajectory:'轨迹查看', powerlines:'电力线管理',
  stations:'站点管理', users:'用户管理', personnel:'告警联系人', whitelist:'白名单',
  devices:'设备管理', licenses:'密钥管理', audit:'审计日志', settings:'系统设置', profile:'用户信息管理'
};
let monitoringPages = {drones:1, alerts:1, trajectory:1};

// ═══════════ Sidebar Navigation ═══════════
document.querySelectorAll('.nav-item[data-page]').forEach(function(el){
  el.addEventListener('click', function(){
    document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});
    this.classList.add('active');
    document.querySelectorAll('.panel-page').forEach(function(p){p.classList.remove('active')});
    var page = document.getElementById('page-'+this.dataset.page);
    if(page) page.classList.add('active');
    document.getElementById('pageTitle').textContent = pageTitles[this.dataset.page]||'';
    document.getElementById('statsRow').style.display = monitoringPages[this.dataset.page]?'':'none';
    if(this.dataset.page==='alerts') _lastAlertViewTime = Date.now();
    if(this.dataset.page==='trajectory') loadTrajectories();
    if(this.dataset.page==='powerlines') loadPowerLines();
    if(this.dataset.page==='stations') loadStations();
    if(this.dataset.page==='users') loadUsers();
    if(this.dataset.page==='personnel') loadPersonnel();
    if(this.dataset.page==='whitelist') loadWhitelist();
    if(this.dataset.page==='devices') loadDevices();
    if(this.dataset.page==='licenses') openLicPage();
    if(this.dataset.page==='audit') openAuditPage();
    if(this.dataset.page==='settings') loadSettings();
    if(this.dataset.page==='profile') loadProfile();
  });
});

// ═══════════ Click-outside-to-close for modals ═══════════
document.addEventListener('click', function(e) {
  var modalCloseMap = {
    'plModal': closePlModal, 'stModal': closeStModal,
    'usrModal': closeUsrModal, 'psModal': closePsModal,
    'wlModal': closeWlModal, 'devModal': closeDevModal, 'licModal': closeLicModal
  };
  if (e.target.id && modalCloseMap[e.target.id]) {
    modalCloseMap[e.target.id]();
  }
});

// ═══════════ Notification ═══════════
function requestNotify(){
  if(window.Notification && Notification.permission==='default') Notification.requestPermission();
}
document.addEventListener('click', requestNotify, {once:true});

function notifyAlert(droneId, level, distance, lineName){
  if(!window.Notification || Notification.permission!=='granted') return;
  var labels = {critical:'危险', severe:'严重', warning:'警告'};
  new Notification('['+ (labels[level]||level) +'] '+droneId, {
    body: '距离 '+lineName+' '+distance.toFixed(0)+'m',
    icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" fill="white"><path d="M340.65 809.17a138.26 138.26 0 1 1-114.43-114.43 330 330 0 0 1 40.41-46.06 193.1 193.1 0 0 0-198.82 46.09c-75.21 75.21-75.21 197.59 0 272.81s197.6 75.22 272.83 0a193.1 193.1 0 0 0 46.1-198.75c-14.72 11.99-30.17 25.49-46.09 40.34zM764.81 641.69a330 330 0 0 1 39.8 46.32 138.27 138.27 0 1 1-114.77 114.84c-15.99-14.63-31.47-27.96-46.33-39.76a193.1 193.1 0 0 0 46.33 196.8c75.22 75.22 197.62 75.22 272.83 0s75.22-197.6 0-272.83a193.1 193.1 0 0 0-197.86-46.37zM692.82 227.86a138.27 138.27 0 1 1 114.7 114.67c-15.25 16.52-28.54 31.93-40.05 46.23a193.1 193.1 0 0 0 198.23-46.27c75.22-75.22 75.22-197.6 0-272.83s-197.62-75.22-272.83 0a193.1 193.1 0 0 0-46.24 198.33c13.95-11.26 29.32-24.53 46.19-40.13zM258.29 374.94a330 330 0 0 1-41.12-45.77 138.26 138.26 0 1 1 113.83-113.79c15.65 14.9 31 28.61 45.77 41.12a193.1 193.1 0 0 0-45.69-200.09c-75.18-75.22-197.6-75.22-272.78 0s-75.22 197.6 0 272.83a193.18 193.18 0 0 0 199.99 45.7zM518.34 460.18a56.33 56.33 0 1 0 39.91 16.49 56.01 56.01 0 0 0-39.91-16.49zM787.95 845.34c3.2 3.42 11.06 12.32 12.7 13.95l.82.79a8 8 0 0 0 1.43 1.3c19.2 17.26 46.82 18.59 62.94 2.42 15.13-15.13 14.95-40.39.61-59.32a170 170 0 0 0-12.24-12.17c-1.59-1.34-2.86-2.52-3.48-3-44.13-40.88-188.14-180.73-185.3-262.66 0-3.42 0-17.78 0-21.93-.4-82.2 141.6-220.08 185.35-260.61.54-.49 1.89-1.66 3.48-3.02a167 167 0 0 0 12.24-12.16c14.3-18.92 14.52-44.18-.62-59.32-16.12-16.12-43.74-14.85-62.94 2.42a8 8 0 0 0-1.43 1.3l-.81.77c-1.65 1.64-9.5 10.54-12.7 13.97-43.07 46.13-170.6 175.53-251.23 181.66-6.35.49-28.06.39-33.37.17-82.55-3.25-217.42-142.11-257.35-185.29-.5-.54-1.66-1.89-3.02-3.48a164 164 0 0 0-12.16-12.24c-18.92-14.3-44.18-14.52-59.32.6-16.12 16.13-14.86 43.75 2.42 62.94a8.7 8.7 0 0 0 1.3 1.43l.77.8c1.65 1.66 10.54 9.5 13.96 12.7 46.3 45.98 170.22 168.06 182.27 248.9 1.3 8.7 1.22 44.21-1.12 55.23-17.16 80.73-136 197.73-179.81 238.63-3.42 3.22-12.32 11.06-13.96 12.7l-.77.82a8 8 0 0 0-1.3 1.43c-17.28 19.2-18.59 46.82-2.42 62.94 15.13 15.13 40.39 14.96 59.32.61a167 167 0 0 0 12.16-12.24c1.36-1.59 2.52-2.86 3.03-3.48 46.51-43.97 175.93-177.35 258.85-186.66 7.48-.84 33.96-.82 40.87-.16 80.71 7.45 206.9 135.54 249.7 181.31zM580.25 578.41a87.55 87.55 0 1 1 0-123.81 86.98 86.98 0 0 1 0 123.81z"/></svg>',
    tag: droneId,
  });
}

// ═══════════ SVG drone icon ═══════════
function droneSvg(status){
  var colors={active:'#67c23a',warning:'#e6a23c',severe:'#f56c6c',critical:'#f56c6c',gone:'#c0c4cc'};
  var c=colors[status]||colors.active;
  return '<svg class="drone-svg '+status+'" width="16" height="16" viewBox="0 0 1024 1024"><path d="M340.65 809.17a138.26 138.26 0 1 1-114.43-114.43 330 330 0 0 1 40.41-46.06 193.1 193.1 0 0 0-198.82 46.09c-75.21 75.21-75.21 197.59 0 272.81s197.6 75.22 272.83 0a193.1 193.1 0 0 0 46.1-198.75c-14.72 11.99-30.17 25.49-46.09 40.34zM764.81 641.69a330 330 0 0 1 39.8 46.32 138.27 138.27 0 1 1-114.77 114.84c-15.99-14.63-31.47-27.96-46.33-39.76a193.1 193.1 0 0 0 46.33 196.8c75.22 75.22 197.62 75.22 272.83 0s75.22-197.6 0-272.83a193.1 193.1 0 0 0-197.86-46.37zM692.82 227.86a138.27 138.27 0 1 1 114.7 114.67c-15.25 16.52-28.54 31.93-40.05 46.23a193.1 193.1 0 0 0 198.23-46.27c75.22-75.22 75.22-197.6 0-272.83s-197.62-75.22-272.83 0a193.1 193.1 0 0 0-46.24 198.33c13.95-11.26 29.32-24.53 46.19-40.13zM258.29 374.94a330 330 0 0 1-41.12-45.77 138.26 138.26 0 1 1 113.83-113.79c15.65 14.9 31 28.61 45.77 41.12a193.1 193.1 0 0 0-45.69-200.09c-75.18-75.22-197.6-75.22-272.78 0s-75.22 197.6 0 272.83a193.18 193.18 0 0 0 199.99 45.7zM518.34 460.18a56.33 56.33 0 1 0 39.91 16.49 56.01 56.01 0 0 0-39.91-16.49zM787.95 845.34c3.2 3.42 11.06 12.32 12.7 13.95l.82.79a8 8 0 0 0 1.43 1.3c19.2 17.26 46.82 18.59 62.94 2.42 15.13-15.13 14.95-40.39.61-59.32a170 170 0 0 0-12.24-12.17c-1.59-1.34-2.86-2.52-3.48-3-44.13-40.88-188.14-180.73-185.3-262.66 0-3.42 0-17.78 0-21.93-.4-82.2 141.6-220.08 185.35-260.61.54-.49 1.89-1.66 3.48-3.02a167 167 0 0 0 12.24-12.16c14.3-18.92 14.52-44.18-.62-59.32-16.12-16.12-43.74-14.85-62.94 2.42a8 8 0 0 0-1.43 1.3l-.81.77c-1.65 1.64-9.5 10.54-12.7 13.97-43.07 46.13-170.6 175.53-251.23 181.66-6.35.49-28.06.39-33.37.17-82.55-3.25-217.42-142.11-257.35-185.29-.5-.54-1.66-1.89-3.02-3.48a164 164 0 0 0-12.16-12.24c-18.92-14.3-44.18-14.52-59.32.6-16.12 16.13-14.86 43.75 2.42 62.94a8.7 8.7 0 0 0 1.3 1.43l.77.8c1.65 1.66 10.54 9.5 13.96 12.7 46.3 45.98 170.22 168.06 182.27 248.9 1.3 8.7 1.22 44.21-1.12 55.23-17.16 80.73-136 197.73-179.81 238.63-3.42 3.22-12.32 11.06-13.96 12.7l-.77.82a8 8 0 0 0-1.3 1.43c-17.28 19.2-18.59 46.82-2.42 62.94 15.13 15.13 40.39 14.96 59.32.61a167 167 0 0 0 12.16-12.24c1.36-1.59 2.52-2.86 3.03-3.48 46.51-43.97 175.93-177.35 258.85-186.66 7.48-.84 33.96-.82 40.87-.16 80.71 7.45 206.9 135.54 249.7 181.31zM580.25 578.41a87.55 87.55 0 1 1 0-123.81 86.98 86.98 0 0 1 0 123.81z" fill="'+c+'"/></svg>';
}

// ═══════════ Main poll (with AbortController) ═══════════
var _updateUICtrl = null;
window.updateUI = function(){
  if(_updateUICtrl) _updateUICtrl.abort();
  _updateUICtrl = new AbortController();
  fetch('/api/status', {signal: _updateUICtrl.signal}).then(function(r){return r.json()}).then(function(d){
    // Comms status
    var bh=d.backhaul;
    if(bh){
      var online = bh.mqtt_online || bh.primary_online || false;
      document.getElementById('comm4gDot').className='comm-dot '+(online?'online':'');
      document.getElementById('commLabel').textContent=bh.channel==='4g_wired'?'4G/有线':bh.channel==='beidou_emergency'?'北斗应急':(online?'MQTT 在线':(bh.mqtt_online===false?'MQTT 离线':'通信中断'));
      document.getElementById('commQueue').style.display=bh.queue_size>0?'':'none';
      if(bh.queue_size>0) document.getElementById('commQueue').textContent='积压 '+bh.queue_size;
    }
    // Current user + RBAC
    if(d.current_user){
      currentUser=d.current_user;
      document.getElementById('sidebarAvatar').textContent=(currentUser.username||'U')[0].toUpperCase();
      document.getElementById('sidebarUser').textContent=currentUser.username||'--';
      document.getElementById('sidebarRole').textContent={admin:'系统管理员',tenant_admin:'租户管理员',user:'站点用户'}[currentUser.role]||currentUser.role||'--';
      var isAdmin = currentUser.role==='admin';
      document.getElementById('navUsers').style.display=isAdmin?'':'none';
      document.getElementById('navStations').style.display=(isAdmin||currentUser.role==='tenant_admin')?'':'none';
      document.getElementById('navPersonnel').style.display=(isAdmin||currentUser.role==='tenant_admin')?'':'none';
      document.getElementById('navWhitelist').style.display=(isAdmin||currentUser.role==='tenant_admin')?'':'none';
      document.getElementById('navDevices').style.display=(isAdmin||currentUser.role==='tenant_admin')?'':'none';
      document.getElementById('navLicenses').style.display=isAdmin?'':'none';
      document.getElementById('navAudit').style.display=isAdmin?'':'none';
      document.getElementById('navSettings').style.display=isAdmin?'':'none';
      refreshTenantInfo();
    }
    // Stats
    var warn=0,sev=0,crit=0;
    (d.drones||[]).forEach(function(dr){var s=dr.status;if(s==='warning')warn++;if(s==='severe')sev++;if(s==='critical')crit++;});
    document.getElementById('statDrones').textContent=(d.drones||[]).length;
    document.getElementById('statWarn').textContent=warn;
    document.getElementById('statSev').textContent=sev;
    document.getElementById('statCrit').textContent=crit;
    document.getElementById('droneCountPill').textContent=(d.drones||[]).length;
    // Alert badge — 只统计上次查看后的新告警
    var newAlerts = (d.alerts||[]).filter(function(a){
      return new Date(a.time).getTime() > _lastAlertViewTime;
    });
    var ac = newAlerts.length;
    var ab = document.getElementById('alertBadge');
    if(ac>0){ab.style.display='';ab.textContent=ac;}else{ab.style.display='none';}
    // 概览页和告警页都展示了告警内容，视为已读
    var activeNav = document.querySelector('.nav-item.active');
    if(activeNav){
      var ap = activeNav.dataset.page;
      if(ap==='overview'||ap==='alerts') _lastAlertViewTime = Date.now();
    }
    // Alert transitions
    (d.drones||[]).forEach(function(dr){
      var prev=prevAlertLevels[dr.id], cur=dr.status||'active';
      if(cur!==prev){
        if(cur==='critical'||cur==='severe') notifyAlert(dr.id, cur, dr.min_distance||0, dr.line_name||dr.nearest_line||'?');
        prevAlertLevels[dr.id]=cur;
      }
    });
    lastDrones = d.drones||[];
    window._alpineDrones = lastDrones;
    updateDroneTable();
    updateLogPanel(d.alerts||[]);
    document.getElementById('footerLeft').textContent='更新于 '+(d.server_time||d.now||'');
  }).catch(function(e){
    if(e.name!=='AbortError') console.error(e);
  });
};

// ═══════════ WebSocket real-time push (with polling fallback) ═══════════
var socket = null;
var wsEnabled = false;

function initSocket() {
  socket = io({transports:['websocket','polling'],reconnectionDelay:3000,reconnectionDelayMax:10000});
  socket.on('connect', function() {
    wsEnabled = true;
    console.log('WS connected');
  });
  socket.on('disconnect', function() {
    wsEnabled = false;
    console.log('WS disconnected, fallback to polling');
  });
  socket.on('drone_update', function(d) {
    if (!d || !d.drone_id) return;
    var found = false;
    for (var i = 0; i < lastDrones.length; i++) {
      if (lastDrones[i].id === d.drone_id) {
        lastDrones[i].last_lat = d.lat;
        lastDrones[i].last_lon = d.lon;
        lastDrones[i].last_alt = d.alt;
        lastDrones[i].min_distance = d.distance;
        lastDrones[i].line_name = d.nearest_line || d.line_name || '';
        lastDrones[i].status = d.status;
        if (d.device_name) lastDrones[i].device_name = d.device_name;
        if (d.last_seen) lastDrones[i].last_seen = d.last_seen;
        found = true; break;
      }
    }
    if (!found) {
      lastDrones.push({
        id: d.drone_id, last_lat: d.lat, last_lon: d.lon, last_alt: d.alt,
        min_distance: d.distance || 0, line_name: d.nearest_line || d.line_name || '',
        status: d.status, device_name: d.device_name || '', last_seen: d.last_seen || ''
      });
    }
    var prev = prevAlertLevels[d.drone_id], cur = d.status || 'active';
    if (cur !== prev) {
      if (cur === 'critical' || cur === 'severe') notifyAlert(d.drone_id, cur, d.distance || 0, d.nearest_line || d.line_name || '');
      prevAlertLevels[d.drone_id] = cur;
    }
    window._alpineDrones = lastDrones;
    updateDroneTable();
  });
  socket.on('alert_update', function(a) {
    if (!a) return;
    if (a.level === 'critical' || a.level === 'severe') {
      notifyAlert(a.drone_id, a.level, a.distance || 0, a.line_name || '');
    }
  });
}

function pollFallback() {
  if (wsEnabled) {
    fetch('/api/status').then(function(r) { return r.json(); }).then(function(d) {
      var warn = 0, sev = 0, crit = 0;
      (d.drones || []).forEach(function(dr) { var s = dr.status; if (s === 'warning') warn++; if (s === 'severe') sev++; if (s === 'critical') crit++; });
      document.getElementById('statDrones').textContent = (d.drones || []).length;
      document.getElementById('statWarn').textContent = warn;
      document.getElementById('statSev').textContent = sev;
      document.getElementById('statCrit').textContent = crit;
      document.getElementById('droneCountPill').textContent = (d.drones || []).length;
      document.getElementById('footerLeft').textContent = '更新于 ' + (d.server_time || d.now || '') + ' [WS]';
      var bh = d.backhaul;
      if (bh) {
        var online = bh.mqtt_online || bh.primary_online || false;
        document.getElementById('comm4gDot').className = 'comm-dot ' + (online ? 'online' : '');
        document.getElementById('commLabel').textContent = bh.channel === '4g_wired' ? '4G/有线' : bh.channel === 'beidou_emergency' ? '北斗应急' : (online ? 'MQTT 在线' : (bh.mqtt_online === false ? 'MQTT 离线' : '通信中断'));
      }
    }).catch(function(e) {
      if (e.name !== 'AbortError') console.warn('pollFallback:', e);
    });
  } else {
    updateUI();
  }
}

window.updateDroneTable = function(){
  var searchTerm=(document.getElementById('droneSearch').value||'').toLowerCase();
  var statusFilter=document.getElementById('statusFilter').value;
  var drones=lastDrones;
  if(searchTerm){
    drones=drones.filter(function(dr){
      var id=(dr.id||'').toLowerCase(), model=(dr.model||'').toLowerCase();
      return id.includes(searchTerm)||model.includes(searchTerm);
    });
  }
  if(statusFilter) drones=drones.filter(function(dr){return (dr.status||'active')===statusFilter});
  var table=document.getElementById('droneTable'), empty=document.getElementById('emptyMsg');
  if(drones.length===0){
    table.innerHTML=''; empty.style.display='block';
  }else{
    empty.style.display='none';
    var tags={active:'tag-active',warning:'tag-warning',severe:'tag-severe',critical:'tag-critical',gone:'tag-gone'};
    var txts={active:'正常',warning:'警告',severe:'严重',critical:'危险',gone:'离线'};
    table.innerHTML=drones.map(function(dr){
      var s=dr.status||'active', rc=(s==='critical')?'row-critical':(s==='severe')?'row-severe':'';
      var dist=dr.min_distance!=null?dr.min_distance.toFixed(0)+' m':'-';
      var time=(dr.last_seen||'').substring(11,19);
      var id=(dr.id||'?'), e=UI.escapeHtml;
      return '<tr class="'+rc+'"><td class="mono" title="'+e(id)+'">'+droneSvg(s)+' '+(id.length>14?e(id.substring(0,14))+'...':e(id))+
        '</td><td style="font-weight:500">'+e(dr.model||'-')+'</td>'+
        '<td class="mono">'+(dr.last_lat||0).toFixed(5)+'</td>'+
        '<td class="mono">'+(dr.last_lon||0).toFixed(5)+'</td>'+
        '<td>'+(dr.last_alt||0).toFixed(0)+' m</td>'+
        '<td class="mono" style="font-weight:600">'+dist+'</td>'+
        '<td>'+e(dr.line_name||dr.nearest_line||'-')+'</td>'+
        '<td><span class="tag '+tags[s]+'">'+txts[s]+'</span></td>'+
        '<td class="mono">'+time+'</td>'+
        '<td><button class="btn btn-ghost btn-xs" data-fly-lat="'+dr.last_lat+'" data-fly-lon="'+dr.last_lon+'">定位</button></td></tr>';
    }).join('');
  }
};

// Delegate for drone table fly-to buttons
UI.delegate(document.getElementById('droneTable'), 'click', '[data-fly-lat]', function(){
  var lat = parseFloat(this.dataset.flyLat);
  var lon = parseFloat(this.dataset.flyLon);
  if(!isNaN(lat) && !isNaN(lon)) window.open('/?lat='+lat+'&lon='+lon+'&zoom=15', '_blank');
});

function updateLogPanel(alerts){
  var log=document.getElementById('logPanel');
  if(!alerts||alerts.length===0){
    log.innerHTML='<div class="empty-state"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M8 12h8"/></svg><div class="msg">暂无告警</div><div class="sub">无违规飞行事件</div></div>';
  }else{
    log.innerHTML=alerts.map(function(l){
      return '<div class="log-item log-'+(l.level||'info')+'"><span class="ts">'+(l.time||'')+'</span><span class="log-msg">'+UI.escapeHtml(l.msg||l.message||'')+'</span></div>';
    }).join('');
    log.scrollTop=log.scrollHeight;
  }
}

// ═══════════ Power Lines CRUD ═══════════
var TOWER_HEIGHTS = {'10kV':15,'35kV':18,'66kV':22,'110kV':25,'220kV':35,'330kV':40,'500kV':50,'750kV':60,'±800kV':65,'1000kV':80};
function estTowerHeight(vl){
  if(!vl) return 25;
  for(var k in TOWER_HEIGHTS){if(vl.indexOf(k)>=0) return TOWER_HEIGHTS[k];}
  return 25;
}
window.onVoltageChangeForPl = function(){
  var vl=document.getElementById('plVoltage').value;
  var h=estTowerHeight(vl);
  if(!document.getElementById('plTh1').value) document.getElementById('plTh1').value=h;
  if(!document.getElementById('plTh2').value) document.getElementById('plTh2').value=h;
  onPlFieldChange();
};
window.onPlFieldChange = function(){
  var alt1=parseFloat(document.getElementById('plAlt1').value)||0;
  var th1=parseFloat(document.getElementById('plTh1').value)||0;
  var alt2=parseFloat(document.getElementById('plAlt2').value)||0;
  var th2=parseFloat(document.getElementById('plTh2').value)||0;
  var hint=document.getElementById('plAltHint');
  if(th1||th2){hint.style.display='';hint.textContent='导线海拔: 端点1='+(alt1+th1).toFixed(1)+'m  端点2='+(alt2+th2).toFixed(1)+'m';}
  else{hint.style.display='none';}
};

window.openPlModal = function(){
  document.getElementById('plModal').classList.add('show');
};
window.closePlModal = function(){
  _resetPlForm();
  var csvEl = document.getElementById('plCsv');
  if(csvEl) csvEl.value='';
  var fnEl = document.getElementById('plFileName');
  if(fnEl) fnEl.textContent='';
  var fi = document.getElementById('plFileInput');
  if(fi) fi.value='';
  document.getElementById('plModal').classList.remove('show');
};

function _resetPlForm(){
  _editingPlId=null;
  document.getElementById('plName').value='';document.getElementById('plVoltage').value='';
  document.getElementById('plLat1').value='';document.getElementById('plLon1').value='';document.getElementById('plAlt1').value='0';
  document.getElementById('plLat2').value='';document.getElementById('plLon2').value='';document.getElementById('plAlt2').value='0';
  document.getElementById('plTh1').value='';document.getElementById('plTh2').value='';
  document.getElementById('plAltHint').style.display='none';
  document.getElementById('plSaveBtn').textContent='保存';
}

window.loadPowerLines = function(){
  Api.get('/api/powerlines').then(function(d){
    plData=d||[];
    var el=document.getElementById('plList');
    if(plData.length===0){
      el.innerHTML='<div class="empty-state"><div class="msg">暂无电力线</div><div class="sub">点击「新增电力线」添加</div></div>';
    }else{
      el.innerHTML=plData.map(function(l,i){
        return '<div class="crud-item"><div class="info"><b>'+UI.escapeHtml(l.name)+'</b><div class="meta">('+l.lat1.toFixed(4)+', '+l.lon1.toFixed(4)+') → ('+l.lat2.toFixed(4)+', '+l.lon2.toFixed(4)+') 导线:'+(l.alt1||0).toFixed(0)+'m'+(l.tower_height1?' (塔'+l.tower_height1.toFixed(0)+'m)':'')+' &nbsp;|&nbsp; '+UI.escapeHtml(l.voltage_level||'')+'</div></div><div class="actions"><button class="btn btn-ghost btn-xs" data-edit-pl="'+i+'">编辑</button><button class="del" data-del-pl="'+i+'">删除</button></div></div>';
      }).join('');
    }
  }).catch(catchErr('加载电力线失败'));
};
window.editPowerLine = function(idx){
  var l=plData[idx]; if(!l) return;
  _editingPlId=l.id;
  document.getElementById('plName').value=l.name;
  document.getElementById('plVoltage').value=l.voltage_level||'';
  document.getElementById('plLat1').value=l.lat1;document.getElementById('plLon1').value=l.lon1;
  document.getElementById('plAlt1').value=l.tower_height1!=null?(l.alt1-(l.tower_height1||0)).toFixed(1):(l.alt1||0);
  document.getElementById('plLat2').value=l.lat2;document.getElementById('plLon2').value=l.lon2;
  document.getElementById('plAlt2').value=l.tower_height2!=null?(l.alt2-(l.tower_height2||0)).toFixed(1):(l.alt2||0);
  document.getElementById('plTh1').value=l.tower_height1!=null?l.tower_height1:'';
  document.getElementById('plTh2').value=l.tower_height2!=null?l.tower_height2:'';
  document.getElementById('plSaveBtn').textContent='更新';
  onPlFieldChange();
  openPlModal();
};
window.addPowerLine = function(){
  var data={
    name:document.getElementById('plName').value.trim(),
    voltage_level:document.getElementById('plVoltage').value,
    lat1:parseFloat(document.getElementById('plLat1').value), lon1:parseFloat(document.getElementById('plLon1').value), alt1:parseFloat(document.getElementById('plAlt1').value)||0,
    lat2:parseFloat(document.getElementById('plLat2').value), lon2:parseFloat(document.getElementById('plLon2').value), alt2:parseFloat(document.getElementById('plAlt2').value)||0,
    tower_height1: parseFloat(document.getElementById('plTh1').value)||null,
    tower_height2: parseFloat(document.getElementById('plTh2').value)||null
  };
  if(!data.name){UI.Message.warning('电力线名称不能为空');return}
  if(isNaN(data.lat1)||isNaN(data.lon1)||isNaN(data.lat2)||isNaN(data.lon2)){UI.Message.warning('请填写有效的经纬度坐标');return}
  var method=_editingPlId?'PUT':'POST';
  var url=_editingPlId?'/api/powerlines/'+_editingPlId:'/api/powerlines';
  Api[method.toLowerCase()](url, data).then(function(){
    closePlModal(); loadPowerLines();
  });
};
window.delPowerLine = function(idx){
  UI.Message.confirm('确定删除此电力线？').then(function(ok){
    if(!ok) return;
    var lineId=plData[idx]&&plData[idx].id;
    if(!lineId) return;
    Api.del('/api/powerlines/'+lineId).then(function(){loadPowerLines()});
  });
};

// ═══════════ Region cascade helpers ═══════════
function _findProvince(name) { return regionData.find(function(p){ return p[0]===name; }); }
function _findCity(prov, name) { return prov[1].find(function(c){ return c[0]===name; }); }
function _popProvinceSelect(selId, placeholder) {
  var sel = document.getElementById(selId);
  sel.innerHTML = '<option value="">'+ (placeholder||'全部省') +'</option>';
  regionData.forEach(function(p){ sel.innerHTML += '<option value="'+p[0]+'">'+p[0]+'</option>'; });
}
function _onProvinceChange(provSelId, citySelId, countySelId, placeholder) {
  var prov = document.getElementById(provSelId).value;
  var citySel = document.getElementById(citySelId);
  var countySel = document.getElementById(countySelId);
  citySel.innerHTML = '<option value="">'+ (placeholder||'全部市') +'</option>';
  countySel.innerHTML = '<option value="">全部区/县</option>';
  if (!prov) return;
  var p = _findProvince(prov);
  if (!p) return;
  p[1].forEach(function(c){ citySel.innerHTML += '<option value="'+c[0]+'">'+c[0]+'</option>'; });
}
function _onCityChange(provSelId, citySelId, countySelId) {
  var prov = document.getElementById(provSelId).value;
  var city = document.getElementById(citySelId).value;
  var countySel = document.getElementById(countySelId);
  countySel.innerHTML = '<option value="">全部区/县</option>';
  if (!prov || !city) return;
  var p = _findProvince(prov);
  if (!p) return;
  var c = _findCity(p, city);
  if (!c) return;
  c[1].forEach(function(x){ countySel.innerHTML += '<option value="'+x+'">'+x+'</option>'; });
}
// Set region selects to specific values (for edit mode)
function _setRegionValues(province, city, county) {
  document.getElementById('stProvince').value = province || '';
  if (province) {
    _onProvinceChange('stProvince', 'stCity', 'stCounty', '选择市');
    document.getElementById('stCity').value = city || '';
    if (city) {
      _onCityChange('stProvince', 'stCity', 'stCounty');
      document.getElementById('stCounty').value = county || '';
    }
  }
}

// ═══════════ Geocode helpers ═══════════
let _geocodeInFlight = false;

function _stripRegionSuffix(name) {
  var suffixes = ['省','市','区','县','自治州','自治县','自治旗','特区','林区',
                  '特别行政区','自治区','盟','地区','哈萨克自治州',
                  '回族自治区','维吾尔自治区','壮族自治区'];
  for (var i = 0; i < suffixes.length; i++) {
    var s = suffixes[i];
    if (name.length > s.length && name.slice(-s.length) === s) {
      return name.slice(0, -s.length);
    }
  }
  return name;
}

function _matchGeocodeName(name, level, provinceName, cityName) {
  if (!name) return '';
  var candidates = [];
  var norm = _stripRegionSuffix(name);

  if (level === 'province') {
    candidates = regionData.map(function(p) { return p[0]; });
  } else if (level === 'city') {
    var pData = _findProvince(provinceName || name);
    if (pData) {
      candidates = pData[1].map(function(c) { return c[0]; });
    }
    if (candidates.length === 0) {
      regionData.forEach(function(p) {
        p[1].forEach(function(c) { candidates.push(c[0]); });
      });
    }
  } else if (level === 'county') {
    var pData2 = _findProvince(provinceName || '');
    if (pData2) {
      if (cityName) {
        var cData = _findCity(pData2, cityName);
        if (cData) candidates = cData[1];
      }
      if (candidates.length === 0) {
        pData2[1].forEach(function(c) {
          c[1].forEach(function(x) { candidates.push(x); });
        });
      }
    }
  }

  for (var i = 0; i < candidates.length; i++) {
    if (candidates[i] === name) return candidates[i];
  }
  for (var i = 0; i < candidates.length; i++) {
    if (_stripRegionSuffix(candidates[i]) === norm) return candidates[i];
  }
  for (var i = 0; i < candidates.length; i++) {
    if (candidates[i].indexOf(name) === 0) return candidates[i];
  }
  if (norm.length > 1) {
    for (var i = 0; i < candidates.length; i++) {
      if (candidates[i].indexOf(norm) >= 0) return candidates[i];
    }
  }
  return name;
}

function _doGeocode(lat, lon) {
  if (!lat || !lon) return;
  if (_geocodeInFlight) return;
  var btn = document.getElementById('stGeocodeBtn');
  if (btn) { btn.disabled = true; btn.textContent = '获取中...'; }
  _geocodeInFlight = true;
  Api.post('/api/geocode', {lat: parseFloat(lat), lon: parseFloat(lon)})
    .then(function(data) {
      if (data.error) { UI.toast(data.error, 'error'); return; }
      var prov = _matchGeocodeName(data.province || '', 'province');
      var city = data.city ? _matchGeocodeName(data.city, 'city', prov || data.province) : '';
      var county = data.county ? _matchGeocodeName(data.county, 'county', prov || data.province, city || data.city) : '';
      if (prov) {
        _setRegionValues(prov, city, county);
        var label = [prov, city, county].filter(Boolean).join(' ');
        UI.toast('已获取位置: ' + label);
      } else {
        UI.toast('未能匹配到行政区划', 'warn');
      }
    })
    .catch(function(err) {
      UI.toast('位置获取失败: ' + (err.message || '网络错误'), 'error');
    })
    .finally(function() {
      _geocodeInFlight = false;
      if (btn) { btn.disabled = false; btn.textContent = '从坐标获取位置'; }
    });
}

window.doGeocode = function() {
  var lat = document.getElementById('stLat').value;
  var lon = document.getElementById('stLon').value;
  if (!lat || !lon) { UI.toast('请先输入经纬度坐标', 'warn'); return; }
  _doGeocode(lat, lon);
};

// Auto-trigger geocode on lat/lon blur
document.addEventListener('focusout', function(e) {
  if (e.target.id === 'stLat' || e.target.id === 'stLon') {
    var lat = document.getElementById('stLat').value;
    var lon = document.getElementById('stLon').value;
    var modal = document.getElementById('stModal');
    if (lat && lon && modal && modal.classList.contains('show')) {
      _doGeocode(lat, lon);
    }
  }
});

// ═══════════ Stations CRUD ═══════════
function _populateDeviceSelect(){
  Api.get('/api/devices').then(function(devices){
    var sel = document.getElementById('stDevice');
    sel.innerHTML = '<option value="">关联设备…</option>';
    (devices||[]).forEach(function(d){
      if(!d.revoked) sel.innerHTML += '<option value="'+UI.escapeAttr(d.device_name)+'">'+UI.escapeHtml(d.device_name)+(d.station?' ('+UI.escapeHtml(d.station)+')':'')+'</option>';
    });
  }).catch(function(){});
}

window.openStModal = function(){
  _popProvinceSelect('stProvince', '选择省');
  _populateDeviceSelect();
  document.getElementById('stModal').classList.add('show');
};
window.closeStModal = function(){
  _resetStForm2();
  document.getElementById('stModal').classList.remove('show');
};

function _resetStForm2(){
  _editingStName2=null;
  document.getElementById('stName').value='';document.getElementById('stName').readOnly=false;
  document.getElementById('stDevice').value='';
  document.getElementById('stLocation').value='';
  _popProvinceSelect('stProvince', '选择省');
  document.getElementById('stCity').innerHTML='<option value="">选择市</option>';
  document.getElementById('stCounty').innerHTML='<option value="">选择区/县</option>';
  document.getElementById('stLat').value='';document.getElementById('stLon').value='';document.getElementById('stAlt').value='';
  document.getElementById('stSaveBtn').textContent='保存';
  _populateDeviceSelect();
}

function _stLocationLabel(s){
  var parts=[];
  if(s.province) parts.push(s.province);
  if(s.city) parts.push(s.city);
  if(s.county) parts.push(s.county);
  return parts.length?parts.join(' '):(s.location||'');
}
window.filterStationList = function(){
  var prov=(document.getElementById('stFilterProv').value||'').trim().toLowerCase();
  var city=(document.getElementById('stFilterCity').value||'').trim().toLowerCase();
  var county=(document.getElementById('stFilterCounty').value||'').trim().toLowerCase();
  var name=(document.getElementById('stFilterName').value||'').trim().toLowerCase();
  var filtered=_allStations.filter(function(s){
    if(prov && (s.province||'').toLowerCase().indexOf(prov)<0) return false;
    if(city && (s.city||'').toLowerCase().indexOf(city)<0) return false;
    if(county && (s.county||'').toLowerCase().indexOf(county)<0) return false;
    if(name && s.name.toLowerCase().indexOf(name)<0 && (s.location||'').toLowerCase().indexOf(name)<0) return false;
    return true;
  });
  renderStationList(filtered);
};
window.loadStations = function(){
  _popProvinceSelect('stFilterProv', '全部省');
  document.getElementById('stFilterCity').innerHTML='<option value="">全部市</option>';
  document.getElementById('stFilterCounty').innerHTML='<option value="">全部区/县</option>';
  Api.get('/api/stations').then(function(d){
    _allStations=d||[];
    renderStationList(_allStations);
  }).catch(catchErr('加载站点失败'));
};
// Filter cascade listeners
document.addEventListener('change', function(e){
  var t=e.target;
  if(t.id==='stFilterProv'){ _onProvinceChange('stFilterProv','stFilterCity','stFilterCounty','全部市'); filterStationList(); }
  else if(t.id==='stFilterCity'){ _onCityChange('stFilterProv','stFilterCity','stFilterCounty'); filterStationList(); }
  else if(t.id==='stFilterCounty'){ filterStationList(); }
  else if(t.id==='stProvince'){ _onProvinceChange('stProvince','stCity','stCounty','选择市'); }
  else if(t.id==='stCity'){ _onCityChange('stProvince','stCity','stCounty'); }
});
function renderStationList(list){
  var el=document.getElementById('stList');
  if(!list.length){
    el.innerHTML='<div class="empty-state"><div class="msg">暂无站点</div><div class="sub">点击「新增站点」添加</div></div>';
  }else{
    el.innerHTML=list.map(function(s,i){
      var loc=_stLocationLabel(s);
      return '<div class="crud-item"><div class="info"><b>'+UI.escapeHtml(s.name)+'</b><div class="meta">'+(loc?loc+' &nbsp;|&nbsp; ':'')+'设备: '+UI.escapeHtml(s.device_name||'-')+' &nbsp;|&nbsp; 坐标: ('+(s.lat||0).toFixed(4)+', '+(s.lon||0).toFixed(4)+')</div></div><div class="actions"><button class="btn btn-ghost btn-xs" data-edit-st="'+UI.escapeAttr(s.name)+'">编辑</button><button class="del" data-del-st="'+UI.escapeAttr(s.name)+'">删除</button></div></div>';
    }).join('');
  }
}
window.editStation2 = function(name){
  var s=_allStations.find(function(x){return x.name===name}); if(!s) return;
  _editingStName2=s.name;
  document.getElementById('stName').value=s.name;
  document.getElementById('stName').readOnly=true;
  _populateDeviceSelect();
  document.getElementById('stDevice').value=s.device_name||'';
  document.getElementById('stLocation').value=s.location||'';
  _popProvinceSelect('stProvince', '选择省');
  _setRegionValues(s.province||'', s.city||'', s.county||'');
  document.getElementById('stLat').value=s.lat||0;
  document.getElementById('stLon').value=s.lon||0;
  document.getElementById('stAlt').value=s.alt||0;
  document.getElementById('stSaveBtn').textContent='更新';
  openStModal();
};
window.addStation = function(){
  var devSel = document.getElementById('stDevice');
  var data={
    name:document.getElementById('stName').value.trim(),
    device_name: devSel ? devSel.value.trim() : '',
    location:document.getElementById('stLocation').value.trim(),
    province:document.getElementById('stProvince').value.trim(),
    city:document.getElementById('stCity').value.trim(),
    county:document.getElementById('stCounty').value.trim(),
    lat:parseFloat(document.getElementById('stLat').value)||0,
    lon:parseFloat(document.getElementById('stLon').value)||0,
    alt:parseFloat(document.getElementById('stAlt').value)||0
  };
  if(!data.name){UI.Message.warning('站点名称不能为空');return}
  var method=_editingStName2?'PUT':'POST';
  Api[method.toLowerCase()]('/api/stations', data).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    document.getElementById('stName').readOnly=false;
    closeStModal(); loadStations();
  });
};
window.delStation = function(name){
  UI.Message.confirm('确定删除站点 '+name+'？').then(function(ok){
    if(!ok) return;
    Api.del('/api/stations', {name:name}).then(function(){loadStations()});
  });
};

// ═══════════ Users CRUD ═══════════
function _populateStationSelect(selId){
  Api.get('/api/stations').then(function(stations){
    var sel = document.getElementById(selId);
    sel.innerHTML = '<option value="">全部站点</option>';
    (stations||[]).forEach(function(s){
      sel.innerHTML += '<option value="'+UI.escapeAttr(s.name)+'">'+UI.escapeHtml(s.name)+(s.location?' ('+UI.escapeHtml(s.location)+')':'')+'</option>';
    });
  }).catch(function(){});
}

window.openUsrModal = function(){
  _populateStationSelect('uStation');
  document.getElementById('usrModal').classList.add('show');
};
window.closeUsrModal = function(){
  _resetUserForm2();
  document.getElementById('usrModal').classList.remove('show');
};

function _resetUserForm2(){
  _editingUsername2=null;
  document.getElementById('uName').value='';document.getElementById('uName').readOnly=false;
  document.getElementById('uPwd').value='';document.getElementById('uPwd').placeholder='密码';
  document.getElementById('uRole').value='user';
  document.getElementById('uScope').value='station';
  var uSel = document.getElementById('uStation');
  if(uSel) uSel.value='';
  document.getElementById('uSaveBtn').textContent='保存';
}

window.loadUsers = function(){
  Api.get('/api/users').then(function(d){
    userData=Array.isArray(d)?d:(d.users||[]);
    var el=document.getElementById('userList');
    if(userData.length===0){
      el.innerHTML='<div class="empty-state"><div class="msg">暂无用户</div><div class="sub">点击「新增用户」添加</div></div>';
    }else{
      el.innerHTML=userData.map(function(u,i){
        return '<div class="crud-item"><div class="info"><b>'+UI.escapeHtml(u.username)+'</b><div class="meta">角色: '+UI.escapeHtml(u.role)+' &nbsp;|&nbsp; 站点: '+UI.escapeHtml(u.assigned_station||u.station||'-')+'</div></div><div class="actions"><button class="btn btn-ghost btn-xs" data-reset-pw="'+UI.escapeAttr(u.username)+'">重置</button><button class="btn btn-ghost btn-xs" data-edit-user="'+i+'">编辑</button><button class="del" data-del-user="'+UI.escapeAttr(u.username)+'">删除</button></div></div>';
      }).join('');
    }
  }).catch(catchErr('加载用户列表失败'));
};
window.editUser2 = function(idx){
  var u=userData[idx]; if(!u) return;
  _editingUsername2=u.username;
  document.getElementById('uName').value=u.username;
  document.getElementById('uName').readOnly=true;
  document.getElementById('uPwd').value='';document.getElementById('uPwd').placeholder='留空则不改密码';
  document.getElementById('uRole').value=u.role||'user';
  document.getElementById('uScope').value=u.scope||'station';
  _populateStationSelect('uStation');
  document.getElementById('uStation').value=u.assigned_station||u.station||'';
  document.getElementById('uSaveBtn').textContent='更新';
  openUsrModal();
};
window.addUser = function(){
  var uSel = document.getElementById('uStation');
  var data={username:document.getElementById('uName').value, password:document.getElementById('uPwd').value, role:document.getElementById('uRole').value, scope:document.getElementById('uScope').value, station: uSel ? uSel.value : ''};
  if(!data.username){UI.Message.warning('用户名不能为空');return}
  if(!_editingUsername2&&!data.password){UI.Message.warning('密码不能为空');return}
  var method=_editingUsername2?'PUT':'POST';
  Api[method.toLowerCase()]('/api/users', data).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    closeUsrModal(); loadUsers();
  });
};
window.resetUserPw = function(username){
  UI.Message.confirm('确定为 '+username+' 重置密码吗？新密码将设为随机生成。').then(function(ok){
    if(!ok) return;
    var newPw = Array.from(crypto.getRandomValues(new Uint8Array(6))).map(function(b){return b%36<10?String.fromCharCode(48+b%36):String.fromCharCode(65+b%36-10)}).join('');
    Api.post('/api/users/'+encodeURIComponent(username)+'/reset-password', {new_password:newPw}).then(function(r){
      if(r.error){UI.toast(r.error,'error');return}
      UI.Message.success(username+' 密码已重置为: '+newPw);
    }).catch(catchErr('重置密码失败'));
  });
};
window.delUser = function(username){
  UI.Message.confirm('确定删除用户 '+username+'？').then(function(ok){
    if(!ok) return;
    Api.del('/api/users', {username:username}).then(function(){loadUsers()});
  });
};
window.resetUserPwd = function(){
  UI.Message.confirm('此操作将随机生成新密码，确定继续？').then(function(ok){
    if(!ok) return;
    var u=document.getElementById('fUserName')?document.getElementById('fUserName').value.trim():'';
    if(!u){UI.Message.warning('请先输入用户名');return}
    var chars='ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789';
    var newPw=''; for(var i=0;i<10;i++) newPw+=chars[Math.floor(Math.random()*chars.length)];
    Api.post('/api/users/'+encodeURIComponent(u)+'/reset-password', {new_password:newPw}).then(function(r){
      if(r.error){UI.toast(r.error,'error');return}
      UI.Message.success(u+' 密码已重置为: '+newPw);
    });
  });
};

// ═══════════ Personnel CRUD ═══════════
window.openPsModal = function(){
  document.getElementById('psModal').classList.add('show');
  Api.get('/api/stations').then(function(stations){
    var sel=document.getElementById('psStation');
    sel.innerHTML='<option value="">选择关联站点</option>'+stations.map(function(s){
      return '<option value="'+UI.escapeAttr(s.name)+'">'+UI.escapeHtml(s.name)+(s.location?' ('+UI.escapeHtml(s.location)+')':'')+'</option>';
    }).join('');
  }).catch(function(){});
};
window.closePsModal = function(){
  document.getElementById('psName').value='';
  document.getElementById('psPhone').value='';
  document.getElementById('psStation').value='';
  document.getElementById('psModal').classList.remove('show');
};

window.loadPersonnel = function(){
  Api.get('/api/personnel').then(function(d){
    psData=Array.isArray(d)?d:[];
    var el=document.getElementById('psList');
    if(psData.length===0){
      el.innerHTML='<div class="empty-state"><div class="msg">暂无联系人</div><div class="sub">点击「新增联系人」添加</div></div>';
    }else{
      el.innerHTML=psData.map(function(p,i){
        return '<div class="crud-item"><div class="info"><b>'+UI.escapeHtml(p.name)+'</b><div class="meta">手机: '+UI.escapeHtml(p.phone)+' &nbsp;|&nbsp; 站点: '+UI.escapeHtml(p.station_name||'-')+'</div></div><div class="actions"><button class="del" data-del-ps="'+i+'">删除</button></div></div>';
      }).join('');
    }
  }).catch(catchErr('加载人员列表失败'));
};
window.addPerson = function(){
  var sel=document.getElementById('psStation');
  var data={name:document.getElementById('psName').value, phone:document.getElementById('psPhone').value, station_name:sel.value};
  if(!data.name||!data.phone||!data.station_name){UI.Message.warning('请完整填写姓名、联系电话并选择关联站点');return}
  if(!/^1\d{10}$/.test(data.phone)){UI.Message.warning('联系电话格式无效，需为11位手机号');return}
  Api.post('/api/personnel', data).then(function(){
    closePsModal(); loadPersonnel();
  }).catch(catchErr('加载人员列表失败'));
};
window.delPerson = function(idx){
  UI.Message.confirm('确定删除此联系人？').then(function(ok){
    if(!ok) return;
    var p=psData[idx]; if(!p||!p.id) return;
    Api.del('/api/personnel', {id:p.id}).then(function(){loadPersonnel()});
  });
};

// ═══════════ Whitelist CRUD ═══════════
window.openWlModal = function(){
  document.getElementById('wlModal').classList.add('show');
};
window.closeWlModal = function(){
  document.getElementById('wlSn').value='';
  document.getElementById('wlNote').value='';
  document.getElementById('wlModal').classList.remove('show');
};

window.loadWhitelist = function(){
  Api.get('/api/whitelist').then(function(d){
    wlData=d||[];
    var el=document.getElementById('wlList');
    if(wlData.length===0){
      el.innerHTML='<div class="empty-state"><div class="msg">暂无白名单</div><div class="sub">白名单中的无人机 SN 不会触发告警</div></div>';
    }else{
      el.innerHTML=wlData.map(function(w){
        return '<div class="crud-item"><div class="info"><b>'+UI.escapeHtml(w.sn)+'</b><div class="meta">匹配: '+(w.match_mode==='prefix'?'前缀':'精确')+' &nbsp;|&nbsp; 备注: '+UI.escapeHtml(w.note||'无')+' &nbsp;|&nbsp; '+UI.escapeHtml(w.created_by)+' @ '+(w.created_at||'').slice(0,10)+'</div></div><div class="actions"><button class="del" data-del-wl="'+w.id+'">删除</button></div></div>';
      }).join('');
    }
  });
};
window.addWhitelist = function(){
  var data={
    sn:document.getElementById('wlSn').value.trim(),
    match_mode:document.getElementById('wlMode').value,
    note:document.getElementById('wlNote').value.trim()
  };
  if(!data.sn){UI.Message.warning('SN 不能为空');return}
  Api.post('/api/whitelist', data).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    closeWlModal(); loadWhitelist();
  });
};
window.delWhitelist = function(id){
  UI.Message.confirm('确定移除此白名单？').then(function(ok){
    if(!ok) return;
    Api.del('/api/whitelist', {id:id}).then(function(){loadWhitelist()});
  });
};

// ═══════════ Profile ═══════════
window.loadProfile = function(){
  Api.get('/api/status').then(function(d){
    var u=d.current_user||{};
    document.getElementById('profAvatar').textContent=(u.username||'U')[0].toUpperCase();
    document.getElementById('profUser').textContent=u.username||'--';
    document.getElementById('profRole').textContent={admin:'系统管理员',tenant_admin:'租户管理员',user:'站点用户'}[u.role]||u.role||'--';
    Api.get('/api/tenant/info').then(function(t){
      document.getElementById('profTenant').textContent=t.name||'--';
    }).catch(function(){});
    document.getElementById('profFields').innerHTML=[
      ['用户名',UI.escapeHtml(u.username||'-')],['角色',UI.escapeHtml({admin:'系统管理员',tenant_admin:'租户管理员',user:'站点用户'}[u.role]||u.role||'-')],
      ['管辖站点',UI.escapeHtml(u.assigned_station||u.station||(u.scope==='tenant'?'全部站点':'-'))],
      ['租户 ID',UI.escapeHtml(u.tenant_id||'-')],['数据范围',u.role==='admin'?'全局':(u.scope==='tenant'?'租户级':'站点级')],
      ['会话状态','<span style="color:var(--success);font-weight:600">● 已登录</span>']
    ].map(function(f){return '<div class="field"><div class="f-label">'+f[0]+'</div><div class="f-value">'+f[1]+'</div></div>'}).join('');
  });
};

// ═══════════ Password Change ═══════════
window.changePassword = function(){
  var oldPwd=document.getElementById('pwdOld').value;
  var n=document.getElementById('pwdNew').value;
  var c=document.getElementById('pwdConfirm').value;
  var msg=document.getElementById('pwdMsg');
  msg.className='msg'; msg.style.display='none';
  if(!oldPwd){msg.className='msg err';msg.textContent='请输入当前密码';msg.style.display='block';return}
  if(n.length<6){msg.className='msg err';msg.textContent='新密码至少 6 位';msg.style.display='block';return}
  if(n!==c){msg.className='msg err';msg.textContent='两次输入不一致';msg.style.display='block';return}
  Api.put('/api/password', {old_password:oldPwd,new_password:n}).then(function(d){
    msg.className='msg '+(d.error?'err':'ok');
    msg.textContent=d.error||'密码修改成功';
    msg.style.display='block';
    if(!d.error){document.getElementById('pwdOld').value='';document.getElementById('pwdNew').value='';document.getElementById('pwdConfirm').value='';}
  });
};

// ═══════════ Trajectory ═══════════
window.loadTrajectories = function(){
  var params=new URLSearchParams();
  var did=document.getElementById('fDroneId').value.trim();
  var df=document.getElementById('fDateFrom').value;
  var dt=document.getElementById('fDateTo').value;
  if(did) params.set('drone_id',did);
  if(df) params.set('date_from',df+'T00:00:00');
  if(dt) params.set('date_to',dt+'T23:59:59');
  var qs = params.toString();
  Api.get('/api/trajectories'+(qs?'?'+qs:'')).then(function(d){
    var keys=Object.keys(d||{});
    document.getElementById('trajCount').textContent=keys.length;
    var table=document.getElementById('trajTable'), empty=document.getElementById('trajEmpty');
    if(keys.length===0){
      table.innerHTML=''; empty.style.display='block';
    }else{
      empty.style.display='none';
      table.innerHTML=keys.map(function(k){
        var t=d[k];
        return '<tr data-trajectory-drone="'+UI.escapeAttr(k)+'" style="cursor:pointer"><td class="mono" style="color:var(--brand);font-weight:500">'+UI.escapeHtml(k)+'</td><td>'+t.count+'</td><td class="mono">'+(t.min_dist||0).toFixed(1)+'</td><td class="mono">'+(t.first||'')+'</td><td class="mono">'+(t.last||'')+'</td><td>'+UI.escapeHtml(t.device_name||'-')+'</td></tr>';
      }).join('');
    }
  }).catch(function(e){console.error(e)});
};

window.showTrajDetail = function(droneId){
  selectedDrone=droneId;
  document.querySelectorAll('#trajTable tr').forEach(function(r){r.classList.remove('selected')});
  var row=document.querySelector('#trajTable tr[data-trajectory-drone="'+CSS.escape(droneId)+'"]');
  if(row) row.classList.add('selected');
  Api.get('/api/trajectories/'+encodeURIComponent(droneId)+'/points?limit=500').then(function(pts){
    var dp=document.getElementById('trajDetailPanel');
    dp.style.display='';
    document.getElementById('detailTitle').textContent=droneId;
    if(pts.length>0){
      document.getElementById('detailInfo').innerHTML=
        '<span>点数: <b>'+pts.length+'</b></span><span>时间跨度: <b>'+(pts[pts.length-1].time||'')+' → '+(pts[0].time||'')+'</b></span><span>最小距离: <b>'+(Math.min.apply(null,pts.map(function(p){return p.distance||9999}))||0).toFixed(1)+' m</b></span>';
      document.getElementById('pointsTable').innerHTML=pts.map(function(p){
        return '<tr><td class="mono">'+(p.time||'')+'</td><td class="mono">'+(p.lat||0).toFixed(6)+'</td><td class="mono">'+(p.lon||0).toFixed(6)+'</td><td>'+(p.alt||0).toFixed(0)+'</td><td class="mono">'+(p.distance!=null?p.distance.toFixed(1):'-')+'</td><td>'+UI.escapeHtml(p.nearest_line||'-')+'</td></tr>';
      }).join('');
      document.getElementById('pgInfo').innerHTML='共 <b>'+pts.length+'</b> 个轨迹点';
    }else{
      document.getElementById('detailInfo').innerHTML=''; document.getElementById('pointsTable').innerHTML='';
      document.getElementById('pgInfo').innerHTML='';
    }
    dp.scrollIntoView({behavior:'smooth',block:'start'});
  });
};

window.closeTrajDetail = function(){
  document.getElementById('trajDetailPanel').style.display='none';
  selectedDrone=null;
};

window.clearTrajFilters = function(){
  document.getElementById('fDroneId').value='';
  document.getElementById('fDateFrom').value='';
  document.getElementById('fDateTo').value='';
  loadTrajectories();
};

// ═══════════ Tenant Info ═══════════
function refreshTenantInfo(){
  var sec=document.getElementById('tenantInfoSidebar');
  if(!sec) return;
  if(currentUser.role!=='tenant_admin'&&currentUser.role!=='user'){sec.style.display='none';return}
  if(!currentUser.tenant_id){sec.style.display='none';return}
  Api.get('/api/tenant/info').then(function(t){
    if(!t){sec.style.display='none';return}
    sec.style.display='';
    var html='<div class="nav-label">我的租户</div>';
    html+='<div style="padding:4px 20px 8px;font-size:12px;color:var(--text-regular)"><b>'+UI.escapeHtml(t.name)+'</b></div>';
    html+='<div style="padding:0 20px 4px;font-size:11px;color:var(--text-secondary)">用户: '+t.current_users+'/'+t.max_users+'</div>';
    if(t.stations&&t.stations.length){
      html+='<div style="padding:0 20px 8px;font-size:11px;color:var(--text-secondary)">站点: '+t.stations.map(function(s){return UI.escapeHtml(s.name)}).join(', ')+'</div>';
    }
    sec.innerHTML=html;
  }).catch(function(){sec.style.display='none'});
}

// ═══════════ Device Management ═══════════
window.openDevModal = function(){
  document.getElementById('devResult').style.display='none';
  document.getElementById('devName').value='';
  document.getElementById('devStation').value='';
  document.getElementById('devSaveBtn').disabled=false;
  document.getElementById('devSaveBtn').textContent='注册';
  document.getElementById('devSaveBtn').style.background=''; document.getElementById('devSaveBtn').style.borderColor='';
  var tidRow=document.getElementById('devTenantRow');
  if(currentUser.role==='admin'){
    tidRow.style.display='flex';
    var sel=document.getElementById('devTenantId');
    sel.innerHTML='<option value="">选择租户…</option>';
    Api.get('/api/licenses').then(function(tenants){
      if(!tenants||!tenants.length){
        sel.innerHTML='<option value="">暂无租户，请先创建</option>';
        document.getElementById('devSaveBtn').disabled=true;
        return;
      }
      tenants.forEach(function(t){
        sel.innerHTML+='<option value="'+t.id+'">'+UI.escapeHtml(t.name)+' (#'+t.id+' - '+UI.escapeHtml(t.license_key)+')</option>';
      });
    }).catch(catchErr('加载租户信息失败'));
  }else{tidRow.style.display='none';}
  document.getElementById('devModal').classList.add('show');
};
window.closeDevModal = function(){
  document.getElementById('devModal').classList.remove('show');
  document.getElementById('devResult').style.display='none';
};

function loadDevices(){
  Api.get('/api/devices').then(function(devices){
    var div=document.getElementById('devList');
    if(!devices||!devices.length){div.innerHTML='<div class="empty-state"><div class="msg">暂无设备</div><div class="sub">点击「注册设备」添加边缘设备</div></div>';return}
    div.innerHTML=devices.map(function(d){
      var status=d.revoked?'<span style="color:#ef4444">已吊销</span>':'<span style="color:#22c55e">正常</span>';
      var certInfo=d.cert_serial?'<br><span style="font-size:10px;color:var(--muted)">证书: '+d.cert_serial.substr(0,16)+'… '+d.cert_issued_at.substr(0,10)+'</span>':'';
      var actions='';
      if(!d.revoked){actions+='<span style="font-size:11px;color:#ca8a04;cursor:pointer;margin-left:8px" data-revoke-dev="'+UI.escapeHtml(d.device_name)+'">吊销</span>';}
      actions+='<span class="pl-del" data-del-dev="'+UI.escapeHtml(d.device_name)+'" title="删除">×</span>';
      var tidTag=(currentUser.role==='admin'&&d.tenant_id)?' <span style="font-size:10px;color:var(--muted)">[T#'+d.tenant_id+']</span>':'';
      return '<div class="pl-entry"><span><b>'+UI.escapeHtml(d.device_name)+'</b> <span style="font-size:10px;color:var(--accent)">'+UI.escapeHtml(d.station||'--')+'</span>'+tidTag+' '+status+certInfo+'</span>'+actions+'</div>';
    }).join('');
  });
}

window.addDevice = function(){
  var data={
    device_name: document.getElementById('devName').value.trim(),
    station: document.getElementById('devStation').value.trim()
  };
  if(!data.device_name){UI.Message.warning('设备名称不能为空');return}
  if(currentUser.role==='admin'){
    var tidVal=document.getElementById('devTenantId').value;
    if(!tidVal){UI.Message.warning('请选择所属租户');return}
    data.tenant_id=parseInt(tidVal,10);
  }
  var btn=document.getElementById('devSaveBtn');
  btn.disabled=true; btn.textContent='注册中…';
  Api.post('/api/devices/provision', data).then(function(res){
    if(res.error){btn.disabled=false; btn.textContent='注册';UI.toast(res.error,'error');return}
    btn.textContent='已注册'; btn.style.background='#16a34a'; btn.style.borderColor='#16a34a';
    document.getElementById('devSecretOut').textContent=res.device_secret;
    document.getElementById('devCertSerial').textContent=res.client_cert?'已签发':'--';
    document.getElementById('devResult').style.display='block';
    loadDevices();
  }).catch(function(e){
    btn.disabled=false; btn.textContent='注册';
    catchErr('注册设备失败')(e);
  });
};

document.getElementById('devList').addEventListener('click', function(e){
  var t=e.target;
  if(t.dataset.delDev){
    var name=t.dataset.delDev;
    UI.Message.confirm('确定要删除设备 '+name+' 吗?').then(function(ok){
      if(!ok) return;
      Api.del('/api/devices/'+encodeURIComponent(name)).then(function(r){
        if(r.error){UI.toast(r.error,'error');return}
        loadDevices();
      }).catch(catchErr('删除设备失败'));
    });
  }
  if(t.dataset.revokeDev){
    var name2=t.dataset.revokeDev;
    UI.Message.confirm('确定要吊销设备 '+name2+' 的证书吗?吊销后设备将无法连接。').then(function(ok){
      if(!ok) return;
      Api.post('/api/devices/'+encodeURIComponent(name2)+'/revoke').then(function(r){
        if(r.error){UI.toast(r.error,'error');return}
        UI.toast('证书已吊销','warning');
        loadDevices();
      }).catch(catchErr('吊销证书失败'));
    });
  }
});

// ═══════════ License Management ═══════════
window.openLicPage = function(){
  loadLicenses();
};
window.openLicModal = function(){
  document.getElementById('licModal').classList.add('show');
};
window.closeLicModal = function(){
  document.getElementById('licName').value='';
  document.getElementById('licContact').value='';
  document.getElementById('licModal').classList.remove('show');
};

function loadLicenses(){
  Api.get('/api/licenses').then(function(tenants){
    var div=document.getElementById('licList');
    if(!tenants||!tenants.length){div.innerHTML='<div class="empty-state"><div class="msg">暂无密钥</div><div class="sub">点击「新增密钥」创建租户</div></div>';return}
    div.innerHTML=tenants.map(function(t){
      var status=t.is_active?'<span style="color:#22c55e">有效</span>':'<span style="color:#ef4444">已停用</span>';
      var actionBtn=t.is_active
        ?'<span class="pl-del" data-del-lic="'+t.id+'" title="停用" style="cursor:pointer">×</span>'
        :'<span style="font-size:11px;color:var(--brand);cursor:pointer" data-reactivate-lic="'+t.id+'">重新激活</span>';
      return '<div class="pl-entry"><span><b>'+UI.escapeHtml(t.name)+'</b> <code style="font-size:10px;color:var(--accent)">'+UI.escapeHtml(t.license_key)+'</code><br><span style="font-size:10px;color:var(--muted)">用户数:'+t.user_count+'/'+t.max_users+' '+status+' 联系人:'+UI.escapeHtml(t.contact||'-')+'</span></span>'+actionBtn+'</div>';
    }).join('');
  });
}

window.addLicense = function(){
  var data={
    name: document.getElementById('licName').value.trim(),
    max_users: parseInt(document.getElementById('licMaxUsers').value)||3,
    contact: document.getElementById('licContact').value.trim()
  };
  if(!data.name){UI.Message.warning('客户名称不能为空');return}
  Api.post('/api/licenses', data).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    UI.Message.success('密钥已生成: '+res.license_key);
    closeLicModal();
    loadLicenses();
  }).catch(catchErr('创建密钥失败'));
};

window.delLicense = function(id){
  UI.Message.confirm('确定要停用该密钥吗？所有关联用户将无法操作。').then(function(ok){
    if(!ok) return;
    Api.del('/api/licenses', {id:id}).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      loadLicenses();
    }).catch(catchErr('停用密钥失败'));
  });
};

window.reactivateLicense = function(id){
  UI.Message.confirm('确定要重新激活该密钥吗？').then(function(ok){
    if(!ok) return;
    Api.put('/api/licenses', {id:id, is_active:true}).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      loadLicenses();
    }).catch(catchErr('激活密钥失败'));
  });
};

// ═══════════ Audit Log ═══════════
window.openAuditPage = function(){
  loadAudit();
};

function loadAudit(){
  var list=document.getElementById('auditTableBody');
  list.innerHTML='<tr><td colspan="4"><div class="empty-state">加载中...</div></td></tr>';
  Api.get('/api/audit?limit=100').then(function(rows){
    if(!rows.length){list.innerHTML='<tr><td colspan="4"><div class="empty-state">暂无操作记录</div></td></tr>';return}
    list.innerHTML=rows.map(function(r){
      var html='<tr style="border-bottom:1px solid var(--border)">';
      html+='<td style="padding:6px 4px">'+UI.escapeHtml(r.timestamp)+'</td>';
      html+='<td style="padding:6px 4px">'+UI.escapeHtml(r.operation)+'</td>';
      html+='<td style="padding:6px 4px">'+UI.escapeHtml(r.table_name||'')+(r.record_id?' #'+r.record_id:'')+'</td>';
      html+='<td style="padding:6px 4px">'+UI.escapeHtml(r.username)+'</td>';
      html+='</tr>';
      if(r.detail){
        html+='<tr style="border-bottom:1px solid var(--border);background:var(--info-light)"><td colspan="4" style="padding:4px 8px;font-size:10px;color:var(--text-secondary)">'+UI.escapeHtml(r.detail)+'</td></tr>';
      }
      return html;
    }).join('');
  }).catch(catchErr('加载审计日志失败'));
}

// ═══════════ Settings ═══════════
window.loadSettings = function(){
  Api.get('/api/settings').then(function(s){
    document.getElementById('scThreshWarn').value=s.threshold_warning||200;
    document.getElementById('scThreshSev').value=s.threshold_severe||100;
    document.getElementById('scThreshCrit').value=s.threshold_critical||50;
    document.getElementById('scFlapEn').checked=s.anti_flapping_enabled==='true';
    document.getElementById('scFlapIn').value=s.debounce_in||3;
    document.getElementById('scFlapOut').value=s.debounce_out||10;
    document.getElementById('scSmsEn').checked=s.sms_enabled==='true';
    document.getElementById('scSmsPhones').value=(s.sms_alert_phones||'').split(',').join('\n');
    document.getElementById('scArchiveEn').checked=s.raw_archive_enabled!=='false';
    document.getElementById('scRetention').value=s.raw_archive_retention_days||30;
  });
};
window.saveSettings = function(){
  var phones=document.getElementById('scSmsPhones').value.split('\n').map(function(s){return s.trim()}).filter(Boolean).join(',');
  var data={
    threshold_warning: String(parseFloat(document.getElementById('scThreshWarn').value)||200),
    threshold_severe: String(parseFloat(document.getElementById('scThreshSev').value)||100),
    threshold_critical: String(parseFloat(document.getElementById('scThreshCrit').value)||50),
    anti_flapping_enabled: document.getElementById('scFlapEn').checked?'true':'false',
    debounce_in: String(parseFloat(document.getElementById('scFlapIn').value)||3),
    debounce_out: String(parseFloat(document.getElementById('scFlapOut').value)||10),
    sms_enabled: document.getElementById('scSmsEn').checked?'true':'false',
    sms_alert_phones: phones,
    raw_archive_enabled: document.getElementById('scArchiveEn').checked?'true':'false',
    raw_archive_retention_days: String(parseInt(document.getElementById('scRetention').value)||30)
  };
  Api.put('/api/settings', data).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    UI.toast('设置已保存', 'ok');
  }).catch(catchErr('保存设置失败'));
};

// ═══════════ Power Line File Upload + Import ═══════════
window.handlePlFileUpload = function(){
  var input = document.getElementById('plFileInput');
  var file = input && input.files && input.files[0];
  if(!file){ UI.Message.warning('请选择文件'); return; }
  var name = file.name.toLowerCase();
  if(name.endsWith('.csv')){
    var reader = new FileReader();
    reader.onload = function(e){
      document.getElementById('plCsv').value = e.target.result;
      document.getElementById('plFileName').textContent = file.name;
    };
    reader.readAsText(file);
  } else if(name.endsWith('.xlsx') || name.endsWith('.xls')){
    document.getElementById('plFileName').textContent = file.name + ' (解析中...)';
    var reader = new FileReader();
    reader.onload = function(e){
      import('xlsx').then(function(XLSX){
        var wb = XLSX.read(e.target.result, {type:'array'});
        var csvText = XLSX.utils.sheet_to_csv(wb.Sheets[wb.SheetNames[0]]);
        document.getElementById('plCsv').value = csvText;
        document.getElementById('plFileName').textContent = file.name + ' (' + (csvText.trim().split('\n').length) + ' 行)';
      }).catch(function(err){
        UI.toast('解析 Excel 文件失败: ' + (err.message||''), 'error');
        document.getElementById('plFileName').textContent = '';
      });
    };
    reader.readAsArrayBuffer(file);
  } else {
    UI.Message.warning('不支持的格式，请选择 .csv、.xlsx 或 .xls 文件');
  }
};

window.importPowerLinesCsv = function(){
  var csvText = document.getElementById('plCsv').value.trim();
  if(!csvText){UI.Message.warning('请粘贴 CSV 内容或选择文件上传');return}
  Api.post('/api/powerlines/import', {csv:csvText}).then(function(res){
    if(res.error){UI.toast(res.error,'error');return}
    UI.toast('成功导入 '+res.imported+' 条电力线', 'ok');
    document.getElementById('plCsv').value='';
    document.getElementById('plFileName').textContent='';
    var fi = document.getElementById('plFileInput'); if(fi) fi.value='';
    loadPowerLines();
  }).catch(catchErr('导入电力线失败'));
};

// ═══════════ Alert CSV Export ═══════════
window.exportAlertsCsv = function(){
  window.open('/api/alerts/export', '_blank');
};

// ═══════════ Event Delegation for data-* buttons ═══════════
UI.delegate(document.getElementById('stList'), 'click', '[data-edit-st]', function(){ editStation2(this.dataset.editSt); });
UI.delegate(document.getElementById('stList'), 'click', '[data-del-st]', function(){ delStation(this.dataset.delSt); });
UI.delegate(document.getElementById('plList'), 'click', '[data-edit-pl]', function(){ editPowerLine(parseInt(this.dataset.editPl)); });
UI.delegate(document.getElementById('plList'), 'click', '[data-del-pl]', function(){ delPowerLine(parseInt(this.dataset.delPl)); });
UI.delegate(document.getElementById('userList'), 'click', '[data-edit-user]', function(){ editUser2(parseInt(this.dataset.editUser)); });
UI.delegate(document.getElementById('userList'), 'click', '[data-del-user]', function(){ delUser(this.dataset.delUser); });
UI.delegate(document.getElementById('userList'), 'click', '[data-reset-pw]', function(){ resetUserPw(this.dataset.resetPw); });
UI.delegate(document.getElementById('psList'), 'click', '[data-del-ps]', function(){ delPerson(parseInt(this.dataset.delPs)); });
UI.delegate(document.getElementById('wlList'), 'click', '[data-del-wl]', function(){ delWhitelist(parseInt(this.dataset.delWl)); });
UI.delegate(document.getElementById('trajTable'), 'click', '[data-trajectory-drone]', function(){ showTrajDetail(this.dataset.trajectoryDrone); });
UI.delegate(document.getElementById('licList'), 'click', '[data-del-lic]', function(){ delLicense(parseInt(this.dataset.delLic)); });
UI.delegate(document.getElementById('licList'), 'click', '[data-reactivate-lic]', function(){ reactivateLicense(parseInt(this.dataset.reactivateLic)); });

// ═══════════ Init ═══════════
initSocket();
updateUI();
(function schedulePoll() {
  pollFallback();
  pollTimer = setTimeout(schedulePoll, wsEnabled ? 5000 : 2000);
})();
