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

# 2. Python & ffmpeg
echo "→ Python & ffmpeg prüfen/installieren…"
brew list python >/dev/null 2>&1 || brew install python
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
echo "✓ Python: $(python3 --version)"
echo "✓ ffmpeg: $(ffmpeg -version | head -1)"

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