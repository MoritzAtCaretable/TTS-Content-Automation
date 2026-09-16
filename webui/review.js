'use strict';
const $ = (s, root=document) => root.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let entries=[], models=[], busy=false, pending=false, lastJob='';
const editors=new Map();
function notice(message=''){ $('#notice').textContent=message; $('#notice').hidden=!message; }
async function request(path,options){const r=await fetch(path,options); const data=await r.json(); if(!r.ok||data.ok===false)throw Error(data.error||'Anfrage fehlgeschlagen');return data;}
function audioURL(e,source='current'){return `audio/${encodeURIComponent(e.key)}?version=${encodeURIComponent(e.version)}&source=${source}`;}
function controls(){ document.querySelectorAll('[data-action],.editor input,.editor select,[data-preview]').forEach(n=>{n.disabled=busy||pending;}); $('#job').textContent=busy||pending?'Vorgang läuft…':'Bereit'; }
function render(){
  for(const editor of editors.values()) editor.close(); editors.clear();
  const needle=$('#search').value.toLocaleLowerCase(), filter=$('#filter').value;
  const shown=entries.filter(e=>(filter==='all'||(filter==='sync'?e.sync_error:e.status===filter))&&`${e.id} ${e.text} ${e.reason}`.toLocaleLowerCase().includes(needle));
  $('#count').textContent=`${shown.length} von ${entries.length} Audios · ${entries.filter(e=>e.status==='passed').length} freigegeben`;
  $('#entries').innerHTML=shown.map(e=>`<article class="card" data-key="${esc(e.key)}"><div class="card-head"><div class="identity"><strong>${esc(e.id)}</strong> · ${esc(e.filename)}<div class="muted">${esc(e.generated_at)} · ${esc(e.model||'Modell unbekannt')} · ${esc(e.mode||'normal')}</div></div><span class="badge ${e.status==='passed'?'passed':''}">${e.status==='passed'?(e.decision?.type==='manual'?'Manuell freigegeben':'Freigegeben'):'Prüfung erforderlich'}</span></div><div class="text">${esc(e.text)}</div><div class="reason">${esc(e.reason)}</div>${e.has_audio?`<audio controls preload="none" src="${esc(audioURL(e))}"></audio>`:'<p>Für diesen Versuch ist keine Audiodatei vorhanden.</p>'}
  ${e.sync_error?`<div class="sync"><strong>Sheet noch nicht synchronisiert</strong><div>${esc(e.sync_error)}</div><button data-action="sync">Synchronisierung wiederholen</button></div>`:''}
  <div class="actions"><label>Notiz zur Entscheidung<input class="note" maxlength="1000" placeholder="Optional: Was wurde geprüft?"></label>${e.has_audio?'<button class="primary" data-action="approve">Freigeben</button>':''}<button data-action="flag">Zur Prüfung markieren</button></div>
  <div class="actions">${e.has_audio?'<button data-action="recheck">Erneut automatisch prüfen</button>':''}<label class="model">Stimmmodell<select class="model-select">${models.map(m=>`<option ${m===e.model?'selected':''}>${esc(m)}</option>`).join('')}</select></label><button data-action="regenerate">Neu generieren…</button></div>
  ${e.has_audio||e.has_original?`<details class="trim"><summary>Audio schneiden</summary><div class="editor"><p>Der ausgewählte Bereich wird behalten. Der Export ergänzt kurze Randpausen und prüft das Format. Anschließend ist eine erneute Freigabe erforderlich.</p><label>Audioquelle<select class="source">${e.has_audio?'<option value="current">Aktuelle Version</option>':''}${e.has_original?'<option value="original">Ungeschnittenes Original</option>':''}</select></label><canvas aria-label="Wellenform mit ausgewähltem Schnittbereich"></canvas><div class="wave-status">Wellenform wird beim Öffnen geladen.</div><div class="ranges"><label>Start (Sekunden)<input class="start" type="number" min="0" step="0.001" value="0"><input class="start-range" aria-label="Schnittstart" type="range" min="0" step="0.001" value="0"></label><label>Ende (Sekunden)<input class="end" type="number" min="0" step="0.001" value="0"><input class="end-range" aria-label="Schnittende" type="range" min="0" step="0.001" value="0"></label></div><div class="actions"><button data-preview>Auswahl vorhören</button><button data-action="trim">Schnitt speichern</button></div></div></details>`:''}
  <details><summary>Prüfbefund und Transkript</summary><div class="checks">${Object.entries(e.qc?.checks||{}).map(([name,c])=>`<div class="check ${esc(c.state)}"><strong>${esc(name)} · ${esc(({passed:'bestanden',failed:'nicht bestanden',error:'Fehler',skipped:'nicht geprüft'})[c.state]||c.state)}</strong><div>${esc(c.reason)}</div>${c.details?.defects?.map(d=>`<div>${esc(d.category)} · ${esc(d.severity)}: ${esc(d.evidence)}</div>`).join('')||''}${c.details?.model?`<div class="muted">${esc(c.details.model)} · Prompt ${esc(c.details.prompt_version||'—')}</div>`:''}</div>`).join('')||'<div>Kein strukturierter Befund vorhanden.</div>'}<div><strong>Transkript:</strong> ${esc(e.transcript||'—')}</div><div><strong>Natürlichkeitswert:</strong> ${esc(e.gemini??'—')} · <strong>WER:</strong> ${esc(e.wer??'—')}</div>${e.decision?.type==='manual'?'<div>Manuelle Entscheidung; der automatische Befund bleibt zur Nachvollziehbarkeit erhalten.</div>':''}</div></details></article>`).join('')||'<div class="empty">Keine passenden Audios vorhanden.</div>';
  document.querySelectorAll('.card').forEach(card=>{
    const e=entries.find(x=>x.key===card.dataset.key);
    card.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>act(e,card,b.dataset.action));
    const details=$('.trim',card); if(details)details.addEventListener('toggle',()=>{if(details.open&&!editors.has(e.key)){const editor=new Editor(e,card);editors.set(e.key,editor);editor.load();}});
  }); controls();
}
async function reload(){ const d=await request('api/entries');entries=d.entries;models=d.models;render(); }
async function act(e,card,action){
  if(busy||pending)return; let options={};
  if(action==='approve'||action==='flag'){options={status:action==='approve'?'passed':'review needed',note:$('.note',card).value};if(action==='approve'&&!confirm('Audio angehört und Inhalt geprüft? Die aktuelle Version wird als finale Datei veröffentlicht und im Sheet freigegeben.'))return;action='status';}
  if(action==='regenerate'){if(!confirm('Dieses Audio neu generieren? Dabei werden ElevenLabs- und gegebenenfalls Gemini-Kosten ausgelöst.'))return;options.model=$('.model-select',card).value;}
  if(action==='trim'){const editor=editors.get(e.key);if(!editor?.buffer){notice('Bitte warten, bis die Audioquelle geladen ist.');return;}options={source:$('.source',card).value,start:Number($('.start',card).value),end:Number($('.end',card).value)};if(options.end-options.start<.08){notice('Bitte mindestens 80 ms auswählen.');return;}}
  pending=true;controls();notice();
  try{await request('api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:e.key,version:e.version,action,options})});busy=true;}
  catch(error){notice(error.message);}
  finally{pending=false;controls();}
}
class Editor{
  constructor(entry,card){this.entry=entry;this.card=card;this.buffer=null;this.context=null;this.player=null;this.serial=0;this.closed=false;
    $('.source',card).onchange=()=>this.load();
    for(const name of ['start','end'])for(const suffix of ['','-range'])$('.'+name+suffix,card).oninput=ev=>{const value=Number(ev.target.value);$('.'+name+(suffix?'':'-range'),card).value=value;this.draw();};
    $('[data-preview]',card).onclick=()=>this.preview();
  }
  async load(){const serial=++this.serial;this.buffer=null;this.stop();const label=$('.wave-status',this.card);label.textContent='Lade Audio und Wellenform…';try{
    if(!this.context)this.context=new AudioContext();
    const response=await fetch(audioURL(this.entry,$('.source',this.card).value));if(!response.ok)throw Error('Audio konnte nicht geladen werden. Bitte Seite neu laden.');
    const buffer=await this.context.decodeAudioData(await response.arrayBuffer());if(this.closed||serial!==this.serial)return;this.buffer=buffer;
    const duration=Math.floor(buffer.duration*1000)/1000;
    for(const name of ['start','end'])for(const suffix of ['','-range']){const input=$('.'+name+suffix,this.card);input.max=duration;input.value=name==='start'?0:duration;}
    label.textContent=`${duration.toFixed(3)} Sekunden · Vorschau spielt nur die Auswahl.`;this.draw();
  }catch(error){if(!this.closed&&serial===this.serial)label.textContent=error.message;}}
  draw(){if(!this.buffer)return;const canvas=$('canvas',this.card),width=canvas.width=Math.max(300,Math.round(canvas.clientWidth*devicePixelRatio)),height=canvas.height=110*devicePixelRatio,ctx=canvas.getContext('2d'),data=this.buffer.getChannelData(0);ctx.clearRect(0,0,width,height);const start=Number($('.start',this.card).value),end=Number($('.end',this.card).value);ctx.fillStyle='#c7eeee';ctx.fillRect(width*start/this.buffer.duration,0,width*(end-start)/this.buffer.duration,height);ctx.strokeStyle='#157c84';ctx.beginPath();const step=Math.max(1,Math.ceil(data.length/width));for(let x=0;x<width;x++){let peak=0;for(let i=x*step;i<Math.min((x+1)*step,data.length);i++)peak=Math.max(peak,Math.abs(data[i]));ctx.moveTo(x,height/2-peak*height*.46);ctx.lineTo(x,height/2+peak*height*.46);}ctx.stroke();}
  stop(){if(this.player){this.player.stop();this.player=null;}const b=$('[data-preview]',this.card);if(b)b.textContent='Auswahl vorhören';}
  async preview(){if(this.player){this.stop();return;}if(!this.buffer)return;const start=Number($('.start',this.card).value),end=Number($('.end',this.card).value);if(!(start>=0&&end>start&&end<=this.buffer.duration+.005)){notice('Bitte gültige Start- und Endzeiten innerhalb des Audios wählen.');return;}document.querySelectorAll('audio').forEach(a=>a.pause());for(const ed of editors.values())if(ed!==this)ed.stop();try{await this.context.resume();const player=this.context.createBufferSource();player.buffer=this.buffer;player.connect(this.context.destination);this.player=player;player.onended=()=>{if(this.player===player){this.player=null;$('[data-preview]',this.card).textContent='Auswahl vorhören';}};player.start(0,start,end-start);$('[data-preview]',this.card).textContent='Vorschau stoppen';}catch(error){notice(error.message);}}
  close(){this.closed=true;this.stop();if(this.context)this.context.close().catch(()=>{});}
}
async function poll(){try{const state=await request('api/state'),outcome=state.outcome||{};busy=state.running;controls();if(busy)$('#job').textContent=outcome.message||'Vorgang läuft…';if(!busy&&outcome.id&&outcome.id!==lastJob){lastJob=outcome.id;notice(outcome.message);await reload();}}catch(error){notice('Verbindung zur App unterbrochen. App geöffnet lassen und die Review-Seite dort erneut öffnen.');busy=true;controls();}setTimeout(poll,1200);}
$('#search').oninput=render;$('#filter').onchange=render;$('#reload').onclick=()=>reload().catch(e=>notice(e.message));
reload().catch(e=>notice(e.message));poll();
