"""Lead CSV ingestion: header mapping, row validation/normalisation, dedupe + merge."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

from .config import LedgerError
from .db import file_already_ingested, make_file_key, now_iso
from .normalize import normalize_header, normalize_phone, parse_date, parse_decimal, parse_enum
from .pii import Pii
from .schema_loader import Field, Schema

csv.field_size_limit(10_000_000)


# ------------------------------------------------------------ parse + validate
def map_header(schema: Schema, header: list) -> dict:
    """field name -> column index. The leftmost matching column wins; unknown columns ignored."""
    lookup = {}
    for field in schema.fields:
        for name in field.match_names:
            lookup.setdefault(name, field.name)
    mapping: dict = {}
    for idx, cell in enumerate(header):
        field_name = lookup.get(normalize_header(cell))
        if field_name is not None and field_name not in mapping:
            mapping[field_name] = idx
    return mapping


def _parse_value(field: Field, value: str):
    if field.type == "string":
        return value
    if field.type == "phone":
        return normalize_phone(value)
    if field.type == "date":
        return parse_date(value)
    if field.type == "decimal":
        return parse_decimal(value)
    return parse_enum(value, field.values)


def validate_row(schema: Schema, raw: dict):
    """-> (clean_values, None, warnings) or (None, reason, 0).

    Fields are walked in schema order; the first failing required field is the single
    rejection reason. An invalid *optional* value becomes None plus one warning.
    """
    clean, warnings = {}, 0
    for field in schema.fields:
        value = raw.get(field.name, "")
        if value == "":
            if field.required:
                return None, f"missing_required:{field.name}", 0
            clean[field.name] = None
            continue
        parsed = _parse_value(field, value)
        if parsed is None:
            if field.required:
                return None, f"invalid_{field.type}", 0
            warnings += 1
        clean[field.name] = parsed
    return clean, None, warnings


def parse_lead_file(schema: Schema, data: bytes, name: str = "") -> list:
    """-> [(row_number, clean | None, reason | None, warnings)] for non-blank rows."""
    text = data.decode("utf-8-sig", errors="replace")  # also strips a BOM
    reader = csv.reader(io.StringIO(text, newline=""))
    out = []
    try:
        header = next(reader, None)
        if header is None:
            return out
        mapping = map_header(schema, header)
        row_number = 0
        for cells in reader:
            if all(c.strip() == "" for c in cells):  # blank rows: skipped, not counted
                continue
            row_number += 1
            raw = {fname: (cells[i].strip() if i < len(cells) else "")
                   for fname, i in mapping.items()}
            clean, reason, warnings = validate_row(schema, raw)
            out.append((row_number, clean, reason, warnings))
    except csv.Error as exc:
        raise LedgerError(f"{name}: unreadable CSV ({exc})") from exc
    return out


# ------------------------------------------------------------------ persistence
def _apply_defaults(attrs: dict, schema: Schema) -> None:
    for field in schema.attr_fields:
        if attrs.get(field.name) is None and field.default is not None:
            attrs[field.name] = field.default


def ingest_lead_file(conn: sqlite3.Connection, pii: Pii, schema: Schema,
                     path: Path, drop_name: str) -> dict | None:
    """Ingest one lead file atomically. Returns counters, or None if already ingested."""
    path = Path(path)
    data = path.read_bytes()
    file_key = make_file_key("leads", schema.workflow_id, data)
    if file_already_ingested(conn, file_key):
        return None

    rows = parse_lead_file(schema, data, path.name)
    batch_id = path.stem
    phone_name = schema.phone_field.name
    ref_name = schema.reference_field.name
    stats = {"rows": len(rows), "created": 0, "merged": 0, "rejected": 0, "warnings": 0}

    conn.execute("BEGIN IMMEDIATE")
    try:
        if file_already_ingested(conn, file_key):
            conn.execute("ROLLBACK")
            return None
        sfid = conn.execute(
            "INSERT INTO source_files (file_key, kind, workflow_id, batch_id, drop_name,"
            " file_name, rows_total, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
            (file_key, "leads", schema.workflow_id, batch_id, drop_name, path.name,
             len(rows), now_iso())).lastrowid

        audit = []
        for row_number, clean, reason, warnings in rows:
            if clean is None:
                audit.append((sfid, row_number, "rejected", reason, 0, None))
                stats["rejected"] += 1
                continue
            phone = clean[phone_name]
            phone_hash = pii.phone_hash(phone)
            incoming_attrs = {f.name: clean[f.name] for f in schema.attr_fields}
            incoming_pii = {f.name: clean[f.name] for f in schema.pii_fields}
            existing = conn.execute(
                "SELECT lead_id, attrs_json, pii_enc FROM leads"
                " WHERE workflow_id = ? AND phone_hash = ?",
                (schema.workflow_id, phone_hash)).fetchone()

            if existing is None:
                _apply_defaults(incoming_attrs, schema)
                lead_id = conn.execute(
                    "INSERT INTO leads (workflow_id, phone_hash, phone_last4, external_ref,"
                    " batch_id, first_source_file_id, attrs_json, pii_enc)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (schema.workflow_id, phone_hash, pii.phone_last4(phone), clean[ref_name],
                     batch_id, sfid, json.dumps(incoming_attrs, sort_keys=True),
                     pii.encrypt_fields(incoming_pii))).lastrowid
                outcome = "created"
            else:
                lead_id, attrs_json, pii_enc = existing
                attrs = json.loads(attrs_json)          # last write wins, non-null only
                attrs.update({k: v for k, v in incoming_attrs.items() if v is not None})
                _apply_defaults(attrs, schema)
                stored_pii = pii.decrypt_fields(pii_enc)
                merged_pii = {**stored_pii,
                              **{k: v for k, v in incoming_pii.items() if v is not None}}
                # batch_id / first_source_file_id are deliberately never touched.
                conn.execute(
                    "UPDATE leads SET external_ref = ?, attrs_json = ?, pii_enc = ?"
                    " WHERE lead_id = ?",
                    (clean[ref_name], json.dumps(attrs, sort_keys=True),
                     pii.encrypt_fields(merged_pii) if merged_pii != stored_pii else pii_enc,
                     lead_id))
                outcome = "merged"
            stats[outcome] += 1
            stats["warnings"] += warnings
            audit.append((sfid, row_number, outcome, None, warnings, lead_id))

        conn.executemany("INSERT INTO lead_rows VALUES (?,?,?,?,?,?)", audit)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats
