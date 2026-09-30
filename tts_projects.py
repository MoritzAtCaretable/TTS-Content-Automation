"""Worksheet-backed projects, with local preferences and isolated review journals."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile


PROJECT_HEADERS = ["id", "text", "mode", "filename", "status", "reason", "generated_at", "qc_state", "qc_details"]


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temporary = f.name
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def validate_project_name(name):
    if not isinstance(name, str):
        raise ValueError("Bitte einen Projektnamen eingeben.")
    name = name.strip()
    if not name or len(name) > 100 or re.search(r"[\x00-\x1f\x7f:\\/?*\[\]]", name):
        raise ValueError("Projektname: 1–100 Zeichen, ohne : \\ / ? * [ ] oder Zeilenumbrüche.")
    return name


def create_project_sheet(book, name):
    """One atomic request: never leave a sheet without its required header."""
    name = validate_project_name(name)
    sheets = book.worksheets()
    if any(ws.title.casefold() == name.casefold() for ws in sheets):
        raise ValueError("Ein Projekt mit diesem Namen existiert bereits. Bitte in der Liste auswählen.")
    used_ids = {ws.id for ws in sheets}
    sheet_id = secrets.randbelow(2**31 - 1)
    while sheet_id in used_ids:
        sheet_id = secrets.randbelow(2**31 - 1)
    requests = [
        {"addSheet": {"properties": {"sheetId": sheet_id, "title": name,
            "gridProperties": {"rowCount": 1000, "columnCount": len(PROJECT_HEADERS), "frozenRowCount": 1}}}},
        {"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": h},
                "userEnteredFormat": {"textFormat": {"bold": True},
                    "backgroundColor": {"red": .78, "green": .93, "blue": .93}}} for h in PROJECT_HEADERS]}],
            "fields": "userEnteredValue,userEnteredFormat"}},
    ]
    for column, choices in [(2, ["Einzelwort", "Normal"]), (4, ["todo", "passed", "review needed", "regenerate"])]:
        requests.append({"setDataValidation": {"range": {"sheetId": sheet_id, "startRowIndex": 1,
            "startColumnIndex": column, "endColumnIndex": column + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in choices]},
                "strict": True, "showCustomUi": True}}})
    requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
        "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 420}, "fields": "pixelSize"}})
    # Do not retry a timed-out mutation automatically: it may already have succeeded.
    try:
        book.batch_update({"requests": requests})
    except Exception as exc:
        raise RuntimeError("Projekt konnte nicht bestätigt werden. Bitte die Projektliste neu laden, bevor du es erneut anlegst. " + str(exc)) from exc
    return {"id": str(sheet_id), "name": name}


class ProjectStore:
    def __init__(self, path, spreadsheet_id):
        self.path = Path(path)
        self.spreadsheet_id = spreadsheet_id
        self.data = {"version": 1, "books": {}}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("books"), dict):
                    raise ValueError("Ungültiges Projektformat")
                for book in data["books"].values():
                    if not isinstance(book, dict) or not isinstance(book.get("projects", {}), dict):
                        raise ValueError("Ungültige Projektliste")
                    for gid, prefs in book.get("projects", {}).items():
                        if not gid.isdecimal() or not isinstance(prefs, dict) or any(not isinstance(v, str) for v in prefs.values()):
                            raise ValueError("Ungültige Projekteinstellungen")
                self.data = data
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise ValueError("Projektkonfiguration nicht lesbar; .tts_projects.json bleibt unverändert.") from exc

    def book(self):
        return copy.deepcopy(self.data["books"].get(self.spreadsheet_id, {"projects": {}}))

    def save_project(self, project_id, preferences, *, select=False, legacy_id=None):
        data = copy.deepcopy(self.data)
        book = data["books"].setdefault(self.spreadsheet_id, {"projects": {}})
        book["projects"].setdefault(str(project_id), {}).update(preferences)
        if select:
            book["selected"] = str(project_id)
        if legacy_id is not None and "legacy_id" not in book:
            book["legacy_id"] = str(legacy_id)
        write_json(self.path, data)
        self.data = data

    def context(self, project, root, output_base):
        gid, name = project["id"], project["name"]
        prefs = self.book().get("projects", {}).get(gid, {})
        slug = re.sub(r"[^\w-]+", "-", name, flags=re.UNICODE).strip("-_")[:60] or "Projekt"
        folder = prefs.get("folder") or str(Path(output_base) / f"{slug}--{gid}")
        scope = hashlib.sha256(self.spreadsheet_id.encode()).hexdigest()[:20]
        journal = Path(root) / ".tts-projects" / scope / gid
        return {**project, "spreadsheet_id": self.spreadsheet_id, "folder": folder,
                "review_data": str(journal / "review_data.json"), "review_html": str(journal / "review.html")}


def import_legacy_reviews(project, source_file, default_name):
    """Copy matching old reviews once; keep original files and all audio in place."""
    destination = Path(project["review_data"])
    marker = destination.parent / "legacy-imported.json"
    if marker.exists():
        return
    source = Path(source_file)
    if source.exists():
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(not isinstance(e, dict) for e in data.values()):
            raise ValueError("Bisherige Review-Daten nicht lesbar; Import abgebrochen.")
        selected = {}
        for key, entry in data.items():
            if (entry.get("sheet_id") and entry["sheet_id"] != project["spreadsheet_id"]
                    or entry.get("sheet_name") and entry["sheet_name"] != default_name):
                continue
            if entry.get("worksheet_id") is not None and str(entry["worksheet_id"]) != project["id"]:
                continue
            selected[key] = {**entry, "worksheet_id": int(project["id"]), "sheet_id": project["spreadsheet_id"]}
        existing = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {}
        if not isinstance(existing, dict):
            raise ValueError("Review-Daten nicht lesbar; Import abgebrochen.")
        if selected:
            write_json(destination, {**selected, **existing})
    write_json(marker, {"imported": True})
