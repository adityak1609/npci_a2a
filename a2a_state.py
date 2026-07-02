"""
a2a_state.py — Change and Certification lifecycle state machines (spec §6).

Each state machine is defined as an adjacency map (current_state → set of
valid next states).  A ``task_type_to_target_state`` mapping links each
incoming message type to the state it transitions the entity into.

Usage:
    validate_transition(envelope, db) → raises ValueError on invalid transition
    advance_state(envelope, db)       → validates AND persists the new state
"""

from __future__ import annotations

from a2a_models import (
    A2ATaskType,
    ChangeLifecycleState as CLS,
    CertLifecycleState as CERT,
)
from database import A2AChange, A2ACflow


# ---------------------------------------------------------------------------
# Change lifecycle transitions (spec §6)
# ---------------------------------------------------------------------------
# Any state can also transition to ABORTED — handled specially in the checker.

CHANGE_TRANSITIONS: dict[CLS, set[CLS]] = {
    CLS.ASSIGNED:                   {CLS.COMMUNICATED},
    CLS.COMMUNICATED:               {CLS.RECEIVED},
    CLS.RECEIVED:                   {CLS.ACCEPTED, CLS.COUNTER_PROPOSAL},
    CLS.ACCEPTED:                   {CLS.DESIGN_COMPLETED},
    CLS.DESIGN_COMPLETED:           {CLS.CODING_COMPLETED},
    CLS.CODING_COMPLETED:           {CLS.TESTED},
    CLS.TESTED:                     {CLS.READY_FOR_CERTIFICATION},
    CLS.READY_FOR_CERTIFICATION:    {CLS.CERTIFYING},
    CLS.CERTIFYING:                 {CLS.CERTIFIED},
    CLS.COUNTER_PROPOSAL:           {CLS.COMMUNICATED, CLS.ABORTED},
    CLS.CERTIFIED:                  set(),  # Terminal state
    CLS.ABORTED:                    set(),  # Terminal state
}


# ---------------------------------------------------------------------------
# Cert lifecycle transitions (spec §6)
# ---------------------------------------------------------------------------

CERT_TRANSITIONS: dict[CERT, set[CERT]] = {
    CERT.NOT_STARTED:       {CERT.CONFIG_REQUESTED},
    CERT.CONFIG_REQUESTED:  {CERT.CONFIG_RECEIVED},
    CERT.CONFIG_RECEIVED:   {CERT.SETUP},
    CERT.SETUP:             {CERT.RUNNING},
    CERT.RUNNING:           {CERT.TRIAGE_PENDING, CERT.COMPLETED},
    CERT.TRIAGE_PENDING:    {CERT.FIX_PENDING, CERT.WAIVER_PENDING, CERT.DISPUTE_PENDING},
    CERT.FIX_PENDING:       {CERT.RUNNING},
    CERT.WAIVER_PENDING:    {CERT.RUNNING, CERT.COMPLETED},
    CERT.DISPUTE_PENDING:   {CERT.RUNNING, CERT.COMPLETED},
    CERT.COMPLETED:         set(),  # Terminal
    CERT.ABORTED:           set(),  # Terminal
}


# ---------------------------------------------------------------------------
# task_type → target state mappings
# ---------------------------------------------------------------------------
# Maps each task_type to the Change lifecycle state it moves the change into.
# Only task_types that affect the Change lifecycle are listed.

CHANGE_TASK_TARGET: dict[str, CLS] = {
    A2ATaskType.CHANGE_COMMUNICATION:       CLS.COMMUNICATED,
    A2ATaskType.PROPOSAL_ACKNOWLEDGED:      CLS.RECEIVED,
    A2ATaskType.COUNTER_PROPOSAL:           CLS.COUNTER_PROPOSAL,
    A2ATaskType.COUNTER_PROPOSAL_RESPONSE:  CLS.COMMUNICATED,
    A2ATaskType.CHANGE_ACCEPTED:            CLS.ACCEPTED,
    A2ATaskType.DESIGN_SUBMISSION:          CLS.DESIGN_COMPLETED,
    A2ATaskType.DESIGN_REVIEW:              CLS.DESIGN_COMPLETED,  # Stays in same state
    A2ATaskType.CODING_COMPLETE:            CLS.CODING_COMPLETED,
    A2ATaskType.TEST_RESULTS:               CLS.TESTED,
    A2ATaskType.CHANGE_REJECTED:            CLS.ABORTED,
    A2ATaskType.CHANGE_ABORTED:             CLS.ABORTED,
}

# Maps each task_type to the Cert lifecycle state it moves the cflow into.
CERT_TASK_TARGET: dict[str, CERT] = {
    A2ATaskType.CERT_CONFIG_REQUEST:        CERT.CONFIG_REQUESTED,
    A2ATaskType.CERT_CONFIG_RESPONSE:       CERT.CONFIG_RECEIVED,
    A2ATaskType.CERT_SETUP_COMPLETE:        CERT.SETUP,
    A2ATaskType.CERT_CASE_RESULT:           CERT.RUNNING,
    A2ATaskType.CERT_CASE_TRIAGE:           CERT.TRIAGE_PENDING,
    A2ATaskType.CERT_FIX_SUBMITTED:         CERT.FIX_PENDING,
    A2ATaskType.CERT_WAIVER_REQUEST:        CERT.WAIVER_PENDING,
    A2ATaskType.CERT_WAIVER_RESPONSE:       CERT.WAIVER_PENDING,
    A2ATaskType.CERT_DISPUTE_RAISED:        CERT.DISPUTE_PENDING,
    A2ATaskType.CERT_DISPUTE_RESOLUTION:    CERT.DISPUTE_PENDING,
    A2ATaskType.CERT_COMPLETED:             CERT.COMPLETED,
    A2ATaskType.CERT_ABORTED:               CERT.ABORTED,
}

# Task types that are "stateless" — they don't transition the lifecycle.
STATELESS_TASK_TYPES: set[str] = {
    A2ATaskType.ECHO,
    A2ATaskType.STATUS_REQUEST,
    A2ATaskType.STATUS_RESPONSE,
    A2ATaskType.NOTIFICATION,
    A2ATaskType.QUERY,
    A2ATaskType.CLARIFICATION_RESPONSE,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_valid_change_transition(current: CLS, target: CLS) -> bool:
    """Check if a change lifecycle transition is valid."""
    if target == CLS.ABORTED:
        # Any non-terminal state can abort
        return current not in {CLS.CERTIFIED, CLS.ABORTED}
    valid_targets = CHANGE_TRANSITIONS.get(current, set())
    return target in valid_targets


def _is_valid_cert_transition(current: CERT, target: CERT) -> bool:
    """Check if a cert lifecycle transition is valid."""
    if target == CERT.ABORTED:
        return current not in {CERT.COMPLETED, CERT.ABORTED}
    valid_targets = CERT_TRANSITIONS.get(current, set())
    return target in valid_targets


def validate_transition(envelope, db) -> None:
    """Validate that the envelope's task_type is a legal transition.

    Raises ValueError with a descriptive message if the transition is invalid.
    Does nothing for stateless task types or task types not in the state machine.
    """
    from a2a_models import A2AEnvelope  # Avoid circular at module level

    task_type = envelope.task_type

    if task_type in STATELESS_TASK_TYPES:
        return

    # --- Change lifecycle ---
    if task_type in CHANGE_TASK_TARGET:
        change_id = envelope.change_id
        if not change_id:
            return  # No change_id — can't validate, let handler decide

        change = db.query(A2AChange).filter(A2AChange.change_id == change_id).first()

        # For change_communication, the change may not exist yet (it creates one)
        if task_type == A2ATaskType.CHANGE_COMMUNICATION:
            if not change:
                return  # Will be created by handler
            # If change exists, validate version bump (re-communication)
            current = CLS(change.current_state)
            target = CHANGE_TASK_TARGET[task_type]
            # Re-communication is valid from COUNTER_PROPOSAL (v+1)
            if current == CLS.COUNTER_PROPOSAL:
                return  # Valid: counter-proposal → re-communicated
            # Also valid as the initial communication (ASSIGNED → COMMUNICATED)
            if not _is_valid_change_transition(current, target):
                raise ValueError(
                    f"Invalid state transition for change '{change_id}': "
                    f"{current} → {target} via {task_type}"
                )
            return

        if not change:
            raise ValueError(
                f"Change '{change_id}' not found for task_type '{task_type}'."
            )

        current = CLS(change.current_state)
        target = CHANGE_TASK_TARGET[task_type]
        if not _is_valid_change_transition(current, target):
            raise ValueError(
                f"Invalid state transition for change '{change_id}': "
                f"{current} → {target} via {task_type}"
            )
        return

    # --- Cert lifecycle ---
    if task_type in CERT_TASK_TARGET:
        cflow_id = envelope.cflow_id
        if not cflow_id:
            return  # No cflow_id — can't validate

        cflow = db.query(A2ACflow).filter(A2ACflow.cflow_id == cflow_id).first()

        # cert_config_request may create a new cflow
        if task_type == A2ATaskType.CERT_CONFIG_REQUEST:
            if not cflow:
                # Validate that the associated change is READY_FOR_CERTIFICATION
                change_id = envelope.change_id
                if change_id:
                    change = db.query(A2AChange).filter(
                        A2AChange.change_id == change_id
                    ).first()
                    if change and CLS(change.current_state) != CLS.READY_FOR_CERTIFICATION:
                        raise ValueError(
                            f"Cannot start certification for change '{change_id}': "
                            f"change is in state {change.current_state}, "
                            f"expected READY_FOR_CERTIFICATION."
                        )
                return  # Will be created by handler

        if not cflow:
            raise ValueError(
                f"Certification flow '{cflow_id}' not found for task_type '{task_type}'."
            )

        current = CERT(cflow.current_state)
        target = CERT_TASK_TARGET[task_type]
        if not _is_valid_cert_transition(current, target):
            raise ValueError(
                f"Invalid state transition for cflow '{cflow_id}': "
                f"{current} → {target} via {task_type}"
            )


# ---------------------------------------------------------------------------
# State advancement (validate + persist)
# ---------------------------------------------------------------------------

def advance_change_state(change_id: str, task_type: str, db) -> CLS:
    """Advance the change lifecycle state after successful handler execution.

    Returns the new state.  Creates the change record if it doesn't exist
    (for change_communication creating a new change).
    """
    target = CHANGE_TASK_TARGET.get(task_type)
    if not target:
        return None  # Not a change-lifecycle task type

    change = db.query(A2AChange).filter(A2AChange.change_id == change_id).first()
    if not change:
        # Create a new change (e.g., first change_communication)
        from datetime import datetime, timezone
        change = A2AChange(
            change_id=change_id,
            current_state=target.value,
        )
        db.add(change)
        db.commit()
        db.refresh(change)
        return target

    change.current_state = target.value
    from datetime import datetime, timezone
    change.updated_at = datetime.now(timezone.utc)
    db.commit()
    return target


def advance_cert_state(cflow_id: str, task_type: str, db, change_id: str = None) -> CERT:
    """Advance the cert lifecycle state after successful handler execution.

    Returns the new state.  Creates the cflow record if needed.
    """
    target = CERT_TASK_TARGET.get(task_type)
    if not target:
        return None

    cflow = db.query(A2ACflow).filter(A2ACflow.cflow_id == cflow_id).first()
    if not cflow:
        from datetime import datetime, timezone
        cflow = A2ACflow(
            cflow_id=cflow_id,
            change_id=change_id,
            current_state=target.value,
        )
        db.add(cflow)
        db.commit()
        db.refresh(cflow)
        return target

    cflow.current_state = target.value
    from datetime import datetime, timezone
    cflow.updated_at = datetime.now(timezone.utc)
    db.commit()
    return target
