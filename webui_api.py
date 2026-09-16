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
            "running": self.process is not None,
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
        path = self.root / pipeline.REVIEW_HTML
        if not path.exists():
            pipeline.generate_review_html()
        webbrowser.open("file://" + str(path))
        return {}

    @_guard
    def reset_review(self) -> dict:
        pipeline.reset_review()
        self._log("🗑 Review-Seite zurückgesetzt — neue Audios werden wieder gesammelt.")
        return {}

    @_guard
    def check_update(self) -> dict:
        if self.process is not None:
            raise ValueError("Bitte warten, bis der aktuelle Lauf beendet ist.")
        if not (self.root / ".git").is_dir():
            return {"state": "nogit",
                    "message": "Dieser Ordner ist kein Git-Checkout. Für "
                               "automatische Updates das Projekt einmal per "
                               "'git clone' einrichten (siehe README)."}
        try:
            r = subprocess.run(["git", "pull"], cwd=str(self.root),
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

    @_guard
    def start(self, rows: list, all_open: bool = False) -> dict:
        """Startet den Generierungslauf.

        rows      = genau diese Sheet-Zeilen (übergeht den Status-Filter).
        all_open  = Rückfall, wenn keine Tabelle geladen ist: dann entscheidet
                    wie früher allein der Status im Sheet.
        """
        if self.process is not None:
            raise ValueError("Es läuft bereits eine Generierung.")
        wanted = [int(n) for n in (rows or [])]
        if not wanted and not all_open:
            raise ValueError("Es ist keine Zeile ausgewählt.")

        self.cancelled = False
        self.progress = {"done": 0, "total": len(wanted)}
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["ELEVENLABS_MODEL"] = self.model
        env["OUTPUT_DIR"] = self.folder
        if wanted:
            env["TTS_ONLY_ROWS"] = ",".join(str(n) for n in wanted)
        else:
            env.pop("TTS_ONLY_ROWS", None)

        script = str(self.root / "sheets_to_elevenlabs_qc_local.py")
        cmd = [sys.executable, script]
        threading.Thread(target=self._run, args=(cmd, env), daemon=True).start()
        return {"total": len(wanted)}

    def _run(self, cmd: List[str], env: dict) -> None:
        try:
            self.process = subprocess.Popen(
                cmd, cwd=str(self.root), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
            for line in self.process.stdout:            # type: ignore[union-attr]
                line = line.rstrip("\n")
                m = PROGRESS_RE.match(line)
                if m:
                    self.progress = {"done": int(m.group(1)),
                                     "total": int(m.group(2))}
                    continue
                self.log_queue.put(line)
            code = self.process.wait()
            if self.cancelled:
                self.log_queue.put("\n⏹ Lauf abgebrochen.")
            elif code == 0:
                self.log_queue.put("\n✔ Lauf beendet.")
            else:
                self.log_queue.put(f"\n❌ Lauf mit Fehlercode {code} beendet.")
        except Exception as e:
            self.log_queue.put(f"\nFehler beim Starten: {e}")
        finally:
            self.process = None

    @_guard
    def cancel(self) -> dict:
        p = self.process
        if p is None:
            return {}
        self.cancelled = True
        self.log_queue.put("\n⏹ Abbruch angefordert — laufendes Item wird beendet…")
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
                "running": self.process is not None,
                "progress": dict(self.progress)}
