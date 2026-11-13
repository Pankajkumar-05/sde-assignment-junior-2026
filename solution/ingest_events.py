"""Event JSONL ingestion into the append-only ledger.

Per line: malformed -> skip; duplicate event_id -> skip; otherwise store. Linking to leads
(and therefore orphan / unknown-disposition classification) is NOT done here: it is derived
from the final state at report time, so events that arrive before their lead reconcile
automatically and counts always reflect everything ingested so far.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .db import file_already_ingested, make_file_key, now_iso
from .normalize import normalize_phone, parse_timestamp
from .pii import Pii

OVERRIDE = "disposition.override"


def _text(value):
    """A non-empty string, or an int rendered as a string, else None."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _parse_line(line: bytes):
    """-> event dict, or None if malformed (not JSON, not an object, or no event_id)."""
    try:
        obj = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(obj, dict) or _text(obj.get("event_id")) is None:
        return None
    return obj


def ingest_event_file(conn: sqlite3.Connection, pii: Pii, path: Path,
                      drop_name: str) -> dict | None:
    """Ingest one events file atomically. Returns counters, or None if already ingested."""
    path = Path(path)
    data = path.read_bytes()
    file_key = make_file_key("events", None, data)
    if file_already_ingested(conn, file_key):
        return None

    stats = {"lines": 0, "malformed": 0, "duplicates": 0, "stored": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        if file_already_ingested(conn, file_key):
            conn.execute("ROLLBACK")
            return None
        sfid = conn.execute(
            "INSERT INTO source_files (file_key, kind, drop_name, file_name, ingested_at)"
            " VALUES (?,?,?,?,?)", (file_key, "events", drop_name, path.name, now_iso())).lastrowid

        for line_number, line in enumerate(data.splitlines(), 1):
            if not line.strip():                      # blank lines are not counted at all
                continue
            stats["lines"] += 1
            obj = _parse_line(line)
            if obj is None:
                stats["malformed"] += 1
                continue

            event_type = _text(obj.get("event_type"))
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            code = _text(payload.get("disposition_code")) if (
                event_type or "").startswith("disposition.") else None

            phone_hash = None                         # overrides link via call_id, never phone
            if event_type != OVERRIDE:
                phone = _text(obj.get("phone"))
                canonical = normalize_phone(phone) if phone else None
                phone_hash = pii.phone_hash(canonical) if canonical else None

            cur = conn.execute(
                "INSERT INTO events (event_id, event_type, workflow_id, call_id,"
                " occurred_at_us, phone_hash, disposition_code, payload_json,"
                " source_file_id, line_number) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (event_id) DO NOTHING",
                (_text(obj["event_id"]), event_type, _text(obj.get("workflow_id")),
                 _text(obj.get("call_id")), parse_timestamp(obj.get("occurred_at")),
                 phone_hash, code, json.dumps(payload, sort_keys=True, ensure_ascii=False),
                 sfid, line_number))
            if cur.rowcount == 0:
                stats["duplicates"] += 1
            else:
                stats["stored"] += 1

        conn.execute(
            "UPDATE source_files SET rows_total = ?, malformed_count = ?, duplicate_count = ?"
            " WHERE source_file_id = ?",
            (stats["lines"], stats["malformed"], stats["duplicates"], sfid))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats
