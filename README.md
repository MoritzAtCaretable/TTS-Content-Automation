# TTS Studio

Automatische Sprachausgabe (TTS) aus einem Google Sheet über ElevenLabs — mit
automatischer Qualitätskontrolle (Whisper-Transkription, Pausen- und Abschnitt-Erkennung,
Gemini-Naturalness-Check), Postprocessing (Normalisierung, Opus-Export) und einer
Review-Oberfläche zum Anhören.

---

## Voraussetzungen

- **Google Sheet** mit den Spalten `id, text, filename, mode, status, reason, generated_at`
  (Vorlage: `TTS_Vorlage.xlsx`)
- **Service-Account-Datei** (`service_account.json`) mit **Bearbeiter**-Zugriff auf das Sheet
- **API-Keys** für ElevenLabs und Google Gemini

---

## Installation

### Einmalig: Projekt holen

Wer mit Git vertraut ist (empfohlen, ermöglicht Updates per Knopfdruck):
```
git clone <REPO-URL> tts-studio
cd tts-studio
```
Alternativ das Repo als ZIP herunterladen und entpacken.

### macOS

Im Terminal im Projektordner:
```
chmod +x install.sh
./install.sh
```
Das installiert Homebrew (falls nötig), Python, ffmpeg, alle Pakete und baut
`TTS Studio.app`.

### Windows

Doppelklick auf **`install.bat`**.
Falls ffmpeg fehlt, installiert das Skript es per winget — danach das Fenster
schließen, ein neues öffnen und `install.bat` noch einmal ausführen.

---

## Konfiguration (beide Systeme)

1. **`.env` ausfüllen** (wurde beim Install aus `.env.example` erstellt):
   ```
   ELEVENLABS_API_KEY=sk_...
   ELEVENLABS_VOICE_ID=...
   GEMINI_API_KEY=...
   SPREADSHEET_ID=...
   SHEET_NAME=Tabellenblatt1
   ```
2. **`service_account.json`** in den Projektordner legen.
3. Das Google Sheet mit der Service-Account-E-Mail als **Bearbeiter** teilen.

> ⚠️ `.env` und `service_account.json` enthalten Geheimnisse und dürfen **nie**
> ins Git-Repo. Die `.gitignore` schließt sie bereits aus.

---

## Starten

- **macOS:** `TTS Studio.app` doppelklicken (ins Dock ziehen für schnellen Zugriff).
  Beim ersten Start ggf. Rechtsklick → „Öffnen" (Gatekeeper-Hinweis bestätigen).
- **Windows:** `TTS_Studio.bat` doppelklicken. Für eine Desktop-Verknüpfung:
  Rechtsklick → „Senden an" → „Desktop (Verknüpfung erstellen)".

In der App: Modell wählen, Zielordner wählen, **Generierung starten**.
Über die Buttons kommst du zum Google Sheet und zur Review-Seite.

---

## Bedienung des Sheets

- Zeilen mit Status **`todo`** oder **`regenerate`** werden verarbeitet.
- Modus **`Einzelwort`** = einzelnes Wort (mit Trimming), **`Normal`** = Satz/Phrase.
- Nach dem Lauf schreibt das Skript **`passed`** oder **`review needed`** samt Grund
  und Zeitstempel zurück.
- Alle Audios landen im gewählten Zielordner; den Status liest man im Sheet
  oder in der Review-Seite (mit Playern und Filter).

---

## Updates

**Mit Git (empfohlen):** In der App auf **⬇ Update** klicken — das holt die neueste
Version. Danach App neu starten. (Funktioniert nur, wenn das Projekt per `git clone`
eingerichtet wurde.)

Alternativ im Terminal:
```
git pull
```

Wenn ein Update **neue Abhängigkeiten** mitbringt, einmal den Installer erneut
ausführen (`./install.sh` bzw. `install.bat`).

---

## Problembehebung

| Symptom | Lösung |
|---|---|
| App startet nicht (Mac) | `python3 fix_app.py` erneut ausführen; ggf. `killall Dock` |
| „ffmpeg nicht gefunden" | Installer erneut ausführen; nach ffmpeg-Install Terminal neu öffnen |
| `.env` wird nicht gefunden | Muss exakt `.env` heißen und im Projektordner liegen |
| Opus spielt in Safari nicht | Review-Seite in Chrome/Firefox öffnen |
| Gemini „API key not valid" | Key prüfen; „Generative Language API" im Google-Cloud-Projekt aktivieren |
| `WorksheetNotFound` | `SHEET_NAME` in der `.env` an den echten Tab-Namen anpassen |

---

## Plattform-Dateien (Überblick)

| Datei | Zweck | System |
|---|---|---|
| `sheets_to_elevenlabs_qc_local.py` | Pipeline (Kern) | beide |
| `tts_gui.py` | Grafische Oberfläche | beide |
| `install.sh` / `fix_app.py` | Einrichtung / App-Bau | macOS |
| `install.bat` / `TTS_Studio.bat` | Einrichtung / Starter | Windows |
| `requirements.txt` | Python-Abhängigkeiten | beide |
| `TTS_Vorlage.xlsx` | Sheet-Vorlage mit Dropdowns | beide |
