"""
Google Sheets → ElevenLabs TTS mit QC und Status-Zentrale
==========================================================
Das Google Sheet ist Input UND Status-Bericht in einem. Pro Zeile werden
Text, Voice, Modus, Ziel-Dateiname und Status gepflegt; das Skript verarbeitet
nur offene Zeilen und schreibt die QC-Ergebnisse (Status, WER, Silence, Gemini-Score,
Transkript, Grund, Zeitstempel) direkt ins Sheet zurück.

ERWARTETE SPALTEN (Kopfzeile in Zeile 1, Reihenfolge egal, Groß/Kleinschreibung egal):
  id            stabile Inhalts-ID (z.B. "bauernhof_kuh")           [Input]
  text          der zu sprechende Text                              [Input]
  filename      gewünschter Dateiname (leer = automatisch)          [Input/Output]
  mode          "Einzelwort" für Einzelwort-Modus, sonst "Normal"   [Input]
  status        todo | regenerate  → wird verarbeitet               [Input/Output]
                passed | review needed                              [Output]
  reason        Grund bei "review needed"                           [Output]
  generated_at  Zeitstempel                                         [Output]

Nutze die mitgelieferte Vorlage TTS_Vorlage.xlsx (mit Dropdowns) als Ausgangspunkt.

SETUP
-----
1. pip install gspread google-auth requests openai-whisper pydub google-genai audioop-lts
2. ffmpeg installieren (macOS: brew install ffmpeg | Windows: winget install ffmpeg)
3. WICHTIG: Der Service Account braucht jetzt SCHREIBrechte. Das Sheet muss mit der
   Service-Account-E-Mail als *Bearbeiter* (nicht nur Betrachter) geteilt sein.
4. CONFIG unten ausfüllen, dann: python3 sheets_to_elevenlabs_qc_local.py
"""

import os
import re
import json
import time
import random
import shutil
import subprocess
import requests
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import whisper
from pydub import AudioSegment
from pydub.silence import detect_silence, detect_nonsilent
from google import genai
from google.genai import types
from tts_quality import (
    CheckResult, QualityResult, compare_text, normalize_for_compare, word_error_rate,
    NATURALNESS_SCHEMA, NATURALNESS_SYSTEM_PROMPT, GEMINI_PROMPT_VERSION,
    SCORE_BY_SEVERITY, naturalness_prompt, parse_naturalness,
)

# .env laden (falls python-dotenv installiert ist). Ohne .env greift os.getenv auf
# echte Umgebungsvariablen zurück – der Rest funktioniert weiterhin.
# Die .env wird explizit NEBEN diesem Skript gesucht, damit sie unabhängig vom
# Arbeitsverzeichnis gefunden wird (wichtig unter Windows / bei App-Start).
try:
    from dotenv import load_dotenv
    _here = os.path.dirname(os.path.abspath(__file__))
    _env_path = os.path.join(_here, ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
    else:
        load_dotenv()  # Fallback: Suche im aktuellen Verzeichnis
except ImportError:
    pass

# ─────────────────────────────────────────────
# CONFIG — fill these in
# ─────────────────────────────────────────────

# Google Sheets
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "service_account.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "YOUR_SPREADSHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "Tabellenblatt1")

# Welche Status-Werte verarbeitet werden
PROCESS_STATUSES = {"todo", "regenerate"}
TREAT_EMPTY_STATUS_AS_TODO = True   # leere Status-Zelle wie "todo" behandeln

# Optionale Einschränkung auf bestimmte Sheet-Zeilen (Zeilennummern wie im Sheet).
# Wird von der GUI gesetzt, wenn dort Zeilen manuell ausgewählt wurden. Eine
# solche Auswahl übergeht den Status-Filter bewusst — so lässt sich eine bereits
# fertige Zeile erneut generieren, ohne im Sheet den Status zu ändern.
# None/leer = normales Verhalten (alle Zeilen nach Status-Filter).
# TTS_ONLY_ROWS ist der Weg, auf dem die Oberfläche die Auswahl an den
# Subprozess übergibt (z.B. "5,9,12").
ONLY_ROWS = None
_env_rows = os.getenv("TTS_ONLY_ROWS", "").strip()
if _env_rows:
    ONLY_ROWS = [int(n) for n in _env_rows.replace(";", ",").split(",")
                 if n.strip().isdigit()]

# ElevenLabs — Keys/Voice kommen aus der .env (siehe .env.example)
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "iMHt6G42evkXunaDU065")
# Modell per Umgebungsvariable überschreibbar (wird von der GUI-App gesetzt)
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")
ELEVENLABS_LANGUAGE_CODE = "de"   # Erzwingt die Sprache (ISO 639-1). None = automatisch.
# Rohformat von ElevenLabs. PCM = verlustfrei → beste Basis fürs Postprocessing.
# pcm_24000 ist in allen Tiers verfügbar; pcm_44100 braucht einen höheren Plan.
# Schlägt PCM fehl (z.B. Plan-Beschränkung), fällt das Skript automatisch auf MP3 zurück.
ELEVENLABS_OUTPUT_FORMAT = "pcm_24000"
VOICE_SETTINGS = {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
    "speed": 0.95,            # <1.0 = langsamer. Gegen "spricht zu schnell".
}
APPEND_PUNCTUATION = True     # Punkt anhängen (Nicht-Wort-Modus), gegen abruptes Abschneiden

# Einzelwort-Modus (aktiviert pro Zeile über die Spalte "mode" = "Einzelwort")
SINGLE_WORD_LEAD_IN = "Das Wort heißt:"
SINGLE_WORD_BREAK = "0.6s"
SINGLE_WORD_TRAILING = ".."
TRIM_BREAK_MIN_MS = 400       # Ab welcher Pausenlänge die Wortgrenze erkannt wird
TRIM_PAD_START_MS = 80        # Puffer vor dem Wort
TRIM_PAD_END_MS = 180         # Puffer nach dem Wort (großzügiger: leise Endungen schützen)
# Sanity-Checks nach dem Trimmen (Einzelwort-Modus)
WORD_MIN_MS = 250             # Kürzer → vermutlich abgeschnitten / Trim danebengegangen
WORD_MAX_MS = 4000            # Länger → vermutlich Einleitungs-Rest noch enthalten
WORD_MIN_DBFS = -35           # Leiser (Durchschnittspegel) → vermutlich fast leer/zu leise

# Postprocessing (nach bestandener Generierung, vor QC)
POSTPROCESS = True            # Stille trimmen + Fades + Loudness-Normalisierung
EXPORT_FORMAT = "opus"        # "opus" (Projekt-Zielformat) oder "mp3"
OPUS_BITRATE = "64k"
TARGET_SAMPLE_RATE = 48000    # Opus-Standard
LOUDNORM_I = -16              # Ziel-Lautheit in LUFS (EBU R128; -16 üblich für Apps/Mobile)
LOUDNORM_TP = -1.5            # True-Peak-Limit dBTP
FADE_IN_MS = 20
FADE_OUT_MS = 60
# Rand-Trimmen: nur ECHTE Stille schneiden, leise Wortenden schützen.
EDGE_KEEP_START_MS = 100      # Rest-Stille am Anfang
EDGE_KEEP_END_MS = 250        # Rest-Stille am Ende (großzügig: ausklingende Endungen!)
EDGE_MIN_SILENCE_MS = 300     # Erst ab dieser Länge gilt etwas als Rand-Stille
EDGE_TRIM_MAX_MS = 2500       # Sicherung: mehr würde nie am Rand weggeschnitten
# Relative Stille-Schwelle: min(-50, Durchschnittspegel - 30). Leise gesprochene
# Endungen (~-35..-48dB) liegen über dieser Schwelle und bleiben erhalten;
# echte TTS-Stille (< -60dB) wird weiterhin erkannt und getrimmt.

# Truncation-Erkennung in der QC (fängt abgeschnittene Sätze/Wörter)
EXPECTED_CHARS_PER_SEC = 15   # Grobe Sprechgeschwindigkeit Deutsch (für Plausibilitäts-Check)
MIN_DURATION_RATIO = 0.5      # Audio kürzer als 50% der Erwartung → vermutlich abgebrochen

# Whisper (lokal)
WHISPER_MODEL = "medium"      # tiny | base | small | medium | large
WHISPER_LANGUAGE = "de"

# Gemini (naturalness check) — Key kommt aus der .env
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
ENABLE_GEMINI_CHECK = True
GEMINI_MIN_SCORE = 7
# Schweregrad → interne Note. Der Code benotet anhand des Schweregrads, nicht das Modell.
# Mit GEMINI_MIN_SCORE = 7 (Default): none & minor bestehen, major fällt durch.
# Strenger (auch minor soll durchfallen): GEMINI_MIN_SCORE = 8.
# Bestätigte major-Defekte werden unabhängig von diesem Wert nie freigegeben.
GEMINI_MAX_RETRIES = 4
GEMINI_MIN_INTERVAL_SEC = 13

# Output — ein gemeinsamer Ordner für ALLE TTS-Dateien (passed + review needed).
# Der Status steht im Sheet und in der review.html, nicht mehr in Ordnernamen.
# Über die .env (OUTPUT_DIR) oder die GUI (Ordner-Auswahl) überschreibbar.
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "tts-output")
REVIEW_DIR = OUTPUT_DIR   # kein separater Review-Ordner mehr — alles am selben Ort
REVIEW_HTML = "review.html"
REVIEW_DATA_FILE = "review_data.json"   # akkumulierte Einträge für die Review-Seite

# QC thresholds
MAX_RETRIES = 3
# WER ist eine Kennzahl, keine Freigabegrenze: verbleibende Wortabweichungen
# brauchen nach der Normalisierung eine neue Aufnahme oder menschliche Prüfung.
MAX_SILENCE_MS = 1500
SILENCE_THRESHOLD_DB = -40

DELAY_BETWEEN_REQUESTS = 0.5

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def col_letter(idx: int) -> str:
    """1-basierter Spaltenindex → Buchstabe (1→A, 27→AA)."""
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def find_ffmpeg():
    """
    Sucht ffmpeg zuerst im PATH, dann an den üblichen Installationsorten.
    Nötig, weil eine per Doppelklick gestartete App den PATH aus der Shell-Konfig
    NICHT immer erbt und ffmpeg sonst nicht findet (Mac wie Windows).
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        # macOS (Homebrew)
        "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg",
        # Windows (winget / choco / manuell)
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe"),
        os.path.expandvars(r"%ProgramData%\chocolatey\bin\ffmpeg.exe"),
    ]
    # winget legt ffmpeg in einem versionierten Ordner ab — dort suchen
    winget_pkgs = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_pkgs):
        for root, _dirs, files in os.walk(winget_pkgs):
            if "ffmpeg.exe" in files:
                candidates.append(os.path.join(root, "ffmpeg.exe"))
                break
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


# Einmal auflösen und wiederverwenden
FFMPEG_BIN = find_ffmpeg()
# pydub den ffmpeg-Pfad explizit mitteilen (falls nicht im PATH, z.B. App-Start)
if FFMPEG_BIN:
    AudioSegment.converter = FFMPEG_BIN
    _ffprobe = FFMPEG_BIN.replace("ffmpeg", "ffprobe")
    if os.path.exists(_ffprobe):
        AudioSegment.ffprobe = _ffprobe


def short_slug(text: str, max_words: int = 4, max_len: int = 40) -> str:
    """Kurzer, dateisicherer Slug aus den ersten Wörtern (Umlaute transliteriert)."""
    t = text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    slug = "_".join(t.split()[:max_words])
    return slug[:max_len] or "audio"


AUDIO_EXTS = (".mp3", ".opus", ".ogg", ".wav", ".m4a")


def build_filename(row: dict) -> str:
    """
    Dateiname: explizit aus Spalte 'filename', sonst
    <row_number>_<content_id>_<short_slug>.<EXPORT_FORMAT>
    Die Endung wird immer an das Exportformat angepasst.
    """
    ext = f".{EXPORT_FORMAT}"
    explicit = row.get("filename", "").strip()
    if explicit:
        base = explicit
        for known in AUDIO_EXTS:
            if base.lower().endswith(known):
                base = base[: -len(known)]
                break
        return base + ext
    num = str(row["_row"]).zfill(3)
    content_id = re.sub(r"[^\w\-]", "", row.get("id", "").strip()) or "item"
    slug = short_slug(row.get("text", ""))
    return f"{num}_{content_id}_{slug}{ext}"


# ─────────────────────────────────────────────
# GOOGLE SHEET (Lesen + Zurückschreiben)
# ─────────────────────────────────────────────

def open_sheet():
    """Öffnet das Sheet mit Schreibrechten. Returns (worksheet, header_map, records)."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    ws = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

    all_values = ws.get_all_values()
    if not all_values:
        return ws, {}, []

    header = all_values[0]
    header_map = {h.strip().lower(): i + 1 for i, h in enumerate(header) if h.strip()}

    records = []
    for r_idx, row_values in enumerate(all_values[1:], start=2):
        row = {}
        for h_lower, col_idx in header_map.items():
            row[h_lower] = row_values[col_idx - 1].strip() if col_idx - 1 < len(row_values) else ""
        row["_row"] = r_idx
        records.append(row)
    return ws, header_map, records


def write_back(ws, header_map: dict, row_number: int, updates: dict):
    """Schreibt die angegebenen Felder in ihre jeweiligen Spalten der Zeile zurück."""
    data = []
    for field, value in updates.items():
        col = header_map.get(field.lower())
        if not col:
            continue  # Spalte existiert im Sheet nicht → überspringen
        cell = f"{col_letter(col)}{row_number}"
        data.append({"range": cell, "values": [[str(value)]]})
    if data:
        try:
            ws.batch_update(data)
        except Exception as e:
            print(f"      ⚠ Konnte Sheet-Zeile {row_number} nicht aktualisieren: {e}")


def should_process(status: str) -> bool:
    s = (status or "").strip().lower()
    if not s:
        return TREAT_EMPTY_STATUS_AS_TODO
    return s in PROCESS_STATUSES


def load_rows():
    """Liest alle Sheet-Zeilen — für die Auswahl-Tabelle in der GUI.

    Anders als main() wird hier nichts verarbeitet und nichts geschrieben:
    nur lesen, damit die GUI anzeigen kann, was im Sheet steht.

    Returns eine Liste von Dicts mit den Spalten der Kopfzeile plus:
      _row   Zeilennummer im Sheet (wie in der Tabelle sichtbar)
      _open  True, wenn der Status diese Zeile normalerweise verarbeiten würde
    """
    _ws, _header_map, records = open_sheet()
    for r in records:
        r["_open"] = should_process(r.get("status", ""))
    return records


def next_ids(prefix: str, count: int, records: list) -> list:
    """Erzeugt fortlaufende IDs zu einem Präfix, anschließend an die höchste
    bereits vorhandene Nummer.

    Trennzeichen und Ziffernbreite werden von den vorhandenen IDs übernommen,
    damit neue Einträge zum bestehenden Schema passen:
      "Buchstaben_001", …   → "Buchstaben_185"
      "Request_Artem01", …  → "Request_Artem04"
    Gibt es das Präfix noch nicht, wird "_" und dreistellig verwendet.
    """
    prefix = prefix.strip()
    if not prefix:
        raise ValueError("Kein ID-Präfix angegeben.")
    muster = re.compile(rf"^{re.escape(prefix)}([_-]?)(\d+)$", re.IGNORECASE)
    hoechste, sep, pad = 0, "_", 3
    treffer = False
    for r in records:
        m = muster.match((r.get("id", "") or "").strip())
        if not m:
            continue
        treffer = True
        nummer = int(m.group(2))
        if nummer >= hoechste:
            hoechste, sep, pad = nummer, m.group(1), len(m.group(2))
    if not treffer:
        hoechste, sep, pad = 0, "_", 3
    return [f"{prefix}{sep}{str(hoechste + i).zfill(pad)}"
            for i in range(1, count + 1)]


def append_rows(entries: list):
    """Hängt neue Zeilen unten an das Sheet an.

    entries: Liste von Dicts {spaltenname: wert}, z.B.
             {"id": "Buchstaben_185", "text": "Zebra", "mode": "Einzelwort",
              "status": "todo"}
    Spalten, die es im Sheet nicht gibt, werden ignoriert; 'filename' bleibt
    bewusst leer, den baut die Pipeline beim Generieren selbst.

    Prüft vorher gegen den aktuellen Sheet-Stand auf doppelte IDs — die ID ist
    der stabile Schlüssel eines Eintrags und darf sich nicht wiederholen.
    Returns (erste_neue_zeilennummer, anzahl).
    """
    if not entries:
        return (None, 0)
    ws, header_map, records = open_sheet()
    if not header_map:
        raise RuntimeError("Das Sheet hat keine Kopfzeile — kann nichts anfügen.")

    vorhanden = {(r.get("id", "") or "").strip().lower()
                 for r in records if (r.get("id", "") or "").strip()}
    neu_ids, doppelt = set(), []
    for e in entries:
        eid = (e.get("id", "") or "").strip().lower()
        if not eid:
            continue
        if eid in vorhanden or eid in neu_ids:
            doppelt.append(e.get("id", ""))
        neu_ids.add(eid)
    if doppelt:
        raise ValueError("Diese ID(s) gibt es schon: " + ", ".join(doppelt))

    breite = max(header_map.values())
    zeilen = []
    for e in entries:
        zeile = [""] * breite
        for feld, wert in e.items():
            col = header_map.get(feld.strip().lower())
            if col:
                zeile[col - 1] = str(wert)
        zeilen.append(zeile)

    # value_input_option="RAW": Texte bleiben Text — Sheets soll aus "1/2" kein
    # Datum und aus "=x" keine Formel machen.
    ws.append_rows(zeilen, value_input_option="RAW")
    erste = len(records) + 2          # +1 Kopfzeile, +1 = erste neue Zeile
    return (erste, len(zeilen))


# ─────────────────────────────────────────────
# ELEVENLABS + AUDIO
# ─────────────────────────────────────────────

def get_elevenlabs_character_count():
    """Aktuell verbrauchte Zeichen (= Credits) vom ElevenLabs-Konto. None bei Fehler."""
    url = "https://api.elevenlabs.io/v1/user/subscription"
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json().get("character_count")
        print(f"   ⚠ Konnte ElevenLabs-Verbrauch nicht abrufen (HTTP {r.status_code})")
    except Exception as e:
        print(f"   ⚠ Konnte ElevenLabs-Verbrauch nicht abrufen: {e}")
    return None


_elevenlabs_pcm_failed = False   # merkt sich, ob PCM abgelehnt wurde (→ MP3-Fallback)


def _pcm_rate(fmt: str):
    m = re.match(r"pcm_(\d+)", fmt or "")
    return int(m.group(1)) if m else None


def text_to_speech(text: str, output_path: str, seed: int = None,
                   single_word_mode: bool = False) -> str:
    """
    Generiert Audio. Returns den tatsächlichen Dateipfad ('' bei Fehler) —
    die Endung hängt vom gelieferten Format ab (.wav bei PCM, .mp3 beim Fallback).
    """
    global _elevenlabs_pcm_failed

    if single_word_mode:
        tts_text = f'{SINGLE_WORD_LEAD_IN} <break time="{SINGLE_WORD_BREAK}" /> {text}{SINGLE_WORD_TRAILING}'
    else:
        tts_text = text
        if APPEND_PUNCTUATION and tts_text and tts_text[-1] not in ".!?,;:":
            tts_text = tts_text + "."

    payload = {
        "text": tts_text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": VOICE_SETTINGS,
    }
    if ELEVENLABS_LANGUAGE_CODE:
        payload["language_code"] = ELEVENLABS_LANGUAGE_CODE
    if seed is not None:
        payload["seed"] = seed
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}

    use_pcm = ELEVENLABS_OUTPUT_FORMAT.startswith("pcm_") and not _elevenlabs_pcm_failed
    fmt = ELEVENLABS_OUTPUT_FORMAT if use_pcm else "mp3_44100_128"
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}?output_format={fmt}"

    response = requests.post(url, headers=headers, json=payload)

    # PCM vom Plan nicht erlaubt o.ä. → einmalig auf MP3 zurückfallen und erneut
    if response.status_code != 200 and use_pcm:
        err = response.text[:300]
        if "output_format" in err or "pcm" in err.lower() or response.status_code in (400, 403):
            print(f"      ⚠ PCM-Format abgelehnt ({response.status_code}) — falle für diesen Lauf auf MP3 zurück.")
            _elevenlabs_pcm_failed = True
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}?output_format=mp3_44100_128"
            response = requests.post(url, headers=headers, json=payload)
            use_pcm = False

    if response.status_code != 200:
        print(f"      ✗ ElevenLabs error {response.status_code}: {response.text[:200]}")
        return ""

    base, _ = os.path.splitext(output_path)
    if use_pcm:
        # Rohe PCM-Bytes (16-bit mono) in einen WAV-Container packen
        rate = _pcm_rate(fmt) or 24000
        audio = AudioSegment(data=response.content, sample_width=2, frame_rate=rate, channels=1)
        actual_path = base + ".wav"
        audio.export(actual_path, format="wav")
    else:
        actual_path = base + ".mp3"
        with open(actual_path, "wb") as f:
            f.write(response.content)
    return actual_path


def _edge_silence_thresh(audio: AudioSegment) -> float:
    """
    Relative Stille-Schwelle für Rand-Trimmen: nur was DEUTLICH unter dem
    Durchschnittspegel liegt, gilt als Stille. Echte TTS-Stille liegt < -60dB,
    leise Wortenden bei ca. -35 bis -48dB — die Schwelle -50 trennt beides sicher.
    """
    if audio.dBFS == float("-inf"):
        return -70.0
    return min(-50.0, audio.dBFS - 30.0)


def trim_to_word(input_path: str, output_path: str) -> bool:
    """
    Schneidet im Einzelwort-Modus die Einleitung weg und behält nur das Wort.
    Schreibt IMMER in eine separate Zieldatei (input bleibt unberührt).
    """
    audio = AudioSegment.from_file(input_path)
    nonsilent = detect_nonsilent(audio, min_silence_len=200, silence_thresh=SILENCE_THRESHOLD_DB)
    if not nonsilent:
        audio.export(output_path, format="wav")
        return False
    word_start = nonsilent[0][0]
    for idx in range(1, len(nonsilent)):
        gap = nonsilent[idx][0] - nonsilent[idx - 1][1]
        if gap >= TRIM_BREAK_MIN_MS:
            word_start = nonsilent[idx][0]
    word_end = nonsilent[-1][1]
    start = max(0, word_start - TRIM_PAD_START_MS)
    end = min(len(audio), word_end + TRIM_PAD_END_MS)
    audio[start:end].export(output_path, format="wav")
    return True


def single_word_sanity_check(audio_path: str):
    """
    Plausibilitäts-Checks nach dem Trimmen im Einzelwort-Modus.
    Returns (ok: bool, reason: str).
    """
    audio = AudioSegment.from_file(audio_path)
    dur = len(audio)

    if dur < WORD_MIN_MS:
        return False, f"Wort zu kurz nach Trimmen ({dur}ms < {WORD_MIN_MS}ms) — vermutlich abgeschnitten"
    if dur > WORD_MAX_MS:
        return False, f"Audio zu lang nach Trimmen ({dur}ms > {WORD_MAX_MS}ms) — vermutlich Einleitungs-Rest enthalten"
    if audio.dBFS == float("-inf") or audio.dBFS < WORD_MIN_DBFS:
        level = "stumm" if audio.dBFS == float("-inf") else f"{audio.dBFS:.1f} dBFS"
        return False, f"Pegel zu niedrig ({level} < {WORD_MIN_DBFS} dBFS)"

    # Nach dem Trimmen sollte nur noch EIN zusammenhängendes Sprach-Segment übrig sein.
    # Eine weitere lange Lücke deutet auf einen Rest der Einleitung hin.
    nonsilent = detect_nonsilent(audio, min_silence_len=200, silence_thresh=SILENCE_THRESHOLD_DB)
    for idx in range(1, len(nonsilent)):
        gap = nonsilent[idx][0] - nonsilent[idx - 1][1]
        if gap >= TRIM_BREAK_MIN_MS:
            return False, f"Lange Lücke ({gap}ms) im getrimmten Audio — Trim vermutlich danebengegangen"

    return True, "ok"


def postprocess_audio(input_path: str, output_path: str) -> bool:
    """
    Postprocessing-Stufe: Stille an Anfang/Ende schonend trimmen, kurze Fades,
    Loudness-Normalisierung (EBU R128) und Export ins Zielformat (Opus/MP3).

    Schutzmechanismen gegen zu aggressives Schneiden:
    - Relative Stille-Schwelle (deutlich unter Durchschnittspegel) statt fixer -40dB,
      damit leise Wortenden nicht als Stille gewertet werden
    - Nur zusammenhängende Stille >= EDGE_MIN_SILENCE_MS gilt als Rand-Stille
    - Großzügiges Keep am Ende (EDGE_KEEP_END_MS)
    - Harte Obergrenze EDGE_TRIM_MAX_MS pro Rand — mehr wird nie weggeschnitten
    """
    audio = AudioSegment.from_file(input_path)
    original_len = len(audio)

    # 1. Rand-Stille schonend entfernen
    thresh = _edge_silence_thresh(audio)
    nonsilent = detect_nonsilent(audio, min_silence_len=EDGE_MIN_SILENCE_MS, silence_thresh=thresh)
    if nonsilent:
        start = max(0, nonsilent[0][0] - EDGE_KEEP_START_MS)
        end = min(original_len, nonsilent[-1][1] + EDGE_KEEP_END_MS)
        # Sicherung: nie mehr als EDGE_TRIM_MAX_MS pro Rand abschneiden
        start = min(start, EDGE_TRIM_MAX_MS)
        end = max(end, original_len - EDGE_TRIM_MAX_MS)
        if start < end:
            audio = audio[start:end]

    # 2. Kurze Fades gegen Klicks an den Schnittkanten
    if len(audio) > FADE_IN_MS + FADE_OUT_MS:
        audio = audio.fade_in(FADE_IN_MS).fade_out(FADE_OUT_MS)

    # 3. Zwischenstand als WAV, dann ffmpeg: loudnorm + Zielformat
    tmp_wav = output_path + ".pre.wav"
    audio.export(tmp_wav, format="wav")

    if EXPORT_FORMAT == "opus":
        codec_args = ["-c:a", "libopus", "-b:a", OPUS_BITRATE]
    else:
        codec_args = ["-c:a", "libmp3lame", "-b:a", "128k"]

    cmd = [
        FFMPEG_BIN or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", tmp_wav,
        "-af", f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA=11",
        "-ar", str(TARGET_SAMPLE_RATE),
        *codec_args,
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"      ⚠ ffmpeg-Postprocessing fehlgeschlagen: {result.stderr[:200]}")
            # Fallback: ohne loudnorm direkt aus pydub exportieren
            fallback_fmt = "opus" if EXPORT_FORMAT == "opus" else "mp3"
            audio.export(output_path, format=fallback_fmt,
                         parameters=["-ar", str(TARGET_SAMPLE_RATE)])
        return True
    except Exception as e:
        print(f"      ⚠ Postprocessing-Fehler: {e}")
        return False
    finally:
        if os.path.exists(tmp_wav):
            os.remove(tmp_wav)


def transcribe_audio(audio_path: str, model) -> str:
    result = model.transcribe(audio_path, language=WHISPER_LANGUAGE, fp16=False)
    return result["text"]


def check_silences(audio_path: str) -> int:
    audio = AudioSegment.from_file(audio_path)
    silences = detect_silence(audio, min_silence_len=500, silence_thresh=SILENCE_THRESHOLD_DB)
    middle_silences = [
        end - start for start, end in silences
        if start > 200 and end < len(audio) - 200
    ]
    return max(middle_silences) if middle_silences else 0


# ─────────────────────────────────────────────
# GEMINI NATURALNESS CHECK
# ─────────────────────────────────────────────

_gemini_last_call = 0.0
_gemini_disabled_for_run = False


def _parse_retry_delay(error_text: str, default: int) -> int:
    m = re.search(r"retry in\s+([\d.]+)s", error_text, re.IGNORECASE)
    if not m:
        m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?([\d.]+)s", error_text)
    if m:
        return int(float(m.group(1))) + 2
    return default


def _audio_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".mp3": "audio/mp3", ".opus": "audio/ogg", ".ogg": "audio/ogg",
            ".wav": "audio/wav"}.get(ext, "audio/mp3")


def check_naturalness(audio_path: str, original_text: str, gemini_client, single_word_mode: bool = False):
    """Structured audio assessment with strict local validation."""
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Part.from_bytes(data=audio_bytes, mime_type=_audio_mime(audio_path)),
                  naturalness_prompt(original_text, single_word_mode)],
        config=types.GenerateContentConfig(
            system_instruction=NATURALNESS_SYSTEM_PROMPT,
            temperature=0.0,
            response_mime_type="application/json",
            response_json_schema=NATURALNESS_SCHEMA,
        ),
    )
    assessment = parse_naturalness(response.text)
    details = {**assessment, "model": GEMINI_MODEL, "prompt_version": GEMINI_PROMPT_VERSION}
    if assessment["assessment"] != "assessed":
        return CheckResult("error", "Nicht sicher beurteilbar: " + assessment["reason"], details=details)
    score = SCORE_BY_SEVERITY[assessment["severity"]]
    details["score"] = score
    # A confirmed major defect is never auto-approved by a permissive threshold.
    state = "failed" if assessment["severity"] == "major" or score < GEMINI_MIN_SCORE else "passed"
    return CheckResult(state, assessment["reason"], details=details)


def check_naturalness_with_retry(audio_path, original_text, gemini_client, single_word_mode=False):
    """Retry the SAME audio on transient API/response errors, never assume success."""
    global _gemini_last_call, _gemini_disabled_for_run
    details = {"model": GEMINI_MODEL, "prompt_version": GEMINI_PROMPT_VERSION}
    if _gemini_disabled_for_run:
        return CheckResult("error", "Gemini-Tageslimit erschöpft; Prüfung ausstehend", details=details)
    last_error = "Kein Prüfergebnis"
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        elapsed = time.monotonic() - _gemini_last_call
        if elapsed < GEMINI_MIN_INTERVAL_SEC:
            time.sleep(GEMINI_MIN_INTERVAL_SEC - elapsed)
        try:
            return check_naturalness(audio_path, original_text, gemini_client, single_word_mode)
        except Exception as e:
            err = str(e)
            last_error = err[:300]
            if "PerDay" in err or "GenerateRequestsPerDay" in err:
                _gemini_disabled_for_run = True
                last_error = "Gemini-Tageslimit erschöpft; Prüfung ausstehend"
                break
            if attempt == GEMINI_MAX_RETRIES:
                break
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                wait = min(_parse_retry_delay(err, 30), 70)
            elif "503" in err or "UNAVAILABLE" in err:
                wait = min(5 * (2 ** (attempt - 1)), 60)
            elif isinstance(e, ValueError) and attempt < 2:
                # One corrective retry for malformed/inconsistent model output.
                wait = 0
            else:
                break
            print(f"      ⏳ Gemini-Prüfung erneut auf demselben Audio ({attempt}/{GEMINI_MAX_RETRIES}): {last_error}")
            time.sleep(wait)
        finally:
            _gemini_last_call = time.monotonic()
    return CheckResult("error", "Gemini-Prüfung fehlgeschlagen: " + last_error, details=details)


def quality_check(audio_path, original_text, whisper_model, gemini_client=None,
                  skip_silence=False, single_word_mode=False):
    """Return explicit per-check states; unknown mandatory results never pass."""
    checks = {name: CheckResult("skipped", "Wegen vorheriger Prüfung nicht ausgeführt")
              for name in ("duration", "transcription", "text", "silence", "gemini")}
    if skip_silence:
        checks["silence"] = CheckResult("skipped", "Einzelwort: separater Sanity-Check", required=False)
    if not ENABLE_GEMINI_CHECK:
        checks["gemini"] = CheckResult("skipped", "In der Konfiguration deaktiviert", required=False)
    qc = QualityResult(checks)
    try:
        duration_ms = len(AudioSegment.from_file(audio_path))
        expected_ms = (len(original_text) / EXPECTED_CHARS_PER_SEC) * 1000
        if duration_ms <= 0:
            checks["duration"] = CheckResult("failed", "Leeres Audio")
            return qc
        if not single_word_mode and expected_ms > 800 and duration_ms < expected_ms * MIN_DURATION_RATIO:
            checks["duration"] = CheckResult("failed", f"Audio vermutlich abgebrochen ({duration_ms/1000:.1f}s, erwartet ~{expected_ms/1000:.1f}s)")
            return qc
        checks["duration"] = CheckResult("passed", "Dauer plausibel", details={"duration_ms": duration_ms})
    except Exception as e:
        checks["duration"] = CheckResult("error", f"Audio/Dauer nicht prüfbar: {e}")
        return qc

    try:
        qc.transcript = transcribe_audio(audio_path, whisper_model).strip()
        checks["transcription"] = CheckResult("passed", "Transkription abgeschlossen")
    except Exception as e:
        checks["transcription"] = CheckResult("error", f"Transkription fehlgeschlagen: {e}")
        return qc
    comparison = compare_text(original_text, qc.transcript)
    qc.wer = comparison["wer"]
    checks["text"] = CheckResult("passed" if comparison["passed"] else "failed",
                                  comparison["reason"], details=comparison)
    if not comparison["passed"]:
        return qc

    if not skip_silence:
        try:
            qc.silence_ms = check_silences(audio_path)
            checks["silence"] = CheckResult(
                "failed" if qc.silence_ms > MAX_SILENCE_MS else "passed",
                f"Längste innere Pause: {qc.silence_ms} ms")
        except Exception as e:
            checks["silence"] = CheckResult("error", f"Pausenprüfung fehlgeschlagen: {e}")
        if checks["silence"].state != "passed":
            return qc

    if ENABLE_GEMINI_CHECK:
        if gemini_client is None:
            checks["gemini"] = CheckResult("error", "Gemini-Prüfer nicht verfügbar")
        else:
            try:
                result = check_naturalness_with_retry(audio_path, original_text, gemini_client, single_word_mode)
                checks["gemini"] = result if isinstance(result, CheckResult) else CheckResult("error", "Gemini lieferte kein gültiges Prüfergebnis")
                qc.gemini_score = checks["gemini"].details.get("score")
            except Exception as e:
                checks["gemini"] = CheckResult("error", f"Gemini-Prüfung fehlgeschlagen: {e}")
    return qc


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def process_row(row, ws, header_map, whisper_model, gemini_client, counts):
    """Verarbeitet eine einzelne Sheet-Zeile: generieren, QC, Datei ablegen, zurückschreiben."""
    row_num = row["_row"]
    text = row.get("text", "").strip()
    label = row.get("id", "").strip() or f"Zeile {row_num}"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Leerer Text → review needed (nicht in der Dropdown-Liste ist "skipped", daher markieren)
    if not text:
        print(f"[Zeile {row_num}] '{label}' → kein Text, übersprungen.")
        write_back(ws, header_map, row_num,
                   {"status": "review needed", "reason": "kein Text", "generated_at": now,
                    "qc_state": "error", "qc_details": ""})
        counts["skipped"] += 1
        return

    # mode: "Einzelwort" → Einzelwort-Modus, sonst Normal
    single_word_mode = row.get("mode", "").strip().lower() in ("einzelwort", "word")
    filename = build_filename(row)
    final_path = os.path.join(OUTPUT_DIR, filename)

    mode_note = "Einzelwort" if single_word_mode else "Normal"
    preview = text[:50] + "…" if len(text) > 50 else text
    print(f"[Zeile {row_num}] '{label}' ({mode_note}) → {filename}")
    print(f"            \"{preview}\"")

    best_attempt = None  # (path, wer, silence_ms, gemini_score, reason, transcript, score)
    attempt_files = []   # alle Zwischen-Dateien dieses Items (für Aufräumen)
    attempt_qc = {}      # Befunde immer dem zugehörigen Audioversuch zuordnen

    for attempt in range(1, MAX_RETRIES + 1):
        seed = random.randint(1, 1_000_000) if attempt > 1 else None
        seed_note = f" (seed={seed})" if seed else ""
        print(f"   Attempt {attempt}/{MAX_RETRIES}{seed_note}...")

        stem = os.path.join(OUTPUT_DIR, f"_tmp_{row_num}_{attempt}")

        # 1. Generieren (PCM/WAV bevorzugt; Rückgabe = tatsächlicher Pfad)
        raw_path = text_to_speech(text, stem + "_raw", seed=seed, single_word_mode=single_word_mode)
        if not raw_path:
            continue
        attempt_files.append(raw_path)
        work_path = raw_path

        # 2. Einzelwort: Einleitung in SEPARATE Datei wegschneiden + Sanity-Checks
        if single_word_mode:
            trimmed_path = stem + "_trimmed.wav"
            try:
                trim_to_word(raw_path, trimmed_path)
                attempt_files.append(trimmed_path)
                work_path = trimmed_path
            except Exception as e:
                print(f"      ⚠ Trimming fehlgeschlagen (nutze ungetrimmtes Audio): {e}")

            ok, sanity_reason = single_word_sanity_check(work_path)
            if not ok:
                print(f"   ✗ Sanity-Check: {sanity_reason}")
                # zählt als fehlgeschlagener Versuch; als best_attempt-Kandidat aufnehmen
                score = 2.0  # schlechter als jeder QC-Fail, aber vorhanden falls alles scheitert
                if best_attempt is None:
                    best_attempt = (work_path, 1.0, 0, 0, sanity_reason, "", score)
                time.sleep(DELAY_BETWEEN_REQUESTS)
                continue

        # 3. Postprocessing: Stille trimmen, Fades, Loudness-Normalisierung, Zielformat
        if POSTPROCESS:
            processed_path = stem + f"_final.{EXPORT_FORMAT}"
            if postprocess_audio(work_path, processed_path):
                attempt_files.append(processed_path)
                work_path = processed_path
            else:
                print("      ⚠ Postprocessing übersprungen (nutze unbearbeitetes Audio)")

        # 4. QC auf dem FINALEN (bearbeiteten) Audio
        qc = quality_check(
            work_path, text, whisper_model, gemini_client,
            skip_silence=single_word_mode, single_word_mode=single_word_mode
        )
        attempt_qc[work_path] = qc.to_dict()
        reason, wer, silence_ms = qc.reason, qc.wer, qc.silence_ms
        gemini_score, transcript = qc.gemini_score, qc.transcript

        if qc.passed:
            shutil.move(work_path, final_path)
            gemini_note = f", Gemini={gemini_score}/10" if ENABLE_GEMINI_CHECK and gemini_score else ""
            print(f"   ✅ Passed QC (WER={wer:.0%}, silence={silence_ms if silence_ms is not None else 'nicht geprüft'}{gemini_note}) → {filename}")
            write_back(ws, header_map, row_num, {
                "status": "passed", "filename": filename,
                "reason": "", "generated_at": now,
                "qc_state": "passed", "qc_details": json.dumps(qc.to_dict(), ensure_ascii=False),
            })
            row.update({"status": "passed", "filename": filename, "reason": "",
                        "generated_at": now, "_transcript": transcript,
                        "_wer": wer, "_gemini": gemini_score, "_qc": qc.to_dict()})
            _record_review(row)
            counts["passed"] += 1
            _cleanup(attempt_files)
            return

        print(f"   ✗ Failed: {reason}")
        if qc.has_error:
            # Infrastruktur-/Prüffehler nicht durch weitere kostenpflichtige
            # Generierungen behandeln. Audio + offene Prüfung für Review behalten.
            best_attempt = (work_path, wer, silence_ms, gemini_score, reason, transcript, float("inf"))
            print("      ⚠ QC unvollständig — Audio behalten, keine weitere TTS-Generierung für dieses Item.")
            break
        naturalness_penalty = (10 - gemini_score) / 10 if gemini_score else 0
        score = (wer if wer is not None else 1.0) + ((silence_ms or 0) / 10000) + naturalness_penalty
        if best_attempt is None or score < best_attempt[6]:
            best_attempt = (work_path, wer, silence_ms, gemini_score, reason, transcript, score)

        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Alle Versuche fehlgeschlagen → bester Versuch behalten, Status "review needed"
    if best_attempt:
        saved_qc = attempt_qc.get(best_attempt[0])
        shutil.move(best_attempt[0], final_path)
        _, b_wer, b_sil, b_gem, b_reason, b_transcript, _ = best_attempt
        wer_note = f"{b_wer:.0%}" if b_wer is not None else "nicht geprüft"
        print(f"   ⚠ Keine vollständige Freigabe → 'review needed' (WER={wer_note}, Gemini={b_gem if b_gem is not None else 'nicht geprüft'})")
        write_back(ws, header_map, row_num, {
            "status": "review needed", "filename": filename,
            "reason": b_reason, "generated_at": now,
            "qc_state": "error" if saved_qc and any(c["state"] == "error" for c in saved_qc["checks"].values()) else "failed",
            "qc_details": json.dumps(saved_qc, ensure_ascii=False) if saved_qc else "",
        })
        row.update({"status": "review needed", "filename": filename, "reason": b_reason,
                    "generated_at": now, "_transcript": b_transcript,
                    "_wer": b_wer, "_gemini": b_gem, "_qc": saved_qc})
        _record_review(row)
        counts["review"] += 1
    else:
        # gar kein Audio erzeugt
        print(f"   ✗ Keine Audio-Generierung möglich → review needed")
        failed_qc = QualityResult({"generation": CheckResult("error", "Kein neues Audio erzeugt")}).to_dict()
        write_back(ws, header_map, row_num, {
            "status": "review needed", "filename": filename,
            "reason": "ElevenLabs-Generierung fehlgeschlagen", "generated_at": now,
            "qc_state": "error", "qc_details": json.dumps(failed_qc, ensure_ascii=False),
        })
        row.update({"status": "review needed", "filename": filename,
                    "reason": "ElevenLabs-Generierung fehlgeschlagen", "generated_at": now,
                    "_qc": failed_qc, "_wer": None, "_gemini": None, "_transcript": ""})
        _record_review(row)
        counts["failed"] += 1
    _cleanup(attempt_files)


def _cleanup(paths):
    """Löscht alle noch vorhandenen Zwischen-Dateien (bereits verschobene existieren nicht mehr)."""
    for p in paths:
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


# ─────────────────────────────────────────────
# REVIEW-HTML (akkumulierend, Reset über die GUI)
# ─────────────────────────────────────────────

def _esc(s) -> str:
    """HTML-Escaping für Textinhalte."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _review_qc_html(qc):
    """Readable evidence, including skipped checks and accepted minor defects."""
    if not isinstance(qc, dict):
        return '<div class="cmeta">Älterer Eintrag: keine detaillierten QC-Befunde gespeichert.</div>'
    labels = {"passed": "bestanden", "failed": "auffällig", "error": "Prüffehler",
              "skipped": "nicht ausgeführt"}
    names = {"generation": "Generierung", "duration": "Audiodauer", "transcription": "Transkription", "text": "Textvergleich",
             "silence": "Pausen", "gemini": "Natürlichkeit"}
    parts = []
    for name, check in qc.get("checks", {}).items():
        title = names.get(name, name)
        state = labels.get(check.get("state"), "unbekannt")
        parts.append(f'<p><strong>{_esc(title)}: {_esc(state)}</strong><br>{_esc(check.get("reason", ""))}</p>')
        detail = check.get("details", {})
        for defect in detail.get("defects", []):
            parts.append(f'<p>{_esc(defect.get("category", ""))} ({_esc(defect.get("severity", ""))}): {_esc(defect.get("evidence", ""))}</p>')
        if name == "gemini" and detail.get("model"):
            parts.append(f'<p>Prüfmodell: {_esc(detail["model"])} · Prompt: {_esc(detail.get("prompt_version", ""))}</p>')
    summary = "QC unvollständig / Review nötig" if qc.get("incomplete") else "QC-Befunde"
    return f'<details class="cmeta"><summary>{summary}</summary>{"".join(parts)}</details>'


def _load_review_data() -> dict:
    if os.path.exists(REVIEW_DATA_FILE):
        try:
            with open(REVIEW_DATA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_review_entry(entry: dict):
    """
    Fügt einen Eintrag zur akkumulierten Review-Datenbasis hinzu (oder aktualisiert
    ihn, wenn dieselbe Datei neu generiert wurde). Bleibt über Läufe hinweg erhalten,
    bis die GUI die Review-Seite zurücksetzt.
    """
    data = _load_review_data()
    key = entry.get("filename") or f"row_{entry.get('row', '?')}"
    data[key] = entry
    try:
        with open(REVIEW_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"      ⚠ Review-Daten konnten nicht gespeichert werden: {e}")


def _record_review(row: dict):
    """Übernimmt das Ergebnis einer verarbeiteten Zeile in die Review-Datenbasis."""
    filename = row.get("filename", "")
    abspath = os.path.abspath(os.path.join(OUTPUT_DIR, filename)) if filename else ""
    _save_review_entry({
        "row": row.get("_row"),
        "id": row.get("id", ""),
        "text": row.get("text", ""),
        "filename": filename,
        "abspath": abspath,   # voller Pfad → HTML-Player funktioniert auch bei externem Zielordner
        "mode": row.get("mode", ""),
        "status": row.get("status", ""),
        "reason": row.get("reason", ""),
        "generated_at": row.get("generated_at", ""),
        "transcript": row.get("_transcript", ""),
        "wer": row.get("_wer"),
        "gemini": row.get("_gemini"),
        "qc": row.get("_qc"),
        "model": ELEVENLABS_MODEL,
    })


# ─────────────────────────────────────────────
# REVIEW-SEITE (Caretable-Design)
# ─────────────────────────────────────────────

_CHIP_ICON = ('<svg viewBox="0 0 24 24" width="13" height="13" fill="none" '
              'stroke="currentColor" stroke-width="3.2" stroke-linecap="round" '
              'stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7"/></svg>')

# Platzhalter __NOW__, __NPASSED__, __NREVIEW__, __NALL__, __CARDS__, __EMPTYALL__
_REVIEW_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TTS Review — __NOW__</title>
<style>
@font-face { font-family:"CardiumA-Regular"; src:url("webui/assets/fonts/CardiumARegular.woff2") format("woff2"); }
@font-face { font-family:"CardiumA-Medium";  src:url("webui/assets/fonts/CardiumAMedium.woff2")  format("woff2"); }
@font-face { font-family:"CardiumA-Bold";    src:url("webui/assets/fonts/CardiumABold.woff2")    format("woff2"); }
:root{
  --ct_font_regular:"CardiumA-Regular",-apple-system,system-ui,sans-serif;
  --ct_font_medium:"CardiumA-Medium",-apple-system,system-ui,sans-serif;
  --ct_font_bold:"CardiumA-Bold",-apple-system,system-ui,sans-serif;
  --ct_A:#000F28; --ct_E1:#F5F7FC; --ct_E2:#7A818E; --ct_E3_30:#ECF1F4;
  --ct_F1:#EE8300; --ct_correct:#64B489; --ct_fail:#F56262;
  --green:#117875; --green-deep:#0C5F5C;
  --app:var(--ct_E1); --card:#FFFFFF; --inset:var(--ct_E3_30); --line:#E4E9F0;
  --tx:var(--ct_A); --tx2:var(--ct_E2); --tx3:#A7AEBA;
  --shadow:0 .125rem .5rem rgba(0,15,40,.07);
  --shadow-lift:0 .375rem 1.125rem rgba(0,15,40,.12);
  --mono:"SF Mono",Menlo,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--app);color:var(--tx);font-family:var(--ct_font_regular);font-size:15px}
.head{display:flex;align-items:flex-start;gap:24px;padding:34px 44px 4px;flex-wrap:wrap}
.head img{width:46px;height:46px;border-radius:11px;margin-top:4px;flex:none;box-shadow:var(--shadow)}
.head .ttl{flex:1 1 340px;min-width:0}
.head h1{margin:0;font-family:var(--ct_font_bold);font-size:34px;line-height:1.1;color:var(--green);letter-spacing:-.01em}
.head .sub{font-family:var(--ct_font_medium);font-size:19px;line-height:1.35;margin-top:4px}
.filters{display:flex;align-items:center;gap:10px;padding-top:8px;flex-wrap:wrap;flex:1 1 auto;min-width:0;justify-content:flex-end}
.filters button{height:42px;padding:0 20px;border-radius:999px;border:0;cursor:pointer;
  font-family:var(--ct_font_medium);font-size:15px;background:#FFFFFF;color:var(--tx);
  box-shadow:var(--shadow);white-space:nowrap}
.filters button.on{background:var(--green);color:#fff}
.wrap{padding:20px 44px 28px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:20px;align-items:start}
.card{background:var(--card);border-radius:12px;box-shadow:var(--shadow);padding:20px 22px 18px;min-width:0}
.chead{display:flex;align-items:center;gap:10px}
.chip{display:flex;align-items:center;gap:7px;height:28px;padding:0 12px;border-radius:999px;
  font-family:var(--ct_font_medium);font-size:13px;flex:none}
.chip.passed{background:#DCEFE5;color:#2C7A56}
.chip.review{background:#FCE6CC;color:#A85B00}
.chip.other{background:var(--inset);color:var(--tx2)}
.cid{font-family:var(--ct_font_medium);font-size:17px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.player{display:flex;align-items:center;gap:12px;margin-top:16px;padding:10px 14px;border-radius:10px;background:var(--inset)}
.play{flex:none;width:34px;height:34px;border-radius:50%;background:var(--green);display:flex;
  align-items:center;justify-content:center;cursor:pointer}
.play:hover{background:var(--green-deep)}
.play .ico-play{margin-left:2px}
.play .ico-pause{display:none}
.play.pause .ico-play{display:none}
.play.pause .ico-pause{display:block}
.track{flex:1;height:6px;border-radius:999px;background:#D3DEE4;cursor:pointer}
.track .fill{width:0;height:100%;border-radius:999px;background:var(--green)}
.time{flex:none;font-family:var(--mono);font-size:12.5px;color:var(--tx2)}
.missing{margin-top:16px;padding:10px 14px;border-radius:10px;background:var(--inset);
  color:var(--ct_fail);font-size:14px}
.ctext{font-size:15.5px;line-height:1.5;margin-top:14px;text-wrap:pretty;overflow-wrap:anywhere}
.cwhisper{font-size:14.5px;line-height:1.5;color:var(--tx2);margin-top:6px;text-wrap:pretty;overflow-wrap:anywhere}
.creason{font-size:14.5px;line-height:1.5;color:#A85B00;margin-top:6px;text-wrap:pretty}
.cmeta{font-size:13px;color:var(--tx3);margin-top:12px;line-height:1.55;overflow-wrap:anywhere}
.empty{padding:60px 0;text-align:center;color:var(--tx2);font-size:17px}
.hint{padding:0 44px 32px;color:var(--tx3);font-size:13px}
@media (max-width:760px){
  .head{padding:22px 20px 4px;gap:16px}
  .wrap{padding:16px 20px 24px}
  .hint{padding:0 20px 24px}
  .grid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="head">
  <img src="webui/assets/brand/app-icon.png" alt="">
  <div class="ttl">
    <h1>TTS Review</h1>
    <div class="sub">Stand __NOW__ — __NPASSED__ passed · __NREVIEW__ review needed</div>
  </div>
  <div class="filters">
    <button class="on" data-f="all">Alle (__NALL__)</button>
    <button data-f="passed">Passed (__NPASSED__)</button>
    <button data-f="review">Review needed (__NREVIEW__)</button>
  </div>
</div>
<div class="wrap">
  <div class="grid" id="grid">__CARDS__</div>
  <div class="empty" id="empty" style="display:__EMPTYALL__">Noch keine Audios generiert.</div>
</div>
<div class="hint">Hinweis: Falls .opus-Dateien in Safari nicht abspielbar sind, die Seite in Chrome oder Firefox öffnen.</div>
<script>
/* ---------- Filter ---------- */
document.querySelectorAll(".filters button").forEach(function (b) {
  b.onclick = function () {
    document.querySelectorAll(".filters button").forEach(function (x) { x.classList.remove("on"); });
    b.classList.add("on");
    var f = b.dataset.f, sichtbar = 0;
    document.querySelectorAll(".card").forEach(function (c) {
      var zeig = (f === "all" || c.dataset.status === f);
      c.style.display = zeig ? "" : "none";
      if (zeig) sichtbar++;
    });
    var leer = document.getElementById("empty");
    leer.textContent = sichtbar ? "" : "Keine Einträge in dieser Ansicht.";
    leer.style.display = sichtbar ? "none" : "block";
  };
});

/* ---------- Abspieler ---------- */
/* Ein eigener Player statt <audio controls>, damit die Karten dem Design
   entsprechen. Es läuft immer nur ein Audio gleichzeitig. */
var laufend = null;
function mmss(t) {
  if (!isFinite(t)) return "–:––";
  var m = Math.floor(t / 60), s = Math.floor(t % 60);
  return m + ":" + (s < 10 ? "0" : "") + s;
}
document.querySelectorAll(".player").forEach(function (p) {
  var audio = new Audio();
  audio.preload = "metadata";
  audio.src = p.dataset.src;
  var knopf = p.querySelector(".play");
  var fill = p.querySelector(".fill");
  var zeit = p.querySelector(".time");
  var track = p.querySelector(".track");

  function zeigeDauer() { zeit.textContent = mmss(audio.duration); }
  function zeigeStand() {
    if (audio.duration) fill.style.width = (audio.currentTime / audio.duration * 100) + "%";
  }
  audio.addEventListener("loadedmetadata", zeigeDauer);
  audio.addEventListener("durationchange", zeigeDauer);
  /* Die Metadaten können schon da sein, bevor die Listener hängen. */
  if (audio.readyState >= 1) zeigeDauer();

  audio.addEventListener("timeupdate", function () {
    zeigeStand();
    zeit.textContent = mmss(audio.currentTime);
  });
  audio.addEventListener("pause", function () { knopf.classList.remove("pause"); });
  audio.addEventListener("ended", function () {
    knopf.classList.remove("pause"); fill.style.width = "0"; laufend = null;
    audio.currentTime = 0; zeigeDauer();
  });
  audio.addEventListener("error", function () { zeit.textContent = "Fehler"; });

  knopf.onclick = function () {
    if (laufend && laufend.audio !== audio) {
      laufend.audio.pause(); laufend.knopf.classList.remove("pause");
    }
    if (audio.paused) { audio.play(); knopf.classList.add("pause"); laufend = { audio: audio, knopf: knopf }; }
    else { audio.pause(); laufend = null; }
  };
  track.onclick = function (e) {
    if (!audio.duration) return;
    var r = track.getBoundingClientRect();
    audio.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * audio.duration;
    /* Sofort anzeigen — im pausierten Zustand kommt kein timeupdate. */
    zeigeStand();
    zeit.textContent = mmss(audio.currentTime);
  };
});
</script>
</body>
</html>
"""


def generate_review_html(output_file=REVIEW_HTML):
    """
    Erstellt die Review-Seite aus den AKKUMULIERTEN Daten (review_data.json)
    im Caretable-Design. Neue Audios kommen hinzu, bestehende bleiben —
    bis zum Reset über die App.
    """
    data = _load_review_data()
    entries = sorted(data.values(), key=lambda e: e.get("generated_at", ""), reverse=True)

    cards = []
    n_passed = n_review = 0

    for e in entries:
        filename = e.get("filename", "")
        audio_src = None
        # Zuerst der beim Generieren gespeicherte absolute Pfad (robust bei externem
        # Ordner), sonst im aktuellen OUTPUT_DIR nachsehen.
        stored = e.get("abspath", "")
        if stored and os.path.exists(stored):
            audio_src = "file://" + stored
        elif filename:
            candidate = os.path.join(OUTPUT_DIR, filename)
            if os.path.exists(candidate):
                audio_src = "file://" + os.path.abspath(candidate)

        status = e.get("status", "").strip().lower()
        if status == "passed":
            n_passed += 1
            kind, label_txt = "passed", "passed"
        elif status == "review needed":
            n_review += 1
            kind, label_txt = "review", "review needed"
        else:
            kind, label_txt = "other", (status or "unbekannt")

        label = e.get("id", "") or filename or f"Zeile {e.get('row', '?')}"

        if audio_src:
            player = (f'<div class="player" data-src="{_esc(audio_src)}">'
                      f'<div class="play" role="button" tabindex="0" aria-label="Abspielen">'
                      f'<svg class="ico-play" viewBox="0 0 29.195 33.368" width="11" height="13" fill="#fff">'
                      f'<path d="M27.658 13.99 4.718.428A3.111 3.111 0 0 0 0 3.12v27.117a3.125 '
                      f'3.125 0 0 0 4.718 2.692l22.94-13.555a3.125 3.125 0 0 0 0-5.384"/></svg>'
                      f'<svg class="ico-pause" viewBox="0 0 24 24" width="12" height="13" fill="#fff">'
                      f'<rect x="5" y="4" width="5" height="16" rx="1.4"/>'
                      f'<rect x="14" y="4" width="5" height="16" rx="1.4"/></svg>'
                      f'</div><div class="track"><div class="fill"></div></div>'
                      f'<div class="time">–:––</div></div>')
        else:
            player = '<div class="missing">Keine Audio-Datei gefunden</div>'

        whisper = (f'<div class="cwhisper">Whisper: {_esc(e["transcript"])}</div>'
                   if e.get("transcript") else "")
        reason = (f'<div class="creason">Grund: {_esc(e["reason"])}</div>'
                  if e.get("reason") else "")
        qc_html = _review_qc_html(e.get("qc"))

        metrics = []
        if e.get("wer") is not None:
            metrics.append(f"WER {e['wer']:.0%}")
        if e.get("gemini"):
            metrics.append(f"Gemini {e['gemini']}/10")
        if e.get("model"):
            metrics.append(_esc(e["model"]))
        line2 = " · ".join(x for x in (filename, e.get("mode", ""),
                                       e.get("generated_at", "")) if x)
        meta = "<br>".join(x for x in (" · ".join(metrics), _esc(line2)) if x)

        cards.append(f'''
      <div class="card" data-status="{kind}">
        <div class="chead">
          <div class="chip {kind}">{_CHIP_ICON if kind == "passed" else ""}{_esc(label_txt)}</div>
          <div class="cid">{_esc(label)}</div>
        </div>
        {player}
        <div class="ctext">{_esc(e.get("text", ""))}</div>
        {whisper}
        {reason}
        {qc_html}
        <div class="cmeta">{meta}</div>
      </div>''')

    now = datetime.now().strftime("%d.%m.%Y, %H:%M")
    n_all = n_passed + n_review
    body = "".join(cards)
    empty_all = "" if entries else "block"

    html = (_REVIEW_TEMPLATE
            .replace("__NOW__", now)
            .replace("__NPASSED__", str(n_passed))
            .replace("__NREVIEW__", str(n_review))
            .replace("__NALL__", str(len(entries)))
            .replace("__CARDS__", body)
            .replace("__EMPTYALL__", empty_all))

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html)
    return output_file, n_all


def reset_review(output_file=REVIEW_HTML):
    """Setzt die Review-Seite zurück: Daten löschen, leere Seite schreiben."""
    if os.path.exists(REVIEW_DATA_FILE):
        os.remove(REVIEW_DATA_FILE)
    generate_review_html(output_file)


def main(only_rows=None):
    global _gemini_last_call, _gemini_disabled_for_run
    _gemini_last_call = 0.0
    _gemini_disabled_for_run = False
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(REVIEW_DIR, exist_ok=True)

    # Keys prüfen (kommen aus der .env)
    if not ELEVENLABS_API_KEY:
        print("❌ ELEVENLABS_API_KEY fehlt. Trage ihn in die .env ein (siehe .env.example).")
        return
    if ENABLE_GEMINI_CHECK and not GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY fehlt. Trage ihn in die .env ein oder setze ENABLE_GEMINI_CHECK = False.")
        return
    if POSTPROCESS and FFMPEG_BIN is None:
        print("❌ ffmpeg nicht gefunden — wird für Postprocessing/Opus benötigt.")
        print("   macOS: brew install ffmpeg | Windows: winget install ffmpeg")
        print("   (Tipp: Falls ffmpeg installiert ist, App über fix_app.py neu bauen —")
        print("    der Launcher ergänzt dann die Homebrew-Pfade im PATH.)")
        return

    print(f"🧠 Loading Whisper model '{WHISPER_MODEL}'... (first run downloads it)")
    whisper_model = whisper.load_model(WHISPER_MODEL)
    print("   Model loaded.")

    gemini_client = None
    if ENABLE_GEMINI_CHECK:
        print(f"✨ Initializing Gemini client ({GEMINI_MODEL})...")
        gemini_client = genai.Client(api_key=GEMINI_API_KEY,
                                     http_options=types.HttpOptions(timeout=60000))
    print()

    print("📄 Öffne Google Sheet...")
    ws, header_map, records = open_sheet()

    # Pflichtspalten prüfen
    for col in ("id", "text", "status"):
        if col not in header_map:
            print(f"   ⚠ Warnung: Spalte '{col}' fehlt in der Kopfzeile.")

    # Wurde in der GUI eine konkrete Auswahl getroffen, gilt genau die —
    # unabhängig vom Status. Sonst wie gehabt der Status-Filter.
    if only_rows is None:
        only_rows = ONLY_ROWS
    if only_rows:
        wanted = {int(n) for n in only_rows}
        to_process = [r for r in records if r["_row"] in wanted]
        print(f"   {len(records)} Zeilen gesamt, {len(to_process)} ausgewählt "
              f"(Auswahl aus der App).")
        fehlend = wanted - {r["_row"] for r in records}
        if fehlend:
            print(f"   ⚠ Nicht mehr im Sheet gefunden: Zeile(n) "
                  f"{', '.join(str(n) for n in sorted(fehlend))}")
        print()
    else:
        to_process = [r for r in records if should_process(r.get("status", ""))]
        print(f"   {len(records)} Zeilen gesamt, {len(to_process)} zu verarbeiten.\n")

    if not to_process:
        print("   Nichts zu tun — keine passende Zeile gefunden.")
        return

    usage_start = get_elevenlabs_character_count()
    counts = {"passed": 0, "review": 0, "failed": 0, "skipped": 0}

    gesamt = len(to_process)
    print(f"@@PROGRESS 0/{gesamt}", flush=True)
    for i, row in enumerate(to_process, 1):
        process_row(row, ws, header_map, whisper_model, gemini_client, counts)
        print(f"@@PROGRESS {i}/{gesamt}", flush=True)
        time.sleep(DELAY_BETWEEN_REQUESTS)

    print(f"\n{'='*60}")
    print(f"✅ passed:  {counts['passed']}")
    print(f"⚠  review:  {counts['review']}")
    print(f"✗  failed:  {counts['failed']}")
    print(f"–  skipped: {counts['skipped']}")
    print(f"📁 Ordner: {os.path.abspath(OUTPUT_DIR)}")

    usage_end = get_elevenlabs_character_count()
    if usage_start is not None and usage_end is not None:
        print(f"🔊 ElevenLabs verbraucht: {usage_end - usage_start} Zeichen/Credits (Konto-Gesamt: {usage_end})")
    else:
        print("🔊 ElevenLabs-Verbrauch konnte nicht ermittelt werden.")

    # Review-HTML aus den akkumulierten Daten aktualisieren (wächst bis zum Reset)
    try:
        html_file, n_items = generate_review_html()
        print(f"🎧 Review-Seite: {html_file} ({n_items} Audios) — im Browser öffnen zum Anhören")
    except Exception as e:
        print(f"⚠ Review-HTML konnte nicht erstellt werden: {e}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
