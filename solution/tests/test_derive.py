from solution.derive import Call, Ev, bucket, derive, effective_code
from solution.normalize import parse_timestamp

DISP = {
    ("w", "renewed"): (True, True), ("w", "callback"): (False, False),
    ("w", "dnd"): (True, False), ("w", "payment"): (False, False),
}
LEADS = {("w", "h1"): 1, ("w", "h2"): 2}


def T(s):
    return parse_timestamp(s)


def E(eid, typ, call, t, phone="h1", code=None, wf="w"):
    return Ev(eid, typ, wf, call, T(t), phone, code)


def calls_for(lead, events):
    return derive(LEADS, events, DISP).calls_by_lead.get(lead, [])


def test_effective_disposition_override_beats_later_set():
    sets = [(5, "s2", "callback")]
    overrides = [(1, "o1", "renewed")]      # override is EARLIER but still wins
    assert effective_code(overrides, sets) == "renewed"


def test_effective_disposition_latest_override_wins():
    assert effective_code([(1, "o1", "renewed"), (3, "o2", "dnd"), (2, "o3", "callback")], []) == "dnd"


def test_effective_disposition_latest_set_when_no_override():
    assert effective_code([], [(1, "s1", "callback"), (9, "s2", "renewed"), (4, "s3", "dnd")]) == "renewed"


def test_effective_disposition_none():
    assert effective_code([], []) is None


def test_equal_timestamps_break_by_event_id_regardless_of_input_order():
    a = [(5, "evt_1", "renewed"), (5, "evt_2", "dnd")]
    assert effective_code([], a) == effective_code([], list(reversed(a))) == "dnd"


def base_call(call="c1", t="2026-10-03T10:00:00Z"):
    return [E("i" + call, "call.initiated", call, t)]


def test_override_links_through_call_and_is_ordered_by_time_not_arrival():
    evs = base_call() + [
        E("s", "disposition.set", "c1", "2026-10-03T10:05:00Z", code="callback"),
        E("o2", "disposition.override", "c1", "2026-10-05T00:00:00Z", phone=None, code="renewed"),
        E("o1", "disposition.override", "c1", "2026-10-04T00:00:00Z", phone=None, code="dnd"),
    ]
    assert calls_for(1, evs)[0].code == "renewed"


def test_override_does_not_attach_to_a_different_workflow_call():
    evs = base_call() + [E("o", "disposition.override", "c1", "2026-10-05T00:00:00Z",
                           phone=None, code="renewed", wf="other")]
    d = derive(LEADS, evs, DISP)
    assert d.orphan_events == 1 and d.calls_by_lead[1][0].code is None


def test_call_requires_initiated_event():
    evs = [E("c", "call.connected", "c1", "2026-10-03T10:00:05Z"),
           E("s", "disposition.set", "c1", "2026-10-03T10:01:00Z", code="renewed")]
    assert calls_for(1, evs) == []
    assert bucket([], "w", DISP) == "yet_to_reach_out"


def test_connected_and_failed_flags_and_multiple_calls_sorted():
    evs = (base_call("c2", "2026-10-04T10:00:00Z") + base_call("c1", "2026-10-03T10:00:00Z")
           + [E("f", "call.telephony_failed", "c1", "2026-10-03T10:00:03Z"),
              E("x", "call.connected", "c2", "2026-10-04T10:00:10Z")])
    calls = calls_for(1, evs)
    assert [c.call_id for c in calls] == ["c1", "c2"]
    assert [(c.failed, c.connected) for c in calls] == [(True, False), (False, True)]


def test_calls_for_other_leads_are_separate():
    evs = base_call("c1") + [E("i2", "call.initiated", "c2", "2026-10-03T10:00:00Z", phone="h2")]
    d = derive(LEADS, evs, DISP)
    assert len(d.calls_by_lead[1]) == 1 and len(d.calls_by_lead[2]) == 1


def test_bucket_precedence():
    ok = Call("a", 1, False, False, None)
    failed = Call("b", 2, False, True, None)
    done = Call("c", 3, True, False, "renewed")
    nonterm = Call("d", 4, True, False, "callback")
    assert bucket([], "w", DISP) == "yet_to_reach_out"
    assert bucket([failed], "w", DISP) == "in_progress_telephony_failed"
    assert bucket([failed, failed], "w", DISP) == "in_progress_telephony_failed"
    assert bucket([failed, ok], "w", DISP) == "in_progress_dialed"
    assert bucket([ok], "w", DISP) == "in_progress_dialed"
    assert bucket([nonterm], "w", DISP) == "in_progress_dialed"
    assert bucket([failed, nonterm], "w", DISP) == "in_progress_dialed"
    assert bucket([failed, done], "w", DISP) == "completed"          # terminal beats failed
    assert bucket([done, ok], "w", DISP) == "completed"


def test_terminal_override_flips_bucket_and_non_terminal_override_unflips_it():
    evs = base_call() + [E("s", "disposition.set", "c1", "2026-10-03T10:05:00Z", code="dnd")]
    assert bucket(calls_for(1, evs), "w", DISP) == "completed"
    evs += [E("o", "disposition.override", "c1", "2026-10-04T10:05:00Z", phone=None, code="payment")]
    assert bucket(calls_for(1, evs), "w", DISP) == "in_progress_dialed"


def test_orphan_and_unknown_counts():
    evs = base_call() + [
        E("a", "call.initiated", "z", "2026-10-03T10:00:00Z", phone="nobody"),
        E("b", "disposition.set", "c1", "2026-10-03T10:05:00Z", code="nope"),
        E("c", "disposition.set", "z", "2026-10-03T10:05:00Z", phone="nobody", code="nope")]
    d = derive(LEADS, evs, DISP)
    assert (d.orphan_events, d.unknown_disposition_events) == (2, 1)


def test_derive_is_independent_of_event_order():
    evs = base_call() + [
        E("s", "disposition.set", "c1", "2026-10-03T10:05:00Z", code="callback"),
        E("o", "disposition.override", "c1", "2026-10-04T10:05:00Z", phone=None, code="renewed"),
        E("x", "call.connected", "c1", "2026-10-03T10:00:04Z")]
    assert derive(LEADS, evs, DISP) == derive(LEADS, list(reversed(evs)), DISP)
