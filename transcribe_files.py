#!/usr/bin/env python3
"""
transcribe_files.py — Transkribiert Audiodateien mit Whisper.

Läuft als Subprozess der Oberfläche (wie die Generierung), damit das Fenster
flüssig bleibt, der Fortschritt mitläuft und „Abbrechen“ funktioniert.

Eingabe:  Umgebungsvariable TTS_TRANSCRIBE_FILES = JSON-Liste von Dateipfaden
Ausgabe:  Zeilen auf stdout
          @@PROGRESS i/n        Fortschritt (die Oberfläche filtert das heraus)
          @@RESULT {json}       ein Ergebnis je Datei
          sonstige Zeilen       Protokolltext für den Nutzer

Zu jedem Wort wird die Länge der Sprechpause davor mitgeliefert. Damit kann die
Oberfläche den Text an Pausen in einzelne Sprechzeilen schneiden — und zwar
sofort beim Verschieben des Reglers, ohne neu zu transkribieren.

WARUM DIE PAUSEN AUS DEM AUDIO KOMMEN
Whisper liefert zwar Wort-Zeitstempel, aber die Endzeiten laufen in die Stille
hinein: ein 0,70 s langes „Banane“ endet laut Whisper erst bei 1,26 s. Die
Lücke zwischen zwei Wörtern wird dadurch deutlich zu klein gemessen. Deshalb
werden die Sprechbereiche zusätzlich direkt im Audio bestimmt (pydub) und die
Pause aus dem Abstand dieser Bereiche berechnet.
"""

from __future__ import annotations

import os
import re
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import whisper                                        # noqa: E402
from pydub import AudioSegment                        # noqa: E402
from pydub.silence import detect_nonsilent            # noqa: E402
import sheets_to_elevenlabs_qc_local as pipeline      # noqa: E402

# Formate, die ffmpeg (und damit Whisper) zuverlässig öffnet
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".opus", ".ogg", ".oga",
                  ".flac", ".aac", ".mp4", ".webm", ".mov", ".aiff", ".aif"}

# Für die Transkription bewusst ein anderes Modell als in der
# Qualitätskontrolle: large-v3 setzt Satzzeichen und Großschreibung deutlich
# verlässlicher als medium. Es ist rund 1,7x langsamer, was hier nicht stört —
# transkribiert wird gelegentlich, die QC dagegen bei jedem generierten Audio.
# Über TTS_WHISPER_MODEL überschreibbar.
DEFAULT_MODEL = "large-v3"

# Kürzeste Stille, die überhaupt als Pause in Frage kommt. Bewusst klein —
# geschnitten wird erst nach der Schwelle, die in der Oberfläche eingestellt
# ist; hier sollen nur alle Kandidaten erfasst werden.
MIN_SILENCE_MS = 120
# Toleranz beim Zuordnen eines Wortes zu einem Sprechbereich
MATCH_TOLERANCE_S = 0.20


def id_from_filename(path: str) -> str:
    """Macht aus einem Dateinamen eine brauchbare, stabile ID.

    "Wäsche waschen 01.wav" → "Waesche_waschen_01". Umlaute werden
    transliteriert, Trenner zu "_" vereinheitlicht, alles andere entfällt —
    so passt die ID zum Schema der übrigen Einträge im Sheet.
    """
    stem = Path(path).stem
    s = (stem.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
             .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
             .replace("ß", "ss"))
    s = re.sub(r"[\s.\-]+", "_", s)       # Leerzeichen/Punkte/Striche → _
    s = re.sub(r"[^0-9A-Za-z_]", "", s)   # Rest verwerfen
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "audio"


def speech_regions(path: str):
    """Sprechbereiche (Start, Ende) in Sekunden — alles dazwischen ist Pause."""
    try:
        audio = AudioSegment.from_file(path)
    except Exception:
        return []
    if len(audio) == 0:
        return []
    # Relative Schwelle: leise Aufnahmen sollen nicht komplett als Stille gelten.
    pegel = audio.dBFS
    schwelle = -45.0 if pegel == float("-inf") else min(-45.0, pegel - 25.0)
    try:
        roh = detect_nonsilent(audio, min_silence_len=MIN_SILENCE_MS,
                               silence_thresh=schwelle, seek_step=10)
    except Exception:
        return []
    return [(a / 1000.0, b / 1000.0) for a, b in roh]


def region_index(t: float, regions) -> int:
    """Index des Sprechbereichs, in den der Zeitpunkt fällt (-1 = keiner)."""
    for i, (a, b) in enumerate(regions):
        if a - MATCH_TOLERANCE_S <= t <= b + MATCH_TOLERANCE_S:
            return i
    return -1


def words_with_pauses(result, regions):
    """Wörter mit der Pausenlänge davor (Sekunden).

    Liegt ein Wortpaar in zwei verschiedenen Sprechbereichen, ist die Pause der
    echte stille Abstand dieser Bereiche. Sonst dient die Wortlücke als Rückfall
    (z.B. wenn die Stilleerkennung nichts gefunden hat).
    """
    roh = [w for s in result.get("segments", []) for w in s.get("words", [])]
    out = []
    vor_ende = None          # Ende des vorigen Wortes laut Whisper
    vor_region = None        # Sprechbereich des vorigen Wortes
    for w in roh:
        text = str(w.get("word", ""))
        if not text.strip():
            continue
        start = float(w.get("start", 0.0))
        ende = float(w.get("end", start))
        r = region_index(start, regions)
        if vor_ende is None:
            pause = 0.0
        elif r >= 0 and vor_region is not None and vor_region >= 0 and r != vor_region:
            pause = max(0.0, regions[r][0] - regions[vor_region][1])
        else:
            pause = max(0.0, start - vor_ende)
        out.append({"w": text, "p": round(pause, 2)})
        vor_ende, vor_region = ende, r
    return out


def main() -> int:
    try:
        dateien = json.loads(os.environ.get("TTS_TRANSCRIBE_FILES", "[]"))
    except json.JSONDecodeError:
        print("❌ Dateiliste konnte nicht gelesen werden.")
        return 1
    dateien = [str(d) for d in dateien if str(d).strip()]
    if not dateien:
        print("❌ Keine Dateien übergeben.")
        return 1

    if pipeline.FFMPEG_BIN is None:
        print("❌ ffmpeg nicht gefunden — Whisper braucht es zum Öffnen der Audiodateien.")
        print("   macOS: brew install ffmpeg")
        return 1

    modell_name = os.environ.get("TTS_WHISPER_MODEL") or DEFAULT_MODEL
    print(f"🧠 Lade Whisper-Modell „{modell_name}“… (beim ersten Mal wird es heruntergeladen)")
    modell = whisper.load_model(modell_name)
    print(f"   Modell geladen. {len(dateien)} Datei(en) zu transkribieren.\n")

    gesamt = len(dateien)
    print(f"@@PROGRESS 0/{gesamt}", flush=True)
    fehler = 0

    for i, pfad in enumerate(dateien, 1):
        name = Path(pfad).name
        try:
            if not os.path.exists(pfad):
                raise FileNotFoundError("Datei nicht gefunden")
            # condition_on_previous_text=False: Whisper reicht sonst den
            # bisherigen Text als Kontext ins nächste 30-Sekunden-Fenster.
            # Bei langen oder wiederkehrenden Aufnahmen (Refrain!) schaukeln
            # sich Fehler darüber auf — Satzzeichen und Großschreibung brechen
            # weg, Wörter verfälschen sich fortlaufend. Ohne die Kopplung wird
            # jedes Fenster für sich ausgewertet.
            result = modell.transcribe(pfad, language=pipeline.WHISPER_LANGUAGE,
                                       fp16=False, word_timestamps=True,
                                       condition_on_previous_text=False)
            text = str(result.get("text", "")).strip()
            if not text:
                raise ValueError("Whisper hat keinen Text erkannt")
            woerter = words_with_pauses(result, speech_regions(pfad))
            pausen = sorted((w["p"] for w in woerter[1:]), reverse=True)[:3]
            print(f"[{i}/{gesamt}] {name}")
            print(f"            „{text}“")
            if pausen:
                print("            längste Pausen: "
                      + ", ".join(f"{p:.2f}s" for p in pausen))
            print("@@RESULT " + json.dumps(
                {"file": name, "path": pfad, "id": id_from_filename(pfad),
                 "text": text, "words": woerter},
                ensure_ascii=False), flush=True)
        except Exception as e:
            fehler += 1
            print(f"[{i}/{gesamt}] {name} — ⚠ {e}")
        print(f"@@PROGRESS {i}/{gesamt}", flush=True)

    erfolg = gesamt - fehler
    print(f"\n{'=' * 60}")
    print(f"✅ {erfolg} von {gesamt} Datei(en) transkribiert"
          + (f", {fehler} fehlgeschlagen" if fehler else ""))
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
