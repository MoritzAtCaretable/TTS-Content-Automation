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
from tkinter import messagebox, filedialog, ttk

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
# Fenster-Hintergrund: sehr blasses, unaufdringliches Grün (Light-Mode) bzw. ein
# minimal grünstichiges Dunkelgrau (Dark-Mode). Hebt die App vom hellgrauen
# Converter-Hintergrund ab, ohne im Einzelnen zu stören.
APP_BG = ("#eef4ef", "#1c2320")
# Karten liegen als leicht abgesetzte Flächen auf dem grünen Grund.
CARD_BG = ("#92bba0", "#242b28")

MUTED_COLOR = ("gray40", "gray60")

# Zeilen-Tabelle. Bewusst ein ttk.Treeview und keine CustomTkinter-Widgets:
# das Sheet hat leicht dreistellig viele Zeilen, und pro Zeile eigene CTk-Widgets
# zu bauen blockiert den Event-Loop minutenlang. Treeview ist ein natives
# Tabellen-Widget und kommt mit tausenden Zeilen zurecht.
TABLE_COLS = (
    ("sel",    "",       34,  "center"),
    ("row",    "Zeile",  46,  "center"),
    ("id",     "ID",     120, "w"),
    ("text",   "Text",   220, "w"),
    ("mode",   "Modus",  80,  "w"),
    ("status", "Status", 110, "w"),
)
CHECKED, UNCHECKED = "☑", "☐"
# Statusfarben je Erscheinungsbild (Light, Dark): grün = fertig,
# orange = braucht Aufmerksamkeit, petrol = steht noch an.
STATUS_COLORS = {
    "passed": ("#15803d", "#4ade80"),
    "review needed": ("#b45309", "#fbbf24"),
    "todo": ("#0f766e", "#5eead4"),
    "regenerate": ("#0f766e", "#5eead4"),
    "_other": ("#57534e", "#a8a29e"),
}
# Treeview-Flächen passend zum Rest der App (Light, Dark)
TABLE_BG = ("#f4f7f5", "#1f2623")
TABLE_FG = ("#111827", "#e5e7eb")
TABLE_SEL = ("#cfe3d6", "#2f3b36")


def _shorten(s: str, n: int) -> str:
    """Kürzt Text für eine Tabellenzelle und glättet Umbrüche/Doppel-Leerzeichen."""
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


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
        w, h = min(880, sw - 40), min(780, sh - 80)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")
        self.minsize(620, 460)

        ctk.set_appearance_mode("system")
        # "green" als Basis, damit ungestylte Widgets (Fokusrahmen etc.) nicht
        # blau bleiben; die Kern-Widgets werden zusätzlich petrol eingefärbt.
        ctk.set_default_color_theme("green")
        # Dezenter blassgrüner Fenster-Hintergrund (hebt sich vom hellgrauen
        # Converter-Fenster ab, ohne aufdringlich zu wirken).
        self.configure(fg_color=APP_BG)

        self.log_queue = queue.Queue()
        self.running = False
        self.model_var = ctk.StringVar(
            value=pipeline.ELEVENLABS_MODEL
            if pipeline.ELEVENLABS_MODEL in VOICE_MODELS else VOICE_MODELS[0])
        self.folder_var = ctk.StringVar(value=os.path.abspath(pipeline.OUTPUT_DIR))

        # Sheet-Auswahl: die geladenen Zeilen und je Zeile eine Checkbox-Variable.
        # Solange nichts geladen ist, verhält sich die App wie vorher (Status-Filter).
        self.sheet_rows = []
        self.row_sel = {}          # Zeilennummer → angehakt?
        self.rows_loaded = False
        self._loading_rows = False
        self._pending_rows = None

        # resizable-panel state (wie Converter.py — hier nur das Log-Panel)
        self._default_heights = {"rows": 200, "log": 260}
        self._heights = dict(self._default_heights)
        self._containers = {}

        self._build_ui()
        self._append_log(
            "TTS Studio bereit. Modell und Zielordner wählen, dann Start.\n")
        self._poll_queue()
        # Zeilen direkt beim Start holen (im Hintergrund). Schlägt es fehl —
        # z.B. keine Zugangsdaten —, bleibt es bei einer Log-Zeile ohne Dialog.
        self.after(300, lambda: self.load_sheet_rows(auto=True))

    # -- UI ----------------------------------------------------------------

    @staticmethod
    def _pick(pair):
        """Wählt aus einem (Light, Dark)-Paar die Farbe fürs aktuelle Erscheinungsbild.
        ttk kennt keine CTk-Farbtupel, deshalb muss hier aufgelöst werden."""
        return pair[1] if ctk.get_appearance_mode() == "Dark" else pair[0]

    def _style_table(self):
        """Bringt das ttk.Treeview optisch auf Linie mit dem Rest der App."""
        style = ttk.Style()
        try:
            style.theme_use("clam")      # lässt sich zuverlässig durchfärben
        except Exception:
            pass
        bg, fg = self._pick(TABLE_BG), self._pick(TABLE_FG)
        style.configure("TTS.Treeview", background=bg, fieldbackground=bg,
                        foreground=fg, borderwidth=0, rowheight=22,
                        font=("", 11))
        style.configure("TTS.Treeview.Heading", background=self._pick(TABLE_SEL),
                        foreground=fg, borderwidth=0,
                        font=("", 11, "bold"))
        style.map("TTS.Treeview.Heading",
                  background=[("active", self._pick(TABLE_SEL))])

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
        voice_frame = ctk.CTkFrame(c, fg_color=CARD_BG)
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
        folder_frame = ctk.CTkFrame(c, fg_color=CARD_BG)
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

        # Karte: Sheet-Zeilen — hier wird bestimmt, was verarbeitet wird.
        # Ersetzt den Umweg über das Google Sheet: anhaken statt Status tippen.
        rows_card = ctk.CTkFrame(c, fg_color=CARD_BG)
        rows_card.grid(row=3, column=0, padx=20, pady=8, sticky="ew")
        rows_card.grid_columnconfigure(0, weight=1)

        rhead = ctk.CTkFrame(rows_card, fg_color="transparent")
        rhead.grid(row=0, column=0, padx=12, pady=(8, 2), sticky="ew")
        ctk.CTkLabel(rhead, text="Sheet-Zeilen", font=bold13).pack(side="left")
        self.reload_btn = ctk.CTkButton(
            rhead, text="⟳  Neu laden", width=118, height=26,
            font=ctk.CTkFont(size=11), fg_color=PETROL, hover_color=PETROL_HOVER,
            command=self.load_sheet_rows)
        self.reload_btn.pack(side="right")

        # Schnellauswahl + Zähler
        tools = ctk.CTkFrame(rows_card, fg_color="transparent")
        tools.grid(row=1, column=0, padx=12, pady=(2, 6), sticky="ew")
        self.sel_buttons = []
        for label, cmd in (("Nur offene", self.select_open),
                           ("Nur Review", self.select_review),
                           ("Alle", self.select_all),
                           ("Keine", self.select_none)):
            b = ctk.CTkButton(tools, text=label, width=84, height=24,
                              font=ctk.CTkFont(size=11), fg_color=GREY,
                              hover_color=GREY_HOVER, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.sel_buttons.append(b)
        self.sel_label = ctk.CTkLabel(tools, text="Noch nicht geladen.",
                                      font=ctk.CTkFont(size=11),
                                      text_color=MUTED_COLOR)
        self.sel_label.pack(side="right")

        # Tabelle: fester Höhen-Container + Zieh-Griff wie beim Protokoll
        rows_outer = ctk.CTkFrame(rows_card, fg_color="transparent",
                                  height=self._heights["rows"])
        rows_outer.grid(row=2, column=0, padx=12, pady=(0, 0), sticky="ew")
        rows_outer.grid_propagate(False)
        rows_outer.grid_columnconfigure(0, weight=1)
        rows_outer.grid_rowconfigure(0, weight=1)

        self._style_table()
        self.table = ttk.Treeview(
            rows_outer, style="TTS.Treeview", selectmode="none",
            columns=[col[0] for col in TABLE_COLS], show="headings")
        for key, title, width, anchor in TABLE_COLS:
            self.table.heading(key, text=title)
            self.table.column(key, width=width, anchor=anchor,
                              stretch=(key == "text"),
                              minwidth=34 if key == "sel" else 40)
        self.table.grid(row=0, column=0, sticky="nsew")
        tscroll = ttk.Scrollbar(rows_outer, orient="vertical",
                                command=self.table.yview)
        tscroll.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=tscroll.set)
        # Klick irgendwo in eine Zeile schaltet deren Haken um.
        self.table.bind("<Button-1>", self._on_table_click)
        for key, colors in STATUS_COLORS.items():
            self.table.tag_configure(f"st_{key}", foreground=self._pick(colors))

        self._containers["rows"] = rows_outer
        self._make_grip(rows_card, "rows", rows_outer, min_h=120).grid(
            row=3, column=0, padx=12, pady=(3, 10), sticky="ew")
        self._render_rows()

        # Karte: Aktionen
        act_frame = ctk.CTkFrame(c, fg_color=CARD_BG)
        act_frame.grid(row=4, column=0, padx=20, pady=8, sticky="ew")
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
        log_card = ctk.CTkFrame(c, fg_color=CARD_BG)
        log_card.grid(row=5, column=0, padx=20, pady=8, sticky="ew")
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

    # -- Sheet-Zeilen laden & auswählen ------------------------------------

    def load_sheet_rows(self, auto: bool = False):
        """Liest die Zeilen aus dem Sheet und baut die Tabelle neu auf.

        auto=True bei automatischen Aufrufen (App-Start, nach einem Lauf):
        dann bei Fehlern nur eine Log-Zeile statt eines Dialogs.
        """
        if self.running or self._loading_rows:
            return
        self._loading_rows = True
        self.reload_btn.configure(state="disabled", text="Lade…")
        self.sel_label.configure(text="Lade Sheet…")
        threading.Thread(target=self._load_rows_worker, args=(auto,),
                         daemon=True).start()

    def _load_rows_worker(self, auto: bool):
        try:
            records = pipeline.load_rows()
        except Exception as e:
            self.after(0, self._rows_load_failed, e, auto)
            return
        self.after(0, self._rows_loaded, records)

    def _rows_load_failed(self, err, auto: bool):
        self._loading_rows = False
        self.reload_btn.configure(state="normal", text="⟳  Neu laden")
        self.sel_label.configure(text="Laden fehlgeschlagen.")
        self._append_log(f"⚠ Sheet konnte nicht geladen werden: {err}\n")
        if not auto:
            messagebox.showerror(
                "Sheet laden",
                f"Die Zeilen konnten nicht geladen werden:\n\n{err}\n\n"
                "Prüfe service_account.json sowie SPREADSHEET_ID und "
                "SHEET_NAME in der .env.")

    def _rows_loaded(self, records):
        self._loading_rows = False
        self.rows_loaded = True
        self.reload_btn.configure(state="normal", text="⟳  Neu laden")
        # Auswahl beim Neuladen erhalten, sofern die Zeile schon bekannt war —
        # sonst die Vorauswahl "was die Pipeline sonst automatisch nähme".
        vorher = dict(self.row_sel)
        self.sheet_rows = records
        self.row_sel = {r["_row"]: vorher.get(r["_row"], r["_open"])
                        for r in records}
        self._render_rows()
        offen = sum(1 for r in records if r["_open"])
        self._append_log(
            f"📄 {len(records)} Zeilen geladen, {offen} davon offen.\n")

    def _render_rows(self):
        """Füllt die Tabelle neu. Ein Treeview-Item pro Sheet-Zeile."""
        self.table.delete(*self.table.get_children())
        if not self.sheet_rows:
            self._update_sel_label()
            return
        for rec in self.sheet_rows:
            n = rec["_row"]
            status_raw = (rec.get("status", "") or "").strip()
            key = status_raw.lower() if status_raw else "todo"
            tag = f"st_{key}" if key in STATUS_COLORS else "st__other"
            einzel = (rec.get("mode", "") or "").strip().lower() in ("einzelwort", "word")
            self.table.insert(
                "", "end", iid=str(n), tags=(tag,),
                values=(CHECKED if self.row_sel.get(n) else UNCHECKED,
                        n,
                        _shorten(rec.get("id", ""), 24),
                        _shorten(rec.get("text", ""), 60),
                        "Einzelwort" if einzel else "Normal",
                        status_raw or "(leer → todo)"))
        self._update_sel_label()

    def _on_table_click(self, event):
        """Klick in eine Zeile schaltet ihren Haken um."""
        if self.running or self._loading_rows:
            return "break"
        if self.table.identify_region(event.x, event.y) != "cell":
            return None          # Kopfzeile/Rand: normal weiterreichen
        iid = self.table.identify_row(event.y)
        if not iid:
            return None
        n = int(iid)
        self.row_sel[n] = not self.row_sel.get(n, False)
        self.table.set(iid, "sel", CHECKED if self.row_sel[n] else UNCHECKED)
        self._update_sel_label()
        return "break"

    def _refresh_checks(self):
        """Schreibt die Haken-Spalte aus row_sel zurück (nach Alle/Keine/Offene)."""
        for iid in self.table.get_children():
            self.table.set(iid, "sel",
                           CHECKED if self.row_sel.get(int(iid)) else UNCHECKED)
        self._update_sel_label()

    def _selected_rows(self) -> list:
        """Zeilennummern der angehakten Zeilen, aufsteigend."""
        return sorted(n for n, on in self.row_sel.items() if on)

    def _update_sel_label(self):
        if not self.sheet_rows:
            self.sel_label.configure(
                text="Keine Zeilen." if self.rows_loaded else "Noch nicht geladen.")
            return
        self.sel_label.configure(
            text=f"{len(self._selected_rows())} von {len(self.sheet_rows)} ausgewählt")

    def select_all(self):
        for n in self.row_sel:
            self.row_sel[n] = True
        self._refresh_checks()

    def select_none(self):
        for n in self.row_sel:
            self.row_sel[n] = False
        self._refresh_checks()

    def select_open(self):
        """Nur die Zeilen, die laut Status noch offen sind (todo/regenerate/leer)."""
        for rec in self.sheet_rows:
            self.row_sel[rec["_row"]] = rec["_open"]
        self._refresh_checks()

    def select_review(self):
        """Nur die Zeilen mit "review needed" — die üblichen Nachzügler."""
        for rec in self.sheet_rows:
            self.row_sel[rec["_row"]] = (
                (rec.get("status", "") or "").strip().lower() == "review needed")
        self._refresh_checks()

    def _set_table_enabled(self, on: bool):
        """Sperrt die Auswahl während eines Laufs (Klicks prüft _on_table_click)."""
        state = "normal" if on else "disabled"
        for b in [self.reload_btn] + self.sel_buttons:
            b.configure(state=state)

    # -- Lauf --------------------------------------------------------------

    def start(self):
        if self.running:
            return
        if self._loading_rows:
            messagebox.showinfo("Start", "Die Sheet-Zeilen werden noch geladen.")
            return

        # Sind Zeilen geladen, gilt genau die Auswahl in der Tabelle — auch für
        # bereits fertige Zeilen. Ohne geladene Zeilen bleibt es beim
        # Status-Filter aus dem Sheet (Verhalten wie vor der Tabelle).
        if self.sheet_rows:
            selected = self._selected_rows()
            if not selected:
                messagebox.showinfo(
                    "Keine Auswahl",
                    "Es ist keine Zeile ausgewählt.\n\n"
                    "Hake in der Tabelle an, was verarbeitet werden soll — "
                    "oder nutze „Nur offene“.")
                return
            self._pending_rows = selected
            liste = ", ".join(str(n) for n in selected[:12])
            if len(selected) > 12:
                liste += " …"
            umfang = f"{len(selected)} ausgewählte Zeile(n) — {liste}"
        else:
            self._pending_rows = None
            umfang = "alle offenen Zeilen (Status-Filter aus dem Sheet)"

        self.running = True
        self.start_btn.configure(state="disabled")
        self.model_menu.configure(state="disabled")
        self._set_table_enabled(False)
        self.status.configure(text="Läuft…")
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self._append_log(
            f"\n{'─' * 60}\nStarte mit Modell: {self.model_var.get()}\n"
            f"Umfang: {umfang}\n{'─' * 60}\n")
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
            pipeline.main(only_rows=self._pending_rows)
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
        self._set_table_enabled(True)
        self.status.configure(text="Fertig.")
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(1.0)
        # Die Pipeline hat die Status-Spalte im Sheet fortgeschrieben —
        # Tabelle nachziehen, damit die Anzeige stimmt.
        if self.rows_loaded:
            self.load_sheet_rows(auto=True)

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