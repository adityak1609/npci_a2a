"""JSON-RPC method name constants for Mindgate A2A internal admin API.

These custom method names are used ONLY on /a2a/internal (admin-frontend).
/a2a/external uses spec task_type dispatch — see agent/external_dispatcher.py.
"""

GET_SUMMARY = "agent.get_summary"
QUERY_RAG = "agent.query_rag"
ESCALATE_TO_NPCI = "agent.escalate_to_npci"

# Part A (Phase C) outbound Bank→NPCI actions, fired from the portal.
SUBMIT_COUNTER_PROPOSAL = "agent.submit_counter_proposal"
REPORT_MILESTONE = "agent.report_milestone"
DECLARE_READINESS = "agent.declare_readiness"
RAISE_BLOCKER = "agent.raise_blocker"
