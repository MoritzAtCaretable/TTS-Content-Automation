#!/bin/bash
#
# TTS_Setup.command — Kompletter Doppelklick-Installer für macOS.
#
# Diese EINE Datei genügt: sie installiert Git (falls nötig), klont das Repo
# und richtet alles ein. Man legt sie z.B. auf den Schreibtisch und doppelklickt.
#
# EINMALIGER HAKEN beim ersten Start (macOS-Sicherheit):
#   Falls "kann nicht geöffnet werden" erscheint:
#   Rechtsklick auf die Datei -> "Öffnen" -> im Dialog nochmal "Öffnen".

# ─────────────────────────────────────────────
# HIER ANPASSEN: URL des Git-Repos und Zielordnername
# ─────────────────────────────────────────────
REPO_URL="https://github.com/MoritzAtCaretable/TTS-Content-Automation.git"
TARGET_DIR="$HOME/TTS-Studio"
# ─────────────────────────────────────────────

clear
echo "════════════════════════════════════════"
echo "   TTS Studio — Komplett-Einrichtung"
echo "════════════════════════════════════════"
echo "  Installiert Git, lädt das Projekt und"
echo "  richtet alles ein. Beim ersten Mal"
echo "  10–20 Min (Downloads). Einfach warten."
echo "════════════════════════════════════════"
echo ""

# 1. Homebrew sicherstellen (nötig, um Git zu installieren, falls es fehlt)
if ! command -v brew >/dev/null 2>&1; then
    echo "→ Homebrew wird installiert (einmalig)…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
    if [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; fi
fi

# 2. Git sicherstellen
if ! command -v git >/dev/null 2>&1; then
    echo "→ Git wird installiert…"
    brew install git
fi

# 3. Repo klonen oder aktualisieren
if [ -d "$TARGET_DIR/.git" ]; then
    echo "→ Projekt existiert bereits — hole Updates…"
    git -C "$TARGET_DIR" pull
else
    echo "→ Projekt wird nach '$TARGET_DIR' geladen…"
    git clone "$REPO_URL" "$TARGET_DIR"
fi

# 4. Einrichtung starten
cd "$TARGET_DIR" || { echo "❌ Zielordner nicht gefunden."; exit 1; }
chmod +x install.sh 2>/dev/null
bash install.sh

echo ""
echo "════════════════════════════════════════"
echo "  Projektordner: $TARGET_DIR"
echo "  Als Nächstes dort die .env mit Keys füllen"
echo "  und service_account.json hineinlegen."
echo "  Dann 'TTS Studio.app' starten."
echo "════════════════════════════════════════"
echo "Fenster kann geschlossen werden."
