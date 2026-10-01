"""
TTS Studio — Brücke zwischen der HTML-Oberfläche und der Fachlogik.

Die Methoden dieser Klasse ruft das Frontend über window.pywebview.api auf.
Die eigentliche Arbeit bleibt unverändert in sheets_to_elevenlabs_qc_local.py:
- lesende/schreibende Sheet-Zugriffe laufen direkt im Prozess (sie sind kurz),
- der Generierungslauf läuft als Subprozess. Das hält die Oberfläche flüssig
  und macht "Abbrechen" möglich, was in einem Thread nicht ginge.
"""

from __future__ import annotations

import os
import json
import uuid
import re
import sys
import queue
import threading
import subprocess
import webbrowser
import copy
from functools import wraps
from pathlib import Path
from typing import List, Optional

from tts_voices import VoiceStore, fetch_voice, validate_voice_id
from tts_projects import ProjectStore, create_project_sheet, import_legacy_reviews

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

import sheets_to_elevenlabs_qc_local as pipeline   # noqa: E402

VOICE_MODELS = [
    "eleven_turbo_v2_5",
    "eleven_flash_v2_5",
    "eleven_multilingual_v2",
    "eleven_v3",
]

PROGRESS_RE = re.compile(r"^@@PROGRESS (\d+)/(\d+)$")


def _guard(fn):
    """Fehler nie ins Frontend werfen — immer als Ergebnis melden."""
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and "ok" not in result:
                result = {"ok": True, **result}
            return result if result is not None else {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{e}"}
    wrapper.__name__ = fn.__name__
    return wrapper


def _idle(fn):
    """Serialize project changes with sheet actions and job admission."""
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._job_lock:
            if self._busy:
                raise ValueError("Bitte den laufenden Vorgang abwarten.")
            return fn(self, *args, **kwargs)
    return wrapper


def sheet_url() -> str:
    """URL zum Google Sheet aus der Spreadsheet-ID."""
    sid = pipeline.SPREADSHEET_ID
    if sid and sid != "YOUR_SPREADSHEET_ID":
        return f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    return ""


class Api:
    def __init__(self, voice_config_path=None, project_config_path=None) -> None:
        self._window = None
        self.root = PROJECT_DIR
        self.log_queue: queue.Queue = queue.Queue()
        self.process: Optional[subprocess.Popen] = None
        self.progress = {"done": 0, "total": 0}
        self.cancelled = False
        self._busy = False
        self._job_lock = threading.RLock()
        self._outcome = {"state": "idle", "id": "", "message": "Bereit"}
        self._review_servers = {}
        self.voices = VoiceStore(voice_config_path or self.root / ".tts_voices.json", pipeline.VOICE_ID)
        self.model = (pipeline.ELEVENLABS_MODEL
                      if pipeline.ELEVENLABS_MODEL in VOICE_MODELS else VOICE_MODELS[0])
        self.folder = str((PROJECT_DIR / pipeline.OUTPUT_DIR).resolve()
                          if not os.path.isabs(pipeline.OUTPUT_DIR)
                          else Path(pipeline.OUTPUT_DIR))
        self.rows: List[dict] = []          # zuletzt geladener Sheet-Stand
        self.rows_loaded = False
        self.output_base = self.folder
        self.projects = []
        self.project = None
        self.project_store = ProjectStore(project_config_path or self.root / ".tts_projects.json", pipeline.SPREADSHEET_ID)
        saved_model = self.project_store.model(self.model)
        if saved_model in VOICE_MODELS:
            self.model = saved_model
        self._project_voice = None

    # ---------------------------------------------------------------- Zustand

    @_guard
    def get_state(self) -> dict:
        return {
            **self.voices.state(),
            "voice_id": self._project_voice or self.voices.state()["voice_id"],
            "models": VOICE_MODELS,
            "model": self.model,
            "folder": self.folder,
            "sheet_name": self.project["name"] if self.project else pipeline.SHEET_NAME,
            "projects": copy.deepcopy(self.projects),
            "project_id": self.project["id"] if self.project else None,
            "has_sheet_id": bool(sheet_url()),
            "running": self._busy,
        }

    @_guard
    @_idle
    def set_model(self, model: str, project_id=None) -> dict:
        if project_id is not None:
            self._require_project(project_id)
        if model in VOICE_MODELS:
            self.project_store.set_model(model)
            self.model = model
        else:
            raise ValueError("Unbekanntes TTS-Modell")
        return {"model": self.model}

    @_guard
    def set_voice(self, voice_id: str, project_id=None) -> dict:
        with self._job_lock:
            if project_id is not None:
                self._require_project(project_id)
            if self._busy:
                raise ValueError("Bitte den laufenden Vorgang abwarten.")
            result = self.voices.select(voice_id)
            self._remember_project({"voice_id": voice_id})
            self._project_voice = voice_id if self.project else None
            return result

    @_guard
    def add_voice(self, voice_id: str, name: str = "", project_id=None) -> dict:
        voice_id = validate_voice_id(voice_id)
        with self._job_lock:
            if project_id is not None:
                self._require_project(project_id)
            if self._busy:
                raise ValueError("Bitte den laufenden Vorgang abwarten.")
        existing = next((v for v in self.voices.state()["voices"] if v["id"] == voice_id), None)
        voice = existing or fetch_voice(voice_id, pipeline.ELEVENLABS_API_KEY)
        if name.strip():
            voice["name"] = " ".join(name.split())[:100]
        with self._job_lock:
            if project_id is not None:
                self._require_project(project_id)
            if self._busy:
                raise ValueError("Inzwischen wurde ein Lauf gestartet. Bitte danach erneut hinzufügen.")
            result = self.voices.add(voice)
            self._remember_project({"voice_id": voice_id})
            self._project_voice = voice_id if self.project else None
            return result

    # -------------------------------------------------------------- Projekte

    def _require_project(self, expected_id=None):
        if self.project is None:
            raise ValueError("Bitte zuerst ein Projekt auswählen oder über + anlegen.")
        if expected_id is not None and str(expected_id) != self.project["id"]:
            raise ValueError("Das Projekt wurde inzwischen gewechselt. Bitte die Ansicht neu laden.")
        return copy.deepcopy(self.project)

    def _remember_project(self, preferences):
        if self.project:
            self.project_store.save_project(self.project["id"], preferences)

    def _activate_project(self, selected):
        context = self.project_store.context(selected, self.root, self.output_base)
        book = self.project_store.book()
        legacy_id = book.get("legacy_id")
        if legacy_id is None:
            legacy_id = next((p["id"] for p in self.projects if p["name"] == pipeline.SHEET_NAME), "")
        if context["id"] == legacy_id:
            import_legacy_reviews(context, self.root / pipeline.REVIEW_DATA_FILE, pipeline.SHEET_NAME)
        prefs = book.get("projects", {}).get(context["id"], {})
        voice_id = prefs.get("voice_id") or self.voices.state()["voice_id"]
        if voice_id and not any(v["id"] == voice_id for v in self.voices.state()["voices"]):
            voice_id = self.voices.state()["voice_id"]
        self.project_store.save_project(context["id"], {"name": context["name"], "folder": context["folder"],
            "voice_id": voice_id}, select=True, legacy_id=legacy_id or None, model=self.model)
        self.project, self.folder, self._project_voice = context, context["folder"], voice_id
        self.rows, self.rows_loaded = [], False

    @_guard
    @_idle
    def list_projects(self):
        sheets = pipeline.open_spreadsheet().worksheets()
        self.projects = [{"id": str(ws.id), "name": ws.title} for ws in sheets
                         if ws._properties.get("sheetType", "GRID") == "GRID"]
        selected_id = self.project["id"] if self.project else self.project_store.book().get("selected")
        if selected_id is None:
            selected_id = next((p["id"] for p in self.projects if p["name"] == pipeline.SHEET_NAME), None)
        selected = next((p for p in self.projects if p["id"] == selected_id), None)
        if selected:
            self._activate_project(selected)
        else:
            self.project, self.rows, self.rows_loaded = None, [], False
        return self.get_state()

    @_guard
    @_idle
    def set_project(self, project_id):
        # Refresh by ID so deleted or renamed worksheets never redirect an action.
        ws = pipeline.open_spreadsheet().get_worksheet_by_id(int(project_id))
        selected = {"id": str(ws.id), "name": ws.title}
        self.projects = [selected if p["id"] == selected["id"] else p for p in self.projects]
        if not any(p["id"] == selected["id"] for p in self.projects):
            self.projects.append(selected)
        self._activate_project(selected)
        return self.get_state()

    @_guard
    @_idle
    def create_project(self, name):
        selected = create_project_sheet(pipeline.open_spreadsheet(), name)
        self.projects.append(selected)
        try:
            self._activate_project(selected)
        except Exception as exc:
            raise RuntimeError("Tabellenblatt wurde angelegt. Bitte die Projektliste neu laden und das Projekt auswählen. " + str(exc)) from exc
        self._log(f"📁 Projekt „{selected['name']}“ angelegt.")
        return self.get_state()

    # ------------------------------------------------------------ Sheet lesen

    @_guard
    @_idle
    def load_rows(self, project_id=None) -> dict:
        """Liest den aktuellen Sheet-Stand für die Tabelle."""
        project = self._require_project(project_id)
        self.rows_loaded = False
        rows = pipeline.load_rows(worksheet_id=int(project["id"]))
        self.rows = rows
        self.rows_loaded = True
        out = []
        for r in rows:
            status = (r.get("status", "") or "").strip()
            mode = (r.get("mode", "") or "").strip().lower()
            out.append({
                "row": r["_row"],
                "id": (r.get("id", "") or "").strip(),
                "text": " ".join((r.get("text", "") or "").split()),
                "mode": "Einzelwort" if mode in ("einzelwort", "word") else "Normal",
                "status": status,
                "open": bool(r.get("_open")),
            })
        return {"rows": out, "project_id": project["id"]}

    # -------------------------------------------------------- Zeilen anfügen

    @_guard
    @_idle
    def plan_rows(self, prefix: str, mode: str, text: str, project_id=None) -> dict:
        """Baut aus der Eingabe die neuen Zeilen — ohne etwas zu schreiben.

        Eine Zeile = ein Text. "ID | Text" setzt eine eigene ID, sonst wird aus
        dem Präfix fortlaufend numeriert. Das Frontend zeigt das Ergebnis zur
        Bestätigung, bevor commit_rows() tatsächlich schreibt.
        """
        project = self._require_project(project_id)
        zeilen = [z.strip() for z in (text or "").splitlines() if z.strip()]
        if not zeilen:
            raise ValueError("Kein Text eingegeben. Eine Zeile pro Text.")

        modus = mode if mode in ("Einzelwort", "Normal") else "Normal"
        prefix = (prefix or "").strip()

        explizit, auto = [], []
        for i, z in enumerate(zeilen):
            if "|" in z:
                eid, _, txt = z.partition("|")
                eid, txt = eid.strip(), txt.strip()
                if not eid or not txt:
                    raise ValueError(f"Zeile {i + 1} passt nicht zum Format "
                                     f"„ID | Text“: {z}")
                explizit.append((i, eid, txt))
            else:
                auto.append((i, z))

        if auto and not prefix:
            raise ValueError("Für die automatischen IDs fehlt der ID-Präfix. "
                             "Entweder oben einen Präfix eintragen — oder je "
                             "Zeile „ID | Text“ schreiben.")
        if auto and not self.rows_loaded:
            raise ValueError("Die Sheet-Zeilen sind noch nicht geladen — ohne "
                             "sie ist die nächste freie Nummer unbekannt. "
                             "Bitte „Neu laden“ drücken.")

        neue_ids = pipeline.next_ids(prefix, len(auto), self.rows) if auto else []
        eintraege: List[Optional[dict]] = [None] * len(zeilen)
        for i, eid, txt in explizit:
            eintraege[i] = {"id": eid, "text": txt, "mode": modus, "status": "todo"}
        for (i, txt), eid in zip(auto, neue_ids):
            eintraege[i] = {"id": eid, "text": txt, "mode": modus, "status": "todo"}
        return {"entries": eintraege, "project_id": project["id"]}

    @_guard
    @_idle
    def commit_rows(self, entries: list, project_id=None) -> dict:
        """Schreibt die zuvor geplanten Zeilen ans Sheet."""
        project = self._require_project(project_id)
        erste, anzahl = pipeline.append_rows(entries or [], worksheet_id=int(project["id"]))
        self._log(f"➕ {anzahl} Zeile(n) ins Sheet eingefügt (ab Zeile {erste}).")
        return {"first": erste, "count": anzahl}

    # ------------------------------------------------------------- Aktionen

    @_guard
    @_idle
    def choose_folder(self, project_id=None) -> dict:
        project = self._require_project(project_id)
        import webview
        start = self.folder if os.path.isdir(self.folder) else str(self.root)
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG,
                                                 directory=start)
        if not result:
            return {"path": None}
        chosen = Path(result[0])
        folder = str(chosen if chosen == Path(project["folder"]) else chosen / Path(project["folder"]).name)
        self._remember_project({"folder": folder})
        self.folder = self.project["folder"] = folder
        return {"path": self.folder}

    @_guard
    def open_sheet(self) -> dict:
        url = sheet_url()
        if not url:
            raise ValueError("Keine Spreadsheet-ID gefunden. "
                             "Trage SPREADSHEET_ID in die .env ein.")
        project = self._require_project()
        webbrowser.open(url + "#gid=" + project["id"])
        return {}

    @_guard
    def open_review(self) -> dict:
        from review_service import ReviewServer
        with self._job_lock:
            project = self._require_project()
            if project["id"] not in self._review_servers:
                self._review_servers[project["id"]] = ReviewServer(self, pipeline, self.root, project)
            server = self._review_servers[project["id"]]
        webbrowser.open(server.url)
        return {"url": server.url}

    @_guard
    def reset_review(self, project_id=None) -> dict:
        with self._job_lock:
            if self._busy:
                raise ValueError("Bitte den laufenden Vorgang abwarten.")
            project = self._require_project(project_id)
            pipeline.reset_review(project["review_html"], project["review_data"])
        self._log("🗑 Review-Seite zurückgesetzt — neue Audios werden wieder gesammelt.")
        return {}

    @_guard
    def check_update(self) -> dict:
        if self._busy:
            raise ValueError("Bitte warten, bis der aktuelle Lauf beendet ist.")
        if not (self.root / ".git").is_dir():
            return {"state": "nogit",
                    "message": "Dieser Ordner ist kein Git-Checkout. Für "
                               "automatische Updates das Projekt einmal per "
                               "'git clone' einrichten (siehe README)."}
        try:
            r = subprocess.run(["git", "pull", "--ff-only"], cwd=str(self.root),
                               capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise ValueError("git ist nicht installiert.")
        out = (r.stdout + r.stderr).strip()
        self._log(f"⬇ git pull:\n{out}")
        if "Already up to date" in out or "Bereits aktuell" in out:
            return {"state": "current", "message": "Bereits auf dem neuesten Stand."}
        if r.returncode == 0:
            return {"state": "updated",
                    "message": "Update geladen. Bitte die App einmal schließen "
                               "und neu starten, damit die Änderungen aktiv werden."}
        return {"state": "failed", "message": out[-400:]}

    # ----------------------------------------------------------------- Lauf

    def job_state(self):
        with self._job_lock:
            return {"running": self._busy, "cancellable": self.process is not None, "progress": dict(self.progress), "outcome": dict(self._outcome)}

    def launch_job(self, operation, message="Vorgang läuft"):
        with self._job_lock:
            if self._busy:
                raise ValueError("Es läuft bereits ein Vorgang. Bitte warten.")
            self._busy = True
            self.cancelled = False
            self.progress = {"done": 0, "total": 0}
            self._outcome = {"state": "running", "id": uuid.uuid4().hex, "message": message}
            job_id = self._outcome["id"]
        def worker():
            state, message = "failed", "Vorgang unerwartet beendet"
            try:
                result = operation() or {}
                state = "cancelled" if self.cancelled else "completed"
                message = result.get("message", "Vorgang abgeschlossen.")
                self._log(message)
            except Exception as e:
                state, message = "failed", str(e)
                self._log("❌ " + message)
            finally:
                with self._job_lock:
                    self._outcome = {"state": state, "id": job_id, "message": message}
                    self._busy = False
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            with self._job_lock:
                self._busy = False
            raise
        return {"job_id": job_id}

    def run_generation(self, selection=None, folder=None, model=None, voice_id=None, project=None):
        project = copy.deepcopy(project) if project is not None else self._require_project()
        voice = self.voices.get(voice_id or self._project_voice)
        env = os.environ.copy()
        env.update(PYTHONUNBUFFERED="1", ELEVENLABS_MODEL=model or self.model,
                   OUTPUT_DIR=folder or project["folder"],
                   ELEVENLABS_VOICE_ID=voice["id"], TTS_VOICE_NAME=voice["name"],
                   SHEET_NAME=project["name"], TTS_WORKSHEET_ID=project["id"],
                   SPREADSHEET_ID=project["spreadsheet_id"],
                   TTS_REVIEW_DATA_FILE=project["review_data"], TTS_REVIEW_HTML=project["review_html"])
        self._log(f"📁 Projekt: {project['name']}")
        self._log(f"🎙 Stimme: {voice['name']} ({voice['id']})")
        env.pop("TTS_ONLY_ROWS", None)
        env.pop("TTS_SELECTION", None)
        if selection is not None:
            env["TTS_SELECTION"] = json.dumps(selection, ensure_ascii=False)
        cmd = [sys.executable, str(self.root / "sheets_to_elevenlabs_qc_local.py")]
        try:
            p = subprocess.Popen(cmd, cwd=str(self.root), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
            self.process = p
            if self.cancelled:
                p.terminate()
            for line in p.stdout:
                line = line.rstrip("\n")
                m = PROGRESS_RE.match(line)
                if m:
                    self.progress = {"done": int(m.group(1)), "total": int(m.group(2))}
                else:
                    self.log_queue.put(line)
            code = p.wait()
            if self.cancelled:
                return {"message": "Lauf abgebrochen. Bereits gespeicherte Ergebnisse bleiben erhalten."}
            if code:
                raise RuntimeError(f"Generierung mit Fehlercode {code} beendet. Siehe Protokoll.")
            return {"message": "Generierung abgeschlossen. Qualitätsbefunde stehen in der Review-Seite."}
        finally:
            self.process = None

    @_guard
    @_idle
    def start(self, rows: list, all_open: bool = False, project_id=None) -> dict:
        project = self._require_project(project_id)
        wanted = {int(n) for n in (rows or [])}
        if not wanted and not all_open:
            raise ValueError("Es ist keine Zeile ausgewählt.")
        selection = None
        if wanted:
            if not self.rows_loaded:
                raise ValueError("Bitte die Projektzeilen zuerst neu laden.")
            selected = [r for r in self.rows if r["_row"] in wanted]
            if len(selected) != len(wanted):
                raise ValueError("Auswahl nicht mehr aktuell. Bitte Sheet neu laden.")
            from tts_identity import resolve_record
            selection = [{k: resolve_record(self.rows, r).get(k, "") for k in ("id", "text", "mode")} for r in selected]
        self.progress = {"done": 0, "total": len(wanted)}
        folder, model, voice_id = self.folder, self.model, self.voices.get(self._project_voice)["id"]
        result = self.launch_job(lambda: self.run_generation(selection, folder, model, voice_id, project), "Generierung läuft")
        return {**result, "total": len(wanted)}

    def shutdown(self):
        for server in self._review_servers.values():
            server.close()
        if self.process is not None:
            self.cancelled = True
            self.process.terminate()

    @_guard
    def cancel(self) -> dict:
        p = self.process
        if p is None:
            return {}
        self.cancelled = True
        self.log_queue.put("\n⏹ Abbruch angefordert…")
        try:
            p.terminate()
        except Exception:
            pass
        return {}

    # ------------------------------------------------------------------ Log

    def _log(self, text: str) -> None:
        for line in str(text).splitlines() or [""]:
            self.log_queue.put(line)

    @_guard
    def poll(self) -> dict:
        lines = []
        try:
            while True:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        return {"lines": lines,
                "running": self._busy, "cancellable": self.process is not None,
                "progress": dict(self.progress), "outcome": dict(self._outcome)}
