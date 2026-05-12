# Enquesta DQ Agent — Graph Topology

This is the LangGraph state machine that orchestrates the data quality
agent. Each node is a module; edges show the flow of execution.
Conditional edges branch on `is_empty` and `should_notify`.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	ingest_file(ingest_file)
	validate_rules(validate_rules)
	route_decisions(route_decisions)
	apply_corrections(apply_corrections)
	split_outputs(split_outputs)
	evaluate_supervisor(evaluate_supervisor)
	send_notifications(send_notifications)
	generate_run_report(generate_run_report)
	empty_file_path(empty_file_path)
	__end__([<p>__end__</p>]):::last
	__start__ --> ingest_file;
	apply_corrections --> split_outputs;
	empty_file_path --> generate_run_report;
	evaluate_supervisor -. &nbsp;skip&nbsp; .-> generate_run_report;
	evaluate_supervisor -. &nbsp;notify&nbsp; .-> send_notifications;
	ingest_file -. &nbsp;empty&nbsp; .-> empty_file_path;
	ingest_file -. &nbsp;process&nbsp; .-> validate_rules;
	route_decisions --> apply_corrections;
	send_notifications --> generate_run_report;
	split_outputs --> evaluate_supervisor;
	validate_rules --> route_decisions;
	generate_run_report --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

This diagram is generated programmatically from the live graph. Regenerate
with: `python -m src.graph.agent_graph`
