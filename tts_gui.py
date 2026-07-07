"""
TTS Studio — GUI für die Google-Sheets→ElevenLabs-Pipeline.

Start:  python tts_gui.py   (oder per Doppelklick über die "TTS Studio.app",
        siehe make_app.sh)

Funktionen:
- Start-Button führt die Pipeline aus (Log läuft live im Fenster mit)
- Auswahl des ElevenLabs-Voice-Modells
- Button öffnet die review.html im Browser
- Reset-Button leert die (kumulative) Review-Seite
"""

import os
import sys
import queue
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Ins Projektverzeichnis wechseln, damit alle relativen Pfade
# (.env, service_account.json, Ordner, review.html) stimmen.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

import sheets_to_elevenlabs_qc_local as pipeline

VOICE_MODELS = [
    "eleven_turbo_v2_5",
    "eleven_flash_v2_5",
    "eleven_multilingual_v2",
    "eleven_v3",
]


def sheet_url() -> str:
    """Baut die URL zum Google Sheet aus der Spreadsheet-ID."""
    sid = pipeline.SPREADSHEET_ID
    if sid and sid != "YOUR_SPREADSHEET_ID":
        return f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    return ""


class QueueWriter:
    """Leitet print()-Ausgaben der Pipeline in die GUI um."""
    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)

    def flush(self):
        pass


class TTSStudio:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("TTS Studio")
        root.geometry("760x560")
        root.minsize(620, 420)

        self.log_queue = queue.Queue()
        self.running = False
        self.root.geometry("760x620")

        # ── Kopfbereich: Modell + Start ──
        top = ttk.Frame(root, padding=(14, 12, 14, 6))
        top.pack(fill="x")

        ttk.Label(top, text="Voice-Modell:").pack(side="left")
        default_model = pipeline.ELEVENLABS_MODEL if pipeline.ELEVENLABS_MODEL in VOICE_MODELS else VOICE_MODELS[0]
        self.model_var = tk.StringVar(value=default_model)
        self.model_box = ttk.Combobox(top, textvariable=self.model_var,
                                      values=VOICE_MODELS, state="readonly", width=26)
        self.model_box.pack(side="left", padx=(8, 16))

        self.start_btn = ttk.Button(top, text="▶  Generierung starten", command=self.start)
        self.start_btn.pack(side="left")

        # ── Ausgabeordner-Leiste ──
        folder = ttk.Frame(root, padding=(14, 0, 14, 6))
        folder.pack(fill="x")
        ttk.Label(folder, text="Zielordner:").pack(side="left")
        self.folder_var = tk.StringVar(value=os.path.abspath(pipeline.OUTPUT_DIR))
        self.folder_entry = ttk.Entry(folder, textvariable=self.folder_var)
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(folder, text="Durchsuchen…", command=self.choose_folder).pack(side="left")

        # ── Aktions-Buttons ──
        actions = ttk.Frame(root, padding=(14, 0, 14, 6))
        actions.pack(fill="x")
        ttk.Button(actions, text="📄 Google Sheet öffnen", command=self.open_sheet).pack(side="left")
        ttk.Button(actions, text="🎧 Review-Seite öffnen", command=self.open_review).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="🗑 Review zurücksetzen", command=self.reset_review).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="⬇ Update", command=self.check_update).pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="Bereit.")
        ttk.Label(actions, textvariable=self.status_var, foreground="#666").pack(side="right")

        # ── Log ──
        logframe = ttk.Frame(root, padding=(14, 4, 14, 12))
        logframe.pack(fill="both", expand=True)
        self.log = tk.Text(logframe, wrap="word", state="disabled",
                           bg="#1e1e1e", fg="#d7d7d7", insertbackground="#d7d7d7",
                           font=("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 10))
        scroll = ttk.Scrollbar(logframe, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._append_log("TTS Studio bereit. Modell und Zielordner wählen, dann Start.\n")
        self._poll_queue()

    # ── Aktionen ──

    def choose_folder(self):
        current = self.folder_var.get() or os.path.expanduser("~")
        initial = current if os.path.isdir(current) else os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="Zielordner für TTS-Dateien wählen",
                                         initialdir=initial)
        if chosen:
            self.folder_var.set(chosen)

    def open_sheet(self):
        url = sheet_url()
        if not url:
            messagebox.showinfo(
                "Google Sheet",
                "Keine Spreadsheet-ID gefunden.\n\nTrage SPREADSHEET_ID in die .env ein.")
            return
        webbrowser.open(url)

    def check_update(self):
        """Holt die neueste Version per 'git pull' (falls der Ordner ein Git-Checkout ist)."""
        import subprocess
        if self.running:
            messagebox.showinfo("Update", "Bitte warten, bis der aktuelle Lauf beendet ist.")
            return
        if not os.path.isdir(os.path.join(PROJECT_DIR, ".git")):
            messagebox.showinfo(
                "Update",
                "Dieser Ordner ist kein Git-Checkout.\n\n"
                "Für automatische Updates das Projekt einmal per 'git clone' einrichten "
                "(siehe README).")
            return
        try:
            result = subprocess.run(["git", "pull"], cwd=PROJECT_DIR,
                                    capture_output=True, text=True, timeout=60)
            out = (result.stdout + result.stderr).strip()
            self._append_log(f"\n⬇ git pull:\n{out}\n")
            if "Already up to date" in out or "Bereits aktuell" in out:
                messagebox.showinfo("Update", "Bereits auf dem neuesten Stand.")
            elif result.returncode == 0:
                messagebox.showinfo(
                    "Update",
                    "Update geladen. Bitte die App einmal schließen und neu starten, "
                    "damit die Änderungen aktiv werden.")
            else:
                messagebox.showwarning("Update", f"git pull meldete ein Problem:\n\n{out[-400:]}")
        except FileNotFoundError:
            messagebox.showerror("Update", "git ist nicht installiert.")
        except Exception as e:
            messagebox.showerror("Update", f"Update fehlgeschlagen:\n{e}")

    def start(self):
        if self.running:
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        self.model_box.configure(state="disabled")
        self.status_var.set("Läuft…")
        self._append_log(f"\n{'─'*60}\nStarte mit Modell: {self.model_var.get()}\n{'─'*60}\n")
        threading.Thread(target=self._run_pipeline, daemon=True).start()

    def _run_pipeline(self):
        pipeline.ELEVENLABS_MODEL = self.model_var.get()
        # Gewählten Zielordner übernehmen (OUTPUT_DIR und REVIEW_DIR = derselbe Ordner)
        folder = self.folder_var.get().strip()
        if folder:
            pipeline.OUTPUT_DIR = folder
            pipeline.REVIEW_DIR = folder
        writer = QueueWriter(self.log_queue)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, writer
        try:
            pipeline.main()
        except Exception as e:
            print(f"\n❌ Unerwarteter Fehler: {e}")
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            self.log_queue.put("\n✔ Lauf beendet.\n")
            self.root.after(0, self._run_finished)

    def _run_finished(self):
        self.running = False
        self.start_btn.configure(state="normal")
        self.model_box.configure(state="readonly")
        self.status_var.set("Fertig.")

    def open_review(self):
        path = os.path.join(PROJECT_DIR, pipeline.REVIEW_HTML)
        if not os.path.exists(path):
            # Leere Seite erzeugen, damit der Link immer funktioniert
            try:
                pipeline.generate_review_html()
            except Exception as e:
                messagebox.showerror("Review", f"Review-Seite konnte nicht erstellt werden:\n{e}")
                return
        webbrowser.open("file://" + path)

    def reset_review(self):
        if self.running:
            messagebox.showinfo("Review", "Bitte warten, bis der aktuelle Lauf beendet ist.")
            return
        if not messagebox.askyesno(
            "Review zurücksetzen",
            "Alle Einträge von der Review-Seite entfernen?\n\n"
            "(Die Audio-Dateien selbst bleiben erhalten — nur die Übersicht wird geleert. "
            "Neu generierte Audios erscheinen danach wieder.)"
        ):
            return
        try:
            pipeline.reset_review()
            self._append_log("🗑 Review-Seite zurückgesetzt — neue Audios werden wieder gesammelt.\n")
            self.status_var.set("Review zurückgesetzt.")
        except Exception as e:
            messagebox.showerror("Review", f"Zurücksetzen fehlgeschlagen:\n{e}")

    # ── Log-Handling ──

    def _append_log(self, s: str):
        self.log.configure(state="normal")
        self.log.insert("end", s)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    TTSStudio(root)
    root.mainloop()