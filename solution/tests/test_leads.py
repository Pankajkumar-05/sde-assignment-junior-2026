import json
import sqlite3

import pytest

from .helpers import Env, MOTOR_HEADER, lead_row


def lead_rows(env):
    return env.conn.execute(
        "SELECT row_number, outcome, reject_reason, field_warnings FROM lead_rows"
        " ORDER BY source_file_id, row_number").fetchall()


def test_normal_ingestion(tmp_path):
    env = Env(tmp_path)
    s = env.leads([lead_row(ref="P1"), lead_row(phone="9123456780", ref="P2")])
    assert s == {"rows": 2, "created": 2, "merged": 0, "rejected": 0, "warnings": 0}
    assert env.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2
    batch = env.reports()["batch_summary.csv"]
    assert batch == [{"workflow_id": "motor_renewal", "batch_id": "batch_001", "total_leads": "2",
                      "yet_to_reach_out": "2", "in_progress_telephony_failed": "0",
                      "in_progress_dialed": "0", "completed": "0"}]


def test_reordered_headers_aliases_and_extra_columns(tmp_path):
    env = Env(tmp_path)
    header = ["Language", "Insured_Name", "agent_notes", "POLICY NO", "Mobile  No", "Expiry Date",
              "Premium", "Unknown Column"]
    s = env.leads([["HI", "A B", "vip", "P-9", "9876543210", "01-Dec-2026", "₹5,000", "x"]],
                  header=header, name="batch_003")
    assert s["created"] == 1 and s["rejected"] == 0 and s["warnings"] == 0
    row = env.conn.execute("SELECT external_ref, attrs_json FROM leads").fetchone()
    assert row[0] == "P-9"
    assert json.loads(row[1]) == {"policy_expiry": "2026-12-01", "preferred_language": "hi",
                                  "premium_amount": "5000.00"}


def test_feedback_workflow_uses_its_own_reference_and_aliases(tmp_path):
    env = Env(tmp_path)
    env.leads([["ORD-1", "Pat", "9876543210", "Phone", "2026-09-01", "ta"]],
              workflow="policy_feedback",
              header=["Order ID", "Name", "Contact", "Item", "Purchase Date", "Language"])
    assert env.reports()["lead_report.csv"][0]["external_ref"] == "ORD-1"


def test_utf8_bom_does_not_break_first_header(tmp_path):
    env = Env(tmp_path)
    content = "\ufeffcustomer_name,phone,policy_number\nAnn,9876543210,P1\n".encode("utf-8")
    s = env.leads(None, raw_bytes=content)
    assert s["created"] == 1 and s["rejected"] == 0


def test_missing_required_column_rejects_rows_not_the_file(tmp_path):
    env = Env(tmp_path)
    content = b"customer_name,phone\nAnn,9876543210\nBob,9123456780\n"
    s = env.leads(None, raw_bytes=content)
    assert s["rejected"] == 2 and s["created"] == 0
    assert [r[2] for r in lead_rows(env)] == ["missing_required:policy_number"] * 2


def test_blank_rows_skipped_and_not_counted_in_row_numbers(tmp_path):
    env = Env(tmp_path)
    content = ("customer_name,phone,policy_number\r\n"
               ",,\r\n"
               "Ann,9876543210,P1\r\n"
               "   , ,  \r\n"
               "\r\n"
               ",,\r\n"
               ",9876543210,P2\r\n").encode()
    env.leads(None, raw_bytes=content)
    assert lead_rows(env) == [(1, "created", None, 0),
                              (2, "rejected", "missing_required:customer_name", 0)]
    assert env.reports()["data_quality.json"]["lead_rows_total"] == 2


def test_first_failure_wins_in_schema_order(tmp_path):
    env = Env(tmp_path)
    env.leads([
        ["", "bad", "P1", "", "", ""],         # name missing AND phone invalid -> name first
        ["Ann", "bad", "", "", "", ""],        # phone invalid AND ref missing -> phone first
        ["Ann", "", "P3", "", "", ""],         # phone absent -> missing_required:phone
        ["Ann", "9876543210", "", "", "", ""],  # only the ref is missing
        ["  ", "9876543210", "P5", "", "", ""],  # whitespace-only name counts as missing
    ])
    assert [r[2] for r in lead_rows(env)] == [
        "missing_required:customer_name", "invalid_phone", "missing_required:phone",
        "missing_required:policy_number", "missing_required:customer_name"]
    assert env.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_rejected_rows_have_no_warnings_even_with_bad_optionals(tmp_path):
    env = Env(tmp_path)
    s = env.leads([["Ann", "bad", "P1", "TBD", "N/A", "klingon"]])
    assert s["rejected"] == 1 and s["warnings"] == 0
    assert env.reports()["data_quality.json"]["field_warnings"] == 0


def test_optional_invalid_becomes_null_with_one_warning_each(tmp_path):
    env = Env(tmp_path)
    s = env.leads([["Ann", "9876543210", "P1", "31/02/2026", "twelve", "Hindi"]])
    assert s["warnings"] == 3
    attrs = json.loads(env.conn.execute("SELECT attrs_json FROM leads").fetchone()[0])
    assert attrs["policy_expiry"] is None and attrs["premium_amount"] is None
    assert attrs["preferred_language"] == "en"   # default fills the nulled field


def test_missing_optionals_are_not_warnings(tmp_path):
    env = Env(tmp_path)
    s = env.leads([["Ann", "9876543210", "P1", "", "  ", ""]])
    assert s["warnings"] == 0
    attrs = json.loads(env.conn.execute("SELECT attrs_json FROM leads").fetchone()[0])
    assert attrs["preferred_language"] == "en"
    assert attrs["policy_expiry"] is None


def test_enum_stored_lowercase_and_trimmed(tmp_path):
    env = Env(tmp_path)
    env.leads([["Ann", "9876543210", "P1", "", "", " HI "]])
    attrs = json.loads(env.conn.execute("SELECT attrs_json FROM leads").fetchone()[0])
    assert attrs["preferred_language"] == "hi"


def test_phone_formats_collapse_to_one_identity(tmp_path):
    env = Env(tmp_path)
    s = env.leads([lead_row(phone=p, ref=f"P{i}") for i, p in enumerate(
        ["9876543210", "+91 98765 43210", "09876543210", "91-98765-43210", "(+91) 9876543210"])])
    assert s["created"] == 1 and s["merged"] == 4
    assert env.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1


def test_duplicate_merge_last_non_null_wins_and_nulls_do_not_erase(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(name="Old Name", ref="OLD", expiry="2026-12-01", premium="1000", lang="hi")])
    s = env.leads([lead_row(name="New Name", ref="NEW", expiry="", premium="2,500", lang="Klingon")],
                  name="batch_002")
    assert s["merged"] == 1 and s["created"] == 0 and s["warnings"] == 1  # bad language = warning
    row = env.conn.execute("SELECT external_ref, attrs_json, pii_enc FROM leads").fetchone()
    assert row[0] == "NEW"
    attrs = json.loads(row[1])
    assert attrs == {"policy_expiry": "2026-12-01",      # empty incoming did not erase
                     "premium_amount": "2500.00",         # non-null incoming overwrote
                     "preferred_language": "hi"}          # invalid incoming (null) did not erase
    assert env.pii.decrypt_fields(row[2]) == {"customer_name": "New Name"}


def test_batch_id_is_permanent_and_merges_are_counted(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref="A")], name="batch_001")
    env.leads([lead_row(ref="A2")], name="batch_002", drop="drop_2")
    assert env.reports()["lead_report.csv"][0]["batch_id"] == "batch_001"
    dq = env.reports()["data_quality.json"]
    assert dq["duplicate_lead_rows_merged"] == 1 and dq["lead_rows_total"] == 2
    # the same stem in another workflow is a different batch
    env.leads([["O1", "Pat", "9123456780", "", "", ""]], workflow="policy_feedback",
              header=["order_id", "customer_name", "phone", "product", "order_date", "language"])
    assert {(r["workflow_id"], r["batch_id"]) for r in env.reports()["batch_summary.csv"]} == {
        ("motor_renewal", "batch_001"), ("policy_feedback", "batch_001")}


def test_rejected_first_then_valid_creates_lead_in_the_later_batch(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(name="", ref="P1")], name="batch_001")
    s = env.leads([lead_row(ref="P1")], name="batch_002")
    assert s["created"] == 1 and s["merged"] == 0
    assert env.reports()["lead_report.csv"][0]["batch_id"] == "batch_002"


def test_duplicate_warnings_count_but_rejected_duplicates_do_not_merge(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row(ref="P1")])
    s = env.leads([lead_row(ref="P1", expiry="soon"), lead_row(name="", ref="P1")], name="batch_002")
    assert s == {"rows": 2, "created": 0, "merged": 1, "rejected": 1, "warnings": 1}
    assert env.reports()["data_quality.json"]["duplicate_lead_rows_merged"] == 1


def test_ragged_rows_and_quoted_commas(tmp_path):
    env = Env(tmp_path)
    content = b'customer_name,phone,policy_number,premium_amount\n"Ann, Jr",9876543210,P1,"\xe2\x82\xb91,000"\nBob,9123456780\n'
    s = env.leads(None, raw_bytes=content)
    assert s["created"] == 1 and s["rejected"] == 1   # Bob's row is short -> missing policy_number


def test_empty_and_header_only_files(tmp_path):
    env = Env(tmp_path)
    assert env.leads(None, raw_bytes=b"")["rows"] == 0
    assert env.leads(None, raw_bytes=b"customer_name,phone,policy_number\n", name="batch_002")["rows"] == 0


def test_lead_rows_check_constraints_hold(tmp_path):
    env = Env(tmp_path)
    env.leads([lead_row()])
    with pytest.raises(sqlite3.IntegrityError):
        env.conn.execute("INSERT INTO lead_rows VALUES (1, 99, 'rejected', NULL, 0, NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        env.conn.execute("INSERT INTO lead_rows VALUES (1, 99, 'rejected', 'x', 2, NULL)")
