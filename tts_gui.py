"""
TTS Studio — GUI für die Google-Sheets→ElevenLabs-Pipeline.

Start:  python tts_gui.py   (oder per Doppelklick über die "TTS Studio.app",
        siehe make_app.sh)

Funktionen (unverändert):
- Start-Button führt die Pipeline aus (Log läuft live im Fenster mit)
- Auswahl des ElevenLabs-Voice-Modells
- Zielordner wählen (Durchsuchen…)
- Buttons: Google Sheet öffnen, Review-Seite öffnen, Review zurücksetzen
- Update suchen (git pull, falls Git-Checkout)

Design: gleiche Sprache wie "Folder Converter" — Scroll-Layout mit gepinntem
Footer, Karten-Frames, runde Buttons, resizable Log mit Zieh-Griff und
"Reset size"-Chip. Akzente in Grün/Petrol statt Blau.

Requirements (einmalig):
    pip install customtkinter
"""

import os
import sys
import queue
import threading
import webbrowser
from tkinter import messagebox, filedialog

import customtkinter as ctk

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

# ---------------------------------------------------------------------------
# Farbschema — grün/petrol (ersetzt das blaue Standard-Theme von Converter.py)
# ---------------------------------------------------------------------------
PETROL = "#0f766e"          # Haupt-Akzent (Menüs, Fortschritt, Sekundär-Buttons)
PETROL_HOVER = "#0b5d57"
GREEN = "#2fa572"           # primäre "Los geht's"-Aktion (Start)
GREEN_HOVER = "#1d6f4d"
RED = "#c0392b"             # destruktiv (Review zurücksetzen)
RED_HOVER = "#922b21"
GREY = ("gray75", "gray30")         # dezente Hilfs-Buttons (Update)
GREY_HOVER = ("gray65", "gray40")


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


class TTSStudio(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("TTS Studio")

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(760, sw - 40), min(660, sh - 80)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")
        self.minsize(620, 460)

        ctk.set_appearance_mode("system")
        # "green" als Basis, damit ungestylte Widgets (Fokusrahmen etc.) nicht
        # blau bleiben; die Kern-Widgets werden zusätzlich petrol eingefärbt.
        ctk.set_default_color_theme("green")

        self.log_queue = queue.Queue()
        self.running = False
        self.model_var = ctk.StringVar(
            value=pipeline.ELEVENLABS_MODEL
            if pipeline.ELEVENLABS_MODEL in VOICE_MODELS else VOICE_MODELS[0])
        self.folder_var = ctk.StringVar(value=os.path.abspath(pipeline.OUTPUT_DIR))

        # resizable-panel state (wie Converter.py — hier nur das Log-Panel)
        self._default_heights = {"log": 260}
        self._heights = dict(self._default_heights)
        self._containers = {}

        self._build_ui()
        self._append_log(
            "TTS Studio bereit. Modell und Zielordner wählen, dann Start.\n")
        self._poll_queue()

    # -- UI ----------------------------------------------------------------

    def _build_ui(self):
        bold14 = ctk.CTkFont(size=14, weight="bold")
        bold13 = ctk.CTkFont(size=13, weight="bold")

        # Gepinnter Footer: Status oben, Fortschritt + Start unten-rechts
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=20, pady=(6, 14))
        self.status = ctk.CTkLabel(footer, text="Bereit.", text_color="gray",
                                   anchor="w")
        self.status.pack(side="top", fill="x")
        row = ctk.CTkFrame(footer, fg_color="transparent")
        row.pack(side="top", fill="x", pady=(6, 0))
        self.progress = ctk.CTkProgressBar(row, progress_color=PETROL)
        self.progress.set(0)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.start_btn = ctk.CTkButton(
            row, text="▶  Generierung starten", width=200, height=46,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=GREEN, hover_color=GREEN_HOVER, command=self.start)
        self.start_btn.pack(side="right")

        # Scrollbarer Inhalt (alles übrige lebt hier drin)
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(side="top", fill="both", expand=True)
        self.scroll.grid_columnconfigure(0, weight=1)
        c = self.scroll

        # "Update suchen": oben im Scrollbereich, scrollt beim Runterscrollen weg
        upd_row = ctk.CTkFrame(c, fg_color="transparent")
        upd_row.grid(row=0, column=0, padx=20, pady=(10, 0), sticky="ew")
        self.update_btn = ctk.CTkButton(
            upd_row, text="Update suchen", width=118, height=26,
            font=ctk.CTkFont(size=11), fg_color=GREY, hover_color=GREY_HOVER,
            command=self.check_update)
        self.update_btn.pack(side="right")
        # "Reset size": oben-links als Overlay, sichtbar wenn das Log vergrößert ist
        self.reset_btn = ctk.CTkButton(
            self, text="⤢ Reset size", width=110, height=26,
            font=ctk.CTkFont(size=11), fg_color=GREY, hover_color=GREY_HOVER,
            command=self.reset_layout)

        # Karte: ElevenLabs / Voice-Modell
        voice_frame = ctk.CTkFrame(c)
        voice_frame.grid(row=1, column=0, padx=20, pady=(16, 8), sticky="ew")
        voice_frame.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(voice_frame, text="ElevenLabs", font=bold13).grid(
            row=0, column=0, columnspan=3, padx=12, pady=(8, 2), sticky="w")
        ctk.CTkLabel(voice_frame, text="Voice-Modell:").grid(
            row=1, column=0, padx=(12, 8), pady=(4, 12), sticky="w")
        self.model_menu = ctk.CTkOptionMenu(
            voice_frame, values=VOICE_MODELS, variable=self.model_var, width=240,
            fg_color=PETROL, button_color=PETROL_HOVER, button_hover_color=PETROL,
            dropdown_hover_color=PETROL)
        self.model_menu.grid(row=1, column=1, pady=(4, 12), sticky="w")

        # Karte: Zielordner
        folder_frame = ctk.CTkFrame(c)
        folder_frame.grid(row=2, column=0, padx=20, pady=8, sticky="ew")
        folder_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(folder_frame, text="Zielordner", font=bold13).grid(
            row=0, column=0, columnspan=2, padx=12, pady=(8, 2), sticky="w")
        self.folder_entry = ctk.CTkEntry(folder_frame, textvariable=self.folder_var)
        self.folder_entry.grid(row=1, column=0, padx=(12, 8), pady=(4, 12),
                               sticky="ew")
        ctk.CTkButton(folder_frame, text="Durchsuchen…", width=130,
                      fg_color=PETROL, hover_color=PETROL_HOVER,
                      command=self.choose_folder).grid(
            row=1, column=1, padx=(0, 12), pady=(4, 12))

        # Karte: Aktionen
        act_frame = ctk.CTkFrame(c)
        act_frame.grid(row=3, column=0, padx=20, pady=8, sticky="ew")
        ctk.CTkLabel(act_frame, text="Aktionen", font=bold13).grid(
            row=0, column=0, columnspan=3, padx=12, pady=(8, 2), sticky="w")
        arow = ctk.CTkFrame(act_frame, fg_color="transparent")
        arow.grid(row=1, column=0, padx=12, pady=(4, 12), sticky="w")
        ctk.CTkButton(arow, text="📄  Google Sheet öffnen", width=180,
                      fg_color=PETROL, hover_color=PETROL_HOVER,
                      command=self.open_sheet).pack(side="left")
        ctk.CTkButton(arow, text="🎧  Review-Seite öffnen", width=180,
                      fg_color=PETROL, hover_color=PETROL_HOVER,
                      command=self.open_review).pack(side="left", padx=(10, 0))
        ctk.CTkButton(arow, text="🗑  Review zurücksetzen", width=180,
                      fg_color=RED, hover_color=RED_HOVER,
                      command=self.reset_review).pack(side="left", padx=(10, 0))

        # Karte: Protokoll (resizable, wie das Log in Converter.py)
        log_card = ctk.CTkFrame(c)
        log_card.grid(row=4, column=0, padx=20, pady=8, sticky="ew")
        log_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_card, text="Protokoll", font=bold14).grid(
            row=0, column=0, padx=12, pady=(10, 2), sticky="w")
        log_outer = ctk.CTkFrame(log_card, fg_color="transparent",
                                 height=self._heights["log"])
        log_outer.grid(row=1, column=0, padx=12, pady=(4, 0), sticky="ew")
        log_outer.grid_propagate(False)
        log_outer.grid_columnconfigure(0, weight=1)
        log_outer.grid_rowconfigure(0, weight=1)
        self.log = ctk.CTkTextbox(log_outer)
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.configure(state="disabled")
        self._containers["log"] = log_outer
        self._make_grip(log_card, "log", log_outer, min_h=120).grid(
            row=2, column=0, padx=12, pady=(3, 10), sticky="ew")

        self._setup_qol()

    def _setup_qol(self):
        """Inner-first Mausrad-Scrolling für das Log (wie Converter.py)."""
        inner = getattr(self.log, "_textbox", None)
        if inner is not None:
            inner.bind("<MouseWheel>", lambda e: self._inner_wheel(e, inner))
            inner.bind("<Button-4>", lambda e: self._inner_wheel(e, inner))
            inner.bind("<Button-5>", lambda e: self._inner_wheel(e, inner))

    # ---- inner-first scrolling ----
    def _wheel_units(self, event):
        n = getattr(event, "num", None)
        if n == 4:
            return -1
        if n == 5:
            return 1
        d = getattr(event, "delta", 0)
        if d == 0:
            return 0
        if abs(d) >= 120:      # Windows: Vielfache von 120
            return -int(d / 120)
        return -1 if d > 0 else 1   # macOS: kleine Integer-Deltas

    def _outer_scroll(self, units):
        cv = getattr(self.scroll, "_parent_canvas", None)
        if cv is not None and units:
            cv.yview_scroll(units, "units")

    def _inner_wheel(self, event, target):
        units = self._wheel_units(event)
        if not units:
            return "break"
        try:
            top, bottom = target.yview()
        except Exception:
            self._outer_scroll(units)
            return "break"
        # inneres Widget scrollen bis zum Rand, dann an das Fenster übergeben
        if units < 0 and top <= 0.0001:
            self._outer_scroll(units)
        elif units > 0 and bottom >= 0.9999:
            self._outer_scroll(units)
        else:
            target.yview_scroll(units, "units")
        return "break"

    # -- Resizable panels --------------------------------------------------

    def _make_grip(self, parent, key, container, min_h):
        grip = ctk.CTkFrame(parent, height=8, corner_radius=4,
                            fg_color=("gray70", "gray35"),
                            cursor="sb_v_double_arrow")
        grip.bind("<Button-1>", lambda e: self._grip_press(e, key))
        grip.bind("<B1-Motion>", lambda e: self._grip_drag(e, container, key, min_h))
        return grip

    def _grip_press(self, event, key):
        self._drag_y0 = event.y_root
        self._drag_h0 = self._heights[key]

    def _grip_drag(self, event, container, key, min_h):
        new_h = max(min_h, self._drag_h0 + (event.y_root - self._drag_y0))
        self._heights[key] = new_h
        container.configure(height=new_h)
        self._update_reset_visibility()

    def _update_reset_visibility(self):
        bigger = any(self._heights[k] > self._default_heights[k] + 2
                     for k in self._heights)
        if bigger:
            self.reset_btn.place(relx=0.0, y=10, x=16, anchor="nw")
            self.reset_btn.lift()
        else:
            self.reset_btn.place_forget()

    def reset_layout(self):
        for key, container in self._containers.items():
            self._heights[key] = self._default_heights[key]
            container.configure(height=self._heights[key])
        self._update_reset_visibility()
        try:
            self.scroll._parent_canvas.yview_moveto(0)
        except Exception:
            pass

    # -- Aktionen ----------------------------------------------------------

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
        """Holt die neueste Version per 'git pull' (falls Git-Checkout)."""
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
        self.model_menu.configure(state="disabled")
        self.status.configure(text="Läuft…")
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self._append_log(
            f"\n{'─' * 60}\nStarte mit Modell: {self.model_var.get()}\n{'─' * 60}\n")
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
            self.after(0, self._run_finished)

    def _run_finished(self):
        self.running = False
        self.start_btn.configure(state="normal")
        self.model_menu.configure(state="normal")
        self.status.configure(text="Fertig.")
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(1.0)

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
            self.status.configure(text="Review zurückgesetzt.")
        except Exception as e:
            messagebox.showerror("Review", f"Zurücksetzen fehlgeschlagen:\n{e}")

    # -- Log-Handling ------------------------------------------------------

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
        self.after(100, self._poll_queue)


if __name__ == "__main__":
    TTSStudio().mainloop()