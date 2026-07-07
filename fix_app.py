#!/usr/bin/env python3
"""
fix_app.py — Baut "TTS Studio.app" mit garantiert sauberen (LF) Zeilenenden neu.

Nutzt Python statt Heredocs, damit Windows-Zeilenenden (CRLF) ausgeschlossen sind —
die sind die häufigste Ursache dafür, dass ein Bundle beim Doppelklick stumm nichts tut.

Ausführen im Projektordner:
    python3 fix_app.py
"""

import os
import stat
import subprocess

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(PROJECT_DIR, "TTS Studio.app")
MACOS_DIR = os.path.join(APP, "Contents", "MacOS")
RESOURCES_DIR = os.path.join(APP, "Contents", "Resources")
ICON_SRC = os.path.join(PROJECT_DIR, "AppIcon.icns")

# venv-Python bestimmen (Fallback: system python3)
venv_py = os.path.join(PROJECT_DIR, "venv", "bin", "python")
pybin = venv_py if os.path.exists(venv_py) else "python3"

# Altes Bundle entfernen
subprocess.run(["rm", "-rf", APP])
os.makedirs(MACOS_DIR, exist_ok=True)
os.makedirs(RESOURCES_DIR, exist_ok=True)

# Icon einbinden, falls vorhanden
has_icon = os.path.exists(ICON_SRC)
if has_icon:
    subprocess.run(["cp", ICON_SRC, os.path.join(RESOURCES_DIR, "AppIcon.icns")])

# Info.plist
icon_key = "    <key>CFBundleIconFile</key><string>AppIcon</string>\n" if has_icon else ""
info_plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>TTS Studio</string>
    <key>CFBundleDisplayName</key><string>TTS Studio</string>
    <key>CFBundleIdentifier</key><string>local.tts.studio</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundlePackageType</key><string>APPL</string>
{icon_key}    <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
"""

# launcher — bewusst mit \n und ohne \r geschrieben
launcher = f"""#!/bin/bash
cd "{PROJECT_DIR}" || exit 1

# PATH um die üblichen Homebrew-Pfade ergänzen. Eine per Doppelklick gestartete
# App erbt NICHT den PATH aus ~/.zprofile, deshalb würde ffmpeg sonst fehlen.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PYBIN="{pybin}"
if [ ! -x "$PYBIN" ]; then
    PYBIN="$(command -v python3)"
fi
LOGFILE="{PROJECT_DIR}/.tts_gui_error.log"
if ! "$PYBIN" tts_gui.py 2> "$LOGFILE"; then
    if [ -s "$LOGFILE" ]; then
        osascript -e "display dialog \\"TTS Studio konnte nicht starten:\\n\\n$(cat "$LOGFILE" | tail -c 900)\\" buttons {{\\"OK\\"}} with icon stop with title \\"TTS Studio\\""
    fi
fi
"""

# WICHTIG: newline="\n" erzwingt Unix-Zeilenenden, egal auf welchem System
with open(os.path.join(APP, "Contents", "Info.plist"), "w", newline="\n") as f:
    f.write(info_plist)

launcher_path = os.path.join(MACOS_DIR, "launcher")
with open(launcher_path, "w", newline="\n") as f:
    f.write(launcher)

# Ausführbar machen
os.chmod(launcher_path, os.stat(launcher_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

# Quarantäne-Flag entfernen (falls vorhanden) und LaunchServices neu registrieren
try:
    subprocess.run(["xattr", "-cr", APP], check=False)
except FileNotFoundError:
    pass
lsregister = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")
if os.path.exists(lsregister):
    subprocess.run([lsregister, "-f", APP], check=False)

# Plist validieren (plutil gibt es nur auf macOS)
try:
    result = subprocess.run(["plutil", os.path.join(APP, "Contents", "Info.plist")],
                            capture_output=True, text=True)
    print("Info.plist:", result.stdout.strip() or result.stderr.strip())
except FileNotFoundError:
    print("Info.plist: (plutil nicht verfügbar — auf dem Mac wird validiert)")
print(f"launcher nutzt Python: {pybin}")
print(f"Icon: {'eingebunden (AppIcon.icns)' if has_icon else 'kein AppIcon.icns gefunden — generisches Icon'}")
print(f"✅ '{APP}' neu gebaut (saubere LF-Zeilenenden).")
print("   Jetzt per Doppelklick oder mit  open \"TTS Studio.app\"  starten.")
if has_icon:
    print("   Hinweis: Falls das alte Icon noch angezeigt wird, hilft ein Neustart des Docks:")
    print("            killall Dock")