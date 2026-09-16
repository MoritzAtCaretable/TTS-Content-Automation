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
from pathlib import Path
from typing import List, Optional

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


def sheet_url() -> str:
    """URL zum Google Sheet aus der Spreadsheet-ID."""
    sid = pipeline.SPREADSHEET_ID
    if sid and sid != "YOUR_SPREADSHEET_ID":
        return f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    return ""


class Api:
    def __init__(self) -> None:
        self._window = None
        self.root = PROJECT_DIR
        self.log_queue: queue.Queue = queue.Queue()
        self.process: Optional[subprocess.Popen] = None
        self.progress = {"done": 0, "total": 0}
        self.cancelled = False
        self._busy = False
        self._job_lock = threading.Lock()
        self._outcome = {"state": "idle", "id": "", "message": "Bereit"}
        self._review_server = None
        self.model = (pipeline.ELEVENLABS_MODEL
                      if pipeline.ELEVENLABS_MODEL in VOICE_MODELS else VOICE_MODELS[0])
        self.folder = str((PROJECT_DIR / pipeline.OUTPUT_DIR).resolve()
                          if not os.path.isabs(pipeline.OUTPUT_DIR)
                          else Path(pipeline.OUTPUT_DIR))
        self.rows: List[dict] = []          # zuletzt geladener Sheet-Stand

    # ---------------------------------------------------------------- Zustand

    @_guard
    def get_state(self) -> dict:
        return {
            "models": VOICE_MODELS,
            "model": self.model,
            "folder": self.folder,
            "sheet_name": pipeline.SHEET_NAME,
            "has_sheet_id": bool(sheet_url()),
            "running": self._busy,
        }

    @_guard
    def set_model(self, model: str) -> dict:
        if model in VOICE_MODELS:
            self.model = model
        return {}

    # ------------------------------------------------------------ Sheet lesen

    @_guard
    def load_rows(self) -> dict:
        """Liest den aktuellen Sheet-Stand für die Tabelle."""
        rows = pipeline.load_rows()
        self.rows = rows
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
        return {"rows": out}

    # -------------------------------------------------------- Zeilen anfügen

    @_guard
    def plan_rows(self, prefix: str, mode: str, text: str) -> dict:
        """Baut aus der Eingabe die neuen Zeilen — ohne etwas zu schreiben.

        Eine Zeile = ein Text. "ID | Text" setzt eine eigene ID, sonst wird aus
        dem Präfix fortlaufend numeriert. Das Frontend zeigt das Ergebnis zur
        Bestätigung, bevor commit_rows() tatsächlich schreibt.
        """
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
        if auto and not self.rows:
            raise ValueError("Die Sheet-Zeilen sind noch nicht geladen — ohne "
                             "sie ist die nächste freie Nummer unbekannt. "
                             "Bitte „Neu laden“ drücken.")

        neue_ids = pipeline.next_ids(prefix, len(auto), self.rows) if auto else []
        eintraege: List[Optional[dict]] = [None] * len(zeilen)
        for i, eid, txt in explizit:
            eintraege[i] = {"id": eid, "text": txt, "mode": modus, "status": "todo"}
        for (i, txt), eid in zip(auto, neue_ids):
            eintraege[i] = {"id": eid, "text": txt, "mode": modus, "status": "todo"}
        return {"entries": eintraege}

    @_guard
    def commit_rows(self, entries: list) -> dict:
        """Schreibt die zuvor geplanten Zeilen ans Sheet."""
        erste, anzahl = pipeline.append_rows(entries or [])
        self._log(f"➕ {anzahl} Zeile(n) ins Sheet eingefügt (ab Zeile {erste}).")
        return {"first": erste, "count": anzahl}

    # ------------------------------------------------------------- Aktionen

    @_guard
    def choose_folder(self) -> dict:
        import webview
        start = self.folder if os.path.isdir(self.folder) else str(self.root)
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG,
                                                 directory=start)
        if not result:
            return {"path": None}
        self.folder = str(Path(result[0]))
        return {"path": self.folder}

    @_guard
    def open_sheet(self) -> dict:
        url = sheet_url()
        if not url:
            raise ValueError("Keine Spreadsheet-ID gefunden. "
                             "Trage SPREADSHEET_ID in die .env ein.")
        webbrowser.open(url)
        return {}

    @_guard
    def open_review(self) -> dict:
        from review_service import ReviewServer
        with self._job_lock:
            if self._review_server is None:
                self._review_server = ReviewServer(self, pipeline, self.root)
        webbrowser.open(self._review_server.url)
        return {"url": self._review_server.url}

    @_guard
    def reset_review(self) -> dict:
        with self._job_lock:
            if self._busy:
                raise ValueError("Bitte den laufenden Vorgang abwarten.")
            pipeline.reset_review()
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

    def run_generation(self, selection=None, folder=None, model=None):
        env = os.environ.copy()
        env.update(PYTHONUNBUFFERED="1", ELEVENLABS_MODEL=model or self.model,
                   OUTPUT_DIR=folder or self.folder)
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
    def start(self, rows: list, all_open: bool = False) -> dict:
        wanted = {int(n) for n in (rows or [])}
        if not wanted and not all_open:
            raise ValueError("Es ist keine Zeile ausgewählt.")
        selection = None
        if wanted:
            selected = [r for r in self.rows if r["_row"] in wanted]
            if len(selected) != len(wanted):
                raise ValueError("Auswahl nicht mehr aktuell. Bitte Sheet neu laden.")
            from tts_identity import resolve_record
            selection = [{k: resolve_record(self.rows, r).get(k, "") for k in ("id", "text", "mode")} for r in selected]
        self.progress = {"done": 0, "total": len(wanted)}
        folder, model = self.folder, self.model
        result = self.launch_job(lambda: self.run_generation(selection, folder, model), "Generierung läuft")
        return {**result, "total": len(wanted)}

    def shutdown(self):
        if self._review_server is not None:
            self._review_server.close()
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
