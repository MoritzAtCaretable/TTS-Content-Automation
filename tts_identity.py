"""Match sheet content by stable identity, never by a cached row number."""


def canonical_mode(mode):
    return "word" if (mode or "").strip().lower() in {"word", "einzelwort"} else "normal"


def resolve_record(records, expected):
    identity = str(expected.get("id", "")).strip().casefold()
    if not identity:
        raise ValueError("Eine eindeutige Inhalts-ID ist erforderlich.")
    matches = [r for r in records if str(r.get("id", "")).strip().casefold() == identity]
    if len(matches) != 1:
        raise ValueError("Inhalts-ID fehlt oder ist mehrfach im Sheet vorhanden. Bitte Sheet neu laden.")
    row = matches[0]
    if (str(row.get("text", "")).strip() != str(expected.get("text", "")).strip()
            or canonical_mode(row.get("mode")) != canonical_mode(expected.get("mode"))):
        raise ValueError("Text oder Modus wurde im Sheet geändert. Bitte dort neu laden und generieren.")
    return row


def parse_sheet_values(values):
    if not values:
        return {}, []
    headers = {h.strip().lower(): i + 1 for i, h in enumerate(values[0]) if h.strip()}
    records = []
    for index, cells in enumerate(values[1:], 2):
        records.append({"_row": index, **{name: cells[col - 1].strip() if col <= len(cells) else ""
                                         for name, col in headers.items()}})
    return headers, records
