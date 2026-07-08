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

### Projekt holen — zwei Wege

**A) Per Git (empfohlen):**
```
git clone https://github.com/MoritzAtCaretable/TTS-Content-Automation.git tts-studio
cd tts-studio
```

**B) Per ZIP:** Auf GitHub „Code → Download ZIP", entpacken, in den Ordner wechseln.
Der Installer richtet die Git-Verbindung nachträglich selbst ein, damit der
Update-Button trotzdem funktioniert.

### macOS

Im Terminal im Projektordner:
```
chmod +x install.sh
./install.sh
```
Installiert Homebrew, Python, ffmpeg und Git (falls nötig), legt die venv an,
installiert alle Pakete und baut `TTS Studio.app`. Bei einem ZIP-Download stellt
es zusätzlich die Git-Verbindung her (ohne vorhandene Dateien zu löschen).

### Windows

Doppelklick auf **`install.bat`**.
Fehlen ffmpeg oder Git, installiert das Skript sie per winget — danach das Fenster
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
> ins Git-Repo. Die `.gitignore` schließt sie bereits aus. Der Installer lässt
> beide Dateien auch beim ZIP→Git-Schritt unangetastet.

---

## Starten

- **macOS:** `TTS Studio.app` (im Projektordner) doppelklicken. Ins Dock ziehen für
  schnellen Zugriff. Beim ersten Start ggf. Rechtsklick → „Öffnen" (Gatekeeper).
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

In der App auf **⬇ Update** klicken — das holt die neueste Version. Danach App
neu starten. Funktioniert bei beiden Installationswegen (Git wie ZIP), da der
Installer immer eine Git-Verbindung herstellt.

Alternativ im Terminal im Projektordner:
```
git pull
```

Wenn ein Update **neue Abhängigkeiten** mitbringt, einmal den Installer erneut
ausführen (`./install.sh` bzw. `install.bat`).

---

## Problembehebung

| Symptom | Lösung |
|---|---|
| `install.sh` startet nicht per Doppelklick | Im Terminal ausführen: `chmod +x install.sh && ./install.sh` |
| App startet nicht (Mac) | `python3 fix_app.py` erneut ausführen; ggf. `killall Dock` |
| „ffmpeg nicht gefunden" | Installer erneut ausführen; nach Installation Terminal neu öffnen |
| `.env` wird nicht gefunden | Muss exakt `.env` heißen und im Projektordner liegen |
| Opus spielt in Safari nicht | Review-Seite in Chrome/Firefox öffnen |
| Gemini „API key not valid" | Key prüfen; „Generative Language API" im Google-Cloud-Projekt aktivieren |
| `WorksheetNotFound` | `SHEET_NAME` in der `.env` an den echten Tab-Namen anpassen |
| Update-Button meldet „kein Git-Checkout" | `install.sh`/`install.bat` erneut ausführen — stellt die Git-Verbindung her |
| Projektordner verschoben, App startet nicht | `python3 fix_app.py` im neuen Ordner erneut ausführen |

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