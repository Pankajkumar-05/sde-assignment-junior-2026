"""End-to-end verification on data from the repo's own generator, on several seeds,
diffed against an independent reference implementation (oracle.py)."""
import csv
import json

import pytest

from solution import db
from solution.pii import Pii
from solution.pipeline import ingest_paths
from solution.reports import build_reports, write_reports
from solution.schema_loader import load_dispositions, load_schemas

from .helpers import SCHEMA_DIR, generated
from .oracle import run_oracle

SEEDS = [1, 7, 2024]


class _Env:
    def __init__(self, tmp_path, key=b"seed-test-key"):
        self.db_path = str(tmp_path / "ledger.db")
        self.pii = Pii(key)
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        db.check_pii_key(self.conn, self.pii.fingerprint)
        db.sync_dispositions(self.conn, load_dispositions(SCHEMA_DIR))
        self.schemas = load_schemas(SCHEMA_DIR)

    def ingest(self, *paths):
        ingest_paths(self.conn, self.pii, self.schemas, paths, log=lambda *_: None)


def ingest_all(tmp_path, drops):
    env = _Env(tmp_path)
    env.ingest(*drops)
    return env


def parsed(files):
    out = {k: list(csv.DictReader(v.splitlines())) for k, v in files.items() if k.endswith(".csv")}
    out["data_quality.json"] = json.loads(files["data_quality.json"])
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_pipeline_matches_independent_oracle(tmp_path, seed):
    data = generated(seed)
    env = ingest_all(tmp_path, [data / "drop_1", data / "drop_2"])
    got = parsed(build_reports(env.conn))
    want = run_oracle([data / "drop_1", data / "drop_2"], SCHEMA_DIR)
    for name in want:
        assert got[name] == want[name], name


@pytest.mark.parametrize("seed", SEEDS)
def test_rerunning_the_whole_drop_changes_nothing(tmp_path, seed):
    data = generated(seed)
    env = ingest_all(tmp_path, [data / "drop_1", data / "drop_2"])
    snapshot_db, snapshot_reports = "\n".join(env.conn.iterdump()), build_reports(env.conn)
    env.ingest(data / "drop_1")
    env.ingest(data / "drop_2")
    env.ingest(data)                       # parent directory form
    assert "\n".join(env.conn.iterdump()) == snapshot_db
    assert build_reports(env.conn) == snapshot_reports


@pytest.mark.parametrize("seed", SEEDS)
def test_incremental_equals_all_at_once(tmp_path, seed):
    data = generated(seed)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    inc = _Env(a)
    inc.ingest(data / "drop_1")
    after_drop1 = build_reports(inc.conn)
    inc.ingest(data / "drop_2")
    once = _Env(b)
    once.ingest(data)                      # both drops in one command
    final_inc, final_once = build_reports(inc.conn), build_reports(once.conn)
    assert final_inc == final_once
    # reports really do change as more is ingested, and orphans reconcile (never grow)
    dq1 = json.loads(after_drop1["data_quality.json"])
    dq2 = json.loads(final_inc["data_quality.json"])
    assert dq2["lead_rows_total"] > dq1["lead_rows_total"]
    assert dq2["events_total"] > dq1["events_total"]


@pytest.mark.parametrize("seed", SEEDS)
def test_different_pii_keys_give_identical_reports(tmp_path, seed):
    data = generated(seed)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    one, two = _Env(a, b"key-one"), _Env(b, b"key-two")
    one.ingest(data), two.ingest(data)
    assert build_reports(one.conn) == build_reports(two.conn)


@pytest.mark.parametrize("seed", SEEDS)
def test_invariants(tmp_path, seed):
    data = generated(seed)
    env = ingest_all(tmp_path, [data / "drop_1", data / "drop_2"])
    r = parsed(build_reports(env.conn))
    dq = r["data_quality.json"]
    leads, batches, daily, rejects = (r["lead_report.csv"], r["batch_summary.csv"],
                                      r["daily_overview.csv"], r["rejections.csv"])

    # every lead appears exactly once, in sorted order
    keys = [(x["workflow_id"], x["external_ref"]) for x in leads]
    assert len(keys) == len(set(keys)) and keys == sorted(keys)
    assert len(leads) == env.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    # batch buckets reconcile with totals and with the lead report
    for b in batches:
        assert sum(int(b[k]) for k in ("yet_to_reach_out", "in_progress_telephony_failed",
                                       "in_progress_dialed", "completed")) == int(b["total_leads"])
    assert sum(int(b["total_leads"]) for b in batches) == len(leads)
    # calls reconcile between the per-lead and per-day views
    assert sum(int(x["total_calls"]) for x in leads) == sum(int(d["total_calls"]) for d in daily)
    assert sum(int(x["connected_calls"]) for x in leads) == sum(int(d["connected_calls"]) for d in daily)
    for d in daily:
        assert 0 <= int(d["connected_calls"]) <= int(d["total_calls"])
        assert d["connectivity_rate"] == f"{int(d['connected_calls']) / int(d['total_calls']):.4f}"
        assert int(d["success_leads"]) <= int(d["total_calls"])
    for x in leads:
        assert int(x["connected_calls"]) <= int(x["total_calls"])
        assert (x["bucket"] == "yet_to_reach_out") == (x["total_calls"] == "0")
        assert (x["last_call_at"] == "") == (x["total_calls"] == "0")
        assert x["phone_masked"].startswith("+91******") and len(x["phone_masked"]) == 13
    # lead-row accounting and rejected rows are not leads
    assert dq["lead_rows_total"] == (len(leads) + dq["duplicate_lead_rows_merged"] + dq["lead_rows_rejected"])
    assert dq["lead_rows_rejected"] == len(rejects) == sum(dq["rejections_by_reason"].values())
    # event accounting
    stored = env.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert dq["events_total"] == dq["malformed_events"] + dq["duplicate_events_dropped"] + stored
    assert dq["orphan_events"] + dq["unknown_disposition_events"] <= stored
    # rejection reasons are only the allowed shapes
    assert all(k == "invalid_phone" or k.startswith("missing_required:") for k in dq["rejections_by_reason"])


def test_report_output_is_deterministic_across_runs(tmp_path):
    data = generated(5)
    env = ingest_all(tmp_path, [data])
    a, b = tmp_path / "r1", tmp_path / "r2"
    write_reports(env.conn, a)
    write_reports(env.conn, b)
    for p in a.iterdir():
        assert p.read_bytes() == (b / p.name).read_bytes()


def test_generated_data_exercises_the_messy_cases(tmp_path):
    data = generated(3)
    env = ingest_all(tmp_path, [data])
    dq = parsed(build_reports(env.conn))["data_quality.json"]
    for key in ("lead_rows_rejected", "duplicate_lead_rows_merged", "field_warnings",
                "malformed_events", "duplicate_events_dropped", "orphan_events",
                "unknown_disposition_events"):
        assert dq[key] > 0, key
