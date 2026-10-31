'use strict';
const $=id=>document.getElementById(id);
let resumeGeneration=0;
let csrf='',viewer=null,heartbeat=null,poller=null,generation=0,lastStatus=null,source='bambu';
try{source=localStorage.getItem('print-camera')||'bambu';}catch{}
if(!['bambu','webcam'].includes(source))source='bambu';$('source').value=source;
async function api(path,body){const r=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});let d;try{d=await r.json();}catch{throw Error('Connection unavailable');}if(!r.ok){if(r.status===401){suspend();$('monitor').hidden=true;$('enrollment').hidden=false;}throw Error(d.error||'Connection unavailable');}return d;}
function age(timestamp){return timestamp===null||timestamp===undefined?null:Math.max(0,Date.now()/1000-timestamp);}
function freshness(timestamp){const seconds=age(timestamp);return seconds===null?'No observation yet':`Observed ${Math.floor(seconds)} seconds ago`;}
function render(){if(!lastStatus)return;const d=lastStatus,a=age(d.observed_at_unix),stale=a===null||a>30||!d.connected;$('connection').textContent=!d.connected?'Disconnected':stale?'Status stale':'Connected';$('state').textContent=d.state||'Unknown';$('stage').textContent=stale?'':(d.stage&&age(d.stage_observed_at_unix)<=30?d.stage:'');$('filename').textContent=d.filename||'Print filename unknown';$('freshness').textContent=freshness(d.observed_at_unix);$('freshness').classList.toggle('stale',stale);if(d.progress_percent===null){$('progress').removeAttribute('value');$('percent').textContent='Progress unknown';}else{$('progress').value=d.progress_percent;$('percent').textContent=`${d.progress_percent}%`;}$('remaining').textContent=d.remaining_minutes===null?'Time unknown':`${Math.floor(d.remaining_minutes/60)}h ${Math.round(d.remaining_minutes%60)}m remaining`;$('layers').textContent=d.layer===null||d.total_layers===null?'Layers unknown':`Layer ${d.layer} / ${d.total_layers}`;$('ams-age').textContent=freshness(d.ams_observed_at_unix);$('ams-age').classList.toggle('stale',age(d.ams_observed_at_unix)===null||age(d.ams_observed_at_unix)>30);$('slots').replaceChildren();for(let i=0;i<4;i++){const s=d.ams.find(x=>x.unit===0&&x.slot_index===i);const tile=document.createElement('div');tile.className='slot';const swatch=document.createElement('span');swatch.className='swatch';if(s&&/^#[0-9a-f]{6}$/i.test(s.color||''))swatch.style.backgroundColor=s.color;const label=document.createElement('strong');label.textContent=`Slot ${i+1}`;const detail=document.createElement('p');detail.textContent=s?`${s.name||s.material||'Unknown material'} · ${s.remaining_percent===null?'Amount unknown':s.remaining_percent+'% left'}`:'Not reported';tile.append(swatch,label,detail);$('slots').append(tile);}}
async function poll(){if(document.hidden)return;try{lastStatus=await api('/api/status');render();const h=(await api('/api/camera-health'))[source];$('camera-age').textContent=h.frame_age_seconds===null?'No fresh frame yet':`Frame age: ${Math.floor(h.frame_age_seconds)} seconds`;$('camera-message').hidden=h.state==='live';$('camera-message').textContent=h.state==='live'?'':h.state==='connecting'?'Connecting camera…':'Camera stalled or unavailable; reconnecting…';$('message').textContent='';showNotificationEvent();}catch(e){render();$('connection').textContent='Connection unavailable';$('message').textContent=e.message;}}
function stopCamera(){generation++;clearInterval(heartbeat);heartbeat=null;const old=viewer;viewer=null;$('camera').removeAttribute('src');$('camera').hidden=true;if(old&&csrf)fetch(`/api/viewer/${old}/release`,{method:'POST',credentials:'same-origin',keepalive:true,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}'}).catch(()=>{});}
async function startCamera(){stopCamera();if(document.hidden||!csrf)return;const current=generation;$('camera-message').hidden=false;$('camera-message').textContent='Connecting camera…';try{const d=await api('/api/viewer',{source});if(current!==generation||document.hidden){api(`/api/viewer/${d.viewer}/release`,{}).catch(()=>{});return;}viewer=d.viewer;$('camera').src=`/api/camera/${viewer}`;$('camera').hidden=false;heartbeat=setInterval(async()=>{try{await api(`/api/viewer/${viewer}/renew`,{});}catch{if(current===generation)startCamera();}},5000);}catch(e){$('camera-message').textContent=e.message;}}
function suspend(){clearInterval(poller);poller=null;stopCamera();}
async function resume(){if(document.hidden)return;const current=++resumeGeneration;suspend();try{const d=await api('/api/session');if(current!==resumeGeneration||document.hidden)return;csrf=d.csrf;$('enrollment').hidden=true;$('monitor').hidden=false;setupPush();showNotificationEvent();await poll();if(current!==resumeGeneration||document.hidden)return;poller=setInterval(poll,5000);startCamera();}catch(e){$('connection').textContent='Connect device';$('enrollment').hidden=false;}}
$('enroll').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/enroll',{code:$('code').value.trim(),name:$('device-name').value});$('code').value='';await resume();}catch(e){$('enroll-error').textContent=e.message;}});
$('source').addEventListener('change',()=>{source=$('source').value;try{localStorage.setItem('print-camera',source);}catch{}startCamera();});
$('camera').addEventListener('error',()=>{if(viewer&&!document.hidden)setTimeout(()=>{if(viewer&&!document.hidden)startCamera();},3000);});
$('enlarge').addEventListener('click',()=>$('camera-tile').classList.toggle('enlarged'));$('camera-tile').addEventListener('click',()=>$('camera-tile').classList.remove('enlarged'));
$('logout').addEventListener('click',async()=>{suspend();try{await api('/api/logout',{});}finally{csrf='';$('monitor').hidden=true;$('enrollment').hidden=false;}});
document.addEventListener('visibilitychange',()=>document.hidden?suspend():resume());window.addEventListener('pagehide',suspend);window.addEventListener('pageshow',()=>{if(!poller)resume();});window.addEventListener('online',resume);

let pushRegistration=null,pushKey=null,pushSetup=null,shownEvent=null;
function decodeKey(value){const raw=atob(value.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-value.length%4)%4));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
async function setupPush(){
  if(!('serviceWorker' in navigator)||!('PushManager' in window)||!('Notification' in window)){$('push-status').textContent='For iPhone alerts, add this app to your Home Screen and open it there.';return;}
  try{
    pushRegistration=await navigator.serviceWorker.register('/sw.js',{scope:'/',updateViaCache:'none'});
    await navigator.serviceWorker.ready;
    const info=await api('/api/push');pushKey=decodeKey(info.public_key);
    const subscription=await pushRegistration.pushManager.getSubscription();
    const enabled=info.enabled&&!!subscription&&Notification.permission==='granted';
    $('push-enable').hidden=enabled;$('push-enable').disabled=Notification.permission==='denied';
    $('push-test').hidden=!enabled;$('push-disable').hidden=!info.enabled;
    $('push-status').textContent=enabled?'Error and completion alerts enabled for this device.':Notification.permission==='denied'?'Notifications are blocked. Allow them in iPhone Settings → Notifications → Print Station.':'Enable error and completion alerts on this device. On iPhone, open the Home Screen app first.';
  }catch(e){$('push-status').textContent=e.message;}
}
$('push-enable').addEventListener('click',async()=>{
  // Permission request must run directly from the tap, before any network await.
  const permission=await Notification.requestPermission();
  if(permission!=='granted'){await setupPush();return;}
  $('push-enable').disabled=true;
  try{
    const registration=await navigator.serviceWorker.ready;
    const subscription=await registration.pushManager.getSubscription()||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:pushKey});
    await api('/api/push/subscribe',subscription.toJSON());await setupPush();
  }catch(e){$('push-status').textContent=e.message;$('push-enable').disabled=false;}
});
$('push-disable').addEventListener('click',async()=>{
  try{await api('/api/push/unsubscribe',{});const registration=await navigator.serviceWorker.ready;const subscription=await registration.pushManager.getSubscription();if(subscription)await subscription.unsubscribe();await setupPush();}catch(e){$('push-status').textContent=e.message;}
});
$('push-test').addEventListener('click',async()=>{
  $('push-test').disabled=true;
  try{await api('/api/push/test',{});$('push-status').textContent='Test queued. It should arrive shortly, with a bed photo available in the app.';}catch(e){$('push-status').textContent=e.message;}
  finally{setTimeout(()=>$('push-test').disabled=false,60000);}
});
async function showNotificationEvent(){
  const wanted=new URLSearchParams(location.search).get('event');
  const valid=wanted&&/^[a-f0-9]{32}$/.test(wanted);
  try{
    const value=await api(valid?'/api/notifications/'+wanted:'/api/notifications/latest');
    if(!value){$('notification-event').hidden=true;return;}
    if(shownEvent===value.id)return;shownEvent=value.id;
    $('notification-event').hidden=false;$('notification-title').textContent=value.title;
    $('notification-time').textContent=new Date(value.created*1000).toLocaleString();$('notification-body').textContent=value.body;
    $('notification-photo').hidden=!value.image;
    if(value.image)$('notification-photo').src='/api/notifications/'+value.id+'/photo';else $('notification-photo').removeAttribute('src');
    if(valid)$('notification-event').open=true;
  }catch(e){if(valid){$('notification-event').hidden=false;$('notification-title').textContent='Notification no longer available';$('notification-body').textContent='Saved notifications and photos are kept for up to 24 hours.';}}
}

resume();
