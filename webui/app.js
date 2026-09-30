/* ===========================================================================
   TTS Studio — Oberfläche
   Rendert das Caretable-Design und ruft die Python-Logik über
   window.pywebview.api auf. Fachlogik liegt vollständig im Backend.
   =========================================================================== */

const S = {
  state: null,
  rows: [],            // [{row,id,text,mode,status,open}]
  sel: new Set(),      // angehakte Zeilennummern
  loaded: false,
  loading: false,
  running: false,
  adding: false,
  lastJob: "",
  voicePending: false,
  projectPending: false,
  drafts: {},
};

/* ---------- kleine Helfer ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const short = (s, n = 70) => { s = String(s ?? "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; };

async function api(name, ...args) {
  try {
    const res = await window.pywebview.api[name](...args);
    if (res && res.ok === false) { toast(res.error || "Unbekannter Fehler", "err"); return null; }
    return res || {};
  } catch (e) { toast("Fehler: " + e, "err"); return null; }
}

function toast(msg, kind = "") {
  const box = $("#toast");
  const t = el(`<div class="toast ${kind}">${esc(msg)}</div>`);
  box.appendChild(t);
  setTimeout(() => {
    t.style.opacity = "0"; t.style.transition = "opacity .3s";
    setTimeout(() => t.remove(), 300);
  }, kind === "err" ? 6500 : 3400);
}

/* Bestätigungsdialog — liefert true/false. */
function confirmBox({ title, body, pre = "", ok = "Weiter", danger = false }) {
  return new Promise(resolve => {
    const host = $("#modal");
    host.innerHTML = "";
    const sheet = el(`
      <div class="sheet">
        <h3>${esc(title)}</h3>
        <div class="body">${esc(body)}</div>
        ${pre ? `<div class="pre">${esc(pre)}</div>` : ""}
        <div class="acts">
          <button class="btn quiet" data-no>Abbrechen</button>
          <button class="btn ${danger ? "danger" : "turq"}" data-yes>${esc(ok)}</button>
        </div>
      </div>`);
    host.appendChild(sheet);
    host.classList.add("on");
    const done = (v) => { host.classList.remove("on"); host.innerHTML = ""; resolve(v); };
    $("[data-no]", sheet).onclick = () => done(false);
    $("[data-yes]", sheet).onclick = () => done(true);
    host.onclick = (e) => { if (e.target === host) done(false); };
  });
}

/* ---------- Stimmen ---------- */
function renderVoices(data) {
  S.state = {...S.state, voices: data.voices || [], voice_id: data.voice_id || ""};
  $("#voice").innerHTML = S.state.voices.length
    ? S.state.voices.map(v => `<option value="${esc(v.id)}" ${v.id === S.state.voice_id ? "selected" : ""}>${esc(v.name)}</option>`).join("")
    : '<option value="">Über + eine Stimme hinzufügen</option>';
  $("#voice").title = S.state.voice_id;
  voiceControls();
}
function voiceControls() {
  $("#voice").disabled = S.running || S.voicePending || S.projectPending;
  $("#btnAddVoice").disabled = S.running || S.voicePending || S.projectPending;
  projectControls();
}
async function changeVoice() {
  S.voicePending = true;
  voiceControls();
  const result = await api("set_voice", $("#voice").value, S.state?.project_id);
  S.voicePending = false;
  renderVoices(result || S.state);
}
function openVoiceDialog() {
  if (S.running || S.voicePending) return;
  $("#voiceForm").reset();
  $("#voiceError").hidden = true;
  $("#voiceDialog").showModal();
}
async function saveVoice(event) {
  event.preventDefault();
  if (S.voicePending || S.running) return;
  S.voicePending = true;
  voiceControls();
  $("#saveVoice").disabled = $("#cancelVoice").disabled = true;
  $("#newVoiceId").disabled = $("#newVoiceName").disabled = true;
  $("#saveVoice").textContent = "Stimme wird geprüft…";
  $("#voiceError").hidden = true;
  try {
    const result = await window.pywebview.api.add_voice($("#newVoiceId").value, $("#newVoiceName").value, S.state?.project_id);
    if (!result || result.ok === false) throw Error(result?.error || "Stimme konnte nicht hinzugefügt werden.");
    renderVoices(result);
    $("#voiceDialog").close();
    toast("Stimme hinzugefügt und ausgewählt.", "ok");
  } catch (error) {
    $("#voiceError").textContent = error.message;
    $("#voiceError").hidden = false;
  } finally {
    S.voicePending = false;
    $("#saveVoice").disabled = $("#cancelVoice").disabled = false;
    $("#newVoiceId").disabled = $("#newVoiceName").disabled = false;
    $("#saveVoice").textContent = "Hinzufügen";
    voiceControls();
  }
}

/* ---------- Projekte ---------- */
function projectControls() {
  const blocked = S.running || S.projectPending || S.loading || S.adding || S.voicePending;
  for (const id of ['project', 'btnAddProject', 'btnReloadProjects']) $("#" + id).disabled = blocked;
  for (const id of ['btnSheet', 'btnReview', 'btnResetReview', 'model', 'btnFolder', 'btnReload', 'btnAdd']) {
    $("#" + id).disabled = blocked || !S.state?.project_id;
  }
  for (const id of ['prefix', 'newMode', 'newText']) $("#" + id).disabled = blocked || !S.state?.project_id;
  $('#btnAdd').disabled = blocked || !S.state?.project_id || !S.loaded;
  updateSel();
}
function applyProject(data) {
  const oldId = S.state?.project_id;
  if (oldId) S.drafts[oldId] = {prefix: $('#prefix').value, mode: $('#newMode').value, text: $('#newText').value};
  S.state = data;
  const options = (data.projects || []).map(p => `<option value="${esc(p.id)}" ${p.id === data.project_id ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
  $('#project').innerHTML = (data.project_id ? '' : '<option value="">Projekt auswählen oder über + erstellen</option>') + options;
  $('#project').title = data.sheet_name || '';
  $('#folder').textContent = data.project_id ? data.folder : 'Bitte ein Projekt auswählen';
  $('#folder').title = data.project_id ? data.folder : '';
  $('#model').innerHTML = (data.models || []).map(m => `<option${m === data.model ? ' selected' : ''}>${esc(m)}</option>`).join('');
  if (oldId !== data.project_id) {
    const draft = S.drafts[data.project_id] || {};
    $('#prefix').value = draft.prefix || '';
    $('#newMode').value = draft.mode || 'Einzelwort';
    $('#newText').value = draft.text || '';
  }
  S.rows = []; S.sel.clear(); S.loaded = false;
  renderRows(); renderVoices(data);
}
async function refreshProjects() {
  if (S.running || S.projectPending || S.loading || S.adding || S.voicePending) return;
  S.projectPending = true; voiceControls();
  const data = await api('list_projects');
  S.projectPending = false;
  if (data) applyProject(data);
  else if (!S.state?.project_id) {
    $('#project').innerHTML = '<option value="">Projekte konnten nicht geladen werden</option>';
    $('#shDot').style.background = 'var(--ct_fail)';
    $('#shVal').textContent = 'nicht erreichbar';
  }
  voiceControls();
  if (data?.project_id) await loadRows(true);
}
async function changeProject() {
  const id = $('#project').value;
  if (!id || S.projectPending || S.running) return;
  S.projectPending = true; voiceControls();
  const data = await api('set_project', id);
  S.projectPending = false;
  if (data) applyProject(data);
  else $('#project').value = S.state?.project_id || '';
  voiceControls();
  if (data) await loadRows(false);
}
function openProjectDialog() {
  if (S.running || S.projectPending) return;
  $('#projectForm').reset(); $('#projectError').hidden = true;
  $('#projectDialog').showModal();
}
async function saveProject(event) {
  event.preventDefault();
  if (S.projectPending || S.running) return;
  S.projectPending = true; voiceControls();
  for (const id of ['newProjectName', 'saveProject', 'cancelProject']) $('#' + id).disabled = true;
  $('#saveProject').textContent = 'Wird angelegt…'; $('#projectError').hidden = true;
  let created = false;
  try {
    const data = await window.pywebview.api.create_project($('#newProjectName').value);
    if (!data || data.ok === false) throw Error(data?.error || 'Projekt konnte nicht erstellt werden.');
    applyProject(data); $('#projectDialog').close(); created = true;
    toast('Projekt und Tabellenblatt angelegt.', 'ok');
  } catch (error) {
    $('#projectError').textContent = error.message; $('#projectError').hidden = false;
  } finally {
    S.projectPending = false;
    for (const id of ['newProjectName', 'saveProject', 'cancelProject']) $('#' + id).disabled = false;
    $('#saveProject').textContent = 'Projekt erstellen'; voiceControls();
  }
  if (created) await loadRows(false);
}

/* ---------- Sheet-Tabelle ---------- */
function statusClass(st) {
  const s = (st || "").trim().toLowerCase();
  if (s === "passed") return "passed";
  if (s === "review needed") return "review";
  if (!s) return "empty";
  if (s === "todo" || s === "regenerate") return "todo";
  return "empty";
}

function renderRows() {
  const body = $("#tbody");
  if (!S.rows.length) {
    body.innerHTML = `<div class="tempty">${S.loaded
      ? "Das Sheet enthält keine Datenzeilen."
      : (S.loading ? "Zeilen werden geladen…" : "Noch nichts geladen — „Neu laden“ drücken.")}</div>`;
    updateSel();
    return;
  }
  const html = S.rows.map(r => {
    const on = S.sel.has(r.row);
    const cls = statusClass(r.status);
    const label = r.status.trim() || "leer → todo";
    return `<div class="grid trow" data-row="${r.row}">
      <div><div class="box${on ? " on" : ""}"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7"></path></svg></div></div>
      <div class="num">${r.row}</div>
      <div class="rid" title="${esc(r.id)}">${esc(r.id)}</div>
      <div class="txt" title="${esc(r.text)}">${esc(r.text)}</div>
      <div class="mode">${esc(r.mode)}</div>
      <div class="st ${cls}"><div class="d"></div>${esc(label)}</div>
    </div>`;
  }).join("");
  body.innerHTML = html;
  updateSel();
}

function updateSel() {
  const n = S.sel.size, total = S.rows.length;
  $("#selText").textContent = total
    ? `${n} von ${total} ausgewählt`
    : (S.loaded ? "Keine Zeilen." : "Noch nicht geladen.");
  if (!S.running) {
    $("#scope").textContent = total
      ? `${n} ausgewählt — verarbeitet wird genau diese Auswahl`
      : "";
    $("#btnStart").disabled = S.loading || S.adding || S.voicePending || S.projectPending || !S.state?.project_id || !S.state?.voice_id || !S.loaded || n === 0;
  }
}

function toggleRow(row) {
  if (S.running) return;
  if (S.sel.has(row)) S.sel.delete(row); else S.sel.add(row);
  const node = $(`.trow[data-row="${row}"] .box`);
  if (node) node.classList.toggle("on", S.sel.has(row));
  updateSel();
}

async function loadRows(quiet, selectOpen = false) {
  if (S.loading || S.running || S.projectPending || !S.state?.project_id) return;
  S.loading = true;
  projectControls();
  $("#btnReload").disabled = true;
  $("#selText").textContent = "Lade Sheet…";
  if (!S.rows.length) renderRows();

  const projectId = S.state.project_id;
  const r = await api("load_rows", projectId);
  S.loading = false;
  projectControls();

  if (!r || r.project_id !== S.state.project_id) {
    S.rows = []; S.sel.clear(); S.loaded = false; renderRows();
    $("#shDot").style.background = "var(--ct_fail)";
    $("#shVal").textContent = "nicht erreichbar";
    $("#selText").textContent = "Laden fehlgeschlagen.";
    return;
  }
  const vorher = new Map(S.rows.map(x => [x.id, S.sel.has(x.row)]));
  S.rows = r.rows || [];
  S.loaded = true;
  // Auswahl beim Neuladen erhalten; neue Zeilen nach Status vorwählen.
  S.sel = new Set(S.rows.filter(x =>
    selectOpen ? x.open : (vorher.has(x.id) ? vorher.get(x.id) : x.open)).map(x => x.row));

  $("#shDot").style.background = "var(--ct_correct)";
  $("#shVal").textContent = "verbunden";
  renderRows();
  projectControls();
  const offen = S.rows.filter(x => x.open).length;
  if (!quiet) addLog(`📄 ${S.state.sheet_name}: ${S.rows.length} Zeilen geladen, ${offen} davon offen.`);
}

/* ---------- Neue Texte ---------- */
async function addRows() {
  if (S.running) { toast("Bitte warten, bis der aktuelle Lauf beendet ist."); return; }
  if (S.adding || S.loading || S.projectPending || !S.state?.project_id) return;
  S.adding = true; projectControls();
  try {
    const plan = await api("plan_rows", $("#prefix").value, $("#newMode").value, $("#newText").value, S.state.project_id);
    if (!plan) return;
    const entries = plan.entries || [];

    const pre = entries.slice(0, 10).map(e => `${e.id}   →   ${short(e.text, 46)}`).join("\n")
      + (entries.length > 10 ? `\n… und ${entries.length - 10} weitere` : "");
    const ok = await confirmBox({
      title: "Ins Sheet einfügen",
      body: `Projekt: ${S.state.sheet_name}\n${entries.length} neue Zeile(n) unten anfügen?\nModus: ${$("#newMode").value} · Status: todo`,
      pre, ok: "Einfügen",
    });
    if (!ok) return;

    const res = await api("commit_rows", entries, plan.project_id);
    if (!res) return;

    $("#newText").value = "";
    toast(`${res.count} Zeile(n) eingefügt.`, "ok");
    await loadRows(true);
  } finally { S.adding = false; projectControls(); }
}

/* ---------- Lauf ---------- */
async function start() {
  if (S.running || S.loading || S.adding || S.voicePending || S.projectPending || !S.state?.project_id || !S.state?.voice_id || !S.loaded) return;
  const rows = Array.from(S.sel).sort((a, b) => a - b);
  const allOpen = !S.loaded || !S.rows.length;     // Tabelle leer → Status-Filter
  if (!rows.length && !allOpen) {
    toast("Es ist keine Zeile ausgewählt. Hake an, was verarbeitet werden soll — oder nutze „Nur offene“.", "err");
    return;
  }
  const r = await api("start", rows, allOpen, S.state.project_id);
  if (!r) return;

  const liste = rows.slice(0, 12).join(", ") + (rows.length > 12 ? " …" : "");
  addLog(`\n${"─".repeat(60)}\nProjekt: ${S.state.sheet_name}\nStarte mit Modell: ${$("#model").value}`);
  addLog(rows.length
    ? `Umfang: ${rows.length} ausgewählte Zeile(n) — ${liste}\n${"─".repeat(60)}`
    : `Umfang: alle offenen Zeilen (Status-Filter aus dem Sheet)\n${"─".repeat(60)}`);
  setRunning(true, rows.length);
}

async function stop() {
  if (!S.running) return;
  $("#btnStop").disabled = true;
  await api("cancel");
}

function setRunning(on, total) {
  S.running = on;
  $("#btnStart").disabled = on;
  $("#btnStop").disabled = !on;
  $("#btnStop").classList.toggle("on", on);
  $("#btnReload").disabled = on;
  $("#btnAdd").disabled = on;
  $("#btnUpdate").disabled = on;
  $("#model").disabled = on;
  voiceControls();
  $("#btnFolder").disabled = on;
  $$(".pill").forEach(b => b.disabled = on);
  $("#state").textContent = on ? "Läuft…" : "Bereit";
  if (on) {
    $("#bar").classList.add("busy");
    $("#scope").textContent = total ? `0 von ${total} verarbeitet` : "läuft…";
  } else {
    $("#bar").classList.remove("busy");
    $("#barFill").style.width = "0%";
    updateSel();
  }
  projectControls();
}

/* ---------- Protokoll ---------- */
function addLog(text) {
  const box = $("#log");
  const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.textContent += (box.textContent ? "\n" : "") + text;
  if (atEnd) box.scrollTop = box.scrollHeight;
}

async function tick() {
  const r = await api("poll");
  if (r) {
    if (r.lines && r.lines.length) addLog(r.lines.join("\n"));
    const p = r.progress || {};
    if (r.running && !S.running) setRunning(true, p.total);
    $("#btnStop").disabled = !r.cancellable;
    if (r.running && p.total) {
      $("#bar").classList.remove("busy");
      $("#barFill").style.width = Math.round((p.done / p.total) * 100) + "%";
      $("#scope").textContent = `${p.done} von ${p.total} verarbeitet`;
    }
    if (!r.running && (S.running || (r.outcome?.id && r.outcome.id !== S.lastJob))) {
      S.lastJob = r.outcome?.id || S.lastJob;
      setRunning(false, 0);
      const outcome = r.outcome || {};
      $("#state").textContent = outcome.state === "failed" ? "Fehlgeschlagen" : outcome.state === "cancelled" ? "Abgebrochen" : "Fertig.";
      toast(outcome.message || "Lauf beendet.", outcome.state === "failed" ? "err" : "ok");
      loadRows(true, true);        // Status im Sheet hat sich geändert
    }
  }
  setTimeout(tick, 300);
}

/* ---------- Zieh-Griffe (Tabelle / Protokoll) ---------- */
function initGrips() {
  $$(".grip").forEach(g => {
    const varName = g.dataset.grip, min = parseInt(g.dataset.min, 10);
    g.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const y0 = e.clientY;
      const h0 = parseInt(getComputedStyle(document.documentElement)
        .getPropertyValue(varName), 10);
      const move = (ev) => document.documentElement.style.setProperty(
        varName, Math.max(min, h0 + ev.clientY - y0) + "px");
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  });
}

/* ---------- Start ---------- */
async function boot() {
  const st = await api("get_state");
  if (st) {
    S.state = st;
    renderVoices(st);
    $("#model").innerHTML = (st.models || []).map(m =>
      `<option${m === st.model ? " selected" : ""}>${esc(m)}</option>`).join("");
    $("#folder").textContent = st.folder;
    $("#folder").title = st.folder;
    $("#shVal").textContent = st.has_sheet_id ? "…" : "keine ID";
    if (!st.has_sheet_id) $("#shDot").style.background = "var(--ct_F1)";
  }
  addLog("TTS Studio bereit. Projekt und Stimme wählen, dann Start.");

  /* Ereignisse */
  $('#project').onchange = changeProject;
  $('#btnReloadProjects').onclick = refreshProjects;
  $('#btnAddProject').onclick = openProjectDialog;
  $('#projectForm').onsubmit = saveProject;
  $('#newProjectName').oninput = () => { $('#projectError').hidden = true; };
  $('#cancelProject').onclick = () => $('#projectDialog').close();
  $('#projectDialog').addEventListener('cancel', event => { if (S.projectPending) event.preventDefault(); });
  $("#voice").onchange = changeVoice;
  $("#btnAddVoice").onclick = openVoiceDialog;
  $("#voiceForm").onsubmit = saveVoice;
  $("#cancelVoice").onclick = () => $("#voiceDialog").close();
  $("#voiceDialog").addEventListener("cancel", event => { if (S.voicePending) event.preventDefault(); });
  $("#model").onchange = () => api("set_model", $("#model").value, S.state?.project_id);
  $("#btnFolder").onclick = async () => {
    const r = await api("choose_folder", S.state?.project_id);
    if (r && r.path) { $("#folder").textContent = r.path; $("#folder").title = r.path; }
  };
  $("#btnReload").onclick = () => loadRows(false);
  $("#tbody").onclick = (e) => {
    const row = e.target.closest(".trow");
    if (row) toggleRow(parseInt(row.dataset.row, 10));
  };
  $$(".pill").forEach(b => b.onclick = () => {
    const k = b.dataset.sel;
    if (k === "all") S.sel = new Set(S.rows.map(r => r.row));
    else if (k === "none") S.sel = new Set();
    else if (k === "open") S.sel = new Set(S.rows.filter(r => r.open).map(r => r.row));
    else if (k === "review") S.sel = new Set(S.rows.filter(r =>
      (r.status || "").trim().toLowerCase() === "review needed").map(r => r.row));
    renderRows();
  });
  $("#btnAdd").onclick = addRows;
  $("#btnSheet").onclick = () => api("open_sheet");
  $("#btnReview").onclick = () => api("open_review");
  $("#btnResetReview").onclick = async () => {
    if (S.running) { toast("Bitte warten, bis der aktuelle Lauf beendet ist."); return; }
    const projectId = S.state.project_id;
    const ok = await confirmBox({
      title: "Review zurücksetzen",
      body: `Alle Prüfungen im Projekt „${S.state.sheet_name}“ aus der Übersicht entfernen?\n\nDie Audio-Dateien bleiben erhalten. Andere Projekte bleiben unverändert.`,
      ok: "Zurücksetzen", danger: true,
    });
    if (!ok) return;
    if (await api("reset_review", projectId)) toast("Projekt-Review zurückgesetzt.", "ok");
  };
  $("#btnUpdate").onclick = async () => {
    const r = await api("check_update");
    if (!r) return;
    const kind = r.state === "failed" ? "err" : (r.state === "updated" ? "ok" : "");
    toast(r.message, kind);
  };
  $("#btnStart").onclick = start;
  $("#btnStop").onclick = stop;

  initGrips();
  tick();
  refreshProjects();
}

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);
