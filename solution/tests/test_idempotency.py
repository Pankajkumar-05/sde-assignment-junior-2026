import json

import pytest

from solution.ingest_events import ingest_event_file
from solution.ingest_leads import ingest_lead_file
from solution.reports import build_reports

from .helpers import Env, ev, lead_row


def populate(env):
    env.leads([lead_row(ref="A", phone="9876543210"), lead_row(ref="A", phone="9876543210", premium="2000"),
               lead_row(name="", ref="B", phone="9123456780"), lead_row(ref="C", phone="9123456781", expiry="soon")])
    good = ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z")
    env.events(None, raw_lines=[json.dumps(good), json.dumps(good), "garbage", "",
                                json.dumps(ev("e2", "disposition.set", "c1", "2026-10-03T10:01:00Z",
                                              disposition_code="bogus")),
                                json.dumps(ev("e3", "call.initiated", "c9", "2026-10-03T10:00:00Z", phone="9000000009"))])


def test_reingesting_same_files_changes_nothing(tmp_path):
    env = Env(tmp_path)
    populate(env)
    before_db, before_reports = env.dump(), build_reports(env.conn)
    assert env.leads([lead_row(ref="A", phone="9876543210"), lead_row(ref="A", phone="9876543210", premium="2000"),
                      lead_row(name="", ref="B", phone="9123456780"), lead_row(ref="C", phone="9123456781", expiry="soon")]) is None
    good = ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z")
    assert env.events(None, raw_lines=[json.dumps(good), json.dumps(good), "garbage", "",
                                       json.dumps(ev("e2", "disposition.set", "c1", "2026-10-03T10:01:00Z",
                                                     disposition_code="bogus")),
                                       json.dumps(ev("e3", "call.initiated", "c9", "2026-10-03T10:00:00Z", phone="9000000009"))]) is None
    assert env.dump() == before_db
    assert build_reports(env.conn) == before_reports


def test_same_content_at_a_different_path_is_still_recognised(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref="A")], drop="drop_1")
    src = tmp_path / "drop_1" / "leads" / "motor_renewal" / "batch_001.csv"
    copy = tmp_path / "elsewhere" / "batch_001.csv"
    copy.parent.mkdir()
    copy.write_bytes(src.read_bytes())
    assert ingest_lead_file(env.conn, env.pii, env.schemas["motor_renewal"], copy, "other") is None


def test_changed_file_content_is_treated_as_new_file(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref="A")])
    s = env.leads([lead_row(ref="A", premium="9")])         # same path, different bytes
    assert s["merged"] == 1


def test_incremental_equals_all_at_once_with_reconciliation(tmp_path):
    def run(sub, split):
        (tmp_path / sub).mkdir()
        env = Env(tmp_path / sub)
        e1 = [ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z"),
              ev("e2", "call.connected", "c1", "2026-10-03T10:00:05Z"),
              ev("e3", "call.initiated", "c8", "2026-10-03T10:00:00Z", phone="9111111111")]
        e2 = [ev("e4", "disposition.set", "c1", "2026-10-03T10:02:00Z", disposition_code="renewed"),
              ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z"),
              ev("e5", "disposition.override", "c1", "2026-10-04T10:00:00Z", phone=None, disposition_code="dnd")]
        steps = [lambda: env.events(e1, name="events_0001", drop="drop_1"),
                 lambda: env.leads([lead_row(ref="A")], name="batch_001", drop="drop_1"),
                 lambda: env.leads([lead_row(ref="A2", premium="5")], name="batch_003", drop="drop_2"),
                 lambda: env.events(e2, name="events_1001", drop="drop_2")]
        for s in steps:
            s()
            if split:
                build_reports(env.conn)      # reporting in between must not disturb anything
        return build_reports(env.conn)
    assert run("a", True) == run("b", False)


def test_crash_mid_lead_file_leaves_nothing_and_rerun_succeeds(tmp_path, monkeypatch):
    env = Env(tmp_path)
    calls = {"n": 0}
    real = env.pii.encrypt_fields

    def boom(values):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash")
        return real(values)
    monkeypatch.setattr(env.pii, "encrypt_fields", boom)
    rows = [lead_row(ref=f"R{i}", phone=f"90000000{i:02d}") for i in range(5)]
    with pytest.raises(RuntimeError):
        env.leads(rows)
    for table in ("leads", "lead_rows", "source_files"):
        assert env.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.setattr(env.pii, "encrypt_fields", real)
    assert env.leads(rows)["created"] == 5


def test_crash_mid_event_file_leaves_nothing_and_rerun_succeeds(tmp_path, monkeypatch):
    env = Env(tmp_path)
    import solution.ingest_events as ie
    real = ie.parse_timestamp
    seen = {"n": 0}

    def boom(raw):
        seen["n"] += 1
        if seen["n"] == 4:
            raise RuntimeError("simulated crash")
        return real(raw)
    monkeypatch.setattr(ie, "parse_timestamp", boom)
    events = [ev(f"e{i}", "call.initiated", f"c{i}", "2026-10-03T10:00:00Z") for i in range(6)]
    with pytest.raises(RuntimeError):
        env.events(events)
    assert env.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert env.conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 0
    monkeypatch.setattr(ie, "parse_timestamp", real)
    assert env.events(events)["stored"] == 6


def test_dispositions_sync_is_a_noop_when_unchanged(tmp_path):
    env = Env(tmp_path)
    from solution import db
    from solution.schema_loader import load_dispositions
    from .helpers import SCHEMA_DIR
    env.conn.set_trace_callback(None)
    before = env.dump()
    db.sync_dispositions(env.conn, load_dispositions(SCHEMA_DIR))
    assert env.dump() == before
