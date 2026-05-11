"""
Agent Graph — Orchestration (LangGraph)

Wires all the modules into one state machine. This is the
"single agent, multiple brains" architecture from the deck.

Flow:
  [ingest] -> [validate_schema] -> [run_rules] -> [detect_anomalies]
            -> [route_decisions] -> [apply_corrections]
            -> [supervisor_check] -> {ok: write_outputs | hitl: notify_and_pause}
            -> [audit_log] -> [generate_report]

Why LangGraph and not plain Python:
  - State machine is explicit, not hidden in if/else
  - Conditional branching (HITL pause) is first-class
  - Easy to add retry, parallel branches, replay later
  - The graph itself is a demo asset — visualizes the agent

TODO (next milestone): build the StateGraph with all nodes
and edges, including the conditional edge to supervisor HITL.
"""
