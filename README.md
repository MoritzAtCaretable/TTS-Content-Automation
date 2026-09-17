# TTS Studio

Automatische Sprachausgabe (TTS) aus einem Google Sheet über ElevenLabs — mit
automatischer Qualitätskontrolle (Whisper-Transkription, Pausen- und Abschnitt-Erkennung,
Gemini-Naturalness-Check), Postprocessing (Normalisierung, Opus-Export) und einer
Review-Oberfläche zum Anhören.

Die Bedienoberfläche läuft als natives Fenster im Caretable-Design (HTML/CSS in
`webui/`, angezeigt über WKWebView bzw. WebView2) — die Fachlogik liegt
unverändert in `sheets_to_elevenlabs_qc_local.py`.

---

## Voraussetzungen

- **Google Sheet** mit den Spalten `id, text, filename, mode, status, reason, generated_at`
  (Vorlage: `TTS_Vorlage.xlsx`)
- **Service-Account-Datei** (`service_account.json`) mit **Bearbeiter**-Zugriff auf das Sheet
- **API-Keys** für ElevenLabs und Google Gemini
- **GitHub-Zugriff** auf dieses Repo (öffentlich — Lesen/Updaten braucht keinen Token)

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

In der App: Modell wählen, Zielordner wählen, in **Sheet-Zeilen** anhaken was
verarbeitet werden soll, dann **Generierung starten**. Neue Texte lassen sich
unter **Neue Texte** direkt anlegen.
Über die Buttons kommst du zum Google Sheet und zur Review-Seite.

### Sheet-Zeilen (Auswahl in der App)

Die Karte **Sheet-Zeilen** zeigt den Inhalt des Google Sheets direkt in der App —
Zeilennummer, ID, Text, Modus und Status (farbig). Damit muss man das Sheet nicht
mehr öffnen, um zu sehen oder zu bestimmen, was generiert wird.

- **Klick auf eine Zeile** setzt/entfernt den Haken.
- **Nur offene** — Zeilen mit `todo`, `regenerate` oder leerem Status.
- **Nur Review** — alle Zeilen mit `review needed` (die üblichen Nachzügler).
- **Alle** / **Keine** — Komplettauswahl.
- **⟳ Neu laden** holt den aktuellen Sheet-Stand; nach jedem Lauf passiert das automatisch.

Ein laufender Vorgang lässt sich über den Stopp-Knopf rechts neben
**Generierung starten** abbrechen; der Fortschrittsbalken zeigt, wie viele der
ausgewählten Zeilen schon verarbeitet sind.

Wichtig: Eine Auswahl in der Tabelle **gilt genau so** — auch für Zeilen, die schon
`passed` sind. So lässt sich eine fertige Zeile neu generieren, ohne im Sheet den
Status zu ändern. Ist die Tabelle leer (z.B. kein Sheet-Zugriff), verhält sich der
Start wie früher und nimmt alle offenen Zeilen nach Status-Filter.

### Neue Texte (anlegen in der App)

Die Karte **Neue Texte** hängt neue Zeilen unten ans Sheet an — ohne das Sheet zu
öffnen. **Eine Zeile im Eingabefeld = ein Text.**

- **ID-Präfix** (z.B. `Buchstaben`): die Nummern laufen automatisch weiter und
  übernehmen das vorhandene Schema — aus `Buchstaben_161` wird `Buchstaben_162`,
  aus `Request_Artem03` wird `Request_Artem04`. Ein neues Präfix startet bei `_001`.
- **Eigene ID** je Zeile: `meine_id | Mein Text` (dann braucht es keinen Präfix).
  Beides lässt sich mischen.
- **Modus** gilt für alle Zeilen des Einfügevorgangs.
- Neue Zeilen bekommen **Status `todo`**, `filename` bleibt leer (den baut die
  Pipeline beim Generieren). Vor dem Schreiben zeigt ein Dialog, was genau angefügt wird.
- Doppelte IDs werden **vor** dem Schreiben abgelehnt — die ID ist der stabile
  Schlüssel eines Eintrags.
- Danach lädt die Tabelle neu; die neuen Zeilen sind `todo` und damit gleich angehakt.

---

## Bedienung des Sheets

- Zeilen mit Status **`todo`** oder **`regenerate`** werden verarbeitet — sofern in
  der App keine ausdrückliche Zeilen-Auswahl getroffen wurde (siehe oben).
- Modus **`Einzelwort`** = einzelnes Wort (mit Trimming), **`Normal`** = Satz/Phrase.
- Nach dem Lauf schreibt das Skript **`passed`** oder **`review needed`** samt Grund
  und Zeitstempel zurück.
- Alle Audios landen im gewählten Zielordner; den Status liest man im Sheet
  oder in der Review-Seite (mit Playern und Filter).

### Review-Seite

`review.html` wird nach jedem Lauf neu erzeugt und im Browser geöffnet
(Aktionen → **Review-Seite öffnen**). Sie zeigt pro Audio eine Karte mit
Abspieler, Text, Whisper-Transkript und den Kennzahlen (WER, Gemini, Modell) und
lässt sich nach *Alle / Passed / Review needed* filtern. Die Einträge sammeln
sich an, bis man in der App **Review zurücksetzen** drückt.

### Qualitätsprüfung

Eine aktivierte Pflichtprüfung muss erfolgreich abgeschlossen sein, bevor ein Audio
`passed` erhält. Jede Prüfung speichert **bestanden**, **auffällig**, **Prüffehler**
oder **nicht ausgeführt**. Gemini-Ausfälle, ungültige Antworten und unklare Urteile
führen zu `review needed` mit Begründung. Bei technischen Prüffehlern bleibt das
Audio erhalten; für dieses Item werden keine weiteren TTS-Versuche gestartet.
Vorübergehende Gemini-Fehler werden begrenzt auf demselben Audio erneut geprüft.

Der Textvergleich normalisiert Großschreibung, Satzzeichen, häufige Abkürzungen
(`z. B.`, `u. a.`, `bzw.`, `usw.`), deutsche Kardinalzahlen bis 999999 und häufige
Maßeinheiten im Zahlenkontext. Zum Beispiel gelten `3 cm` und `drei Zentimetern`
als gleich. Dezimalwerte und Vorzeichen bleiben unterscheidbar. Für Datumsangaben,
Ordnungszahlen, Brüche oder mehrdeutige Abkürzungen gibt es keine umfassende
Sprachnormalisierung; solche Abweichungen benötigen ggf. menschliche Prüfung.
Nach der Normalisierung verhindern **alle verbleibenden Wortabweichungen** die
automatische Freigabe, auch bei niedriger WER. Zahlen, Einheiten und Negationen
werden im Befund als kritisch hervorgehoben. Ein ASR-Unterschied ist dabei ein
Prüfhinweis und noch kein sicherer Beweis für einen Aussprachefehler.

Natürlichkeit wird standardmäßig mit **`gemini-3.8-flash`** geprüft. Über
`GEMINI_MODEL` in `.env` lässt sich ein anderes verfügbares Audiomodell wählen.
Das Prompt unterscheidet Inhalt, Aussprache, abgeschnittene Laute, Tempo, Prosodie,
Artefakte und Hörbarkeit. Normale Sprechvarianten und ruhige Vortragsweise sind
keine Fehler. Unsichere Urteile führen zur manuellen Prüfung. Gemini liefert
validiertes JSON; fehlende oder widersprüchliche Angaben gelten als Prüffehler.

Die gespeicherte Note ist weiterhin eine feste Zuordnung (`none` = 9, `minor` = 7,
`major` = 3), keine kalibrierte Qualitätsmessung. `GEMINI_MIN_SCORE = 7` akzeptiert
kleinere Mängel; 8 lehnt auch diese ab. Bestätigte schwere Mängel werden immer
abgelehnt. **QC-Befunde** in der Review-Seite zeigen Gründe und Defekte auch bei
bestandenen Aufnahmen sowie Prüfmodell und Promptversion. Alte Review-Einträge
bleiben lesbar und werden nicht nachträglich als neu geprüft ausgegeben.

Optional können im Sheet die Spalten `qc_state` und `qc_details` ergänzt werden.
Ohne diese Spalten werden die Details lokal in `review_data.json` gespeichert;
die bisherigen Statuswerte und Pflichtspalten bleiben kompatibel.

Die Regressionstests laufen ohne API-Aufrufe oder Modelldownloads:

```
venv/bin/python -m unittest discover -s tests -v
```

Unter Windows entsprechend `venv\Scripts\python -m unittest discover -s tests -v`.
Ein Qualitätsvorteil von Gemini 3.8 gegenüber früheren Modellen muss anhand
bewerteter Hörbeispiele geprüft werden; die Modellumstellung allein garantiert ihn nicht.

### Schnitt und sicherer Export

Einzelwörter werden mit dem ElevenLabs-Endpunkt `with-timestamps` erzeugt. Die
Zeichen-Zeitstempel müssen Einleitung und Zieltext eindeutig abbilden. Geschnitten
wird mit Sicherheitsabstand an ruhigen Signalgrenzen. Leise Auslaute können die
Endgrenze nach außen verschieben. Fehlen passende Zeitstempel oder sichere Grenzen,
bleibt das Original für Review erhalten. Eleven v3 erhält keine SSML-Pausentags.

Der Export erzeugt zunächst eine temporäre Datei. Bei aktiviertem Postprocessing
werden Randstille und Schutzpausen angepasst, Fades nur innerhalb dieser Pausen
angewandt und die Lautheit in zwei Durchläufen normalisiert. Anschließend prüft
ffprobe Codec, Container, Mono und Samplerate; FFmpeg dekodiert die gesamte Datei.
Die fertige Datei muss das True-Peak-Limit und das Lautheitsziel innerhalb von
±2 LU einhalten. Nicht messbare Lautheit und Encoderfehler führen zu Review,
statt still auf einen unnormalisierten Export auszuweichen. Auch bei deaktiviertem
Postprocessing bleibt die Format- und Dekodierprüfung aktiv.

Nur ein vollständig bestandener Versuch ersetzt die Zieldatei atomar. Fehlversuche
überschreiben keine bestehende Freigabe. Die Review-Seite spielt den tatsächlich
zugehörigen Versuch ab; eine alte Datei wird nicht als neue Aufnahme ausgegeben.
Rohdateien, Zwischenstände, Alignment und `attempts.json` bleiben im Unterordner
`.sources` des Zielordners erhalten. Dadurch steigt der Speicherbedarf; der Ordner
kann nach Abschluss der Reviews bewusst archiviert oder gelöscht werden.
Eigene Dateinamen müssen einfache Basenamen sein; Pfade und doppelte Dateinamen
im Sheet werden vor kostenpflichtigen Aufrufen abgewiesen.

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
| `tts_studio_web.py` | Startet das App-Fenster | beide |
| `webui_api.py` | Brücke Oberfläche ↔ Fachlogik | beide |
| `webui/` | Oberfläche (HTML/CSS/JS, Schriften, Icon) | beide |
| `tts_gui.py` | Frühere CustomTkinter-Oberfläche (abgelöst) | beide |
| `install.sh` / `fix_app.py` | Einrichtung / App-Bau | macOS |
| `install.bat` / `TTS_Studio.bat` | Einrichtung / Starter | Windows |
| `requirements.txt` | Python-Abhängigkeiten | beide |
| `TTS_Vorlage.xlsx` | Sheet-Vorlage mit Dropdowns | beide |

### Interaktive Audio-Prüfung

App neu starten und **Review** öffnen. Die neue Prüfseite läuft auf einer lokalen
Adresse und bleibt bedienbar, solange die App geöffnet ist. Eine früher gespeicherte
`review.html` bleibt ein Bericht zum Anhören; die Bearbeitung startet über die App.
Die Übersicht zeigt auf breiten Bildschirmen drei Audios nebeneinander. Abspielen
und Statuswahl sind direkt im Raster möglich; auf schmalen Bildschirmen passt sich
das Raster auf zwei bzw. eine Spalte an.

- **Status direkt auf der Karte:** `passed`, `review needed` oder `regenerate`
  ohne Begründungsdialog lokal speichern und ins Google Sheet schreiben.
  `regenerate` merkt den Inhalt für den nächsten Generierungslauf vor. Manuelle
  Entscheidungen bleiben vom automatischen Befund unterscheidbar. Freigeben prüft zusätzlich das Exportformat
  und veröffentlicht die ausgewählte Version als finale Datei.
- **Vergrößerte Ansicht:** Eine Karte oder „Bearbeiten“ öffnet Wellenform, Schnitt,
  automatische Prüfung und sofortige Neugenerierung. Escape schließt die Ansicht.
- **Audio schneiden:** Aktuelle Version oder erhaltenes Original auswählen,
  Start/Ende in der Wellenform einstellen und den Ausschnitt vorhören. Speichern
  erstellt einen neuen, geprüften Export. Inhalt und Natürlichkeit müssen danach
  erneut automatisch geprüft oder manuell freigegeben werden.
- **Erneut automatisch prüfen:** Vorhandene Datei mit Whisper und, wenn aktiviert,
  Gemini prüfen. Keine neue ElevenLabs-Generierung. Gemini-Aufrufe können Kosten
  verursachen; Whisper wird im laufenden Review-Dienst wiederverwendet.
- **Neu generieren:** Stimmmodell wählen und nur diesen Inhalt erneut erzeugen.
  Vor dem kostenpflichtigen Aufruf erscheint eine Bestätigung.
- **Synchronisierung wiederholen:** Nach einem Schreibfehler nur das gespeicherte
  Ergebnis ins Sheet übertragen; Audio und Anbieter werden nicht erneut aufgerufen.

Inhalts-IDs müssen eindeutig und nicht leer sein. Auswahl und Rückschreiben werden
anhand von ID, Text und Modus erneut abgeglichen. Veränderte Texte, doppelte IDs und
veraltete Browserstände werden abgewiesen. Alle Aktionen teilen sich mit der App
eine Auftragssperre. Bitte nur eine App-Instanz pro Projekt betreiben; parallele
Änderungen durch andere Programme am Sheet sind nicht transaktional sperrbar.

Die Review-Daten werden atomar gespeichert und nach Sheet, Inhalts-ID und Zielpfad
getrennt. Eine beschädigte Datenbank wird nicht still überschrieben. Originale,
Versuche und vorherige Exporte bleiben in `.sources` erhalten. Beim Zurücksetzen
der Review-Liste werden Audiodateien nicht gelöscht. Suche und Statusfilter helfen,
offene Prüfungen und ausstehende Synchronisierungen zu finden.


### Stimmen hinzufügen und wechseln

Im Bereich **ElevenLabs → Stimme** die gewünschte Stimme auswählen. Über **+**
eine ElevenLabs-Stimm-ID einfügen und **Hinzufügen** drücken. Die App liest den
Namen über den [ElevenLabs-Stimmenabruf](https://elevenlabs.io/docs/api-reference/voices/get),
speichert die Stimme und wählt sie direkt aus. Optional kann ein eigener
Anzeigename vergeben werden. Dafür wird kein Testaudio erzeugt. Der API-Key muss
auf die Stimme zugreifen und Stimmen lesen dürfen; bei einer nicht verfügbaren ID
bleibt die bisherige Konfiguration erhalten. Stimmen aus einer geteilten Bibliothek
müssen gegebenenfalls zuerst im ElevenLabs-Konto hinzugefügt werden.

Die bisherige `ELEVENLABS_VOICE_ID` aus `.env` erscheint automatisch als
**Bisherige Stimme**. Die lokale Datei `.tts_voices.json` speichert die Stimmenliste
und die letzte Auswahl auch über App-Neustarts hinweg. Sie enthält keine API-Keys
und wird nicht mit Git synchronisiert. Dieselbe ID wird nur einmal angelegt; über
**+** mit derselben ID und einem Anzeigenamen kann sie umbenannt werden.

Ein Generierungslauf verwendet durchgehend die beim Start gewählte Stimme.
Alle Stimmen nutzen wie bisher denselben Zielordner und dieselben Dateinamen:
Eine neue Freigabe desselben Inhalts ersetzt die bisherige finale Datei.
Stimm-ID und Name werden in den Versuchen und im Review gespeichert und angezeigt.
**Jetzt neu generieren** auf der Review-Seite verwendet die Stimme dieser Aufnahme;
zum Wechsel eines Inhalts auf eine andere Stimme diese in der App wählen, die
betreffende Zeile auswählen und die Generierung starten. Bei älteren Review-Einträgen
ohne Stimm-Metadaten wird die bisherige Konfigurationsstimme verwendet.
