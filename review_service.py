"""Local, session-scoped review actions. No cloud hosting or arbitrary file routes."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from tts_audio import AudioProcessingError, export_audio, load_audio, safe_filename, validate_export
from tts_identity import resolve_record, canonical_mode
from tts_quality import CheckResult, QualityResult


class ReviewService:
    def __init__(self, api, pipeline):
        self.api, self.p = api, pipeline
        self.whisper_model = None

    def version(self, entry):
        stamps = []
        for field in ("abspath", "raw_abspath"):
            path = Path(entry.get(field) or "__missing__")
            stat = path.stat() if path.is_file() else None
            stamps.append((stat.st_mtime_ns, stat.st_size) if stat else None)
        return hashlib.sha256(json.dumps([entry, stamps], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def entry(self, key, version=None):
        entry = self.p._load_review_data().get(key)
        if not isinstance(entry, dict):
            raise ValueError("Eintrag nicht mehr vorhanden. Bitte neu laden.")
        if version is not None and version != self.version(entry):
            raise ValueError("Audio oder Befund wurde inzwischen geändert. Bitte neu laden.")
        return copy.deepcopy(entry)

    def public_entries(self):
        result = []
        for key, entry in self.p._load_review_data().items():
            result.append({"key": key, "version": self.version(entry), **{
                k: entry.get(k) for k in ("id", "text", "filename", "mode", "status", "reason",
                                         "generated_at", "transcript", "wer", "gemini", "model", "qc",
                                         "sync_error", "decision", "voice_id", "voice_name")},
                "has_audio": bool(entry.get("abspath") and Path(entry["abspath"]).is_file()),
                "has_original": bool(entry.get("raw_abspath") and Path(entry["raw_abspath"]).is_file())})
        return sorted(result, key=lambda e: e.get("generated_at") or "", reverse=True)

    def audio_path(self, entry, source="current"):
        if source not in {"current", "original"}:
            raise ValueError("Unbekannte Audioquelle")
        value = entry.get("raw_abspath" if source == "original" else "abspath")
        if not value or not Path(value).is_file():
            raise ValueError("Für diese Version ist keine Audiodatei vorhanden.")
        return Path(value).resolve()

    def target(self, entry):
        filename = safe_filename(entry.get("filename", ""))
        if entry.get("target_path"):
            target = Path(entry["target_path"]).resolve()
        elif entry.get("abspath"):
            target = Path(entry["abspath"]).resolve().parent / filename
        else:
            raise ValueError("Zielordner unbekannt. Bitte über die App neu generieren.")
        if target.name != filename:
            raise ValueError("Zieldatei passt nicht zum Eintrag")
        return target

    def current_sheet(self, entry):
        if (entry.get("sheet_id") and entry["sheet_id"] != self.p.SPREADSHEET_ID
                or entry.get("sheet_name") and entry["sheet_name"] != self.p.SHEET_NAME):
            raise ValueError("Dieser Eintrag gehört zu einem anderen Sheet.")
        ws, headers, rows = self.p.open_sheet()
        row = resolve_record(rows, entry)
        if row.get("filename") and self.p.build_filename(row) != self.target(entry).name:
            raise ValueError("Dateiname wurde im Sheet geändert. Bitte dort neu laden und generieren.")
        target_name = self.target(entry).name.casefold()
        if any(r["_row"] != row["_row"] and r.get("text") and self.p.build_filename(r).casefold() == target_name for r in rows):
            raise ValueError("Der Dateiname wird von einem anderen Inhalt verwendet.")
        return ws, headers, row

    def new_workdir(self, entry):
        parent = self.target(entry).parent / ".sources"
        parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="review-", dir=parent))

    def remember(self, entry):
        previous = {k: copy.deepcopy(entry.get(k)) for k in
                    ("abspath", "status", "reason", "qc", "decision", "generated_at")}
        entry.setdefault("history", []).append(previous)

    def persist(self, key, entry, status, reason):
        entry.update(status=status, reason=reason, generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     audio_path_recorded=True, sheet_id=self.p.SPREADSHEET_ID, sheet_name=self.p.SHEET_NAME)
        updates = {"status": status, "filename": entry["filename"], "reason": reason,
                   "generated_at": entry["generated_at"],
                   "qc_state": "manual_passed" if status == "passed" and entry.get("decision", {}).get("type") == "manual" else status,
                   "qc_details": json.dumps({"automatic": entry.get("qc"), "decision": entry.get("decision")}, ensure_ascii=False)}
        entry["sync_updates"] = updates
        entry["sync_error"] = "Synchronisierung ausstehend"
        self.p._save_review_entry(entry, key=key)
        try:
            ws, headers, row = self.current_sheet(entry)
            self.p.write_back(ws, headers, row["_row"], updates, expected_row=row)
        except Exception as exc:
            entry["sync_error"] = str(exc)
            self.p._save_review_entry(entry, key=key)
            raise RuntimeError("Änderung lokal gespeichert. Sheet-Synchronisierung fehlgeschlagen: " + str(exc)) from exc
        entry["sync_error"] = ""
        self.p._save_review_entry(entry, key=key)
        self.p.generate_review_html()

    def verify(self, source, entry):
        return validate_export(source, ffmpeg=self.p.FFMPEG_BIN, ffprobe=self.p.FFPROBE_BIN,
                               export_format=self.target(entry).suffix[1:], sample_rate=self.p.TARGET_SAMPLE_RATE,
                               expected_duration_ms=len(load_audio(source)), target_i=self.p.LOUDNORM_I,
                               target_tp=self.p.LOUDNORM_TP, check_loudness=self.p.POSTPROCESS)

    def publish(self, entry):
        source, target = self.audio_path(entry), self.target(entry)
        self.verify(source, entry)
        self.current_sheet(entry)
        if source != target:
            work = self.new_workdir(entry)
            candidate = work / target.name
            shutil.copyfile(source, candidate)
            self.verify(candidate, entry)
            if target.is_file():
                backup = work / ("previous" + target.suffix)
                shutil.copyfile(target, backup)
                for previous in entry.get("history", []):
                    if previous.get("abspath") == str(target):
                        previous["abspath"] = str(backup)
            os.replace(candidate, target)
        else:
            # Legacy reviews may point at the mutable final path. Freeze this version.
            snapshot = self.new_workdir(entry) / ("accepted" + target.suffix)
            shutil.copyfile(source, snapshot)
            entry["abspath"] = str(snapshot)

    def perform(self, key, version, action, options):
        entry = self.entry(key, version)
        if action == "sync":
            ws, headers, row = self.current_sheet(entry)
            if not entry.get("sync_updates"):
                raise ValueError("Keine ausstehende Änderung vorhanden")
            self.p.write_back(ws, headers, row["_row"], entry["sync_updates"], expected_row=row)
            entry["sync_error"] = ""
            self.p._save_review_entry(entry, key=key)
            return {"message": "Sheet-Synchronisierung abgeschlossen."}
        self.current_sheet(entry)
        self.remember(entry)
        if action == "regenerate":
            model = options.get("model") or entry.get("model") or self.api.model
            if model not in self.api.get_state()["models"]:
                raise ValueError("Unbekanntes Stimmmodell")
            selection = [{k: entry.get(k, "") for k in ("id", "text", "mode", "filename")}]
            voice_id = entry.get("voice_id") or self.api.voices.data["legacy_voice_id"] or self.p.VOICE_ID
            return self.api.run_generation(selection, str(self.target(entry).parent), model, voice_id)
        if action == "trim":
            source = self.audio_path(entry, options.get("source", "current"))
            start, end = options.get("start"), options.get("end")
            if any(type(n) not in (int, float) or not math.isfinite(n) for n in (start, end)):
                raise ValueError("Gültige Schnittzeiten in Sekunden angeben")
            audio = load_audio(source)
            if not 0 <= start < end <= len(audio) / 1000 + .005 or end - start < .08:
                raise ValueError("Schnittbereich muss innerhalb des Audios liegen und mindestens 80 ms lang sein")
            work = self.new_workdir(entry)
            wav, candidate = work / "manual-cut.wav", work / ("manual-cut" + self.target(entry).suffix)
            audio[round(start * 1000):round(end * 1000)].export(wav, format="wav").close()
            details = export_audio(wav, candidate, ffmpeg=self.p.FFMPEG_BIN, ffprobe=self.p.FFPROBE_BIN,
                                   export_format=candidate.suffix[1:], sample_rate=self.p.TARGET_SAMPLE_RATE,
                                   bitrate=self.p.OPUS_BITRATE, target_i=self.p.LOUDNORM_I, target_tp=self.p.LOUDNORM_TP,
                                   process=self.p.POSTPROCESS)
            qc = QualityResult({"cut": CheckResult("passed", "Manuell geschnitten", details={"start": start, "end": end}),
                                "export": CheckResult("passed", "Export geprüft", details=details),
                                "text": CheckResult("skipped", "Nach Schnitt erneut prüfen oder manuell freigeben"),
                                "gemini": CheckResult("skipped", "Vorherige Bewertung gilt nicht für diesen Schnitt")})
            original = entry.get("raw_abspath")
            if not original:
                original = str(work / ("original" + source.suffix))
                shutil.copyfile(source, original)
            entry.update(abspath=str(candidate), raw_abspath=original,
                         qc=qc.to_dict(), wer=None, gemini=None, transcript="", decision={"type": "trim"})
            self.persist(key, entry, "review needed", "Manueller Schnitt – Inhalt und Natürlichkeit noch nicht freigegeben")
            return {"message": "Schnitt gespeichert. Bitte anhören und freigeben oder erneut prüfen."}
        if action == "recheck":
            source = self.audio_path(entry)
            try:
                details = self.verify(source, entry)
                if self.whisper_model is None:
                    self.api._log("🧠 Whisper für erneute Qualitätsprüfung laden…")
                    self.whisper_model = self.p.whisper.load_model(self.p.WHISPER_MODEL)
                client = None
                if self.p.ENABLE_GEMINI_CHECK:
                    if not self.p.GEMINI_API_KEY:
                        raise ValueError("Gemini-API-Key fehlt")
                    client = self.p.genai.Client(api_key=self.p.GEMINI_API_KEY,
                                                http_options=self.p.types.HttpOptions(timeout=60000))
                self.p._gemini_disabled_for_run = False
                try:
                    qc = self.p.quality_check(str(source), entry["text"], self.whisper_model, client,
                                              single_word_mode=canonical_mode(entry.get("mode")) == "word",
                                              skip_silence=canonical_mode(entry.get("mode")) == "word")
                finally:
                    if client is not None:
                        client.close()
                qc.checks["export"] = CheckResult("passed", "Export geprüft", details=details)
            except Exception as exc:
                qc = QualityResult({"recheck": CheckResult("error", str(exc))})
            entry.update(qc=qc.to_dict(), wer=qc.wer, gemini=qc.gemini_score, transcript=qc.transcript,
                         decision={"type": "automatic"})
            if qc.passed:
                self.publish(entry)
            self.persist(key, entry, "passed" if qc.passed else "review needed", "" if qc.passed else qc.reason)
            return {"message": "Prüfung abgeschlossen: " + ("bestanden" if qc.passed else "Review erforderlich")}
        if action == "status":
            status = options.get("status")
            if status not in {"passed", "review needed", "regenerate"}:
                raise ValueError("Unbekannter Status")
            note = str(options.get("note", "")).strip()[:1000]
            if status == "passed":
                self.publish(entry)
            entry["decision"] = {"type": "manual", "note": note,
                                 "at": datetime.now().isoformat(timespec="seconds")}
            self.persist(key, entry, status, {"passed": "Manuell freigegeben", "review needed": "Manuell zur Prüfung markiert", "regenerate": "Zur Neugenerierung vorgemerkt"}[status] + (": " + note if note else ""))
            return {"message": "Status gespeichert."}
        raise ValueError("Unbekannte Aktion")


class ReviewServer:
    def __init__(self, api, pipeline, root):
        self.service = ReviewService(api, pipeline)
        self.root = Path(root)
        self.token = secrets.token_urlsafe(32)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Session URLs must not enter logs.

            def route(self):
                if self.headers.get("Host") != owner.host:
                    raise ValueError("Unzulässiger Host")
                parsed = urlsplit(self.path)
                prefix = "/" + owner.token + "/"
                if not parsed.path.startswith(prefix):
                    raise ValueError("Ungültige Review-Sitzung")
                return unquote(parsed.path[len(prefix):]), parse_qs(parsed.query)

            def headers_for(self, status, mime, length):
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")

            def json_response(self, data, status=200):
                content = json.dumps(data, ensure_ascii=False).encode()
                self.headers_for(status, "application/json; charset=utf-8", len(content))
                self.end_headers(); self.wfile.write(content)

            def file_response(self, path, audio=False):
                length = path.stat().st_size
                start, end, status = 0, length - 1, 200
                if audio and self.headers.get("Range"):
                    import re
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers["Range"])
                    if not match or not any(match.groups()):
                        self.send_error(416); return
                    a, b = match.groups()
                    start = int(a) if a else max(0, length - int(b))
                    end = min(length - 1, int(b)) if a and b else length - 1
                    if start > end or start >= length:
                        self.send_error(416); return
                    status = 206
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.headers_for(status, mime, end - start + 1)
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{length}")
                self.end_headers()
                with path.open("rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        data = f.read(min(65536, remaining))
                        if not data: break
                        self.wfile.write(data); remaining -= len(data)

            def do_GET(self):
                try:
                    route, query = self.route()
                    if route == "api/entries":
                        return self.json_response({"entries": owner.service.public_entries(), "models": api.get_state()["models"]})
                    if route == "api/state":
                        return self.json_response(api.job_state())
                    if route.startswith("audio/"):
                        entry = owner.service.entry(route[6:], query.get("version", [None])[0])
                        return self.file_response(owner.service.audio_path(entry, query.get("source", ["current"])[0]), audio=True)
                    assets = {"": "review.html", "review.js": "review.js", "review.css": "review.css"}
                    for asset in ("assets/brand/app-icon.png", "assets/fonts/CardiumARegular.woff2",
                                  "assets/fonts/CardiumAMedium.woff2", "assets/fonts/CardiumABold.woff2"):
                        assets[asset] = asset
                    if route in assets:
                        return self.file_response(owner.root / "webui" / assets[route])
                    raise ValueError("Seite nicht gefunden")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    self.json_response({"error": str(exc)}, 400)

            def do_POST(self):
                try:
                    route, _ = self.route()
                    if route != "api/action" or self.headers.get("Origin") != owner.origin:
                        raise ValueError("Aktion nur aus der lokalen Prüfseite möglich")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 16384 or self.headers.get_content_type() != "application/json":
                        raise ValueError("Ungültige Anfrage")
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("key", "version", "action")):
                        raise ValueError("Eintrag und Version erforderlich")
                    if data["action"] not in {"status", "trim", "regenerate", "recheck", "sync"}:
                        raise ValueError("Unbekannte Aktion")
                    owner.service.entry(data["key"], data["version"])
                    options = data.get("options", {})
                    if not isinstance(options, dict):
                        raise ValueError("Ungültige Aktionsoptionen")
                    job = api.launch_job(lambda: owner.service.perform(data["key"], data["version"], data["action"], options), "Review-Aktion läuft")
                    self.json_response(job, 202)
                except Exception as exc:
                    self.json_response({"error": str(exc)}, 400)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.http.server_port}"
        self.origin = "http://" + self.host
        self.url = self.origin + "/" + self.token + "/"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
