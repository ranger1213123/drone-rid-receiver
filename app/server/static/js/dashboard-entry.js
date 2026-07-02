/**
 * Dashboard (list view) entry point.
 * All page-specific logic extracted from dashboard.html inline script.
 */
import './api.js';

var _lastAlertViewTime = Date.now();  // 上次查看告警的时间戳，用于未读计数
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
  stations:'站点管理', users:'用户管理', personnel:'站点联系人', whitelist:'白名单',
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
    if(this.dataset.page==='alerts'){_lastAlertViewTime = Date.now();var ab=document.getElementById('alertBadge');if(ab)ab.style.display='none';}
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

// ═══════════ RBAC visibility ═══════════
function applyRBAC(){
  var role = (currentUser||{}).role;
  var isAdmin = role==='admin';
  var isTenantAdmin = role==='tenant_admin';
  ['navUsers','navDevices','navAudit','navSettings','navLicenses'].forEach(function(id){
    var el=document.getElementById(id); if(el) el.style.display=isAdmin?'':'none';
  });
  var stEl=document.getElementById('navStations');
  if(stEl) stEl.style.display=(isAdmin||isTenantAdmin)?'':'none';
  var psEl=document.getElementById('navPersonnel');
  if(psEl) psEl.style.display=(isAdmin||isTenantAdmin)?'':'none';
  var wlEl=document.getElementById('navWhitelist');
  if(wlEl) wlEl.style.display=(isAdmin||isTenantAdmin)?'':'none';
}

// ═══════════ SVG drone icon ═══════════
function droneSvg(status){
  var colors={active:'#16a34a',warning:'#ca8a04',severe:'#ea580c',critical:'#dc262e',offline:'#9ca3af',gone:'#9ca3af'};
  var c=colors[status]||colors.active;
  return '<svg class="drone-svg '+status+'" width="18" height="18" viewBox="0 0 1024 1024" fill="'+c+'" style="vertical-align:middle">'
    +'<path d="M340.65 809.17a138.26 138.26 0 1 1-114.43-114.43 330 330 0 0 1 40.41-46.06 193.1 193.1 0 0 0-198.82 46.09c-75.21 75.21-75.21 197.59 0 272.81s197.6 75.22 272.83 0a193.1 193.1 0 0 0 46.1-198.75c-14.72 11.99-30.17 25.49-46.09 40.34zM764.81 641.69a330 330 0 0 1 39.8 46.32 138.27 138.27 0 1 1-114.77 114.84c-15.99-14.63-31.47-27.96-46.33-39.76a193.1 193.1 0 0 0 46.33 196.8c75.22 75.22 197.62 75.22 272.83 0s75.22-197.6 0-272.83a193.1 193.1 0 0 0-197.86-46.37zM692.82 227.86a138.27 138.27 0 1 1 114.7 114.67c-15.25 16.52-28.54 31.93-40.05 46.23a193.1 193.1 0 0 0 198.23-46.27c75.22-75.22 75.22-197.6 0-272.83s-197.62-75.22-272.83 0a193.1 193.1 0 0 0-46.24 198.33c13.95-11.26 29.32-24.53 46.19-40.13zM258.29 374.94a330 330 0 0 1-41.12-45.77 138.26 138.26 0 1 1 113.83-113.79c15.65 14.9 31 28.61 45.77 41.12a193.1 193.1 0 0 0-45.69-200.09c-75.18-75.22-197.6-75.22-272.78 0s-75.22 197.6 0 272.83a193.18 193.18 0 0 0 199.99 45.7zM518.34 460.18a56.33 56.33 0 1 0 39.91 16.49 56.01 56.01 0 0 0-39.91-16.49z"/>'
    +'<path d="M787.95 845.34c3.2 3.42 11.06 12.32 12.7 13.95l.82.79a8 8 0 0 0 1.43 1.3c19.2 17.26 46.82 18.59 62.94 2.42 15.13-15.13 14.95-40.39.61-59.32a170 170 0 0 0-12.24-12.17c-1.59-1.34-2.86-2.52-3.48-3-44.13-40.88-188.14-180.73-185.3-262.66 0-3.42 0-17.78 0-21.93-.4-82.2 141.6-220.08 185.35-260.61.54-.49 1.89-1.66 3.48-3.02a167 167 0 0 0 12.24-12.16c14.3-18.92 14.52-44.18-.62-59.32-16.12-16.12-43.74-14.85-62.94 2.42a8 8 0 0 0-1.43 1.3l-.81.77c-1.65 1.64-9.5 10.54-12.7 13.97-43.07 46.13-170.6 175.53-251.23 181.66-6.35.49-28.06.39-33.37.17-82.55-3.25-217.42-142.11-257.35-185.29-.5-.54-1.66-1.89-3.02-3.48a164 164 0 0 0-12.16-12.24c-18.92-14.3-44.18-14.52-59.32.6-16.12 16.13-14.86 43.75 2.42 62.94a8.7 8.7 0 0 0 1.3 1.43l.77.8c1.65 1.66 10.54 9.5 13.96 12.7 46.3 45.98 170.22 168.06 182.27 248.9 1.3 8.7 1.22 44.21-1.12 55.23-17.16 80.73-136 197.73-179.81 238.63-3.42 3.22-12.32 11.06-13.96 12.7l-.77.82a8 8 0 0 0-1.3 1.43c-17.28 19.2-18.59 46.82-2.42 62.94 15.13 15.13 40.39 14.96 59.32.61a167 167 0 0 0 12.16-12.24c1.36-1.59 2.52-2.86 3.03-3.48 46.51-43.97 175.93-177.35 258.85-186.66 7.48-.84 33.96-.82 40.87-.16 80.71 7.45 206.9 135.54 249.7 181.31zM580.25 578.41a87.55 87.55 0 1 1 0-123.81 86.98 86.98 0 0 1 0 123.81z"/>'
    +'</svg>';
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
      applyRBAC();
      refreshTenantInfo();
    }
    // Stats
    var warn=0,sev=0,crit=0;
    (d.drones||[]).forEach(function(dr){var s=dr.status;if(s==='warning')warn++;if(s==='severe')sev++;if(s==='critical')crit++;});
    document.getElementById('statDrones').textContent=d.drone_count||0;
    document.getElementById('statWarn').textContent=warn;
    document.getElementById('statSev').textContent=sev;
    document.getElementById('statCrit').textContent=crit;
    document.getElementById('droneCountPill').textContent=d.drone_count||0;
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
    updateDroneTable();
  });
}

function pollFallback() {
  if (wsEnabled) return;
  fetch('/api/status').then(function(r) { return r.json(); }).then(function(d) {
    lastDrones = d.drones || [];
    window._alpineDrones = lastDrones;
    updateDroneTable();
    updateLogPanel(d.alerts || []);
  }).catch(function(){});
}

function notifyAlert(droneId, level, distance, lineName) {
  if (window.Notification && Notification.permission === 'granted') {
    new Notification('[' + (level === 'critical' ? '危险' : '严重') + '告警] ' + droneId, {
      body: '距离 ' + lineName + ' ' + (distance || 0).toFixed(0) + 'm',
      tag: droneId
    });
  }
  var audio = document.getElementById('alertSound');
  if (audio) { audio.currentTime = 0; audio.play().catch(function(){}); }
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
    var tags={active:'tag-active',warning:'tag-warning',severe:'tag-severe',critical:'tag-critical',offline:'tag-offline',gone:'tag-offline'};
    var txts={active:'正常',warning:'警告',severe:'严重',critical:'危险',offline:'离线',gone:'离线'};
    table.innerHTML=drones.map(function(dr){
      var s=dr.status||'active', rc=(s==='critical')?'row-critical':(s==='severe')?'row-severe':'';
      var dist=dr.min_distance!=null?dr.min_distance.toFixed(0)+' m':'-';
      var ts=dr.last_seen||''; var time=ts?ts.replace('T',' ').substring(0,16):'';
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
        '<td>'+(s!=='offline'?'<button class="btn btn-ghost btn-xs" data-fly-lat="'+dr.last_lat+'" data-fly-lon="'+dr.last_lon+'">定位</button>':'-')+'</td></tr>';
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
  ['plName','plLat1','plLon1','plAlt1','plTh1','plLat2','plLon2','plAlt2','plTh2','plVoltage'].forEach(function(id){
    document.getElementById(id).value='';
  });
  document.getElementById('plAltHint').style.display='none';
  var saveBtn=document.getElementById('plSaveBtn');
  saveBtn.textContent='保存';
  saveBtn.onclick=savePowerLine;
  document.getElementById('plFormTitle').textContent='新增电力线';
}
function _resetStForm(){
  _editingStName2=null;
  ['stName','stLocation','stLat','stLon','stAlt','stProvince','stCity','stCounty','stWebhook'].forEach(function(id){
    document.getElementById(id).value='';
  });
  document.getElementById('stFormTitle').textContent='新增站点';
  document.getElementById('stSubmitBtn').textContent='添加';
  document.getElementById('stSubmitBtn').onclick=addStation;
  document.getElementById('stCancelEditBtn').style.display='none';
}

function loadPowerLines(){
  Api.get('/api/powerlines').then(function(lines){
    plData=lines||[];
    var div=document.getElementById('plList');
    if(!lines.length){div.innerHTML='<div class="empty-state"><div class="msg">暂无电力线</div><div class="sub">点击「新增电力线」添加</div></div>';return}
    div.innerHTML=lines.map(function(l){
      return '<div class="crud-item"><span><b>'+UI.escapeHtml(l.name)+'</b> <span style="color:var(--blue);font-size:11px">'+UI.escapeHtml(l.voltage_level||'')+'</span></span><span style="color:var(--muted);font-size:10px">('+l.lat1.toFixed(4)+','+l.lon1.toFixed(4)+') → ('+l.lat2.toFixed(4)+','+l.lon2.toFixed(4)+') 导线:'+l.alt1.toFixed(0)+'m</span><span><button class="btn btn-ghost btn-xs" onclick="editPowerLine('+l.id+')">✎</button> <button class="btn btn-ghost btn-xs" onclick="delPowerLine('+l.id+')">×</button></span></div>';
    }).join('');
  }).catch(catchErr('加载电力线失败'));
}

window.editPowerLine = function(lineId){
  var l=plData.find(function(x){return x.id===lineId}); if(!l) return;
  _editingPlId=lineId;
  document.getElementById('plName').value=l.name||'';
  document.getElementById('plLat1').value=l.lat1;
  document.getElementById('plLon1').value=l.lon1;
  document.getElementById('plAlt1').value=l.alt1;
  document.getElementById('plTh1').value=l.tower_height1||'';
  document.getElementById('plLat2').value=l.lat2;
  document.getElementById('plLon2').value=l.lon2;
  document.getElementById('plAlt2').value=l.alt2;
  document.getElementById('plTh2').value=l.tower_height2||'';
  document.getElementById('plVoltage').value=l.voltage_level||'';
  onPlFieldChange();
  document.getElementById('plFormTitle').textContent='编辑电力线: '+l.name;
  var saveBtn=document.getElementById('plSaveBtn');
  saveBtn.textContent='更新';
  saveBtn.onclick=function(){savePowerLine()};
  document.getElementById('plModal').classList.add('show');
};
window.savePowerLine = function(){
  var data={
    name:document.getElementById('plName').value.trim(),
    lat1:parseFloat(document.getElementById('plLat1').value),
    lon1:parseFloat(document.getElementById('plLon1').value),
    alt1:parseFloat(document.getElementById('plAlt1').value),
    tower_height1:parseFloat(document.getElementById('plTh1').value)||0,
    lat2:parseFloat(document.getElementById('plLat2').value),
    lon2:parseFloat(document.getElementById('plLon2').value),
    alt2:parseFloat(document.getElementById('plAlt2').value),
    tower_height2:parseFloat(document.getElementById('plTh2').value)||0,
    voltage_level:document.getElementById('plVoltage').value
  };
  if(!data.name){UI.Message.warning('名称不能为空');return}
  if(_editingPlId){
    data.id=_editingPlId;
    Api.put('/api/powerlines', data).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      _resetPlForm(); closePlModal(); loadPowerLines();
    }).catch(catchErr('更新电力线失败'));
  }else{
    Api.post('/api/powerlines', data).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      _resetPlForm(); closePlModal(); loadPowerLines();
    }).catch(catchErr('添加电力线失败'));
  }
};
window.delPowerLine = function(id){
  UI.Message.confirm('确定删除这条电力线吗？').then(function(ok){
    if(!ok) return;
    Api.del('/api/powerlines', {id:id}).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      loadPowerLines();
    }).catch(catchErr('删除电力线失败'));
  });
};

window.importPowerLinesCsv = function(){
  var csv=document.getElementById('plCsv').value.trim();
  if(!csv){UI.Message.warning('请粘贴 CSV 内容或选择文件上传');return}
  Api.post('/api/powerlines/import', {csv:csv}).then(function(res){
    if(res.error) UI.toast(res.error,'error');
    else{
      UI.toast('成功导入 '+res.imported+' 条电力线','ok');
      document.getElementById('plCsv').value='';
      document.getElementById('plFileName').textContent='';
      var fi=document.getElementById('plFileInput');if(fi) fi.value='';
      loadPowerLines();
    }
  }).catch(catchErr('导入电力线失败'));
};
window.handlePlFileUpload = function(){
  var input=document.getElementById('plFileInput');
  var file=input&&input.files&&input.files[0];
  if(!file){UI.Message.warning('请选择文件');return}
  var name=file.name.toLowerCase();
  if(name.endsWith('.csv')){
    var reader=new FileReader();
    reader.onload=function(e){document.getElementById('plCsv').value=e.target.result;document.getElementById('plFileName').textContent=file.name};
    reader.readAsText(file);
  }else if(name.endsWith('.xlsx')||name.endsWith('.xls')){
    document.getElementById('plFileName').textContent=file.name+' (解析中...)';
    var reader2=new FileReader();
    reader2.onload=function(e){
      import('xlsx').then(function(m){
        var wb=m.read(e.target.result,{type:'array'});
        var csv=m.utils.sheet_to_csv(wb.Sheets[wb.SheetNames[0]]);
        document.getElementById('plCsv').value=csv;
        document.getElementById('plFileName').textContent=file.name+' ('+csv.trim().split('\n').length+' 行)';
      }).catch(function(err){UI.toast('解析 Excel 文件失败: '+(err.message||''),'error');document.getElementById('plFileName').textContent=''});
    };
    reader2.readAsArrayBuffer(file);
  }else{UI.Message.warning('不支持的格式，请选择 .csv、.xlsx 或 .xls 文件')}
};

// ═══════════ Stations ═══════════
function loadStations(){
  Api.get('/api/stations').then(function(stations){
    _allStations=stations||[];
    populateStFilters();
    renderStationList();
  }).catch(catchErr('加载站点失败'));
}
function renderStationList(){
  var stations=_allStations;
  var prov=document.getElementById('stFilterProv').value;
  var city=document.getElementById('stFilterCity').value;
  var county=document.getElementById('stFilterCounty').value;
  var name=(document.getElementById('stFilterName').value||'').trim().toLowerCase();
  if(prov) stations=stations.filter(function(s){return (s.province||'')===prov});
  if(city) stations=stations.filter(function(s){return (s.city||'')===city});
  if(county) stations=stations.filter(function(s){return (s.county||'')==county});
  if(name) stations=stations.filter(function(s){return (s.name||'').toLowerCase().includes(name)});
  var div=document.getElementById('stList');
  if(!stations.length){div.innerHTML='<div class="empty-state"><div class="msg">暂无站点</div><div class="sub">点击「新增站点」添加</div></div>';return}
  div.innerHTML=stations.map(function(s){
    var region=[s.province||'',s.city||'',s.county||''].filter(Boolean).join(' ');
    return '<div class="crud-item"><span><b>'+UI.escapeHtml(s.name)+'</b> <span style="color:var(--muted);font-size:10px">'+UI.escapeHtml(s.location||'')+'</span></span><span style="color:var(--muted);font-size:10px">'+(region||'--')+'</span><span style="color:var(--muted);font-size:10px">'+(s.lat!=null?s.lat.toFixed(2)+','+s.lon.toFixed(2):'--')+'</span><span><button class="btn btn-ghost btn-xs" onclick="editStation(\''+UI.escapeAttr(s.name)+'\')">✎</button> <button class="btn btn-ghost btn-xs" onclick="delStation(\''+UI.escapeAttr(s.name)+'\')">×</button></span></div>';
  }).join('');
}
function populateStFilters(){
  var sel=document.getElementById('stFilterProv');
  // Use full regionData for province list, fallback to station data
  var options='<option value="">全部省</option>';
  regionData.forEach(function(p){options+='<option value="'+p[0]+'">'+p[0]+'</option>'});
  sel.innerHTML=options;
  sel.onchange=function(){
    var v=this.value;
    var citySel=document.getElementById('stFilterCity');
    var opt='<option value="">全部市</option>';
    if(v){
      var p=_findProvince(v);
      if(p) p[1].forEach(function(c){opt+='<option value="'+c[0]+'">'+c[0]+'</option>'});
    }
    citySel.innerHTML=opt;
    citySel.onchange=function(){
      var cv=this.value;
      var countySel=document.getElementById('stFilterCounty');
      var copt='<option value="">全部区/县</option>';
      if(v&&cv){
        var p2=_findProvince(v);
        if(p2){var c2=_findCity(p2,cv);if(c2){(c2[2]||[]).forEach(function(x){copt+='<option value="'+x+'">'+x+'</option>'})}}
      }
      countySel.innerHTML=copt;
      renderStationList();
    };
    citySel.dispatchEvent(new Event('change'));
  };
  sel.dispatchEvent(new Event('change'));
}

function _findProvince(name){return regionData.find(function(p){return p[0]===name})}
function _findCity(prov,name){return prov[1].find(function(c){return c[0]===name})}

function _popProvinceSelect(selId,placeholder){
  var sel=document.getElementById(selId);
  if(!sel||sel.tagName!=='SELECT')return;
  sel.innerHTML='<option value="">'+(placeholder||'选择省')+'</option>';
  regionData.forEach(function(p){sel.innerHTML+='<option value="'+p[0]+'">'+p[0]+'</option>'});
}
function _onProvinceChange(provSelId,citySelId,countySelId,placeholder){
  var prov=document.getElementById(provSelId).value;
  var citySel=document.getElementById(citySelId);
  var countySel=document.getElementById(countySelId);
  citySel.innerHTML='<option value="">'+(placeholder||'选择市')+'</option>';
  countySel.innerHTML='<option value="">选择区/县</option>';
  if(!prov)return;
  var p=_findProvince(prov);if(!p)return;
  p[1].forEach(function(c){citySel.innerHTML+='<option value="'+c[0]+'">'+c[0]+'</option>'});
}
function _onCityChange(provSelId,citySelId,countySelId){
  var prov=document.getElementById(provSelId).value;
  var city=document.getElementById(citySelId).value;
  var countySel=document.getElementById(countySelId);
  countySel.innerHTML='<option value="">选择区/县</option>';
  if(!prov||!city)return;
  var p=_findProvince(prov);if(!p)return;
  var c=_findCity(p,city);if(!c)return;
  (c[2]||[]).forEach(function(x){countySel.innerHTML+='<option value="'+x+'">'+x+'</option>'});
}

function _populateDeviceSelect(){
  Api.get('/api/devices').then(function(devices){
    var sel=document.getElementById('stDevice');
    sel.innerHTML='<option value="">选择设备…</option>';
    (devices||[]).forEach(function(d){
      if(!d.revoked)sel.innerHTML+='<option value="'+UI.escapeAttr(d.device_name)+'">'+UI.escapeHtml(d.device_name)+(d.station?' ('+UI.escapeHtml(d.station)+')':'')+'</option>';
    });
  }).catch(function(){});
}

window.openStModal = function(){
  _resetStForm();
  _popProvinceSelect('stProvince','选择省');
  document.getElementById('stCity').innerHTML='<option value="">选择市</option>';
  document.getElementById('stCounty').innerHTML='<option value="">选择区/县</option>';
  _populateDeviceSelect();
  document.getElementById('stModal').classList.add('show');
};

window.closeStModal = function(){
  _resetStForm();
  document.getElementById('stModal').classList.remove('show');
};

window.doGeocode = function(){
  var lat=parseFloat(document.getElementById('stLat').value);
  var lon=parseFloat(document.getElementById('stLon').value);
  if(isNaN(lat)||isNaN(lon)){UI.Message.warning('请先填写有效的经纬度坐标');return}
  Api.post('/api/geocode',{lat:lat,lon:lon}).then(function(r){
    if(r.error){UI.toast(r.error,'error');return}
    if(r.province)document.getElementById('stProvince').value=r.province;
    if(r.city){_onProvinceChange('stProvince','stCity','stCounty','选择市');document.getElementById('stCity').value=r.city;}
    if(r.county){_onCityChange('stProvince','stCity','stCounty');document.getElementById('stCounty').value=r.county;}
    UI.toast('已填充: '+[r.province,r.city,r.county].filter(Boolean).join(' '),'ok');
  }).catch(catchErr('地理编码失败'));
};
window.editStation = function(name){
  var s=_allStations.find(function(x){return x.name===name}); if(!s) return;
  _editingStName2=name;
  document.getElementById('stName').value=s.name||'';
  document.getElementById('stLocation').value=s.location||'';
  document.getElementById('stLat').value=s.lat!=null?s.lat:'';
  document.getElementById('stLon').value=s.lon!=null?s.lon:'';
  document.getElementById('stAlt').value=s.alt!=null?s.alt:'';
  document.getElementById('stProvince').value=s.province||'';
  document.getElementById('stCity').value=s.city||'';
  document.getElementById('stCounty').value=s.county||'';
  document.getElementById('stWebhook').value=s.webhook_url||'';
  document.getElementById('stFormTitle').textContent='编辑站点: '+s.name;
  document.getElementById('stSubmitBtn').textContent='更新';
  document.getElementById('stSubmitBtn').onclick=function(){addStation()};
  document.getElementById('stCancelEditBtn').style.display='';
};
function addStation(){
  var data={
    name:document.getElementById('stName').value.trim(),
    location:document.getElementById('stLocation').value.trim(),
    lat:parseFloat(document.getElementById('stLat').value),
    lon:parseFloat(document.getElementById('stLon').value),
    alt:parseFloat(document.getElementById('stAlt').value)||0,
    province:document.getElementById('stProvince').value,
    city:document.getElementById('stCity').value,
    county:document.getElementById('stCounty').value,
    webhook_url:document.getElementById('stWebhook').value.trim()
  };
  if(!data.name){UI.Message.warning('站点名称不能为空');return}
  if(_editingStName2){
    Api.put('/api/stations', {original_name:_editingStName2, data:data}).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      _resetStForm(); loadStations();
    }).catch(catchErr('更新站点失败'));
  }else{
    Api.post('/api/stations', data).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      _resetStForm(); loadStations();
    }).catch(catchErr('添加站点失败'));
  }
}
window.delStation = function(name){
  UI.Message.confirm('确定删除站点 '+name+' 吗？').then(function(ok){
    if(!ok) return;
    Api.del('/api/stations', {name:name}).then(function(res){
      if(res.error){UI.toast(res.error,'error');return}
      loadStations();
    }).catch(catchErr('删除站点失败'));
  });
};

// ═══════════ Users ═══════════
function loadUsers(){ Api.get('/api/users').then(function(users){userData=users||[];renderUserList()}).catch(catchErr('加载用户失败'))}
function renderUserList(){
  var div=document.getElementById('userList');
  if(!userData.length){div.innerHTML='<div class="empty-state"><div class="msg">暂无用户</div><div class="sub">点击「新增用户」添加</div></div>';return}
  div.innerHTML=userData.map(function(u){
    return '<div class="crud-item"><span><b>'+UI.escapeHtml(u.username)+'</b> <span style="color:var(--blue);font-size:11px">'+({admin:'系统管理员',tenant_admin:'租户管理员',user:'站点用户'}[u.role]||'操作员')+'</span></span><span style="color:var(--muted);font-size:10px">'+UI.escapeHtml(u.station||'-')+'</span><span><button class="btn btn-ghost btn-xs" onclick="editUser(\''+UI.escapeAttr(u.username)+'\')">✎</button> <button class="btn btn-ghost btn-xs" onclick="resetUserPwd(\''+UI.escapeAttr(u.username)+'\')">🔑</button> <button class="btn btn-ghost btn-xs" onclick="delUser(\''+UI.escapeAttr(u.username)+'\')">×</button></span></div>';
  }).join('');
}
window.editUser = function(username){var u=userData.find(function(x){return x.username===username});if(!u)return;_editingUsername2=username;
  document.getElementById('usrName').value=u.username;document.getElementById('usrRole').value=u.role;document.getElementById('usrStation').value=u.station||'';
  document.getElementById('usrFormTitle').textContent='编辑用户: '+u.username;document.getElementById('usrSubmitBtn').textContent='更新';document.getElementById('usrCancelEditBtn').style.display='';document.getElementById('usrModal').classList.add('show')};
function addUser(){
  var data={username:document.getElementById('usrName').value.trim(),password:document.getElementById('usrPassword').value,role:document.getElementById('usrRole').value,station:document.getElementById('usrStation').value.trim()};
  if(!data.username){UI.Message.warning('用户名不能为空');return}
  if(_editingUsername2){Api.put('/api/users',{original_username:_editingUsername2,data:data}).then(function(res){if(res.error){UI.toast(res.error,'error');return}_resetUsrForm();loadUsers()}).catch(catchErr('更新用户失败'))}
  else{Api.post('/api/users',data).then(function(res){if(res.error){UI.toast(res.error,'error');return}_resetUsrForm();loadUsers()}).catch(catchErr('添加用户失败'))}
}
window.delUser=function(username){UI.Message.confirm('确定删除用户 '+username+' 吗？').then(function(ok){if(!ok)return;Api.del('/api/users',{username:username}).then(function(res){if(res.error){UI.toast(res.error,'error');return}loadUsers()}).catch(catchErr('删除用户失败'))})};
window.resetUserPwd=function(username){UI.Message.confirm('确定重置用户 '+username+' 的密码吗？').then(function(ok){if(!ok)return;var chars='ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789',pwd='';for(var i=0;i<10;i++)pwd+=chars[Math.floor(chars.length*Math.random())];Api.post('/api/users/'+encodeURIComponent(username)+'/reset-password',{new_password:pwd}).then(function(res){if(res.error){UI.toast(res.error,'error');return}UI.toast('密码已重置为: '+pwd,'ok')}).catch(catchErr('重置密码失败'))})};
function _resetUsrForm(){_editingUsername2=null;['usrName','usrPassword','usrStation'].forEach(function(id){document.getElementById(id).value=''});document.getElementById('usrFormTitle').textContent='新增用户';document.getElementById('usrSubmitBtn').textContent='添加';document.getElementById('usrCancelEditBtn').style.display='none';document.getElementById('usrModal').classList.remove('show')}
window.openUsrModal=function(){
  _resetUsrForm();
  Api.get('/api/stations').then(function(stations){
    var sel=document.getElementById('usrStation');
    sel.innerHTML='<option value="">全部站点</option>';
    (stations||[]).forEach(function(s){sel.innerHTML+='<option value="'+UI.escapeAttr(s.name)+'">'+UI.escapeHtml(s.name)+'</option>'});
  }).catch(function(){});
  document.getElementById('usrModal').classList.add('show');
};
window.closeUsrModal=function(){_resetUsrForm()};

// ═══════════ Personnel ═══════════
function loadPersonnel(){Api.get('/api/personnel').then(function(list){psData=list||[];renderPsList()}).catch(catchErr('加载联系人失败'))}
function renderPsList(){
  var div=document.getElementById('psList');
  if(!psData.length){div.innerHTML='<div class="empty-state"><div class="msg">暂无联系人</div><div class="sub">点击「新增联系人」添加</div></div>';return}
  div.innerHTML=psData.map(function(p){return '<div class="crud-item"><span><b>'+UI.escapeHtml(p.name)+'</b> <span style="font-size:10px;color:var(--muted)">'+UI.escapeHtml(p.phone)+'</span></span><span style="font-size:10px;color:var(--muted)">'+UI.escapeHtml(p.station_name||'')+'</span><span><button class="btn btn-ghost btn-xs" onclick="delPersonnel('+p.id+')">×</button></span></div>'}).join('')
}
window.openPsModal=function(){
  document.getElementById('psModal').classList.add('show');
  Api.get('/api/stations').then(function(stations){
    var sel=document.getElementById('psStation');
    sel.innerHTML='<option value="">选择关联站点</option>';
    (stations||[]).forEach(function(s){sel.innerHTML+='<option value="'+UI.escapeAttr(s.name)+'">'+UI.escapeHtml(s.name)+'</option>'});
  }).catch(function(){});
  loadPersonnel();
};
window.closePsModal=function(){['psName','psPhone'].forEach(function(id){document.getElementById(id).value=''});document.getElementById('psModal').classList.remove('show')};
window.addPersonnel=function(){var data={name:document.getElementById('psName').value.trim(),phone:document.getElementById('psPhone').value.trim(),station:document.getElementById('psStation').value.trim()};if(!data.name||!data.phone){UI.Message.warning('姓名和电话不能为空');return}Api.post('/api/personnel',data).then(function(res){if(res.error){UI.toast(res.error,'error');return}closePsModal();loadPersonnel()}).catch(catchErr('添加联系人失败'))};
window.delPersonnel=function(id){UI.Message.confirm('确定删除此联系人吗？').then(function(ok){if(!ok)return;Api.del('/api/personnel',{id:id}).then(function(res){if(res.error){UI.toast(res.error,'error');return}loadPersonnel()}).catch(catchErr('删除联系人失败'))})};

// ═══════════ Whitelist ═══════════
function loadWhitelist(){Api.get('/api/whitelist').then(function(list){wlData=list||[];renderWlList()}).catch(catchErr('加载白名单失败'))}
function renderWlList(){
  var div=document.getElementById('wlList');
  if(!wlData.length){div.innerHTML='<div class="empty-state"><div class="msg">暂无白名单</div><div class="sub">白名单中的无人机 SN 不会触发告警</div></div>';return}
  div.innerHTML=wlData.map(function(w){return '<div class="crud-item"><span><b>'+UI.escapeHtml(w.sn)+'</b> <span style="font-size:10px;color:var(--muted)">'+('prefix'===w.match_mode?'前缀':'精确')+' · '+UI.escapeHtml(w.note||'--')+'</span></span><span><button class="btn btn-ghost btn-xs" onclick="delWhitelist('+w.id+')">×</button></span></div>'}).join('')
}
window.openWlModal=function(){document.getElementById('wlModal').classList.add('show');loadWhitelist()};
window.closeWlModal=function(){['wlSn','wlNote'].forEach(function(id){document.getElementById(id).value=''});document.getElementById('wlModal').classList.remove('show')};
window.addWhitelist=function(){var data={sn:document.getElementById('wlSn').value.trim(),match_mode:document.getElementById('wlMode').value,note:document.getElementById('wlNote').value.trim()};if(!data.sn){UI.Message.warning('SN 不能为空');return}Api.post('/api/whitelist',data).then(function(res){if(res.error){UI.toast(res.error,'error');return}closeWlModal();loadWhitelist()}).catch(catchErr('添加白名单失败'))};
window.delWhitelist=function(id){UI.Message.confirm('确定移除此白名单？').then(function(ok){if(!ok)return;Api.del('/api/whitelist',{id:id}).then(function(){loadWhitelist()}).catch(catchErr('删除白名单失败'))})};

// ═══════════ Devices ═══════════
function loadDevices(){Api.get('/api/devices').then(function(list){var div=document.getElementById('devList');var unbound=0;if(!list||!list.length){div.innerHTML='<div class="empty-state"><div class="msg">暂无设备</div><div class="sub">点击「注册设备」添加</div></div>';updateDevBadge(0);return}div.innerHTML=list.map(function(d){var revoked=d.revoked?'<span style="color:#ef4444">已吊销</span>':'<span style="color:#22c55e">正常</span>';var stationEl=d.station?UI.escapeHtml(d.station):'<span style="color:#f59e0b;font-weight:600">待绑定</span>';if(!d.station)unbound++;return '<div class="crud-item'+(d.station?'':' unbound')+'"><span><b>'+UI.escapeHtml(d.device_name)+'</b> '+revoked+' <span style="font-size:10px;color:var(--muted)">'+stationEl+'</span></span><span><button class="btn btn-ghost btn-xs" onclick="openBindDevice(\''+UI.escapeAttr(d.device_name)+'\',\''+UI.escapeAttr(d.station||'')+'\','+(d.tenant_id||0)+')">编辑</button> <button class="btn btn-ghost btn-xs" onclick="revokeDevice(\''+UI.escapeAttr(d.device_name)+'\')">吊销</button> <button class="btn btn-ghost btn-xs" onclick="delDevice(\''+UI.escapeAttr(d.device_name)+'\')">×</button></span></div>'}).join('');updateDevBadge(unbound)}).catch(catchErr('加载设备失败'))}
function updateDevBadge(n){var el=document.getElementById('devBadge');if(el){el.textContent=n||'';el.style.display=n>0?'':'none'}}
window.openDevModal=function(){document.getElementById('devModal').classList.add('show');loadDevices()};
window.closeDevModal=function(){['devName','devStation'].forEach(function(id){document.getElementById(id).value=''});document.getElementById('devResult').style.display='none';document.getElementById('devModal').classList.remove('show')};
window.addDevice=function(){var data={device_name:document.getElementById('devName').value.trim(),station:document.getElementById('devStation').value.trim()};if(!data.device_name){UI.Message.warning('设备名称不能为空');return}var btn=document.getElementById('devSaveBtn');btn.disabled=true;btn.textContent='注册中…';Api.post('/api/devices/provision',data).then(function(res){if(res.error){btn.disabled=false;btn.textContent='注册';UI.toast(res.error,'error');return}btn.textContent='已注册';btn.style.background='#16a34a';btn.style.borderColor='#16a34a';document.getElementById('devSecretOut').textContent=res.device_secret;document.getElementById('devCertSerial').textContent=res.client_cert?'已签发':'--';document.getElementById('devResult').style.display='block';loadDevices()}).catch(function(e){btn.disabled=false;btn.textContent='注册';catchErr('注册设备失败')(e)})};
window.delDevice=function(name){UI.Message.confirm('确定要删除设备 '+name+' 吗？').then(function(ok){if(!ok)return;Api.del('/api/devices/'+encodeURIComponent(name)).then(function(res){if(res.error){UI.toast(res.error,'error');return}loadDevices()}).catch(catchErr('删除设备失败'))})};
window.revokeDevice=function(name){UI.Message.confirm('确定要吊销设备 '+name+' 的证书吗？').then(function(ok){if(!ok)return;Api.post('/api/devices/'+encodeURIComponent(name)+'/revoke').then(function(res){if(res.error){UI.toast(res.error,'error');return}UI.toast('证书已吊销','warning');loadDevices()}).catch(catchErr('吊销设备失败'))})};
window.openBindDevice=function(name,station,tenantId){var sel=document.getElementById('bindStSelect');Api.get('/api/stations').then(function(stations){sel.innerHTML='<option value="">选择站点…</option>';(stations||[]).forEach(function(s){sel.innerHTML+='<option value="'+UI.escapeAttr(s.name)+'"'+(s.name===station?' selected':'')+'>'+UI.escapeHtml(s.name)+(s.device_name?' ('+UI.escapeHtml(s.device_name)+')':'')+'</option>'});document.getElementById('bindDevName').textContent=name;document.getElementById('bindStation').value=station||'';document.getElementById('bindTenantId').value=tenantId||'';document.getElementById('bindOldStation').value=station||'';document.getElementById('bindModal').classList.add('show')}).catch(function(){sel.innerHTML='<option value="">加载失败</option>';document.getElementById('bindDevName').textContent=name;document.getElementById('bindModal').classList.add('show')})};
window.submitBindDevice=function(){var name=document.getElementById('bindDevName').textContent;var station=document.getElementById('bindStSelect').value;var tenantId=parseInt(document.getElementById('bindTenantId').value)||null;var body={};if(station)body.station=station;if(tenantId)body.tenant_id=tenantId;if(!body.station&&!body.tenant_id){UI.toast('请选择站点或租户','warning');return}Api.put('/api/devices/'+encodeURIComponent(name)+'/binding',body).then(function(res){if(res.error){UI.toast(res.error,'error');return}UI.toast('设备 '+name+' 已绑定','ok');closeBindModal();loadDevices()}).catch(catchErr('绑定失败'))};
window.closeBindModal=function(){document.getElementById('bindModal').classList.remove('show')};

// ═══════════ Licenses ═══════════
function openLicPage(){document.getElementById('page-licenses').classList.add('active');refreshLicList()}
function refreshLicList(){Api.get('/api/licenses').then(function(list){var div=document.getElementById('licList');if(!list||!list.length){div.innerHTML='<div class="empty-state"><div class="msg">暂无密钥</div></div>';return}div.innerHTML=list.map(function(l){return '<div class="crud-item"><span><b>'+UI.escapeHtml(l.license_key)+'</b> <span style="font-size:10px;color:'+(l.is_active?'var(--green)':'var(--red)')+'">'+(l.is_active?'有效':'已停用')+'</span></span><span style="font-size:10px;color:var(--muted)">'+UI.escapeHtml(l.customer_name||'')+'</span><span>'+(l.is_active?'<button class="btn btn-ghost btn-xs" onclick="delLicense('+l.id+')">停用</button>':'<button class="btn btn-ghost btn-xs" onclick="reactivateLicense('+l.id+')">重新激活</button>')+'</span></div>'}).join('')}).catch(catchErr('加载密钥失败'))}
window.openLicModal=function(){document.getElementById('licModal').classList.add('show')};
window.closeLicModal=function(){['licName','licContact'].forEach(function(id){document.getElementById(id).value=''});document.getElementById('licModal').classList.remove('show')};
window.addLicense=function(){var data={customer_name:document.getElementById('licName').value.trim(),contact:document.getElementById('licContact').value.trim()};if(!data.customer_name){UI.Message.warning('客户名称不能为空');return}Api.post('/api/licenses',data).then(function(res){if(res.error){UI.toast(res.error,'error');return}['licName','licContact'].forEach(function(id){document.getElementById(id).value=''});UI.Message.success('密钥已生成: '+res.license_key);refreshLicList()}).catch(catchErr('创建密钥失败'))};
window.delLicense=function(id){UI.Message.confirm('确定要停用该密钥吗？').then(function(ok){if(!ok)return;Api.del('/api/licenses',{id:id}).then(function(res){if(res.error){UI.toast(res.error,'error');return}refreshLicList()}).catch(catchErr('停用密钥失败'))})};
window.reactivateLicense=function(id){UI.Message.confirm('确定要重新激活该密钥吗？').then(function(ok){if(!ok)return;Api.put('/api/licenses',{id:id,is_active:true}).then(function(res){if(res.error){UI.toast(res.error,'error');return}refreshLicList()}).catch(catchErr('激活密钥失败'))})};

// ═══════════ Audit ═══════════
function openAuditPage(){Api.get('/api/audit?limit=200').then(function(rows){var div=document.getElementById('auditTableBody');if(!rows||!rows.length){div.innerHTML='<div class="empty-state">暂无操作记录</div>';return}var html='<table style="width:100%;font-size:12px;border-collapse:collapse"><thead><tr style="border-bottom:1px solid var(--border);color:var(--muted)"><th style="padding:6px 4px;text-align:left">时间</th><th style="padding:6px 4px;text-align:left">操作</th><th style="padding:6px 4px;text-align:left">对象</th><th style="padding:6px 4px;text-align:left">操作者</th></tr></thead><tbody>';rows.forEach(function(r){html+='<tr style="border-bottom:1px solid var(--border-light)"><td style="padding:6px 4px">'+UI.escapeHtml(r.timestamp)+'</td><td style="padding:6px 4px">'+UI.escapeHtml(r.operation)+'</td><td style="padding:6px 4px">'+UI.escapeHtml(r.table_name||'')+(r.record_id?' #'+r.record_id:'')+'</td><td style="padding:6px 4px">'+UI.escapeHtml(r.username)+'</td></tr>';if(r.detail)html+='<tr style="border-bottom:1px solid var(--border-light);background:var(--surface2)"><td colspan="4" style="padding:4px 8px;font-size:11px;color:var(--muted)">'+UI.escapeHtml(r.detail)+'</td></tr>'});html+='</tbody></table>';div.innerHTML=html}).catch(catchErr('加载审计日志失败'))}

// ═══════════ Settings ═══════════
function loadSettings(){Api.get('/api/settings').then(function(s){if(!s)return;var el;el=document.getElementById('scThreshWarn');if(el&&s.threshold_warning!=null)el.value=s.threshold_warning;el=document.getElementById('scThreshSev');if(el&&s.threshold_severe!=null)el.value=s.threshold_severe;el=document.getElementById('scThreshCrit');if(el&&s.threshold_critical!=null)el.value=s.threshold_critical;el=document.getElementById('scFlapEn');if(el)el.checked=s.anti_flapping_enabled==='true';el=document.getElementById('scFlapIn');if(el&&s.debounce_in!=null)el.value=s.debounce_in;el=document.getElementById('scFlapOut');if(el&&s.debounce_out!=null)el.value=s.debounce_out;el=document.getElementById('scWebhookEn');if(el)el.checked=s.webhook_enabled==='true';el=document.getElementById('scWebhookUrl');if(el&&s.webhook_url!=null)el.value=s.webhook_url;el=document.getElementById('scArchiveEn');if(el)el.checked=s.raw_archive_enabled!=='false';el=document.getElementById('scRetention');if(el&&s.raw_archive_retention_days!=null)el.value=s.raw_archive_retention_days}).catch(function(){})}
window.saveSettings=function(){var data={threshold_warning:document.getElementById('scThreshWarn').value,threshold_severe:document.getElementById('scThreshSev').value,threshold_critical:document.getElementById('scThreshCrit').value,anti_flapping_enabled:document.getElementById('scFlapEn').checked?'true':'false',debounce_in:document.getElementById('scFlapIn').value,debounce_out:document.getElementById('scFlapOut').value,webhook_enabled:document.getElementById('scWebhookEn').checked?'true':'false',webhook_url:document.getElementById('scWebhookUrl').value,raw_archive_enabled:document.getElementById('scArchiveEn').checked?'true':'false',raw_archive_retention_days:document.getElementById('scRetention').value};Api.put('/api/settings',data).then(function(res){if(res.error){UI.toast(res.error,'error');return}UI.toast('设置已保存','ok')}).catch(catchErr('保存设置失败'))};

// ═══════════ Profile ═══════════
function loadProfile(){Api.get('/api/profile').then(function(p){if(p){['profUsername','profRole','profStation','profTenant'].forEach(function(id){var el=document.getElementById(id);if(el&&p[id.replace('prof','').toLowerCase()])el.textContent=p[id.replace('prof','').toLowerCase()]})}}).catch(function(){})}
window.changePassword=function(){var oldPwd=document.getElementById('oldPassword').value,newPwd=document.getElementById('newPassword').value.trim();if(!oldPwd||!newPwd){UI.Message.warning('请填写原密码和新密码');return}if(newPwd.length<6){UI.Message.warning('新密码至少6位');return}Api.put('/api/password',{old_password:oldPwd,new_password:newPwd}).then(function(res){if(res.error){UI.Message.warning(res.error);return}UI.Message.success('密码修改成功');['oldPassword','newPassword'].forEach(function(id){document.getElementById(id).value=''})}).catch(catchErr('修改密码失败'))};

// ═══════════ Tenant Info ═══════════
function refreshTenantInfo(){if(currentUser.role!=='tenant_admin'&&currentUser.role!=='user'||!currentUser.tenant_id)return;Api.get('/api/tenant/info').then(function(t){if(!t)return;var el=document.getElementById('tenantInfoSidebar');el.style.display='';el.innerHTML='<div class="nav-label">租户</div><div style="padding:6px 14px;font-size:11px"><b>'+UI.escapeHtml(t.name)+'</b><br>用户: '+t.current_users+'/'+t.max_users+'</div>'}).catch(function(){})}

// ═══════════ Trajectory ═══════════
function loadTrajectories(){
  var droneId=document.getElementById('fDroneId').value.trim();
  var fromDate=document.getElementById('fDateFrom').value;
  var toDate=document.getElementById('fDateTo').value;
  if(fromDate && toDate && fromDate > toDate){
    UI.toast('起始日期不能晚于结束日期', 'warning');
    return;
  }
  var params=new URLSearchParams();
  if(droneId) params.set('drone_id',droneId);
  if(fromDate) params.set('from',fromDate);
  if(toDate) params.set('to',toDate);
  params.set('limit','100');
  Api.get('/api/trajectories?'+params.toString()).then(function(summaries){
    var tbody=document.getElementById('trajTable');
    var empty=document.getElementById('trajEmpty');
    var count=document.getElementById('trajCount');
    if(!summaries||!summaries.length){
      tbody.innerHTML=''; empty.style.display='block'; count.textContent='0'; return;
    }
    empty.style.display='none'; count.textContent=summaries.length;
    tbody.innerHTML=summaries.map(function(s){
      return '<tr><td class="mono">'+UI.escapeHtml(s.drone_id)+'</td><td class="mono">'+(s.point_count||0)+'</td><td>'+(s.min_distance!=null?s.min_distance.toFixed(0):'-')+'</td><td class="mono">'+UI.escapeHtml(s.first_ts||'')+'</td><td class="mono">'+UI.escapeHtml(s.last_ts||'')+'</td><td>'+UI.escapeHtml(s.device_name||'-')+'</td><td><a class="btn btn-ghost btn-xs" href="/api/trajectories/'+encodeURIComponent(s.drone_id)+'/download" download>CSV</a></td></tr>';
    }).join('');
  }).catch(catchErr('加载轨迹失败'));
}
window.loadTrajectories = loadTrajectories;

function clearTrajFilters(){
  document.getElementById('fDroneId').value='';
  document.getElementById('fDateFrom').value='';
  document.getElementById('fDateTo').value='';
  loadTrajectories();
}
window.clearTrajFilters = clearTrajFilters;

// ═══════════ Init ═══════════
initSocket();
updateUI();
setInterval(function() { updateUI(); }, 5000);
setInterval(pollFallback, 10000);
document.addEventListener('click', function() {
  if (window.Notification && Notification.permission === 'default') Notification.requestPermission();
}, { once: true });

document.addEventListener('change', function(e) {
  var t = e.target;
  if (t.id === 'stProvince') { _onProvinceChange('stProvince', 'stCity', 'stCounty', '选择市'); }
  else if (t.id === 'stCity') { _onCityChange('stProvince', 'stCity', 'stCounty'); }
});

// ═══════════ Export ═══════════
window.exportAlertsCsv = function(){
  var params=new URLSearchParams();
  var lv=document.getElementById('histLevel')?document.getElementById('histLevel').value:'';
  if(lv) params.set('level',lv);
  var dr=document.getElementById('histDrone');if(dr&&dr.value.trim()) params.set('drone_id',dr.value.trim());
  window.open('/api/alerts/export?'+params.toString(),'_blank');
};
window.exportDronesCsv = function(){window.open('/api/drones/export','_blank')};
