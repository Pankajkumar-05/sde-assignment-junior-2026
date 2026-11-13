"""Load workflow lead schemas and the disposition catalogue from the schemas directory."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .config import LedgerError
from .normalize import normalize_header

FIELD_TYPES = {"string", "phone", "date", "decimal", "enum"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool
    pii: bool
    reference: bool
    values: tuple
    default: str | None
    match_names: frozenset  # normalised field name + aliases


@dataclass(frozen=True)
class Schema:
    workflow_id: str
    fields: tuple  # in schema order: this is the validation order

    @property
    def phone_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    @property
    def pii_fields(self) -> tuple:
        """PII fields other than the phone (the phone is stored as hash + last 4)."""
        return tuple(f for f in self.fields if f.pii and f.type != "phone")

    @property
    def attr_fields(self) -> tuple:
        """Plain (non-PII, non-key) fields, stored as JSON on the lead."""
        return tuple(f for f in self.fields
                     if f.type != "phone" and not f.pii and not f.reference)


def _parse_schema(path: Path) -> Schema:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields = []
        for f in raw["fields"]:
            if f["type"] not in FIELD_TYPES:
                raise LedgerError(f"{path.name}: unsupported field type {f['type']!r}")
            names = [f["name"], *f.get("aliases", [])]
            fields.append(Field(
                name=f["name"], type=f["type"], required=bool(f.get("required", False)),
                pii=bool(f.get("pii", False)), reference=bool(f.get("reference", False)),
                values=tuple(f.get("values", ())), default=f.get("default"),
                match_names=frozenset(normalize_header(n) for n in names)))
        schema = Schema(workflow_id=raw["workflow_id"], fields=tuple(fields))
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerError(f"{path.name}: invalid schema file ({exc})") from exc
    if sum(f.type == "phone" for f in schema.fields) != 1:
        raise LedgerError(f"{path.name}: schema needs exactly one phone field")
    if sum(f.reference for f in schema.fields) != 1:
        raise LedgerError(f"{path.name}: schema needs exactly one reference field")
    return schema


def load_schemas(schema_dir: Path) -> dict:
    """workflow_id -> Schema, for every *.json file in the directory."""
    schemas = {}
    for path in sorted(Path(schema_dir).glob("*.json")):
        schema = _parse_schema(path)
        schemas[schema.workflow_id] = schema
    if not schemas:
        raise LedgerError(f"no schema files found in {schema_dir}")
    return schemas


def load_dispositions(schema_dir: Path) -> dict:
    """(workflow_id, code) -> (label, is_terminal, is_success)."""
    path = Path(schema_dir) / "dispositions.csv"
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return {
                (r["workflow_id"], r["code"]): (
                    r["label"], r["is_terminal"].strip().lower() == "true",
                    r["is_success"].strip().lower() == "true")
                for r in csv.DictReader(fh)}
    except (OSError, KeyError) as exc:
        raise LedgerError(f"cannot read {path}: {exc}") from exc
