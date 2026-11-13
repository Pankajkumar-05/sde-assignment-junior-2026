"""Shared test helpers: an isolated environment, tiny fixture writers, generated-data cache."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from solution import db
from solution.ingest_events import ingest_event_file
from solution.ingest_leads import ingest_lead_file
from solution.pii import Pii
from solution.reports import build_reports
from solution.schema_loader import load_dispositions, load_schemas

REPO = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO / "assignment-a-data" / "schemas"
GENERATOR = REPO / "assignment-a-data" / "tools" / "generate_data.py"

MOTOR_HEADER = ["customer_name", "phone", "policy_number", "policy_expiry",
                "premium_amount", "preferred_language"]


class Env:
    """A fresh database + fixed test key, with helpers to ingest synthetic files."""

    def __init__(self, tmp_path: Path, key: bytes = b"unit-test-key"):
        self.tmp = Path(tmp_path)
        self.db_path = str(self.tmp / "ledger.db")
        self.pii = Pii(key)
        self.schemas = load_schemas(SCHEMA_DIR)
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        db.check_pii_key(self.conn, self.pii.fingerprint)
        db.sync_dispositions(self.conn, load_dispositions(SCHEMA_DIR))
        self._n = 0

    def leads(self, rows, workflow="motor_renewal", name="batch_001", header=None,
              drop="drop_1", raw_bytes=None):
        d = self.tmp / drop / "leads" / workflow
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.csv"
        if raw_bytes is not None:
            path.write_bytes(raw_bytes)
        else:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(header or MOTOR_HEADER)
                w.writerows(rows)
        return ingest_lead_file(self.conn, self.pii, self.schemas[workflow], path, drop)

    def events(self, events, name="events_0001", drop="drop_1", raw_lines=None):
        d = self.tmp / drop / "events"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.jsonl"
        lines = raw_lines if raw_lines is not None else [json.dumps(e) for e in events]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ingest_event_file(self.conn, self.pii, path, drop)

    def reports(self):
        """-> dict of parsed reports."""
        files = build_reports(self.conn)
        out = {k: list(csv.DictReader(v.splitlines())) for k, v in files.items() if k.endswith(".csv")}
        out["data_quality.json"] = json.loads(files["data_quality.json"])
        return out

    def lead(self, ref, workflow="motor_renewal"):
        rows = self.reports()["lead_report.csv"]
        return next(r for r in rows if r["workflow_id"] == workflow and r["external_ref"] == ref)

    def dump(self) -> str:
        return "\n".join(self.conn.iterdump())


def ev(event_id, etype, call_id, t, phone="9876543210", wf="motor_renewal", **payload):
    """Build an event dict. phone=None omits the key (as overrides do)."""
    e = {"event_id": event_id, "event_type": etype, "workflow_id": wf, "call_id": call_id,
         "occurred_at": t, "payload": payload}
    if phone is not None:
        e["phone"] = phone
    return e


def lead_row(name="Test Person", phone="9876543210", ref="POL-1", expiry="2026-12-01",
             premium="1000", lang="en"):
    return [name, phone, ref, expiry, premium, lang]


_GEN_CACHE: dict = {}
_GEN_DIR = tempfile.TemporaryDirectory(prefix="ledger-gen-")


def generated(seed: int, scale: str = "small") -> Path:
    """Run the repo's own generator once per (seed, scale) and cache the output directory."""
    key = (seed, scale)
    if key not in _GEN_CACHE:
        out = Path(_GEN_DIR.name) / f"{scale}_{seed}"
        subprocess.run([sys.executable, str(GENERATOR), "--seed", str(seed), "--scale", scale,
                        "--out", str(out)], check=True, capture_output=True)
        _GEN_CACHE[key] = out
    return _GEN_CACHE[key]
