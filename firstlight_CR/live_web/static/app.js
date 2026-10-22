const $ = id => document.getElementById(id);
const apiPath = path => new URL(path.replace(/^\//,''), document.baseURI).pathname;
const names = {26000009:'石头人',28000007:'大电',26000048:'暗夜女巫',26000015:'飞龙宝宝',27000009:'墓碑',28000011:'滚木',26000043:'精锐野蛮人',26000039:'重甲亡灵'};
const states = {downloading:'下载中',preparing:'准备中',waiting_battle:'等待对局',matching:'匹配中',waiting_friendly:'等待友谊战',playing:'对战中',finishing:'结算中',stopping:'停止中',completed:'已结束',failed:'异常',stopped:'已停止',interrupted:'已中断'};
const requestedMode = new URLSearchParams(location.search).get('mode');
if (['standard','friendly'].includes(requestedMode)) $('match-mode').value = requestedMode;
let checkpoints = [], busy = false, submitting = false, screenUrl = null, lastHistoryKey = '', lastEventsKey = '', lastJob = null;
let catalogLoading = false, catalogStatus = {};
const icons = () => lucide.createIcons();
const cardName = id => names[id] || (id ? `卡牌 ${id}` : '-');
const clock = tick => {const seconds = Math.floor((tick || 0) / 20); return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, body) {
  const response = await fetch(apiPath(path), body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Live-Control':'1'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成');
  return data;
}
function notice(text='') {$('notice').textContent=text; $('notice').hidden=!text;}
function controls() {
  const active = busy || submitting;
  $('start').disabled = active || !$('checkpoint').value;
  $('stop').disabled = !busy || lastJob?.state === 'stopping';
  $('checkpoint').disabled = active;
  $('match-mode').disabled = active;
  $('friendly-help').hidden = $('match-mode').value !== 'friendly';
  document.title = $('match-mode').value === 'friendly' ? 'CR 工作台 · 人机友谊战' : 'CR 工作台 · 私服对战';
  $('weight-search').disabled = active;
  $('add-weight').disabled = active;
  $('refresh').disabled = catalogStatus.refreshing || catalogLoading;
  $('start').querySelector('span').textContent = active ? '本局运行中' : $('match-mode').value === 'friendly' ? '准备 AI，等待友谊战' : '开始一局';
}
function weightDetails() {
  const selected = checkpoints.find(c=>c.id===$('checkpoint').value);
  $('weight-name').textContent=selected?.name || '暂无可用权重';
  $('weight-name').title=selected?.path || selected?.remote_path || '';
  $('weight-version').textContent=selected?.version?.replace('event-checkpoint.', '') || '-';
  $('weight-residual').textContent=selected?.residual_blocks ?? '-';
  $('weight-size').textContent=selected?.bytes ? `${(selected.bytes/1024/1024).toFixed(1)} MB` : '-';
  $('weight-source').textContent=selected ? (selected.source==='remote' ? (selected.cached?'远端 · 已缓存':'远端 · 待下载') : '本机') : '-';
  $('weight-run').textContent=selected?.run || '';
  if (selected) localStorage.setItem('cr-live-checkpoint',selected.id);
  controls();
}
function renderWeights(prefer) {
  const previous = prefer || $('checkpoint').value || localStorage.getItem('cr-live-checkpoint');
  const terms=$('weight-search').value.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const available = checkpoints.filter(c=>c.supported && c.available!==false);
  const compatible = available.filter(c=>terms.every(term=>`${c.label} ${c.category_label||''} ${c.category||''} ${c.run||''} ${c.name}`.toLowerCase().includes(term)));
  const groups=new Map();
  for(const checkpoint of compatible){
    const label=checkpoint.source==='remote' ? (checkpoint.category_label || `远端 · ${checkpoint.run}`) : '本机权重';
    if(!groups.has(label)){const group=document.createElement('optgroup');group.label=label;groups.set(label,group);}
    groups.get(label).append(new Option(checkpoint.label,checkpoint.id));
  }
  const orderedGroups=[...groups.entries()].sort(([left],[right])=>{
    const priority=label=>label.startsWith('正式训练 ·')?0:label==='正式训练'?1:label==='本机权重'?3:2;
    return priority(left)-priority(right) || left.localeCompare(right,'zh-CN');
  }).map(([,group])=>group);
  $('checkpoint').replaceChildren(...orderedGroups);
  if (!compatible.length) $('checkpoint').add(new Option(terms.length?'无匹配权重':'暂无兼容权重',''));
  if (compatible.some(c=>c.id===previous)) $('checkpoint').value=previous;
  const remote=available.filter(c=>c.source==='remote').length;
  $('catalog-note').textContent = terms.length ? `${compatible.length} 个搜索结果` : `${remote} 个远端 · ${available.length-remote} 个本机`;
  weightDetails();
}
async function loadWeights(prefer) {
  if(catalogLoading)return;
  catalogLoading=true;
  try{
    const data=await api('/api/checkpoints');
    checkpoints=data.items;catalogStatus=data.status||{};
    $('remote-status').textContent=catalogStatus.refreshing?'正在同步远端权重目录':catalogStatus.error?'远端同步失败，保留已有目录':catalogStatus.updated?`已同步 · ${new Date(catalogStatus.updated*1000).toLocaleTimeString('zh-CN')}`:'等待远端同步';
    $('remote-status').title=catalogStatus.error||'';
    $('remote-status').classList.toggle('error',Boolean(catalogStatus.error));
    renderWeights(prefer);
  }finally{catalogLoading=false;controls();}
}
async function pollWeights(){try{await loadWeights();}catch(error){$('remote-status').textContent='权重目录连接中断';}setTimeout(pollWeights,5000);}
function eventText(e) {
  if (e.event==='confirmed') return `已出牌 · ${cardName(e.card_id)}`;
  if (e.event==='unconfirmed_at_terminal') return `对局已结束 · ${cardName(e.card_id)} 未取得出牌确认`;
  if (e.event==='bound') return '对局开始，AI 已接管';
  if (e.event==='terminal') return e.same_battle ? (e.winner===e.own_side ? '本局胜利' : e.winner===-1 ? '本局平局' : '本局结束') : '对局已切换';
  if (e.event==='blocked') return `观测等待 · ${e.reason}`;
  if (e.event==='failed') return `运行中断 · ${e.reason}`;
  if (e.event==='decision') return e.act ? `计划 · ${cardName(e.card_id)} · ${(e.delay_ms/1000).toFixed(1)} 秒` : '等待时机';
  return null;
}
function renderStatus(data) {
  busy=data.busy;
  lastJob=data.job;
  const online=data.device.online;
  $('device-dot').className=`dot ${online?'online':''}`;
  $('device-state').textContent=online?'设备在线':'设备未连接';
  $('scene').textContent={lobby:'对战大厅',result:'对局结算',battle_log:'游戏战绩'}[data.device.scene] || (data.job?.state==='playing'?'正在对战':'游戏画面');
  $('live-dot').className=`dot ${busy?'running':online?'online':''}`;
  const job=data.job;
  if (job) {
    $('state-badge').textContent=job.state==='completed'?({win:'胜利',loss:'落败',draw:'平局'}[job.result]||'已结束'):states[job.state]||job.state;
    $('state-badge').className=`state-badge ${job.state}`;
    $('run-message').textContent=job.message;
    $('download-status').hidden=!job.download || !['downloading','stopping'].includes(job.state);
    if(job.download){
      const {received,total}=job.download;
      $('download-progress').value=total?received/total*100:0;
      $('download-bytes').textContent=`${(received/1048576).toFixed(1)} / ${(total/1048576).toFixed(1)} MB`;
    }
    $('confirmed').textContent=job.counts.confirmed;
    $('decisions').textContent=job.counts.decisions;
    $('battle-time').textContent=clock(job.telemetry?.tick || job.tick);
    $('latency').innerHTML=`${job.inference_ms?Math.round(job.inference_ms):'-'}<small> ms</small>`;
    $('elixir-value').textContent=job.telemetry?job.telemetry.elixir.toFixed(1):'-';
    $('elixir').value=job.telemetry?.elixir||0;
    $('hand').replaceChildren(...(job.telemetry?.hand || [null,null,null,null]).map(id=>{const span=document.createElement('span');span.textContent=cardName(id);return span;}));
    $('warning').textContent=job.warning||'';
    $('warning').hidden=!job.warning;
    $('download-log').hidden=!job.events.length;
    $('download-log').href=apiPath(`/api/jobs/${job.id}/files/policy.jsonl`);
    const eventsKey=job.id+':'+job.events.length+':'+job.events.at(-1)?.monotonic_ns;
    if (eventsKey!==lastEventsKey) {
      const events=job.events.filter(e=>eventText(e) && (e.event!=='blocked'||job.state==='playing')).slice(-24).reverse();
      $('events').innerHTML=events.map(e=>`<li class="${escapeHTML(e.event)}"><time>${clock(e.tick)}</time><span class="event-text">${escapeHTML(eventText(e))}</span></li>`).join('') || '<li class="empty-log">等待对局开始</li>';
      lastEventsKey=eventsKey;
    }
    const historyKey=job.id+':'+job.state;
    if (historyKey!==lastHistoryKey) {lastHistoryKey=historyKey;loadHistory().catch(()=>{});}
  }
  controls();
}
async function poll() {
  try {renderStatus(await api('/api/status'));}
  catch(error) {notice('对战台连接中断：'+error.message);}
  setTimeout(poll,850);
}
async function preview() {
  if (!document.hidden && !$('live-view').hidden) {
    try {
      const response=await fetch(apiPath('/api/screen'));
      if (!response.ok) throw new Error('画面暂不可用，请检查模拟器');
      const url=URL.createObjectURL(await response.blob());
      $('screen').src=url;
      if (screenUrl) URL.revokeObjectURL(screenUrl);
      screenUrl=url;
      $('screen-message').hidden=true;
      $('preview-state').textContent='实时画面';
    } catch(error) {
      $('screen-message').hidden=false;
      $('screen-message').querySelector('span').textContent=error.message;
      $('preview-state').textContent='画面未连接';
    }
  }
  setTimeout(preview,1000);
}
async function loadHistory() {
  const {items}=await api('/api/jobs');
  $('history-count').textContent=items.length;
  if (!items.length) {$('history-list').innerHTML='<div class="empty-history">暂无对局记录</div>';return;}
  $('history-list').innerHTML=`<div class="history-wrap"><table class="history-table"><thead><tr><th>开始时间</th><th>模型权重</th><th>结果</th><th>确认出牌</th><th>记录</th></tr></thead><tbody>${items.map(j=>`<tr><td>${escapeHTML(new Date(j.created_at*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}))}<small>${clock(j.tick)}</small></td><td>${escapeHTML(j.checkpoint.label)}<small>${escapeHTML(j.checkpoint.name)}</small></td><td class="${escapeHTML(j.result)}">${escapeHTML({win:'胜利',loss:'落败',draw:'平局'}[j.result]||states[j.state])}<small>${escapeHTML(j.state==='failed'?j.message.slice(0,90):'')}</small></td><td>${j.counts.confirmed}</td><td><div class="history-actions"><a class="icon-button" title="下载运行记录" aria-label="下载运行记录" href="/api/jobs/${j.id}/files/summary.json"><i data-lucide="download"></i></a>${j.has_result_image?`<a class="icon-button" title="查看终局画面" aria-label="查看终局画面" target="_blank" href="/api/jobs/${j.id}/files/result.png"><i data-lucide="image"></i></a>`:''}</div></td></tr>`).join('')}</tbody></table></div>`;
  icons();
  $('history-list').querySelectorAll('a[href^="/api/"]').forEach(a=>a.setAttribute('href',apiPath(a.getAttribute('href'))));
}
$('start-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(busy||submitting)return;
  submitting=true;controls();notice();
  try {await api('/api/start',{checkpoint_id:$('checkpoint').value,match_mode:$('match-mode').value});renderStatus(await api('/api/status'));}
  catch(error){notice(error.message);}
  finally{submitting=false;controls();}
});
$('stop').addEventListener('click',async()=>{try{renderStatus(await api('/api/stop',{}));}catch(error){notice(error.message);}});
$('checkpoint').addEventListener('change',weightDetails);
$('match-mode').addEventListener('change',controls);
$('weight-search').addEventListener('input',()=>renderWeights());
$('refresh').addEventListener('click',async()=>{try{await api('/api/checkpoints/refresh',{});await loadWeights();}catch(e){notice(e.message);}});
$('refresh-history').addEventListener('click',()=>loadHistory().catch(e=>notice(e.message)));
$('add-weight').addEventListener('click',()=>{$('weight-error').textContent='';$('weight-dialog').showModal();$('weight-path').focus();});
$('close-dialog').addEventListener('click',()=>$('weight-dialog').close());
$('weight-form').addEventListener('submit',async event=>{event.preventDefault();$('import-weight').disabled=true;try{const item=await api('/api/checkpoints',{path:$('weight-path').value.trim()});await loadWeights(item.id);$('weight-dialog').close();}catch(e){$('weight-error').textContent=e.message;}finally{$('import-weight').disabled=false;}});
$('fullscreen').addEventListener('click',async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await $('screen-stage').requestFullscreen();}catch(e){notice(e.message);}});
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>{
  document.querySelectorAll('[data-tab]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-selected',String(b===button));});
  $('live-view').hidden=button.dataset.tab!=='live';$('history-view').hidden=button.dataset.tab!=='history';
  if(button.dataset.tab==='history')loadHistory().catch(e=>notice(e.message));
}));
icons();pollWeights();loadHistory().catch(e=>notice(e.message));poll();preview();
