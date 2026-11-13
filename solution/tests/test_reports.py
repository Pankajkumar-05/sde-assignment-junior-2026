import csv
import json

from solution.reports import build_reports, write_reports

from .helpers import Env, ev, lead_row

P1, P2, P3 = "9876543210", "9123456780", "9000000003"


def full_env(tmp_path):
    """Three leads: P1 (renewed after a failed call), P2 (only failed calls), P3 (no calls)."""
    env = Env(tmp_path)
    env.leads([lead_row(ref="A-1", phone=P1), lead_row(ref="A-2", phone=P2),
               lead_row(ref="B-1", phone=P3)])
    env.events([
        # P1: call c1 fails on 3 Oct (IST); call c2 connects on 4 Oct (IST) and renews
        ev("e01", "call.initiated", "c1", "2026-10-03T09:00:00+05:30", phone=P1),
        ev("e02", "call.telephony_failed", "c1", "2026-10-03T09:00:03+05:30", phone=P1, reason="sip_503"),
        ev("e03", "call.initiated", "c2", "2026-10-03T20:00:00Z", phone="+91" + P1),   # 4 Oct 01:30 IST
        ev("e04", "call.connected", "c2", "2026-10-03T20:00:05Z", phone=P1),
        ev("e05", "disposition.set", "c2", "2026-10-03T20:05:00Z", phone=P1, disposition_code="renewed"),
        # P2: two telephony failures on 3 Oct
        ev("e06", "call.initiated", "c3", "2026-10-03 10:00:00", phone=P2),
        ev("e07", "call.telephony_failed", "c3", "2026-10-03 10:00:02", phone=P2, reason="x"),
        ev("e08", "call.initiated", "c4", "2026-10-03 15:00:00", phone=P2),
        ev("e09", "call.telephony_failed", "c4", "2026-10-03 15:00:02", phone=P2, reason="x"),
    ])
    return env


def test_batch_summary_exact_and_buckets_sum_to_total(tmp_path):
    env = full_env(tmp_path)
    text = build_reports(env.conn)["batch_summary.csv"]
    assert text == ("workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,"
                    "in_progress_dialed,completed\nmotor_renewal,batch_001,3,1,1,0,1\n")


def test_daily_overview_uses_ist_dates_and_exact_rate_format(tmp_path):
    env = full_env(tmp_path)
    text = build_reports(env.conn)["daily_overview.csv"]
    assert text == ("workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads\n"
                    "motor_renewal,2026-10-03,3,0,0.0000,0\n"
                    "motor_renewal,2026-10-04,1,1,1.0000,1\n")


def test_connectivity_rate_four_decimals():
    from solution.reports import _bool
    assert f"{9 / 16:.4f}" == "0.5625" and f"{1 / 3:.4f}" == "0.3333" and _bool(True) == "true"


def test_success_leads_counts_distinct_leads_per_date_not_calls(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref="A", phone=P1)])
    evs = []
    for i in range(3):   # three successful calls on the same IST day -> still one lead
        evs += [ev(f"i{i}", "call.initiated", f"c{i}", f"2026-10-03T1{i}:00:00Z", phone=P1),
                ev(f"s{i}", "disposition.set", f"c{i}", f"2026-10-03T1{i}:05:00Z", phone=P1,
                   disposition_code="renewed")]
    env.events(evs)
    row = env.reports()["daily_overview.csv"][0]
    assert (row["total_calls"], row["success_leads"]) == ("3", "1")


def test_success_date_is_the_call_date_even_if_override_comes_later(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref="A", phone=P1)])
    env.events([ev("i", "call.initiated", "c", "2026-10-03T10:00:00Z", phone=P1),
                ev("s", "disposition.set", "c", "2026-10-03T10:05:00Z", phone=P1, disposition_code="callback_requested"),
                ev("o", "disposition.override", "c", "2026-10-09T10:05:00Z", phone=None, disposition_code="renewed")])
    rows = env.reports()["daily_overview.csv"]
    assert [(r["date"], r["success_leads"]) for r in rows] == [("2026-10-03", "1")]


def test_lead_report_exact_rows(tmp_path):
    env = full_env(tmp_path)
    lines = build_reports(env.conn)["lead_report.csv"].splitlines()
    assert lines[0] == ("workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,"
                        "connected_calls,last_call_at,current_disposition,ever_success")
    assert lines[1:] == [
        "motor_renewal,A-1,batch_001,+91******3210,completed,2,1,2026-10-04T01:30:00+05:30,renewed,true",
        "motor_renewal,A-2,batch_001,+91******6780,in_progress_telephony_failed,2,0,"
        "2026-10-03T15:00:00+05:30,,false",
        "motor_renewal,B-1,batch_001,+91******0003,yet_to_reach_out,0,0,,,false"]


def test_current_disposition_is_latest_call_that_has_one(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref="A", phone=P1)])
    env.events([
        ev("i1", "call.initiated", "c1", "2026-10-03T10:00:00Z", phone=P1),
        ev("s1", "disposition.set", "c1", "2026-10-03T10:05:00Z", phone=P1, disposition_code="callback_requested"),
        ev("i2", "call.initiated", "c2", "2026-10-04T10:00:00Z", phone=P1),      # later call, no disposition
        ev("i3", "call.initiated", "c3", "2026-10-02T10:00:00Z", phone=P1),      # earlier call
        ev("s3", "disposition.set", "c3", "2026-10-02T10:05:00Z", phone=P1, disposition_code="language_barrier")])
    row = env.lead("A")
    assert row["current_disposition"] == "callback_requested"
    assert row["last_call_at"] == "2026-10-04T15:30:00+05:30"
    assert row["total_calls"] == "3"


def test_ever_success_true_even_when_latest_call_is_not_success(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref="A", phone=P1)])
    env.events([
        ev("i1", "call.initiated", "c1", "2026-10-03T10:00:00Z", phone=P1),
        ev("s1", "disposition.set", "c1", "2026-10-03T10:05:00Z", phone=P1, disposition_code="renewed"),
        ev("i2", "call.initiated", "c2", "2026-10-04T10:00:00Z", phone=P1),
        ev("s2", "disposition.set", "c2", "2026-10-04T10:05:00Z", phone=P1, disposition_code="callback_requested")])
    row = env.lead("A")
    assert (row["ever_success"], row["current_disposition"], row["bucket"]) == ("true", "callback_requested", "completed")


def test_sorting_is_by_codepoint_not_numeric_or_case_insensitive(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref=r, phone=f"900000000{i}") for i, r in enumerate(["b-2", "B-10", "B-2", "a-1"])])
    assert [r["external_ref"] for r in env.reports()["lead_report.csv"]] == ["B-10", "B-2", "a-1", "b-2"]


def test_rejections_sorted_by_workflow_batch_row(tmp_path):
    env = Env(tmp_path)
    env.leads([["", P1, "x", "", "", ""], ["n", "bad", "y", "", "", ""]], name="batch_002")
    env.leads([["", P1, "x", "", "", ""]], name="batch_001")
    env.leads([["O1", "", "9123456780", "", "", ""]], workflow="policy_feedback", name="batch_001",
              header=["order_id", "customer_name", "phone", "product", "order_date", "language"])
    rows = env.reports()["rejections.csv"]
    assert [(r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]) for r in rows] == [
        ("motor_renewal", "batch_001", "1", "missing_required:customer_name"),
        ("motor_renewal", "batch_002", "1", "missing_required:customer_name"),
        ("motor_renewal", "batch_002", "2", "invalid_phone"),
        ("policy_feedback", "batch_001", "1", "missing_required:customer_name")]


def test_data_quality_exact_keys_and_values(tmp_path):
    env = full_env(tmp_path)
    env.events(None, raw_lines=["garbage", json.dumps(ev("e01", "call.initiated", "c1", "2026-10-03T09:00:00Z", phone=P1))],
               name="events_0002")
    dq = json.loads(build_reports(env.conn)["data_quality.json"])
    assert list(dq) == ["lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
                        "duplicate_lead_rows_merged", "field_warnings", "events_total",
                        "malformed_events", "duplicate_events_dropped", "orphan_events",
                        "unknown_disposition_events"]
    assert dq["lead_rows_total"] == 3 and dq["events_total"] == 11
    assert dq["malformed_events"] == 1 and dq["duplicate_events_dropped"] == 1
    assert dq["rejections_by_reason"] == {}


def test_empty_database_gives_header_only_files(tmp_path):
    env = Env(tmp_path)
    files = build_reports(env.conn)
    assert files["batch_summary.csv"].count("\n") == 1
    assert files["lead_report.csv"].count("\n") == 1
    assert json.loads(files["data_quality.json"])["events_total"] == 0


def test_write_reports_creates_exactly_five_files_with_lf_endings(tmp_path):
    env = full_env(tmp_path)
    out = tmp_path / "nested" / "reports"
    names = write_reports(env.conn, out)
    assert sorted(p.name for p in out.iterdir()) == sorted(names) == [
        "batch_summary.csv", "daily_overview.csv", "data_quality.json", "lead_report.csv",
        "rejections.csv"]
    for p in out.glob("*.csv"):
        raw = p.read_bytes()
        assert b"\r" not in raw and raw.endswith(b"\n")
        assert next(csv.reader(raw.decode().splitlines()))     # header parses
