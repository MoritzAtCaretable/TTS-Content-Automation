"""Local voice profiles and read-only ElevenLabs voice lookup."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import tempfile
import threading

import requests


def validate_voice_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value.strip()):
        raise ValueError("Bitte nur die ElevenLabs-Stimm-ID eingeben, keine URL.")
    return value.strip()


def fetch_voice(voice_id, api_key):
    voice_id = validate_voice_id(voice_id)
    if not api_key:
        raise ValueError("ElevenLabs-API-Key fehlt in der .env.")
    try:
        response = requests.get(f"https://api.elevenlabs.io/v1/voices/{voice_id}",
                                headers={"xi-api-key": api_key}, timeout=(10, 25), allow_redirects=False)
    except requests.RequestException as exc:
        raise ValueError("ElevenLabs ist nicht erreichbar. Bitte später erneut versuchen.") from exc
    errors = {
        401: "ElevenLabs hat den API-Key abgewiesen. Bitte den Key prüfen.",
        403: "Kein Zugriff auf diese Stimme. Der API-Key benötigt die Berechtigung zum Lesen von Stimmen.",
        404: "Stimme nicht gefunden. Bitte ID prüfen und die Stimme gegebenenfalls zuerst in ElevenLabs zu deinen Stimmen hinzufügen.",
        429: "ElevenLabs-Anfragelimit erreicht. Bitte später erneut versuchen.",
    }
    if response.status_code != 200:
        raise ValueError(errors.get(response.status_code, f"Stimme konnte nicht geprüft werden (HTTP {response.status_code})."))
    try:
        data = response.json()
        if not isinstance(data, dict) or data.get("voice_id") != voice_id:
            raise ValueError("Stimm-ID in der Antwort stimmt nicht überein")
        name = data.get("name") or f"Stimme {voice_id}"
        if not isinstance(name, str):
            raise ValueError("Ungültiger Stimmname")
        return {"id": voice_id, "name": " ".join(name.split())[:100] or voice_id}
    except (ValueError, TypeError) as exc:
        raise ValueError("ElevenLabs hat keine gültigen Stimmdaten zurückgegeben.") from exc


class VoiceStore:
    def __init__(self, path, default_id):
        self.path = Path(path)
        self.lock = threading.RLock()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("voices"), list):
                    raise ValueError("Unbekanntes Stimmenformat")
                ids = [validate_voice_id(v["id"]) for v in data["voices"]]
                if len(ids) != len(set(ids)) or any(not isinstance(v.get("name"), str) or not v["name"].strip() for v in data["voices"]):
                    raise ValueError("Ungültige Stimmenliste")
                if data.get("selected") not in ids and (ids or data.get("selected")):
                    raise ValueError("Gespeicherte Auswahl fehlt")
                if data.get("legacy_voice_id") and data["legacy_voice_id"] not in ids:
                    raise ValueError("Bisherige Stimme fehlt")
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                raise ValueError("Stimmenkonfiguration nicht lesbar; die Datei .tts_voices.json bleibt unverändert.") from exc
        else:
            try:
                default_id = validate_voice_id(default_id)
            except ValueError:
                default_id = ""
            data = {"version": 1, "selected": default_id, "legacy_voice_id": default_id,
                    "voices": [{"id": default_id, "name": "Bisherige Stimme"}] if default_id else []}
        self.data = data

    def state(self):
        with self.lock:
            return {"voices": copy.deepcopy(self.data["voices"]), "voice_id": self.data["selected"]}

    def get(self, voice_id=None):
        with self.lock:
            voice_id = voice_id if voice_id is not None else self.data["selected"]
            voice = next((v for v in self.data["voices"] if v["id"] == voice_id), None)
            if voice is None:
                raise ValueError("Stimme nicht eingerichtet. Bitte zuerst über + hinzufügen.")
            return copy.deepcopy(voice)

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as f:
                temporary = f.name
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.remove(temporary)
        self.data = data

    def select(self, voice_id):
        with self.lock:
            self.get(voice_id)
            data = copy.deepcopy(self.data)
            data["selected"] = voice_id
            self.save(data)
        return self.state()

    def add(self, voice):
        with self.lock:
            voice_id = validate_voice_id(voice["id"])
            name = str(voice["name"]).strip()[:100]
            if not name:
                raise ValueError("Stimmname fehlt")
            data = copy.deepcopy(self.data)
            data["voices"] = [v for v in data["voices"] if v["id"] != voice_id]
            data["voices"].append({"id": voice_id, "name": name})
            data["selected"] = voice_id
            self.save(data)
        return self.state()
