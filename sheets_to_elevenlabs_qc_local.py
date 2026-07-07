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
# Hinweis: flash-lite hat ein höheres Free-Tier-Tageslimit. Exakte ID in AI Studio prüfen.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
ENABLE_GEMINI_CHECK = True
GEMINI_MIN_SCORE = 7
# Schweregrad → interne Note. Der Code benotet anhand des Schweregrads, nicht das Modell.
# Mit GEMINI_MIN_SCORE = 7 (Default): none & minor bestehen, major fällt durch.
# Strenger (auch minor soll durchfallen): GEMINI_MIN_SCORE = 8.
# Lockerer (alles besteht): GEMINI_MIN_SCORE = 0.
SCORE_BY_SEVERITY = {"none": 9, "minor": 7, "major": 3}
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
WER_THRESHOLD = 0.15
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


def normalize_for_compare(s: str) -> list:
    s = s.lower()
    s = re.sub(r"[^\w\sÄÖÜäöüß]", " ", s)
    return s.split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref_words = normalize_for_compare(reference)
    hyp_words = normalize_for_compare(hypothesis)
    if not ref_words:
        return 0.0
    d = [[0] * (len(hyp_words) + 1) for _ in range(len(ref_words) + 1)]
    for i in range(len(ref_words) + 1):
        d[i][0] = i
    for j in range(len(hyp_words) + 1):
        d[0][j] = j
    for i in range(1, len(ref_words) + 1):
        for j in range(1, len(hyp_words) + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                d[i][j] = d[i - 1][j - 1]
            else:
                d[i][j] = 1 + min(d[i - 1][j], d[i][j - 1], d[i - 1][j - 1])
    return d[len(ref_words)][len(hyp_words)] / len(ref_words)


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
    """Have Gemini listen to the audio and rate its naturalness. Returns (score, reason)."""
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    if single_word_mode:
        focus = """This is a SINGLE German word recorded in isolation. Listen for these defects:
- Word is rushed or spoken too fast
- Word is clipped / cut off at the start or end (a syllable missing or truncated)
- Mispronunciation or wrong-language pronunciation (e.g. English instead of German)
- Robotic, buzzy, or audible digital artifacts
- Word is barely audible or trails off"""
    else:
        focus = """This is a German sentence or phrase. Listen for these defects:
- Robotic or flat intonation, unnatural prosody
- OVER-exaggerated, overacted, or theatrical delivery (too much emphasis/drama)
- Rushed sections or unnaturally long pauses
- Mispronunciation or wrong-language pronunciation
- Wrong or unnatural emphasis
- Audible digital artifacts or clipping"""

    prompt = f"""You are a STRICT quality inspector for German text-to-speech audio.
The word/text SHOULD be: "{original_text}"

{focus}

Work in two steps:
STEP 1 — Listen critically and write down EVERY defect you actually hear. Do not assume it
is fine; actively look for problems. If you truly hear none, write "none".

STEP 2 — Classify the overall severity with ONE of these labels:
- none  : no audible issue at all; sounds like a careful human recording
- minor : a small imperfection that is still perfectly usable
- major : a clearly noticeable, distracting or wrong delivery — this INCLUDES being
          rushed, clipped, mispronounced, robotic, OR over-exaggerated / overacted

Be consistent: if you listed ANY real defect in STEP 1, the severity CANNOT be "none".
If the delivery is exaggerated or overacted, that is at least "major".

Respond in EXACTLY this format:
DEFECTS: <what you heard, or "none">
SEVERITY: <none|minor|major>
REASON: <one short sentence>

Do not include anything else."""

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=audio_bytes, mime_type=_audio_mime(audio_path)),
            prompt,
        ],
        config=types.GenerateContentConfig(temperature=0.0),
    )

    text = response.text.strip()
    reason_match = re.search(r"REASON:\s*(.+)", text)
    defects_match = re.search(r"DEFECTS:\s*(.+)", text)
    sev_match = re.search(r"SEVERITY:\s*(none|minor|major)", text, re.IGNORECASE)

    defects = defects_match.group(1).strip() if defects_match else ""
    has_defect = bool(defects) and defects.lower() not in ("none", "none.", "keine", "keine.")

    severity = sev_match.group(1).lower() if sev_match else None
    # Konsistenz erzwingen: Defekt gelistet, aber Schweregrad none/fehlt → mindestens minor
    if severity is None:
        severity = "minor" if has_defect else "none"
    elif severity == "none" and has_defect:
        severity = "minor"

    # Der Code vergibt die Note anhand des Schweregrads (nicht das Modell selbst).
    # SCORE_BY_SEVERITY + GEMINI_MIN_SCORE steuern zusammen, was noch durchkommt.
    score = SCORE_BY_SEVERITY[severity]

    reason = reason_match.group(1).strip() if reason_match else "no reason given"
    reason = f"[{severity}] {reason}"
    if has_defect:
        reason = f"{reason} [Defekte: {defects}]"
    return score, reason


def check_naturalness_with_retry(audio_path, original_text, gemini_client, single_word_mode=False):
    """check_naturalness mit Throttling + automatischem Retry bei 429/503."""
    global _gemini_last_call, _gemini_disabled_for_run
    if _gemini_disabled_for_run:
        return None

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        elapsed = time.time() - _gemini_last_call
        if elapsed < GEMINI_MIN_INTERVAL_SEC:
            time.sleep(GEMINI_MIN_INTERVAL_SEC - elapsed)
        try:
            result = check_naturalness(audio_path, original_text, gemini_client, single_word_mode)
            _gemini_last_call = time.time()
            return result
        except Exception as e:
            _gemini_last_call = time.time()
            err = str(e)
            if "PerDay" in err or "GenerateRequestsPerDay" in err:
                print("      ⚠ Gemini Tages-Limit erschöpft — Naturalness-Check wird für den Rest des Laufs übersprungen.")
                _gemini_disabled_for_run = True
                return None
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                wait = min(_parse_retry_delay(err, 30), 70)
                print(f"      ⏳ Gemini Rate-Limit — warte {wait}s, dann erneut ({attempt}/{GEMINI_MAX_RETRIES})...")
                time.sleep(wait)
                continue
            if "503" in err or "UNAVAILABLE" in err:
                wait = min(5 * (2 ** (attempt - 1)), 60)
                print(f"      ⏳ Gemini überlastet (503) — warte {wait}s, dann erneut ({attempt}/{GEMINI_MAX_RETRIES})...")
                time.sleep(wait)
                continue
            print(f"      ⚠ Gemini-Fehler (übersprungen): {err[:160]}")
            return None

    print(f"      ⚠ Gemini nach {GEMINI_MAX_RETRIES} Versuchen aufgegeben (übersprungen).")
    return None


def quality_check(audio_path, original_text, whisper_model, gemini_client=None,
                  skip_silence=False, single_word_mode=False):
    """Returns (passed, reason, wer, silence_ms, gemini_score, transcript)."""
    # 0. Truncation-Check A: Plausibilität der Dauer.
    # Ist das Audio deutlich kürzer als die Sprechzeit, die der Text braucht,
    # wurde die Generierung vermutlich mittendrin abgebrochen.
    try:
        duration_ms = len(AudioSegment.from_file(audio_path))
        expected_ms = (len(original_text) / EXPECTED_CHARS_PER_SEC) * 1000
        if not single_word_mode and expected_ms > 800 and duration_ms < expected_ms * MIN_DURATION_RATIO:
            return (False,
                    f"Audio vermutlich abgebrochen ({duration_ms/1000:.1f}s, erwartet ~{expected_ms/1000:.1f}s)",
                    1.0, 0, 0, "")
    except Exception as e:
        print(f"      ⚠ Dauer-Check fehlgeschlagen: {e}")

    # 1. Transcription check
    try:
        transcript = transcribe_audio(audio_path, whisper_model)
        wer = word_error_rate(original_text, transcript)
    except Exception as e:
        return False, f"transcription failed: {e}", 1.0, 0, 0, ""

    transcript = transcript.strip()
    if wer > WER_THRESHOLD:
        return False, f"WER too high ({wer:.0%})", wer, 0, 0, transcript

    # 1b. Truncation-Check B: Endet das Audio mit dem richtigen Text?
    # Die WER-Schwelle lässt bei langen Sätzen ein fehlendes Wort am Ende durch —
    # genau das passiert aber bei abgeschnittenen Enden. Deshalb explizit prüfen,
    # ob die letzten Wörter des Originals im Transkript vorkommen.
    ref_words = normalize_for_compare(original_text)
    hyp_words = normalize_for_compare(transcript)
    if len(ref_words) >= 3:
        tail = ref_words[-2:]
        hyp_tail = hyp_words[-4:] if len(hyp_words) >= 4 else hyp_words
        missing = [w for w in tail if w not in hyp_tail]
        if missing:
            return (False,
                    f"Ende fehlt vermutlich (letzte Wörter '{' '.join(tail)}' nicht am Transkript-Ende: '...{' '.join(hyp_words[-4:])}')",
                    wer, 0, 0, transcript)

    # 2. Silence check
    if skip_silence:
        longest_silence = 0
    else:
        try:
            longest_silence = check_silences(audio_path)
        except Exception as e:
            print(f"      ⚠ silence check failed: {e}")
            longest_silence = 0
        if longest_silence > MAX_SILENCE_MS:
            return False, f"long silence ({longest_silence}ms)", wer, longest_silence, 0, transcript

    # 3. Gemini naturalness check
    gemini_score = 0
    if ENABLE_GEMINI_CHECK and gemini_client is not None and not _gemini_disabled_for_run:
        result = check_naturalness_with_retry(audio_path, original_text, gemini_client, single_word_mode)
        if result is not None:
            gemini_score, reason = result
            if gemini_score < GEMINI_MIN_SCORE:
                return False, f"Gemini: {gemini_score}/10 — {reason}", wer, longest_silence, gemini_score, transcript

    return True, "ok", wer, longest_silence, gemini_score, transcript


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
                   {"status": "review needed", "reason": "kein Text", "generated_at": now})
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

    best_attempt = None  # (path, wer, silence_ms, gemini_score, reason, score)
    attempt_files = []   # alle Zwischen-Dateien dieses Items (für Aufräumen)

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
        passed, reason, wer, silence_ms, gemini_score, transcript = quality_check(
            work_path, text, whisper_model, gemini_client,
            skip_silence=single_word_mode, single_word_mode=single_word_mode
        )

        if passed:
            shutil.move(work_path, final_path)
            gemini_note = f", Gemini={gemini_score}/10" if ENABLE_GEMINI_CHECK and gemini_score else ""
            print(f"   ✅ Passed QC (WER={wer:.0%}, silence={silence_ms}ms{gemini_note}) → {filename}")
            write_back(ws, header_map, row_num, {
                "status": "passed", "filename": filename,
                "reason": "", "generated_at": now,
            })
            row.update({"status": "passed", "filename": filename, "reason": "",
                        "generated_at": now, "_transcript": transcript,
                        "_wer": wer, "_gemini": gemini_score})
            _record_review(row)
            counts["passed"] += 1
            _cleanup(attempt_files)
            return

        print(f"   ✗ Failed: {reason}")
        naturalness_penalty = (10 - gemini_score) / 10 if gemini_score else 0
        score = wer + (silence_ms / 10000) + naturalness_penalty
        if best_attempt is None or score < best_attempt[6]:
            best_attempt = (work_path, wer, silence_ms, gemini_score, reason, transcript, score)

        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Alle Versuche fehlgeschlagen → bester Versuch behalten, Status "review needed"
    if best_attempt:
        shutil.move(best_attempt[0], final_path)
        _, b_wer, b_sil, b_gem, b_reason, b_transcript, _ = best_attempt
        print(f"   ⚠ Alle Versuche fehlgeschlagen → als 'review needed' markiert (WER={b_wer:.0%}, Gemini={b_gem}/10)")
        write_back(ws, header_map, row_num, {
            "status": "review needed", "filename": filename,
            "reason": b_reason, "generated_at": now,
        })
        row.update({"status": "review needed", "filename": filename, "reason": b_reason,
                    "generated_at": now, "_transcript": b_transcript,
                    "_wer": b_wer, "_gemini": b_gem})
        _record_review(row)
        counts["review"] += 1
    else:
        # gar kein Audio erzeugt
        print(f"   ✗ Keine Audio-Generierung möglich → review needed")
        write_back(ws, header_map, row_num, {
            "status": "review needed", "filename": filename,
            "reason": "ElevenLabs-Generierung fehlgeschlagen", "generated_at": now,
        })
        row.update({"status": "review needed", "filename": filename,
                    "reason": "ElevenLabs-Generierung fehlgeschlagen", "generated_at": now})
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
        "model": ELEVENLABS_MODEL,
    })


def generate_review_html(output_file=REVIEW_HTML):
    """
    Erstellt die Review-Seite aus den AKKUMULIERTEN Daten (review_data.json).
    Neue Audios kommen hinzu, bestehende bleiben — bis zum Reset über die GUI.
    """
    data = _load_review_data()
    # Neueste zuerst
    entries = sorted(data.values(), key=lambda e: e.get("generated_at", ""), reverse=True)

    cards = []
    n_passed = n_review = 0

    for e in entries:
        filename = e.get("filename", "")
        audio_src = None
        # Zuerst der beim Generieren gespeicherte absolute Pfad (robust bei externem Ordner),
        # sonst im aktuellen OUTPUT_DIR nachsehen.
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
            badge_class, badge_label = "passed", "✓ passed"
        elif status == "review needed":
            n_review += 1
            badge_class, badge_label = "review", "⚠ review needed"
        else:
            badge_class, badge_label = "other", _esc(status or "unbekannt")

        label = e.get("id", "") or filename or f"Zeile {e.get('row', '?')}"
        player = (f'<audio controls preload="none" src="{_esc(audio_src)}"></audio>'
                  if audio_src else '<p class="missing">Keine Audio-Datei gefunden</p>')

        details = [f'<p class="text"><b>Text:</b> {_esc(e.get("text", ""))}</p>']
        if e.get("transcript"):
            details.append(f'<p><b>Whisper:</b> {_esc(e["transcript"])}</p>')
        metrics = []
        if e.get("wer") is not None:
            metrics.append(f"WER {e['wer']:.0%}")
        if e.get("gemini"):
            metrics.append(f"Gemini {e['gemini']}/10")
        if e.get("model"):
            metrics.append(_esc(e["model"]))
        if metrics:
            details.append(f'<p class="meta">{" · ".join(metrics)}</p>')
        if e.get("reason"):
            details.append(f'<p class="reason"><b>Grund:</b> {_esc(e["reason"])}</p>')
        meta_line = " · ".join(x for x in (filename, e.get("mode", ""), e.get("generated_at", "")) if x)
        if meta_line:
            details.append(f'<p class="meta">{_esc(meta_line)}</p>')

        cards.append(f"""
    <div class="card {badge_class}" data-status="{badge_class}">
      <div class="card-head">
        <span class="badge {badge_class}">{badge_label}</span>
        <span class="label">{_esc(label)}</span>
      </div>
      {player}
      {''.join(details)}
    </div>""")

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    body = ''.join(cards) if cards else '<p class="empty">Noch keine Audios generiert.</p>'
    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>TTS Review — {now}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Arial, sans-serif; background: #f4f5f7;
         margin: 0; padding: 24px; color: #222; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 4px; }}
  .sub {{ color: #666; margin-bottom: 18px; }}
  .filters {{ margin-bottom: 18px; }}
  .filters button {{ border: 1px solid #ccc; background: #fff; padding: 6px 14px;
      border-radius: 16px; margin-right: 8px; cursor: pointer; font-size: 0.9rem; }}
  .filters button.active {{ background: #1f4e5f; color: #fff; border-color: #1f4e5f; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 14px 16px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); border-left: 5px solid #bbb; }}
  .card.passed {{ border-left-color: #2e9e5b; }}
  .card.review {{ border-left-color: #e07b00; }}
  .card-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .badge {{ font-size: 0.75rem; font-weight: 600; padding: 2px 10px; border-radius: 10px; color: #fff; }}
  .badge.passed {{ background: #2e9e5b; }}
  .badge.review {{ background: #e07b00; }}
  .badge.other {{ background: #888; }}
  .label {{ font-weight: 600; }}
  audio {{ width: 100%; margin: 6px 0; }}
  p {{ margin: 4px 0; font-size: 0.9rem; }}
  .text {{ font-size: 0.95rem; }}
  .reason {{ color: #b34700; }}
  .meta {{ color: #888; font-size: 0.8rem; }}
  .missing {{ color: #c00; font-style: italic; }}
  .empty {{ color: #999; font-size: 1rem; }}
  .hint {{ margin-top: 22px; color: #999; font-size: 0.8rem; }}
</style>
</head>
<body>
<h1>TTS Review</h1>
<div class="sub">Stand: {now} — {n_passed} passed · {n_review} review needed</div>
<div class="filters">
  <button class="active" onclick="filterCards('all', this)">Alle ({n_passed + n_review})</button>
  <button onclick="filterCards('passed', this)">Passed ({n_passed})</button>
  <button onclick="filterCards('review', this)">Review needed ({n_review})</button>
</div>
<div class="grid">
{body}
</div>
<p class="hint">Hinweis: Falls .opus-Dateien in Safari nicht abspielbar sind, die Datei in Chrome oder Firefox öffnen.</p>
<script>
function filterCards(status, btn) {{
  document.querySelectorAll('.filters button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(c => {{
    c.style.display = (status === 'all' || c.dataset.status === status) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html)
    return output_file, n_passed + n_review


def reset_review(output_file=REVIEW_HTML):
    """Setzt die Review-Seite zurück: Daten löschen, leere Seite schreiben."""
    if os.path.exists(REVIEW_DATA_FILE):
        os.remove(REVIEW_DATA_FILE)
    generate_review_html(output_file)


def main():
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
        print("✨ Initializing Gemini client...")
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    print()

    print("📄 Öffne Google Sheet...")
    ws, header_map, records = open_sheet()

    # Pflichtspalten prüfen
    for col in ("id", "text", "status"):
        if col not in header_map:
            print(f"   ⚠ Warnung: Spalte '{col}' fehlt in der Kopfzeile.")

    to_process = [r for r in records if should_process(r.get("status", ""))]
    print(f"   {len(records)} Zeilen gesamt, {len(to_process)} zu verarbeiten.\n")

    usage_start = get_elevenlabs_character_count()
    counts = {"passed": 0, "review": 0, "failed": 0, "skipped": 0}

    for row in to_process:
        process_row(row, ws, header_map, whisper_model, gemini_client, counts)
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