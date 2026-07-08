#!/bin/bash
#
# install.sh — Einrichtung von TTS Studio auf dem Mac.
#
# Benutzung: Doppelklick geht nicht direkt bei .sh — stattdessen im Terminal:
#     cd <projektordner>
#     chmod +x install.sh
#     ./install.sh
#
# Richtet alles ein: Homebrew (falls nötig), Python, ffmpeg, venv, Pakete,
# .env-Vorlage und die "TTS Studio.app". Danach nie wieder nötig — außer nach
# einem Update, das neue Pakete braucht.

set -e
cd "$(dirname "$0")"
echo "════════════════════════════════════════"
echo "  TTS Studio — Einrichtung (macOS)"
echo "════════════════════════════════════════"

# ─────────────────────────────────────────────
# HIER ANPASSEN: Repo-URL (für den ZIP→Git-Fall)
# ─────────────────────────────────────────────
REPO_URL="https://github.com/MoritzAtCaretable/TTS-Content-Automation.git"

# 1. Homebrew
if ! command -v brew >/dev/null 2>&1; then
    echo "→ Homebrew wird installiert (einmalig)…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Homebrew in die aktuelle Shell laden (Apple Silicon vs. Intel)
    if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
    if [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; fi
else
    echo "✓ Homebrew vorhanden"
fi

# 2. Python & ffmpeg & git
echo "→ Python, ffmpeg & git prüfen/installieren…"
brew list python >/dev/null 2>&1 || brew install python
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
command -v git >/dev/null 2>&1 || brew install git
echo "✓ Python: $(python3 --version)"
echo "✓ ffmpeg: $(ffmpeg -version | head -1)"
echo "✓ git: $(git --version)"

# 2b. Falls dieser Ordner KEIN Git-Checkout ist (ZIP-Download), nachträglich zu
#     einem machen — dann funktioniert der Update-Button. Es wird NICHTS gelöscht:
#     nur der .git-Ordner wird "aufgepfropft". .env & service_account.json bleiben
#     unangetastet (Git ignoriert sie ohnehin).
if [ ! -d ".git" ]; then
    echo "→ Kein Git-Checkout erkannt (vermutlich ZIP). Richte Git-Verbindung ein…"
    TMP_CLONE="$(mktemp -d)"
    if git clone --depth 1 "$REPO_URL" "$TMP_CLONE/repo" >/dev/null 2>&1; then
        mv "$TMP_CLONE/repo/.git" "./.git"
        rm -rf "$TMP_CLONE"
        # Versionierte Dateien auf den Repo-Stand bringen; ignorierte (.env etc.) bleiben.
        git reset --hard HEAD >/dev/null 2>&1 || true
        echo "✓ Git-Verbindung hergestellt — Update-Button ist jetzt aktiv."
    else
        rm -rf "$TMP_CLONE"
        echo "⚠ Konnte Git-Verbindung nicht herstellen (kein Zugriff/Netz?)."
        echo "  Läuft trotzdem — nur der Update-Button bleibt inaktiv."
    fi
fi

# 3. venv + Pakete
if [ ! -d "venv" ]; then
    echo "→ Virtuelle Umgebung anlegen…"
    python3 -m venv venv
fi
echo "→ Pakete installieren (PyTorch/Whisper ~2GB, kann dauern)…"
source venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

# 4. .env vorbereiten
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "→ .env aus Vorlage erstellt — bitte Keys eintragen!"
fi

# 5. App bauen
echo "→ 'TTS Studio.app' bauen…"
python fix_app.py

echo ""
echo "════════════════════════════════════════"
echo "  ✅ Fertig!"
echo "════════════════════════════════════════"
echo "  Nächste Schritte:"
echo "  1. .env öffnen und API-Keys + Spreadsheet-ID eintragen"
echo "  2. service_account.json in diesen Ordner legen"
echo "  3. 'TTS Studio.app' per Doppelklick starten"
echo "════════════════════════════════════════"