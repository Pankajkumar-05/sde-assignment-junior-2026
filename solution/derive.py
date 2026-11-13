"""Derived state: event->lead linking, calls, effective dispositions, lead buckets.

Pure functions over plain data (no database), recomputed from the ledger at report time.
Nothing here is cached or stored, so it always reflects everything ingested so far.
"""
from __future__ import annotations

from collections import defaultdict
from typing import NamedTuple

SET = "disposition.set"
OVERRIDE = "disposition.override"
INITIATED = "call.initiated"
CONNECTED = "call.connected"
TELEPHONY_FAILED = "call.telephony_failed"

BUCKETS = ("yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed")


class Ev(NamedTuple):
    event_id: str
    type: str | None
    workflow_id: str | None
    call_id: str | None
    t: int | None            # UTC epoch microseconds
    phone_hash: str | None
    code: str | None


class Call(NamedTuple):
    call_id: str
    initiated_us: int
    connected: bool
    failed: bool
    code: str | None         # effective disposition code


class Derived(NamedTuple):
    calls_by_lead: dict      # lead_id -> [Call] sorted by (initiated_us, call_id)
    orphan_events: int
    unknown_disposition_events: int


def effective_code(overrides: list, sets: list) -> str | None:
    """Latest override by occurred_at, else latest set, else None.

    Entries are (occurred_at_us, event_id, code); equal timestamps are broken by event_id
    so the result never depends on ingestion order.
    """
    pool = overrides or sets
    return max(pool)[2] if pool else None


def derive(lead_index: dict, events, dispositions: dict) -> Derived:
    """lead_index: (workflow_id, phone_hash) -> lead_id; dispositions: (wf, code) -> (terminal, success)."""
    events = list(events)

    # 1. Non-override events link by (workflow, phone). Remember which lead each call is.
    direct: dict = {}
    call_lead: dict = {}
    for i, e in enumerate(events):
        if e.type == OVERRIDE or e.workflow_id is None or e.phone_hash is None:
            continue
        lead_id = lead_index.get((e.workflow_id, e.phone_hash))
        if lead_id is None:
            continue
        direct[i] = lead_id
        if e.call_id is not None:
            key = (e.workflow_id, e.call_id)
            if key not in call_lead or lead_id < call_lead[key]:  # deterministic on conflict
                call_lead[key] = lead_id

    # 2. Overrides link through their call; classify; group usable events per (lead, call).
    orphans = unknown = 0
    calls: dict = defaultdict(lambda: {"init": [], "connected": False, "failed": False,
                                       "overrides": [], "sets": []})
    for i, e in enumerate(events):
        if e.type == OVERRIDE:
            lead_id = (call_lead.get((e.workflow_id, e.call_id))
                       if e.workflow_id is not None and e.call_id is not None else None)
        else:
            lead_id = direct.get(i)
        if lead_id is None:
            orphans += 1
            continue
        if (e.type or "").startswith("disposition.") and (e.workflow_id, e.code) not in dispositions:
            unknown += 1                                  # counted, then ignored
            continue
        if e.call_id is None:
            continue
        call = calls[(lead_id, e.call_id)]
        if e.type == INITIATED and e.t is not None:
            call["init"].append(e.t)
        elif e.type == CONNECTED:
            call["connected"] = True
        elif e.type == TELEPHONY_FAILED:
            call["failed"] = True
        elif e.type in (SET, OVERRIDE) and e.t is not None:
            call["overrides" if e.type == OVERRIDE else "sets"].append((e.t, e.event_id, e.code))

    by_lead: dict = defaultdict(list)
    for (lead_id, call_id), c in calls.items():
        if not c["init"]:                                 # a call exists only if initiated
            continue
        by_lead[lead_id].append(Call(call_id, min(c["init"]), c["connected"], c["failed"],
                                     effective_code(c["overrides"], c["sets"])))
    for lst in by_lead.values():
        lst.sort(key=lambda c: (c.initiated_us, c.call_id))
    return Derived(dict(by_lead), orphans, unknown)


def bucket(calls: list, workflow_id: str, dispositions: dict) -> str:
    """First match wins: yet_to_reach_out, completed, in_progress_telephony_failed, in_progress_dialed."""
    if not calls:
        return "yet_to_reach_out"
    if any(c.code is not None and dispositions[(workflow_id, c.code)][0] for c in calls):
        return "completed"
    if all(c.failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"
