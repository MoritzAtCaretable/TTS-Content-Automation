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
- **GitHub-Zugriff** auf dieses (private) Repo

> Python, Git und ffmpeg müssen NICHT vorab installiert sein — die Installer
> kümmern sich bei Bedarf selbst darum.

---

## Installation

### macOS — Empfohlen: ein Doppelklick

1. `TTS_Setup.command` besorgen (z.B. auf den Schreibtisch legen).
2. **Doppelklick** darauf.
   - Beim allerersten Start blockiert macOS die Datei evtl. → **Rechtsklick → „Öffnen"**,
     im Dialog nochmal **„Öffnen"**. Danach merkt sich der Mac die Freigabe.
3. Der Rest läuft automatisch: installiert Homebrew, Git, Python, ffmpeg, klont das
   Projekt nach `~/TTS-Studio`, richtet alles ein und baut `TTS Studio.app`.
   (Beim ersten Mal 10–20 Min wegen der Downloads — einfach warten.)

> `TTS_Setup.command` enthält oben die Repo-URL und den Zielordner — dort bei
> Bedarf anpassen.

### macOS — Alternative: manuell im Terminal

```
git clone https://github.com/MoritzAtCaretable/TTS-Content-Automation.git tts-studio
cd tts-studio
chmod +x install.sh
./install.sh
```

### Windows

1. Projekt holen: `git clone https://github.com/MoritzAtCaretable/TTS-Content-Automation.git`
   (falls Git fehlt, von https://git-scm.com installieren) — oder das Repo als ZIP laden.
2. **Doppelklick auf `install.bat`.**
   - Fehlt ffmpeg oder Git, installiert das Skript sie per winget — danach das Fenster
     schließen, ein **neues** öffnen und `install.bat` erneut ausführen (damit sie im PATH landen).

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

- **macOS:** `TTS Studio.app` (liegt im Projektordner, z.B. `~/TTS-Studio`) doppelklicken.
  Ins Dock ziehen für schnellen Zugriff. Beim ersten Start ggf. Rechtsklick → „Öffnen".
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
bzw. über `TTS_Setup.command` eingerichtet wurde — nicht bei ZIP-Download.)

Alternativ im Terminal im Projektordner:
```
git pull
```

Wenn ein Update **neue Abhängigkeiten** mitbringt, einmal den Installer erneut
ausführen (`./install.sh` bzw. `install.bat`).

> **ZIP-Download:** funktioniert zum Ausprobieren, hat aber KEINE Update-Funktion.
> Für Updates per Knopfdruck das Projekt per Git/`TTS_Setup.command` einrichten.

---

## Problembehebung

| Symptom | Lösung |
|---|---|
| `.command`/`.sh` startet nicht per Doppelklick | Rechtsklick → „Öffnen"; oder im Terminal `chmod +x <datei>` |
| App startet nicht (Mac) | `python3 fix_app.py` erneut ausführen; ggf. `killall Dock` |
| „ffmpeg nicht gefunden" | Installer erneut ausführen; nach Installation Terminal neu öffnen |
| `.env` wird nicht gefunden | Muss exakt `.env` heißen und im Projektordner liegen |
| Opus spielt in Safari nicht | Review-Seite in Chrome/Firefox öffnen |
| Gemini „API key not valid" | Key prüfen; „Generative Language API" im Google-Cloud-Projekt aktivieren |
| `WorksheetNotFound` | `SHEET_NAME` in der `.env` an den echten Tab-Namen anpassen |
| Projektordner verschoben, App startet nicht | `python3 fix_app.py` im neuen Ordner erneut ausführen |

---

## Plattform-Dateien (Überblick)

| Datei | Zweck | System |
|---|---|---|
| `sheets_to_elevenlabs_qc_local.py` | Pipeline (Kern) | beide |
| `tts_gui.py` | Grafische Oberfläche | beide |
| `TTS_Setup.command` | Komplett-Installer (klont + richtet ein) | macOS |
| `install.sh` / `fix_app.py` | Einrichtung / App-Bau | macOS |
| `install.bat` / `TTS_Studio.bat` | Einrichtung / Starter | Windows |
| `requirements.txt` | Python-Abhängigkeiten | beide |
| `TTS_Vorlage.xlsx` | Sheet-Vorlage mit Dropdowns | beide |