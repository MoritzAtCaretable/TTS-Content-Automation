#!/usr/bin/env python3
"""
TTS Studio — Oberfläche im Caretable-Design.

Startet ein natives Fenster (kein Browser): auf macOS WKWebView, auf Windows
WebView2. Die Oberfläche ist HTML/CSS/JS aus webui/, die Fachlogik bleibt
unverändert in sheets_to_elevenlabs_qc_local.py.

Start:  python tts_studio_web.py
        (oder per Doppelklick über die "TTS Studio.app")
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
# Ins Projektverzeichnis wechseln, damit die relativen Pfade der Pipeline
# (.env, service_account.json, review.html, tts-output) stimmen.
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PROJECT_DIR))

import webview                                   # noqa: E402

from webui_api import Api                        # noqa: E402

APP_TITLE = "TTS Studio"


def main() -> int:
    index = PROJECT_DIR / "webui" / "index.html"
    if not index.exists():
        print(f"Oberfläche nicht gefunden: {index}")
        return 1

    api = Api()
    window = webview.create_window(
        APP_TITLE,
        url=str(index),
        js_api=api,
        width=1440,
        height=940,
        min_size=(1100, 720),
        background_color="#F5F7FC",
        text_select=False,
    )
    api._window = window
    try:
        webview.start()
    finally:
        api.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
