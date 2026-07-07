#!/bin/bash
#
# run.sh — Startet das TTS-Skript im virtuellen Environment.
#
# Erster Aufruf: erstellt das venv und installiert alle Pakete (dauert einmalig).
# Jeder weitere Aufruf: aktiviert nur das venv und startet das Skript.
#
# Benutzung:
#   chmod +x run.sh      (nur einmal nötig, macht das Skript ausführbar)
#   ./run.sh

# In den Ordner wechseln, in dem dieses Skript liegt
# (so funktioniert es egal von wo aus es aufgerufen wird)
cd "$(dirname "$0")" || exit 1

VENV_DIR="venv"
PYTHON_SCRIPT="sheets_to_elevenlabs_qc_local.py"

# 1. venv erstellen, falls noch nicht vorhanden
if [ ! -d "$VENV_DIR" ]; then
    echo "🔧 Kein virtuelles Environment gefunden — erstelle es jetzt..."
    python3 -m venv "$VENV_DIR"

    echo "📦 Installiere benötigte Pakete (das dauert beim ersten Mal etwas)..."
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip
    pip install gspread google-auth requests openai-whisper pydub google-genai audioop-lts python-dotenv
    echo "✅ Setup abgeschlossen."
else
    # venv existiert bereits — nur aktivieren
    source "$VENV_DIR/bin/activate"
fi

# 2. Skript starten
echo "🚀 Starte $PYTHON_SCRIPT ..."
echo ""
python "$PYTHON_SCRIPT"