/**
 * Dashboard (list view) entry point.
 * All page-specific logic extracted from dashboard.html inline script.
 */
import './api.js';
import './ui.js';

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
  licenses:'密钥管理', audit:'审计日志', profile:'用户信息管理'
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
    if(this.dataset.page==='trajectory') loadTrajectories();
    if(this.dataset.page==='powerlines') loadPowerLines();
    if(this.dataset.page==='stations') loadStations();
    if(this.dataset.page==='users') loadUsers();
    if(this.dataset.page==='personnel') loadPersonnel();
    if(this.dataset.page==='whitelist') loadWhitelist();
    if(this.dataset.page==='licenses') openLicPage();
    if(this.dataset.page==='audit') openAuditPage();
    if(this.dataset.page==='profile') loadProfile();
  });
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
    icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🚁</text></svg>',
    tag: droneId,
  });
}

// ═══════════ SVG drone icon ═══════════
function droneSvg(status){
  var colors={active:'#67c23a',warning:'#e6a23c',severe:'#f56c6c',critical:'#f56c6c',gone:'#c0c4cc'};
  var c=colors[status]||colors.active;
  return '<svg class="drone-svg '+status+'" width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 1L5 4h2v3L3 8l-2-1v1l2 1.5v2l5 2.5 5-2.5v-2l2-1.5V7l-2 1-4-1V4h2L8 1z" stroke="'+c+'" stroke-width="1.5"/><circle cx="8" cy="8" r="1" fill="'+c+'"/></svg>';
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
      document.getElementById('navLicenses').style.display=isAdmin?'':'none';
      document.getElementById('navAudit').style.display=isAdmin?'':'none';
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
    // Alert badge
    var ac=(d.alerts||[]).length;
    var ab=document.getElementById('alertBadge');
    if(ac>0){ab.style.display='';ab.textContent=ac;}else{ab.style.display='none';}
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

window.updateDroneTable = function(){
  var searchTerm=(document.getElementById('droneSearch').value||'').toLowerCase();
  var statusFilter=document.getElementById('statusFilter').value;
  var drones=lastDrones;
  if(searchTerm){
    drones=drones.filter(function(dr){
      var id=(dr.id||'').toLowerCase(), model=(dr.product_model||'').toLowerCase(), cat=(dr.category_name||'').toLowerCase();
      return id.includes(searchTerm)||model.includes(searchTerm)||cat.includes(searchTerm);
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
        '</td><td style="font-weight:500">'+e(dr.product_model||'-')+'</td>'+
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
window.togglePlForm = function(){ document.getElementById('plForm').classList.toggle('show'); };
function _resetPlForm(){
  _editingPlId=null;
  document.getElementById('plName').value='';document.getElementById('plVoltage').value='';
  document.getElementById('plLat1').value='';document.getElementById('plLon1').value='';document.getElementById('plAlt1').value='0';
  document.getElementById('plLat2').value='';document.getElementById('plLon2').value='';document.getElementById('plAlt2').value='0';
  document.getElementById('plTh1').value='';document.getElementById('plTh2').value='';
  document.getElementById('plAltHint').style.display='none';
  document.getElementById('plSaveBtn').textContent='保存';
  document.getElementById('plCancelEditBtn').style.display='none';
  document.getElementById('plCancelBtn').style.display='inline-block';
}
window.cancelEditPl = function(){ _resetPlForm(); };
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
  });
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
  document.getElementById('plCancelEditBtn').style.display='inline-block';
  document.getElementById('plCancelBtn').style.display='none';
  onPlFieldChange();
  if(!document.getElementById('plForm').classList.contains('show')) togglePlForm();
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
  if(!data.name){alert('电力线名称不能为空');return}
  if(isNaN(data.lat1)||isNaN(data.lon1)||isNaN(data.lat2)||isNaN(data.lon2)){alert('请填写有效的经纬度坐标');return}
  var method=_editingPlId?'PUT':'POST';
  var url=_editingPlId?'/api/powerlines/'+_editingPlId:'/api/powerlines';
  Api[method.toLowerCase()](url, data).then(function(){
    _resetPlForm(); togglePlForm(); loadPowerLines();
  });
};
window.delPowerLine = function(idx){
  if(!confirm('确定删除此电力线？')) return;
  var lineId=plData[idx]&&plData[idx].id;
  if(!lineId) return;
  Api.del('/api/powerlines/'+lineId).then(function(){loadPowerLines()});
};

// ═══════════ Stations CRUD ═══════════
window.toggleStForm = function(){ document.getElementById('stForm').classList.toggle('show'); };
function _resetStForm2(){
  _editingStName2=null;
  document.getElementById('stName').value='';document.getElementById('stDevice').value='';
  document.getElementById('stLocation').value='';
  document.getElementById('stProvince').value='';document.getElementById('stCity').value='';document.getElementById('stCounty').value='';
  document.getElementById('stLat').value='';document.getElementById('stLon').value='';document.getElementById('stAlt').value='';
  document.getElementById('stSaveBtn').textContent='保存';
  document.getElementById('stCancelEditBtn').style.display='none';
  document.getElementById('stCancelBtn').style.display='inline-block';
}
window.cancelEditStation = function(){ _resetStForm2(); };
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
  Api.get('/api/stations').then(function(d){
    _allStations=d||[];
    renderStationList(_allStations);
  });
};
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
  document.getElementById('stDevice').value=s.device_name||'';
  document.getElementById('stLocation').value=s.location||'';
  document.getElementById('stProvince').value=s.province||'';
  document.getElementById('stCity').value=s.city||'';
  document.getElementById('stCounty').value=s.county||'';
  document.getElementById('stLat').value=s.lat||0;
  document.getElementById('stLon').value=s.lon||0;
  document.getElementById('stAlt').value=s.alt||0;
  document.getElementById('stSaveBtn').textContent='更新';
  document.getElementById('stCancelEditBtn').style.display='inline-block';
  document.getElementById('stCancelBtn').style.display='none';
  if(!document.getElementById('stForm').classList.contains('show')) toggleStForm();
};
window.addStation = function(){
  var data={
    name:document.getElementById('stName').value.trim(),
    device_name:document.getElementById('stDevice').value.trim(),
    location:document.getElementById('stLocation').value.trim(),
    province:document.getElementById('stProvince').value.trim(),
    city:document.getElementById('stCity').value.trim(),
    county:document.getElementById('stCounty').value.trim(),
    lat:parseFloat(document.getElementById('stLat').value)||0,
    lon:parseFloat(document.getElementById('stLon').value)||0,
    alt:parseFloat(document.getElementById('stAlt').value)||0
  };
  if(!data.name){alert('站点名称不能为空');return}
  var method=_editingStName2?'PUT':'POST';
  Api[method.toLowerCase()]('/api/stations', data).then(function(res){
    if(res.error){alert(res.error);return}
    document.getElementById('stName').readOnly=false;
    _resetStForm2(); toggleStForm(); loadStations();
  });
};
window.delStation = function(name){
  if(!confirm('确定删除站点 '+name+'？')) return;
  Api.del('/api/stations', {name:name}).then(function(){loadStations()});
};

// ═══════════ Users CRUD ═══════════
window.toggleUserForm = function(){ document.getElementById('userForm').classList.toggle('show'); };
function _resetUserForm2(){
  _editingUsername2=null;
  document.getElementById('uName').value='';document.getElementById('uName').readOnly=false;
  document.getElementById('uPwd').value='';document.getElementById('uPwd').placeholder='密码';
  document.getElementById('uRole').value='user';
  document.getElementById('uScope').value='station';
  document.getElementById('uStation').value='';
  document.getElementById('uSaveBtn').textContent='保存';
  document.getElementById('uCancelEditBtn').style.display='none';
  document.getElementById('uCancelBtn').style.display='inline-block';
}
window.cancelEditUser = function(){ _resetUserForm2(); };
window.loadUsers = function(){
  Api.get('/api/users').then(function(d){
    userData=Array.isArray(d)?d:(d.users||[]);
    var el=document.getElementById('userList');
    if(userData.length===0){
      el.innerHTML='<div class="empty-state"><div class="msg">暂无用户</div><div class="sub">点击「新增用户」添加</div></div>';
    }else{
      el.innerHTML=userData.map(function(u,i){
        return '<div class="crud-item"><div class="info"><b>'+UI.escapeHtml(u.username)+'</b><div class="meta">角色: '+UI.escapeHtml(u.role)+' &nbsp;|&nbsp; 站点: '+UI.escapeHtml(u.assigned_station||u.station||'-')+'</div></div><div class="actions"><button class="btn btn-ghost btn-xs" data-edit-user="'+i+'">编辑</button><button class="del" data-del-user="'+UI.escapeAttr(u.username)+'">删除</button></div></div>';
      }).join('');
    }
  });
};
window.editUser2 = function(idx){
  var u=userData[idx]; if(!u) return;
  _editingUsername2=u.username;
  document.getElementById('uName').value=u.username;
  document.getElementById('uName').readOnly=true;
  document.getElementById('uPwd').value='';document.getElementById('uPwd').placeholder='留空则不改密码';
  document.getElementById('uRole').value=u.role||'user';
  document.getElementById('uScope').value=u.scope||'station';
  document.getElementById('uStation').value=u.assigned_station||u.station||'';
  document.getElementById('uSaveBtn').textContent='更新';
  document.getElementById('uCancelEditBtn').style.display='inline-block';
  document.getElementById('uCancelBtn').style.display='none';
  if(!document.getElementById('userForm').classList.contains('show')) toggleUserForm();
};
window.addUser = function(){
  var data={username:document.getElementById('uName').value, password:document.getElementById('uPwd').value, role:document.getElementById('uRole').value, scope:document.getElementById('uScope').value, station:document.getElementById('uStation').value};
  if(!data.username){alert('用户名不能为空');return}
  if(!_editingUsername2&&!data.password){alert('密码不能为空');return}
  var method=_editingUsername2?'PUT':'POST';
  Api[method.toLowerCase()]('/api/users', data).then(function(res){
    if(res.error){alert(res.error);return}
    _resetUserForm2(); toggleUserForm(); loadUsers();
  });
};
window.delUser = function(username){
  if(!confirm('确定删除用户 '+username+'？')) return;
  Api.del('/api/users', {username:username}).then(function(){loadUsers()});
};
window.resetUserPwd = function(){
  var u=prompt('输入要重置密码的用户名:'); if(!u) return;
  var p=prompt('输入新密码:'); if(!p) return;
  Api.post('/api/users/'+u+'/reset-password', {new_password:p}).then(function(d){alert(d.error||'已重置')});
};

// ═══════════ Personnel CRUD ═══════════
window.togglePersonForm = function(){
  var form=document.getElementById('psForm');
  form.classList.toggle('show');
  if(form.classList.contains('show')){
    Api.get('/api/stations').then(function(stations){
      var sel=document.getElementById('psStation');
      sel.innerHTML='<option value="">选择关联站点</option>'+stations.map(function(s){
        return '<option value="'+UI.escapeAttr(s.name)+'">'+UI.escapeHtml(s.name)+(s.location?' ('+UI.escapeHtml(s.location)+')':'')+'</option>';
      }).join('');
    }).catch(function(){});
  }
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
  });
};
window.addPerson = function(){
  var sel=document.getElementById('psStation');
  var data={name:document.getElementById('psName').value, phone:document.getElementById('psPhone').value, station_name:sel.value};
  if(!data.name||!data.phone||!data.station_name){alert('请填写姓名、联系电话和关联站点');return}
  if(!/^1\d{10}$/.test(data.phone)){alert('联系电话格式无效，需为11位手机号');return}
  Api.post('/api/personnel', data).then(function(){
    document.getElementById('psName').value='';document.getElementById('psPhone').value='';
    sel.value='';
    togglePersonForm(); loadPersonnel();
  });
};
window.delPerson = function(idx){
  if(!confirm('确定删除此联系人？')) return;
  var p=psData[idx]; if(!p||!p.id) return;
  Api.del('/api/personnel', {id:p.id}).then(function(){loadPersonnel()});
};

// ═══════════ Whitelist CRUD ═══════════
window.toggleWlForm = function(){ document.getElementById('wlForm').classList.toggle('show'); };
window.loadWhitelist = function(){
  Api.get('/api/whitelist').then(function(d){
    wlData=d||[];
    var el=document.getElementById('wlList');
    if(wlData.length===0){
      el.innerHTML='<div class="empty-state"><div class="msg">暂无白名单</div><div class="sub">白名单中的无人机 SN 不会触发告警</div></div>';
    }else{
      el.innerHTML=wlData.map(function(w){
        return '<div class="crud-item"><div class="info"><b>'+UI.escapeHtml(w.sn)+'</b><div class="meta">匹配: '+(w.match_mode==='prefix'?'前缀':'精确')+' &nbsp;|&nbsp; '+UI.escapeHtml(w.note||'--')+' &nbsp;|&nbsp; '+UI.escapeHtml(w.created_by)+' @ '+(w.created_at||'').slice(0,10)+'</div></div><div class="actions"><button class="del" data-del-wl="'+w.id+'">删除</button></div></div>';
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
  if(!data.sn){alert('SN 不能为空');return}
  Api.post('/api/whitelist', data).then(function(res){
    if(res.error){alert(res.error);return}
    document.getElementById('wlSn').value='';document.getElementById('wlNote').value='';
    toggleWlForm(); loadWhitelist();
  });
};
window.delWhitelist = function(id){
  if(!confirm('确定移除此白名单？')) return;
  Api.del('/api/whitelist', {id:id}).then(function(){loadWhitelist()});
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

// ═══════════ License Management ═══════════
window.openLicPage = function(){
  document.getElementById('licForm').style.display='none';
  loadLicenses();
};
window.toggleLicForm = function(){ document.getElementById('licForm').style.display=document.getElementById('licForm').style.display==='none'?'':'none'; };

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
  if(!data.name){alert('客户名称不能为空');return}
  Api.post('/api/licenses', data).then(function(res){
    if(res.error){alert(res.error);return}
    document.getElementById('licName').value='';
    document.getElementById('licContact').value='';
    alert('密钥已生成: '+res.license_key);
    loadLicenses();
  }).catch(catchErr('创建密钥失败'));
};

window.delLicense = function(id){
  if(!confirm('确定要停用该密钥吗？所有关联用户将无法操作。')) return;
  Api.del('/api/licenses', {id:id}).then(function(res){
    if(res.error){alert(res.error);return}
    loadLicenses();
  }).catch(catchErr('停用密钥失败'));
};

window.reactivateLicense = function(id){
  if(!confirm('确定要重新激活该密钥吗？')) return;
  Api.put('/api/licenses', {id:id, is_active:true}).then(function(res){
    if(res.error){alert(res.error);return}
    loadLicenses();
  }).catch(catchErr('激活密钥失败'));
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
      html+='<td style="padding:6px 4px">'+UI.escapeHtml(r.operator)+'</td>';
      html+='</tr>';
      if(r.details){
        html+='<tr style="border-bottom:1px solid var(--border);background:var(--info-light)"><td colspan="4" style="padding:4px 8px;font-size:10px;color:var(--text-secondary)">'+UI.escapeHtml(r.details)+'</td></tr>';
      }
      return html;
    }).join('');
  }).catch(catchErr('加载审计日志失败'));
}

// ═══════════ Event Delegation for data-* buttons ═══════════
UI.delegate(document.getElementById('stList'), 'click', '[data-edit-st]', function(){ editStation2(this.dataset.editSt); });
UI.delegate(document.getElementById('stList'), 'click', '[data-del-st]', function(){ delStation(this.dataset.delSt); });
UI.delegate(document.getElementById('plList'), 'click', '[data-edit-pl]', function(){ editPowerLine(parseInt(this.dataset.editPl)); });
UI.delegate(document.getElementById('plList'), 'click', '[data-del-pl]', function(){ delPowerLine(parseInt(this.dataset.delPl)); });
UI.delegate(document.getElementById('userList'), 'click', '[data-edit-user]', function(){ editUser2(parseInt(this.dataset.editUser)); });
UI.delegate(document.getElementById('userList'), 'click', '[data-del-user]', function(){ delUser(this.dataset.delUser); });
UI.delegate(document.getElementById('psList'), 'click', '[data-del-ps]', function(){ delPerson(parseInt(this.dataset.delPs)); });
UI.delegate(document.getElementById('wlList'), 'click', '[data-del-wl]', function(){ delWhitelist(parseInt(this.dataset.delWl)); });
UI.delegate(document.getElementById('trajTable'), 'click', '[data-trajectory-drone]', function(){ showTrajDetail(this.dataset.trajectoryDrone); });
UI.delegate(document.getElementById('licList'), 'click', '[data-del-lic]', function(){ delLicense(parseInt(this.dataset.delLic)); });
UI.delegate(document.getElementById('licList'), 'click', '[data-reactivate-lic]', function(){ reactivateLicense(parseInt(this.dataset.reactivateLic)); });

// ═══════════ Init ═══════════
updateUI();
pollTimer=setInterval(updateUI,2000);
