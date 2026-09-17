'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const statuses = ['passed', 'review needed', 'regenerate'];
const statusLabel = value => ({passed:'Passed', 'review needed':'Review needed', regenerate:'Regenerate'})[value] || 'Review needed';
let entries = [], models = [], busy = false, pending = false, lastJob = '';
let selectedKey = null, selectedVersion = null, returnFocusKey = null;
const editors = new Map();

function notice(message = '') {
  for (const id of ['#notice', '#detail-notice']) {
    $(id).textContent = message;
    $(id).hidden = !message;
  }
}
async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok || data.ok === false) throw Error(data.error || 'Anfrage fehlgeschlagen');
  return data;
}
function audioURL(entry, source = 'current') {
  return `audio/${encodeURIComponent(entry.key)}?version=${encodeURIComponent(entry.version)}&source=${source}`;
}
function controls() {
  document.querySelectorAll('[data-action], [data-status], .editor input, .editor select, [data-preview]').forEach(node => {
    node.disabled = busy || pending;
  });
  $('#job').textContent = busy || pending ? 'Vorgang läuft…' : 'Bereit';
}
function statusSelect(entry) {
  const selected = statuses.includes(entry.status) ? entry.status : 'review needed';
  return `<label>Status<select data-status aria-label="Status für ${esc(entry.id)}">${statuses.map(status => `<option value="${status}" ${selected === status ? 'selected' : ''} ${status === 'passed' && !entry.has_audio ? 'disabled' : ''}>${statusLabel(status)}</option>`).join('')}</select></label>`;
}
function player(entry) {
  return entry.has_audio ? `<audio controls preload="none" aria-label="Audio ${esc(entry.id)}" src="${esc(audioURL(entry))}"></audio>` : '<p class="muted">Keine Audiodatei für diesen Versuch.</p>';
}
function syncWarning(entry) {
  return entry.sync_error ? `<div class="sync"><strong>Sheet noch nicht synchronisiert</strong><div>${esc(entry.sync_error)}</div><button data-action="sync">Synchronisierung wiederholen</button></div>` : '';
}
function bindActions(root, entry) {
  root.querySelectorAll('[data-action]').forEach(button => {
    button.onclick = () => act(entry, root, button.dataset.action);
  });
  $('[data-status]', root).onchange = event => act(entry, root, 'status', {status: event.target.value});
  root.querySelectorAll('audio').forEach(audio => audio.onplay = () => {
    document.querySelectorAll('audio').forEach(other => { if (other !== audio) other.pause(); });
    for (const editor of editors.values()) editor.stop();
  });
}
function render() {
  const needle = $('#search').value.toLocaleLowerCase(), filter = $('#filter').value;
  const shown = entries.filter(entry => (filter === 'all' || (filter === 'sync' ? entry.sync_error : entry.status === filter)) && `${entry.id} ${entry.text} ${entry.reason} ${entry.voice_name || ''}`.toLocaleLowerCase().includes(needle));
  $('#count').textContent = `${shown.length} von ${entries.length} Audios · ${entries.filter(e => e.status === 'passed').length} passed · ${entries.filter(e => e.status === 'regenerate').length} regenerate`;
  $('#entries').innerHTML = shown.map(entry => `<article class="card" data-key="${esc(entry.key)}">
    <div class="card-head"><div class="identity">${esc(entry.id)}</div><span class="badge ${entry.status === 'passed' ? 'passed' : entry.status === 'regenerate' ? 'regenerate' : ''}">${statusLabel(entry.status)}</span></div>
    <div class="text">${esc(entry.text)}</div>
    <div class="meta" title="${esc(entry.filename)}">${esc(entry.filename)}</div>
    <div class="muted">Stimme: ${esc(entry.voice_name || entry.voice_id || 'Bisherige Stimme (Altbestand)')}</div>
    <div class="card-bottom">${player(entry)}${syncWarning(entry)}<div class="card-actions">${statusSelect(entry)}<button data-open aria-label="${esc(entry.id)} vergrößert bearbeiten">Bearbeiten ↗</button></div></div>
  </article>`).join('') || '<div class="empty">Keine passenden Audios vorhanden.</div>';
  document.querySelectorAll('.card').forEach(card => {
    const entry = entries.find(e => e.key === card.dataset.key);
    bindActions(card, entry);
    $('[data-open]', card).onclick = () => openDetail(entry.key);
    card.onclick = event => {
      if (!event.target.closest('button, select, label, input, audio, a')) openDetail(entry.key);
    };
  });
  if (selectedKey) {
    const selected = entries.find(e => e.key === selectedKey);
    if (!selected) $('#detail').close();
    else if (selected.version !== selectedVersion) renderDetail(selected);
  }
  controls();
}
function qcDetails(entry) {
  return `<details><summary>Prüfbefund und Transkript</summary><div class="checks">${Object.entries(entry.qc?.checks || {}).map(([name, check]) => `<div class="check ${esc(check.state)}"><strong>${esc(name)} · ${esc(({passed:'bestanden', failed:'nicht bestanden', error:'Fehler', skipped:'nicht geprüft'})[check.state] || check.state)}</strong><div>${esc(check.reason)}</div>${check.details?.defects?.map(d => `<div>${esc(d.category)} · ${esc(d.severity)}: ${esc(d.evidence)}</div>`).join('') || ''}${check.details?.model ? `<div class="muted">${esc(check.details.model)} · Prompt ${esc(check.details.prompt_version || '—')}</div>` : ''}</div>`).join('') || '<div>Kein strukturierter Befund vorhanden.</div>'}<div><strong>Transkript:</strong> ${esc(entry.transcript || '—')}</div><div><strong>Natürlichkeitswert:</strong> ${esc(entry.gemini ?? '—')} · <strong>WER:</strong> ${esc(entry.wer ?? '—')}</div>${entry.decision?.type === 'manual' ? '<div>Manuelle Entscheidung; der automatische Befund bleibt erhalten.</div>' : ''}</div></details>`;
}
function renderDetail(entry) {
  for (const editor of editors.values()) editor.close();
  editors.clear();
  selectedVersion = entry.version;
  const root = $('#detail-content');
  root.innerHTML = `<div class="detail-top"><div><h2 id="detail-title">${esc(entry.id)}</h2><div class="muted">${esc(entry.filename)} · ${esc(entry.model || 'Modell unbekannt')}<br>Stimme: ${esc(entry.voice_name || entry.voice_id || 'Bisherige Stimme (Altbestand)')}</div></div>${statusSelect(entry)}</div>
    <div class="text">${esc(entry.text)}</div><div class="reason">${esc(entry.reason)}</div>${player(entry)}${syncWarning(entry)}
    ${entry.has_audio || entry.has_original ? `<section class="editor"><h3>Audio schneiden</h3><p>Grenzen in der Wellenform ziehen oder Zeiten einstellen. Der ausgewählte Bereich wird behalten; nach dem Speichern ist eine erneute Freigabe nötig.</p>
      <label>Audioquelle<select class="source">${entry.has_audio ? '<option value="current">Aktuelle Version</option>' : ''}${entry.has_original ? '<option value="original">Ungeschnittenes Original</option>' : ''}</select></label>
      <canvas aria-label="Wellenform mit ziehbaren Schnittgrenzen"></canvas><div class="wave-status">Wellenform wird geladen…</div>
      <div class="ranges"><label>Start (Sekunden)<input class="start" type="number" min="0" step="0.001" value="0"><input class="start-range" aria-label="Schnittstart" type="range" min="0" step="0.001" value="0"></label><label>Ende (Sekunden)<input class="end" type="number" min="0" step="0.001" value="0"><input class="end-range" aria-label="Schnittende" type="range" min="0" step="0.001" value="0"></label></div>
      <div class="actions"><button data-preview>Auswahl vorhören</button><button class="primary" data-action="trim">Schnitt speichern</button></div></section>` : ''}
    <div class="actions">${entry.has_audio ? '<button data-action="recheck">Erneut automatisch prüfen</button>' : ''}<label>Stimmmodell<select class="model-select">${models.map(model => `<option ${model === entry.model ? 'selected' : ''}>${esc(model)}</option>`).join('')}</select></label><button data-action="regenerate">Jetzt neu generieren…</button></div>${qcDetails(entry)}`;
  bindActions(root, entry);
  if ($('.editor', root)) {
    const editor = new Editor(entry, root);
    editors.set(entry.key, editor);
    editor.load();
  }
  controls();
}
function openDetail(key) {
  const entry = entries.find(e => e.key === key);
  if (!entry) return;
  document.querySelectorAll('audio').forEach(audio => audio.pause());
  selectedKey = returnFocusKey = key;
  $('#detail').showModal();
  renderDetail(entry);
}
$('#close-detail').onclick = () => $('#detail').close();
$('#detail').addEventListener('click', event => {
  if (event.target === $('#detail')) {
    const bounds = event.target.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.target.close();
  }
});
$('#detail').addEventListener('close', () => {
  for (const editor of editors.values()) editor.close();
  editors.clear();
  $('#detail-content').querySelectorAll('audio').forEach(audio => audio.pause());
  $('#detail-content').innerHTML = '';
  selectedKey = selectedVersion = null;
  const card = Array.from(document.querySelectorAll('.card')).find(node => node.dataset.key === returnFocusKey);
  if (card) $('[data-open]', card).focus({preventScroll: true});
});
async function reload() {
  const data = await request('api/entries');
  const order = new Map(entries.map((entry, index) => [entry.key, index]));
  entries = data.entries.sort((a, b) => (order.get(a.key) ?? -1) - (order.get(b.key) ?? -1));
  models = data.models;
  render();
}
async function act(entry, root, action, options = {}) {
  if (busy || pending) return;
  if (action === 'regenerate') {
    if (!confirm('Dieses Audio jetzt neu generieren? Dabei werden ElevenLabs- und gegebenenfalls Gemini-Kosten ausgelöst.')) return;
    options.model = $('.model-select', root).value;
  }
  if (action === 'trim') {
    const editor = editors.get(entry.key);
    if (!editor?.buffer) { notice('Bitte warten, bis die Audioquelle geladen ist.'); return; }
    options = {source: $('.source', root).value, start: Number($('.start', root).value), end: Number($('.end', root).value)};
    if (options.end - options.start < .08) { notice('Bitte mindestens 80 ms auswählen.'); return; }
  }
  pending = true;
  controls();
  notice();
  try {
    await request('api/action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key: entry.key, version: entry.version, action, options})});
    busy = true;
  } catch (error) {
    notice(error.message);
    $('[data-status]', root).value = entry.status;
  } finally { pending = false; controls(); }
}
class Editor{
  constructor(entry,card){this.entry=entry;this.card=card;this.buffer=null;this.context=null;this.player=null;this.serial=0;this.closed=false;
    $('.source',card).onchange=()=>this.load();
    for(const name of ['start','end'])for(const suffix of ['','-range'])$('.'+name+suffix,card).oninput=ev=>{const value=Number(ev.target.value);$('.'+name+(suffix?'':'-range'),card).value=value;this.draw();};
    $('[data-preview]',card).onclick=()=>this.preview();
    const canvas=$('canvas',card);
    const drag=event=>{
      if(!this.buffer||busy||pending||!this.dragging)return;
      const bounds=canvas.getBoundingClientRect();
      const seconds=Math.max(0,Math.min(this.buffer.duration,(event.clientX-bounds.left)/bounds.width*this.buffer.duration));
      const start=Number($('.start',card).value),end=Number($('.end',card).value);
      const value=this.dragging==='start'?Math.max(0,Math.min(seconds,end-.08)):Math.min(this.buffer.duration,Math.max(seconds,start+.08));
      for(const suffix of ['', '-range'])$('.'+this.dragging+suffix,card).value=value.toFixed(3);
      this.draw();
    };
    canvas.onpointerdown=event=>{
      if(!this.buffer||busy||pending)return;
      const bounds=canvas.getBoundingClientRect(),seconds=(event.clientX-bounds.left)/bounds.width*this.buffer.duration;
      this.dragging=Math.abs(seconds-Number($('.start',card).value))<Math.abs(seconds-Number($('.end',card).value))?'start':'end';
      canvas.setPointerCapture(event.pointerId);drag(event);
    };
    canvas.onpointermove=drag;
    canvas.onpointerup=canvas.onpointercancel=()=>{this.dragging=null;};
    this.resize=new ResizeObserver(()=>this.draw());this.resize.observe(canvas);
  }
  async load(){const serial=++this.serial;this.buffer=null;this.stop();const label=$('.wave-status',this.card);label.textContent='Lade Audio und Wellenform…';try{
    if(!this.context)this.context=new AudioContext();
    const response=await fetch(audioURL(this.entry,$('.source',this.card).value));if(!response.ok)throw Error('Audio konnte nicht geladen werden. Bitte Seite neu laden.');
    const buffer=await this.context.decodeAudioData(await response.arrayBuffer());if(this.closed||serial!==this.serial)return;this.buffer=buffer;
    const duration=Math.floor(buffer.duration*1000)/1000;
    for(const name of ['start','end'])for(const suffix of ['','-range']){const input=$('.'+name+suffix,this.card);input.max=duration;input.value=name==='start'?0:duration;}
    label.textContent=`${duration.toFixed(3)} Sekunden · Vorschau spielt nur die Auswahl.`;this.draw();
  }catch(error){if(!this.closed&&serial===this.serial)label.textContent=error.message;}}
  draw(){if(!this.buffer)return;const canvas=$('canvas',this.card),width=canvas.width=Math.max(300,Math.round(canvas.clientWidth*devicePixelRatio)),height=canvas.height=180*devicePixelRatio,ctx=canvas.getContext('2d'),data=this.buffer.getChannelData(0);ctx.clearRect(0,0,width,height);const start=Number($('.start',this.card).value),end=Number($('.end',this.card).value);ctx.fillStyle='#c7eeee';ctx.fillRect(width*start/this.buffer.duration,0,width*(end-start)/this.buffer.duration,height);ctx.strokeStyle='#157c84';ctx.beginPath();const step=Math.max(1,Math.ceil(data.length/width));for(let x=0;x<width;x++){let peak=0;for(let i=x*step;i<Math.min((x+1)*step,data.length);i++)peak=Math.max(peak,Math.abs(data[i]));ctx.moveTo(x,height/2-peak*height*.46);ctx.lineTo(x,height/2+peak*height*.46);}ctx.stroke();ctx.strokeStyle='#117875';ctx.lineWidth=3*devicePixelRatio;for(const time of [start,end]){const x=Math.max(2,Math.min(width-2,width*time/this.buffer.duration));ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,height);ctx.stroke();}}
  stop(){if(this.player){this.player.stop();this.player=null;}const b=$('[data-preview]',this.card);if(b)b.textContent='Auswahl vorhören';}
  async preview(){if(this.player){this.stop();return;}if(!this.buffer)return;const start=Number($('.start',this.card).value),end=Number($('.end',this.card).value);if(!(start>=0&&end>start&&end<=this.buffer.duration+.005)){notice('Bitte gültige Start- und Endzeiten innerhalb des Audios wählen.');return;}document.querySelectorAll('audio').forEach(a=>a.pause());for(const ed of editors.values())if(ed!==this)ed.stop();try{await this.context.resume();const player=this.context.createBufferSource();player.buffer=this.buffer;player.connect(this.context.destination);this.player=player;player.onended=()=>{if(this.player===player){this.player=null;$('[data-preview]',this.card).textContent='Auswahl vorhören';}};player.start(0,start,end-start);$('[data-preview]',this.card).textContent='Vorschau stoppen';}catch(error){notice(error.message);}}
  close(){this.resize.disconnect();this.closed=true;this.stop();if(this.context)this.context.close().catch(()=>{});}
}

async function poll() {
  try {
    const state = await request('api/state'), outcome = state.outcome || {};
    busy = state.running;
    controls();
    if (busy) $('#job').textContent = outcome.message || 'Vorgang läuft…';
    if (!busy && outcome.id && outcome.id !== lastJob) {
      lastJob = outcome.id;
      notice(outcome.message);
      await reload();
    }
  } catch (error) {
    notice('Verbindung zur App unterbrochen. App geöffnet lassen und die Review-Seite dort erneut öffnen.');
    busy = true;
    controls();
  }
  setTimeout(poll, 1200);
}
$('#search').oninput = render;
$('#filter').onchange = render;
$('#reload').onclick = () => reload().catch(error => notice(error.message));
reload().catch(error => notice(error.message));
poll();
