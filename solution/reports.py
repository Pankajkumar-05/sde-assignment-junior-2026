"""Build the five report files from the database. Pure function of the stored state."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .derive import BUCKETS, Ev, bucket, derive
from .normalize import ist_date, ist_iso
from .pii import Pii

BATCH_COLS = ["workflow_id", "batch_id", "total_leads", *BUCKETS]
DAILY_COLS = ["workflow_id", "date", "total_calls", "connected_calls",
              "connectivity_rate", "success_leads"]
LEAD_COLS = ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
             "connected_calls", "last_call_at", "current_disposition", "ever_success"]
REJECT_COLS = ["workflow_id", "batch_id", "row_number", "reason"]


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _csv_text(columns: list, rows: list) -> str:
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return buf.getvalue()


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _scalar(conn, sql: str) -> int:
    return conn.execute(sql).fetchone()[0] or 0


def build_reports(conn: sqlite3.Connection) -> dict:
    """-> {filename: text}. Runs inside one read transaction for a consistent snapshot."""
    conn.execute("BEGIN")
    try:
        dispositions = {(r[0], r[1]): (bool(r[2]), bool(r[3])) for r in conn.execute(
            "SELECT workflow_id, code, is_terminal, is_success FROM dispositions")}
        leads = conn.execute(
            "SELECT lead_id, workflow_id, phone_hash, phone_last4, external_ref, batch_id"
            " FROM leads").fetchall()
        events = [Ev(*r) for r in conn.execute(
            "SELECT event_id, event_type, workflow_id, call_id, occurred_at_us, phone_hash,"
            " disposition_code FROM events ORDER BY event_pk")]
        rejection_rows = conn.execute(
            "SELECT s.workflow_id, s.batch_id, r.row_number, r.reject_reason"
            " FROM lead_rows r JOIN source_files s USING (source_file_id)"
            " WHERE r.outcome = 'rejected'").fetchall()
        by_reason = dict(conn.execute(
            "SELECT reject_reason, COUNT(*) FROM lead_rows WHERE outcome = 'rejected'"
            " GROUP BY reject_reason").fetchall())
        quality = {
            "lead_rows_total": _scalar(conn, "SELECT COUNT(*) FROM lead_rows"),
            "lead_rows_rejected": _scalar(
                conn, "SELECT COUNT(*) FROM lead_rows WHERE outcome = 'rejected'"),
            "rejections_by_reason": dict(sorted(by_reason.items())),
            "duplicate_lead_rows_merged": _scalar(
                conn, "SELECT COUNT(*) FROM lead_rows WHERE outcome = 'merged'"),
            "field_warnings": _scalar(conn, "SELECT SUM(field_warnings) FROM lead_rows"),
            "events_total": _scalar(
                conn, "SELECT SUM(rows_total) FROM source_files WHERE kind = 'events'"),
            "malformed_events": _scalar(
                conn, "SELECT SUM(malformed_count) FROM source_files WHERE kind = 'events'"),
            "duplicate_events_dropped": _scalar(
                conn, "SELECT SUM(duplicate_count) FROM source_files WHERE kind = 'events'"),
        }
    finally:
        conn.execute("COMMIT")

    lead_index = {(wf, ph): lid for lid, wf, ph, *_ in leads}
    derived = derive(lead_index, events, dispositions)
    quality["orphan_events"] = derived.orphan_events
    quality["unknown_disposition_events"] = derived.unknown_disposition_events

    batch: dict = defaultdict(Counter)
    daily: dict = defaultdict(lambda: {"total": 0, "connected": 0, "success": set()})
    lead_rows = []
    for lead_id, wf, _ph, last4, ref, batch_id in leads:
        calls = derived.calls_by_lead.get(lead_id, [])
        b = bucket(calls, wf, dispositions)
        batch[(wf, batch_id)]["total"] += 1
        batch[(wf, batch_id)][b] += 1

        ever_success = False
        for c in calls:
            is_success = c.code is not None and dispositions[(wf, c.code)][1]
            ever_success = ever_success or is_success
            d = daily[(wf, ist_date(c.initiated_us))]
            d["total"] += 1
            d["connected"] += c.connected
            if is_success:
                d["success"].add(lead_id)
        current = next((c.code for c in reversed(calls) if c.code is not None), None)
        lead_rows.append([
            wf, ref, batch_id, Pii.mask_phone(last4), b, len(calls),
            sum(c.connected for c in calls),
            ist_iso(calls[-1].initiated_us) if calls else "",
            current or "", _bool(ever_success)])

    batch_rows = [[wf, bid, c["total"], *(c[k] for k in BUCKETS)]
                  for (wf, bid), c in sorted(batch.items())]
    daily_rows = [[wf, day, d["total"], d["connected"],
                   f"{d['connected'] / d['total']:.4f}", len(d["success"])]
                  for (wf, day), d in sorted(daily.items())]
    lead_rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    reject_rows = sorted(([wf, bid, rn, reason] for wf, bid, rn, reason in rejection_rows),
                         key=lambda r: (r[0], r[1], r[2]))

    return {
        "batch_summary.csv": _csv_text(BATCH_COLS, batch_rows),
        "daily_overview.csv": _csv_text(DAILY_COLS, daily_rows),
        "lead_report.csv": _csv_text(LEAD_COLS, lead_rows),
        "rejections.csv": _csv_text(REJECT_COLS, reject_rows),
        "data_quality.json": json.dumps(
            {k: quality[k] for k in [
                "lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
                "duplicate_lead_rows_merged", "field_warnings", "events_total",
                "malformed_events", "duplicate_events_dropped", "orphan_events",
                "unknown_disposition_events"]}, indent=2) + "\n",
    }


def write_reports(conn: sqlite3.Connection, out_dir) -> list:
    files = build_reports(conn)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        _write_atomic(out / name, text)
    return sorted(files)
