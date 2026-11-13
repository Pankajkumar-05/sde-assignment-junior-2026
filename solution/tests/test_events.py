import json
import sqlite3

import pytest

from .helpers import Env, ev, lead_row

P = "9876543210"


def seeded(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref="P1", phone=P)])
    return env


def test_event_ingestion_stores_and_links(tmp_path):
    env = seeded(tmp_path)
    s = env.events([ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00+05:30"),
                    ev("e2", "call.connected", "c1", "2026-10-03T10:00:10+05:30")])
    assert s == {"lines": 2, "malformed": 0, "duplicates": 0, "stored": 2}
    row = env.lead("P1")
    assert row["total_calls"] == "1" and row["connected_calls"] == "1"
    assert env.reports()["data_quality.json"]["orphan_events"] == 0


def test_malformed_lines_counted_and_never_fatal(tmp_path):
    env = seeded(tmp_path)
    good = json.dumps(ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z"))
    s = env.events(None, raw_lines=[
        good, '{"event_id": "e2", "event_type"', "not json at all", "[1, 2]", '"str"', "42",
        json.dumps({"event_type": "call.initiated"}),       # no event_id
        json.dumps({"event_id": "", "event_type": "x"}),     # empty event_id
        json.dumps({"event_id": None}), "{"])
    assert s["lines"] == 10 and s["malformed"] == 9 and s["stored"] == 1


def test_blank_lines_are_not_counted(tmp_path):
    env = seeded(tmp_path)
    good = json.dumps(ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z"))
    s = env.events(None, raw_lines=["", "   ", good, "\t", ""])
    assert s["lines"] == 1
    assert env.reports()["data_quality.json"]["events_total"] == 1


def test_non_utf8_line_is_malformed_not_a_crash(tmp_path):
    env = seeded(tmp_path)
    d = tmp_path / "drop_1" / "events"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "events_0001.jsonl"
    f.write_bytes(b'\xff\xfe{"event_id": "x"}\n' + json.dumps(
        ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z")).encode() + b"\n")
    from solution.ingest_events import ingest_event_file
    s = ingest_event_file(env.conn, env.pii, f, "drop_1")
    assert s["malformed"] == 1 and s["stored"] == 1


def test_duplicate_event_id_across_files_and_drops_changes_nothing(tmp_path):
    env = seeded(tmp_path)
    e = ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z")
    env.events([e, e])                                           # same file
    s = env.events([e, ev("e2", "call.connected", "c1", "2026-10-03T10:00:09Z")],
                   name="events_1001", drop="drop_2")             # later drop
    assert s["duplicates"] == 1 and s["stored"] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    dq = env.reports()["data_quality.json"]
    assert dq["duplicate_events_dropped"] == 2 and dq["events_total"] == 4
    assert env.lead("P1")["total_calls"] == "1"


def test_orphan_events_also_take_part_in_dedupe(tmp_path):
    env = Env(tmp_path)
    e = ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z", phone="9000000001")  # no such lead
    env.events([e])
    s = env.events([e, ev("e2", "call.connected", "c1", "2026-10-03T10:00:05Z", phone="9000000001")],
                   name="events_0002")
    assert s["duplicates"] == 1 and s["stored"] == 1
    assert env.reports()["data_quality.json"]["orphan_events"] == 2


def test_event_before_lead_is_orphan_then_reconciles(tmp_path):
    env = Env(tmp_path)
    env.events([ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00+05:30", phone="+91 98765 43210"),
                ev("e2", "call.connected", "c1", "2026-10-03T10:00:08+05:30", phone="09876543210"),
                ev("e3", "disposition.set", "c1", "2026-10-03T10:02:00+05:30", disposition_code="renewed"),
                ev("e4", "disposition.override", "c1", "2026-10-04T10:00:00+05:30", phone=None,
                   disposition_code="not_interested", overridden_by="s1", reason="qa")])
    assert env.reports()["data_quality.json"]["orphan_events"] == 4
    assert env.reports()["lead_report.csv"] == []                # no leads, no rows

    env.leads([lead_row(ref="P1", phone=P)], name="batch_003", drop="drop_2")
    dq = env.reports()["data_quality.json"]
    assert dq["orphan_events"] == 0
    row = env.lead("P1")
    assert (row["total_calls"], row["connected_calls"], row["bucket"]) == ("1", "1", "completed")
    assert row["current_disposition"] == "not_interested" and row["ever_success"] == "false"
    # the ledger itself was never edited
    assert env.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4


def test_override_without_known_call_is_orphan(tmp_path):
    env = seeded(tmp_path)
    env.events([ev("e1", "disposition.override", "ghost_call", "2026-10-04T10:00:00Z", phone=None,
                   disposition_code="renewed")])
    assert env.reports()["data_quality.json"]["orphan_events"] == 1


def test_override_links_via_call_even_if_it_arrives_first(tmp_path):
    env = seeded(tmp_path)
    env.events([ev("o1", "disposition.override", "c1", "2026-10-05T10:00:00Z", phone=None,
                   disposition_code="renewed")], name="events_0001")
    assert env.reports()["data_quality.json"]["orphan_events"] == 1
    env.events([ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z"),
                ev("e2", "call.connected", "c1", "2026-10-03T10:00:09Z")],
               name="events_1001", drop="drop_2")
    assert env.reports()["data_quality.json"]["orphan_events"] == 0
    assert env.lead("P1")["current_disposition"] == "renewed"


def test_events_with_missing_or_invalid_phone_are_orphans(tmp_path):
    env = seeded(tmp_path)
    env.events([ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z", phone="12345"),
                ev("e2", "call.initiated", "c2", "2026-10-03T10:00:00Z", phone=None),
                ev("e3", "call.initiated", "c3", "2026-10-03T10:00:00Z", phone="")])
    assert env.reports()["data_quality.json"]["orphan_events"] == 3
    assert env.lead("P1")["total_calls"] == "0"


def test_event_for_other_workflow_does_not_link(tmp_path):
    env = seeded(tmp_path)
    env.events([ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z", wf="policy_feedback")])
    assert env.reports()["data_quality.json"]["orphan_events"] == 1


def test_unknown_disposition_counted_and_ignored(tmp_path):
    env = seeded(tmp_path)
    env.events([
        ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z"),
        ev("e2", "call.connected", "c1", "2026-10-03T10:00:05Z"),
        ev("e3", "disposition.set", "c1", "2026-10-03T10:01:00Z", disposition_code="callback_requested"),
        ev("e4", "disposition.set", "c1", "2026-10-03T10:02:00Z", disposition_code="RENEWD"),
        ev("e5", "disposition.override", "c1", "2026-10-03T11:00:00Z", phone=None, disposition_code="refused"),
        ev("e6", "disposition.set", "c1", "2026-10-03T10:03:00Z", disposition_code=None),
    ])
    dq = env.reports()["data_quality.json"]
    assert dq["unknown_disposition_events"] == 3          # RENEWD, refused (feedback-only), null
    assert dq["orphan_events"] == 0
    row = env.lead("P1")
    assert row["current_disposition"] == "callback_requested"   # unknown latest set/override ignored
    assert row["bucket"] == "in_progress_dialed"


def test_orphan_wins_over_unknown_until_the_lead_arrives(tmp_path):
    env = Env(tmp_path)
    env.events([ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z"),
                ev("e2", "disposition.set", "c1", "2026-10-03T10:01:00Z", disposition_code="bogus")])
    dq = env.reports()["data_quality.json"]
    assert (dq["orphan_events"], dq["unknown_disposition_events"]) == (2, 0)
    env.leads([lead_row(ref="P1", phone=P)], drop="drop_2", name="batch_003")
    dq = env.reports()["data_quality.json"]
    assert (dq["orphan_events"], dq["unknown_disposition_events"]) == (0, 1)


def test_ledger_is_append_only_at_the_database_level(tmp_path):
    env = seeded(tmp_path)
    env.events([ev("e1", "call.initiated", "c1", "2026-10-03T10:00:00Z")])
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        env.conn.execute("UPDATE events SET call_id = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        env.conn.execute("DELETE FROM events")


def test_event_without_optional_fields_is_stored_and_ignored(tmp_path):
    env = seeded(tmp_path)
    s = env.events([{"event_id": "bare"},
                    ev("e2", "call.weird", "c1", "garbage-time"),
                    ev("e3", "call.initiated", "c9", "garbage-time")])
    assert s["stored"] == 3 and s["malformed"] == 0
    assert env.lead("P1")["total_calls"] == "0"          # initiated w/o usable time is not a call
