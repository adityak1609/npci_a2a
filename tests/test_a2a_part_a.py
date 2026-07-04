"""Unit tests for Part A (Phase C) conformance — DB-free logic.

Covers the reconciled vocabulary, payload-dependent state resolution, business
idempotency keys, dispatcher coverage, and the outbound envelope builder.
"""

import pytest

from a2a_models import (
    A2ATaskType,
    A2AErrorCode,
    BlockerPayload,
    ClarificationResponsePayload,
    CounterDecisionPayload,
    QueryPayload,
)
from a2a_audit import compute_business_key_marker
import a2a_state


PART_A = [
    "change_communication", "proposal_acknowledged", "change_acknowledgement",
    "query", "clarification_response", "counter_proposal", "counter_decision",
    "milestone_update", "milestone_status_request", "milestone_status_report",
    "readiness_declaration", "blocker", "blocker_status_update", "blocker_resolution",
]


def test_all_part_a_task_types_exist():
    values = {t.value for t in A2ATaskType}
    assert set(PART_A) <= values


def test_new_error_codes_present():
    assert A2AErrorCode.UNKNOWN_TASK_TYPE == "unknown_task_type"
    assert A2AErrorCode.PAYLOAD_VALIDATION_ERROR == "payload_validation_error"


@pytest.mark.parametrize(
    ("task", "target"),
    [
        ("change_communication", "COMMUNICATED"),
        ("proposal_acknowledged", "RECEIVED"),
        ("change_acknowledgement", "ACCEPTED"),
        ("counter_proposal", "COUNTER_PROPOSAL"),
        ("counter_decision", "COMMUNICATED"),
        ("readiness_declaration", "READY_FOR_CERTIFICATION"),
    ],
)
def test_change_target_states(task, target):
    assert a2a_state.change_target_state(task) == target


def test_milestone_update_is_payload_dependent():
    assert a2a_state.change_target_state("milestone_update", {"milestone": "design", "state": "completed"}) == "DESIGN_COMPLETED"
    assert a2a_state.change_target_state("milestone_update", {"milestone": "coding", "state": "completed"}) == "CODING_COMPLETED"
    assert a2a_state.change_target_state("milestone_update", {"milestone": "testing", "state": "completed"}) == "TESTED"
    # Not completed → no advance
    assert a2a_state.change_target_state("milestone_update", {"milestone": "design", "state": "in_progress"}) is None


def test_blocker_and_status_are_stateless():
    for t in ("blocker", "blocker_status_update", "blocker_resolution",
              "milestone_status_request", "milestone_status_report"):
        assert t in a2a_state.STATELESS_TASK_TYPES
        assert a2a_state.change_target_state(t) is None


def test_business_keys():
    assert compute_business_key_marker("change_acknowledgement", {"change_id": "c1", "version_accepted": 2}) == "change_acknowledgement|c1|2"
    assert compute_business_key_marker("counter_proposal", {"counter_proposal_id": "cp-1"}) == "counter_proposal|cp-1"
    assert compute_business_key_marker("counter_decision", {"in_response_to": "cp-1"}) == "counter_decision|cp-1"
    assert compute_business_key_marker("blocker", {"blocker_id": "b-1"}) == "blocker|b-1"
    assert compute_business_key_marker("blocker_resolution", {"blocker_id": "b-1"}) == "blocker_resolution|b-1"
    assert compute_business_key_marker("milestone_update", {"change_id": "c1", "milestone": "design", "state": "completed"}) == "milestone_update|c1|design|completed"
    assert compute_business_key_marker("readiness_declaration", {"change_id": "c1", "version_implementing": 2}) == "readiness_declaration|c1|2"


def test_dispatcher_covers_every_task_type_and_part_a_is_implemented():
    from agent import external_dispatcher as d
    assert all(t in d.EXTERNAL_HANDLERS for t in A2ATaskType)
    for t in PART_A:
        assert d.EXTERNAL_HANDLERS[t] is not d._not_implemented_handler


def test_payload_spec_aliases():
    q = QueryPayload(change_id="c1", bank_interpretation="x", question="why?")
    assert q.query_text == "why?"
    cr = ClarificationResponsePayload(change_id="c1", response="ans")
    assert cr.response_text == "ans"


def test_payload_validation_error_maps():
    from agent.external_dispatcher import PayloadValidationError, _validate
    with pytest.raises(PayloadValidationError):
        _validate(CounterDecisionPayload, {"change_id": "c1"})  # missing required in_response_to/per_counter


def test_outbound_envelope_builder():
    from services.npci_outbound import build_outbound_envelope
    env = build_outbound_envelope("blocker", {"change_id": "c1", "blocker_id": "b1", "type": "x", "severity": "high"}, change_id="c1")
    assert env["from"] == "mindgate_a2a_agent"
    assert env["task_type"] == "blocker"
    assert env["protocol_version"] == "1.0"
    assert env["message_id"] and env["agent_run_id"] and env["correlation_id"]
    # payload model still validates
    BlockerPayload(**env["payload"])
