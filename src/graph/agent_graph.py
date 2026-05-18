"""
Agent Graph — Orchestration (LangGraph 1.0)

Wires every module from Layers 1-5 into one explicit state machine.
The graph is the canonical execution path for the agent — calling
run_agent(file_path, settings, rules_config, audit_logger) end-to-ends
the full pipeline for one file and returns a Report.

Flow
----
                            START
                              │
                        ingest_file
                       /            \\
            (is_empty)               (process)
                /                         \\
       empty_file_path               validate_rules
                |                          |
                |                    route_decisions
                |                          |
                |                    apply_corrections
                |                          |
                |                     split_outputs
                |                          |
                |                  evaluate_supervisor
                |                    /            \\
                |        (should_notify)         (else)
                |               |                  |
                |       send_notifications         |
                |               |                  |
                 \\____ generate_run_report _______/
                              |
                             END

Why LangGraph (and not plain Python):
  * Explicit state machine — every node + edge is inspectable and
    rendered as a Mermaid diagram for the demo.
  * Conditional branching (empty-file fast path, should_notify) is
    first-class, not buried in if/else.
  * Lets us add retries / parallelism / human-in-the-loop pauses later
    without re-architecting the agent.

State design
------------
AgentState is a TypedDict (LangGraph 1.0's StateGraph supports this
cleanly). All nodes return *partial* state dicts; LangGraph merges them
into the running state. Each pipeline output has a single owning node.

Nodes close over `audit_logger` via factory functions
(make_<node>(audit_logger)) so the logger doesn't have to live on state.

Run this file directly to self-test against demo_07 + demo_05:
    python -m src.graph.agent_graph
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypedDict

import pandas as pd
from langgraph.graph import StateGraph, START, END

try:
    from src.audit.audit_logger import AuditLogger
    from src.audit.report_generator import Report, generate_report
    from src.decision.corrector import CorrectionRecord, apply_corrections
    from src.decision.quarantine_handler import SplitResult, split_and_write
    from src.decision.router_agent import route_findings
    from src.ingestion.file_loader import LoadResult, load_csv
    from src.models.schemas import Decision, Finding
    from src.supervision.notifier import NotificationResult, notify
    from src.supervision.supervisor_agent import (
        SupervisorVerdict, evaluate_run,
    )
    from src.validation.anomaly_detector import detect_cibil_anomalies
    from src.validation.rule_engine import run_rules
    from src.validation.schema_validator import validate_schema
except ImportError:  # pragma: no cover
    from audit.audit_logger import AuditLogger  # type: ignore
    from audit.report_generator import Report, generate_report  # type: ignore
    from decision.corrector import CorrectionRecord, apply_corrections  # type: ignore
    from decision.quarantine_handler import SplitResult, split_and_write  # type: ignore
    from decision.router_agent import route_findings  # type: ignore
    from ingestion.file_loader import LoadResult, load_csv  # type: ignore
    from models.schemas import Decision, Finding  # type: ignore
    from supervision.notifier import NotificationResult, notify  # type: ignore
    from supervision.supervisor_agent import (  # type: ignore
        SupervisorVerdict, evaluate_run,
    )
    from validation.anomaly_detector import detect_cibil_anomalies  # type: ignore
    from validation.rule_engine import run_rules  # type: ignore
    from validation.schema_validator import validate_schema  # type: ignore


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class AgentState(TypedDict, total=False):
    # Inputs
    file_path: str
    file_name: str
    settings: dict
    rules_config: dict

    # Run identification
    run_id: str
    started_at: datetime

    # Pipeline outputs (populated by nodes as they execute)
    load_result: LoadResult
    findings: list[Finding]
    decisions: list[Decision]
    df_corrected: pd.DataFrame
    correction_records: list[CorrectionRecord]
    split_result: SplitResult
    verdict: SupervisorVerdict
    notification_result: NotificationResult
    report: Report
    finished_at: datetime


NodeFn = Callable[[AgentState], dict[str, Any]]


# ---------------------------------------------------------------------------
# Node factories — each closes over audit_logger so callers don't have to
# carry it on state.
# ---------------------------------------------------------------------------
def make_ingest_file(audit: AuditLogger) -> NodeFn:
    def ingest_file(state: AgentState) -> dict[str, Any]:
        file_path = state["file_path"]
        file_name = state["file_name"]
        rules = state["rules_config"]
        expected_cols = rules["schema"]["expected_columns"]

        run_id = audit.start_run(file_name=file_name, file_path=file_path)
        load_result = load_csv(file_path, expected_cols)
        return {"run_id": run_id, "load_result": load_result}
    return ingest_file


def make_validate_rules(audit: AuditLogger) -> NodeFn:
    def validate_rules(state: AgentState) -> dict[str, Any]:
        run_id = state["run_id"]
        load_result = state["load_result"]
        rules = state["rules_config"]
        settings = state["settings"]

        schema_findings = validate_schema(load_result, run_id=run_id)
        rule_findings = run_rules(load_result.dataframe, rules, run_id=run_id)
        anomaly_findings, _stats = detect_cibil_anomalies(
            df=load_result.dataframe,
            rules_config=rules,
            audit_logger=audit,
            run_id=run_id,
            settings=settings,
        )
        findings = [*schema_findings, *rule_findings, *anomaly_findings]

        # Persist every finding so the audit trail and the report
        # generator's aggregations are populated.
        for f in findings:
            audit.log_finding(f)

        return {"findings": findings}
    return validate_rules


def make_route_decisions(audit: AuditLogger) -> NodeFn:
    def route_decisions(state: AgentState) -> dict[str, Any]:
        findings = state["findings"]
        settings = state["settings"]
        decisions = route_findings(findings, settings)
        for d in decisions:
            audit.log_decision(d)
        return {"decisions": decisions}
    return route_decisions


def make_apply_corrections(audit: AuditLogger) -> NodeFn:
    def apply_corrections_node(state: AgentState) -> dict[str, Any]:
        run_id = state["run_id"]
        findings = state["findings"]
        load_result = state["load_result"]

        # Only R002 is auto-correctable today; pass just those findings.
        r2 = [f for f in findings if f.rule_id == "R002"]
        df_corrected, records = apply_corrections(load_result.dataframe, r2)

        for c in records:
            audit.log_correction(
                run_id=run_id,
                row_index=c.row_index,
                column_name=c.column_name,
                rule_id=c.rule_id,
                value_before=c.value_before,
                value_after=c.value_after,
            )
        return {"df_corrected": df_corrected, "correction_records": records}
    return apply_corrections_node


def make_split_outputs(audit: AuditLogger) -> NodeFn:
    def split_outputs(state: AgentState) -> dict[str, Any]:
        df = state["df_corrected"]
        decisions = state["decisions"]
        run_id = state["run_id"]
        file_name = state["file_name"]
        settings = state["settings"]

        paths_cfg = (settings or {}).get("paths") or {}
        output_dirs = {
            "clean": paths_cfg.get("clean", "data/clean"),
            "quarantine": paths_cfg.get("quarantine", "data/quarantine"),
            "flagged": paths_cfg.get("flagged", "data/flagged"),
        }
        split = split_and_write(
            df=df,
            decisions=decisions,
            run_id=run_id,
            original_filename=file_name,
            output_dirs=output_dirs,
        )
        return {"split_result": split}
    return split_outputs


def make_evaluate_supervisor(audit: AuditLogger) -> NodeFn:
    def evaluate_supervisor(state: AgentState) -> dict[str, Any]:
        split = state["split_result"]
        settings = state["settings"]
        total = state["load_result"].total_rows
        verdict = evaluate_run(split, total_rows=total, settings=settings)
        return {"verdict": verdict}
    return evaluate_supervisor


def make_send_notifications(audit: AuditLogger) -> NodeFn:
    def send_notifications(state: AgentState) -> dict[str, Any]:
        result = notify(
            verdict=state["verdict"],
            split_result=state["split_result"],
            run_id=state["run_id"],
            file_name=state["file_name"],
            settings=state["settings"],
            audit_logger=audit,
            df=state.get("df_corrected"),
        )
        return {"notification_result": result}
    return send_notifications


def make_generate_run_report(audit: AuditLogger) -> NodeFn:
    def generate_run_report(state: AgentState) -> dict[str, Any]:
        finished_at = datetime.now(timezone.utc)
        verdict = state["verdict"]
        split = state["split_result"]
        notif = state.get("notification_result")
        # If notification was skipped via conditional edge, build a "skipped"
        # placeholder so the report still renders cleanly.
        if notif is None:
            notif = NotificationResult(
                console_printed=False,
                email_sent=False,
                email_mode="skipped",
                email_path=None,
                error=None,
            )
        report = generate_report(
            run_id=state["run_id"],
            file_name=state["file_name"],
            verdict=verdict,
            split_result=split,
            notification_result=notif,
            audit_logger=audit,
            started_at=state["started_at"],
            finished_at=finished_at,
        )
        # Close out the run in the audit ledger with the supervisor's status.
        audit.finish_run(
            run_id=state["run_id"],
            total_rows=verdict.total_rows,
            auto_corrected=len(state.get("correction_records") or []),
            quarantined=verdict.quarantined_count,
            flagged=verdict.flagged_count,
            status=verdict.status,
        )
        return {
            "report": report,
            "finished_at": finished_at,
            "notification_result": notif,
        }
    return generate_run_report


def make_empty_file_path(audit: AuditLogger) -> NodeFn:
    """Fast-path node for empty CSV files — skip processing entirely."""
    def empty_file_path(state: AgentState) -> dict[str, Any]:
        load_result = state["load_result"]
        empty_split = SplitResult()  # all zero
        verdict = SupervisorVerdict(
            status="ok",
            reasons=[],
            quarantine_ratio=0.0,
            flag_ratio=0.0,
            quarantined_count=0,
            flagged_count=0,
            total_rows=load_result.total_rows,
            should_notify=False,
        )
        return {
            "findings": [],
            "decisions": [],
            "df_corrected": load_result.dataframe,
            "correction_records": [],
            "split_result": empty_split,
            "verdict": verdict,
            "notification_result": NotificationResult(
                console_printed=False,
                email_sent=False,
                email_mode="skipped",
            ),
        }
    return empty_file_path


# ---------------------------------------------------------------------------
# Conditional-edge predicates
# ---------------------------------------------------------------------------
def _post_ingest_route(state: AgentState) -> str:
    return "empty" if state["load_result"].is_empty else "process"


def _post_supervisor_route(state: AgentState) -> str:
    return "notify" if state["verdict"].should_notify else "skip"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------
def build_graph(audit_logger: AuditLogger):
    """Construct and compile the StateGraph."""
    builder = StateGraph(AgentState)

    builder.add_node("ingest_file", make_ingest_file(audit_logger))
    builder.add_node("validate_rules", make_validate_rules(audit_logger))
    builder.add_node("route_decisions", make_route_decisions(audit_logger))
    builder.add_node("apply_corrections", make_apply_corrections(audit_logger))
    builder.add_node("split_outputs", make_split_outputs(audit_logger))
    builder.add_node("evaluate_supervisor", make_evaluate_supervisor(audit_logger))
    builder.add_node("send_notifications", make_send_notifications(audit_logger))
    builder.add_node("generate_run_report", make_generate_run_report(audit_logger))
    builder.add_node("empty_file_path", make_empty_file_path(audit_logger))

    builder.add_edge(START, "ingest_file")

    builder.add_conditional_edges(
        "ingest_file",
        _post_ingest_route,
        {"empty": "empty_file_path", "process": "validate_rules"},
    )
    builder.add_edge("validate_rules", "route_decisions")
    builder.add_edge("route_decisions", "apply_corrections")
    builder.add_edge("apply_corrections", "split_outputs")
    builder.add_edge("split_outputs", "evaluate_supervisor")

    builder.add_conditional_edges(
        "evaluate_supervisor",
        _post_supervisor_route,
        {"notify": "send_notifications", "skip": "generate_run_report"},
    )
    builder.add_edge("send_notifications", "generate_run_report")
    builder.add_edge("empty_file_path", "generate_run_report")
    builder.add_edge("generate_run_report", END)

    return builder.compile()


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def run_agent(
    file_path: str,
    settings: dict,
    rules_config: dict,
    audit_logger: AuditLogger,
) -> Report:
    """End-to-end one file: load, validate, decide, correct, split, notify, report."""
    graph = build_graph(audit_logger)
    initial_state: dict[str, Any] = {
        "file_path": str(file_path),
        "file_name": Path(file_path).name,
        "settings": settings,
        "rules_config": rules_config,
        "started_at": datetime.now(timezone.utc),
    }
    final_state = graph.invoke(initial_state)
    return final_state["report"]


# ---------------------------------------------------------------------------
# Bonus: graph topology as a Mermaid diagram (for the demo / docs)
# ---------------------------------------------------------------------------
def render_graph_mermaid(audit_logger: AuditLogger | None = None) -> str:
    """Return the graph's topology as a Mermaid diagram string."""
    if audit_logger is None:
        # build_graph only needs the logger reference at node-call time;
        # the topology can be rendered with any placeholder.
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        audit_logger = AuditLogger(Path(tmp.name) / "mermaid.db")
    graph = build_graph(audit_logger)
    return graph.get_graph().draw_mermaid()


# ---------------------------------------------------------------------------
# Self-test — python -m src.graph.agent_graph
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import sqlite3
    import tempfile
    import yaml

    rules = yaml.safe_load(Path("config/rules.yaml").read_text())
    base_settings = yaml.safe_load(Path("config/settings.yaml").read_text())

    # Hermetic test settings — force Ollama off, email -> mock under tempdir.
    tmp = tempfile.TemporaryDirectory()
    settings = {
        **base_settings,
        "llm": {**base_settings.get("llm", {}), "enabled": False},
        "email": {
            **(base_settings.get("email") or {}),
            "mode": "mock",
            "mock": {"output_dir": str(Path(tmp.name) / "sent_emails")},
        },
        "paths": {
            **(base_settings.get("paths") or {}),
            "clean": str(Path(tmp.name) / "clean"),
            "quarantine": str(Path(tmp.name) / "quarantine"),
            "flagged": str(Path(tmp.name) / "flagged"),
        },
    }

    db_path = Path(tmp.name) / "graph_audit.db"
    audit = AuditLogger(db_path)

    print("=" * 72)
    print("AgentGraph self-test  —  end-to-end orchestration")
    print(f"  audit DB:  {db_path}")
    print("=" * 72)

    # ---- Mermaid topology print ----
    print()
    print("Graph topology (Mermaid):")
    print("-" * 60)
    print(render_graph_mermaid(audit))
    print("-" * 60)

    # ---- Test 1: non-empty path on demo_07 ----
    fp_07 = Path("samples/demo_07_showcase_synthetic.csv")
    assert fp_07.exists(), f"missing {fp_07}"
    print()
    print(f"  Test 1: {fp_07.name}  (non-empty processing path)")
    report_07 = run_agent(
        file_path=str(fp_07),
        settings=settings,
        rules_config=rules,
        audit_logger=audit,
    )
    print(f"    run_id:       {report_07.run_id}")
    print(f"    status:       {report_07.verdict['status']}")
    print(f"    duration:     {report_07.duration_seconds:.3f}s")
    print(f"    clean/quar/flag:  {report_07.summary['clean']} / "
          f"{report_07.summary['quarantined']} / "
          f"{report_07.summary['flagged']}")

    assert isinstance(report_07, Report), "run_agent must return a Report"
    assert report_07.as_text, "as_text must be non-empty"
    # demo_07 contains an exact-duplicate pair (DAVIS LINDA rows 7 & 8);
    # with R004 active, the second occurrence is quarantined, so the
    # clean bucket drops from 8 to 7 and quarantine rises from 0 to 1.
    assert report_07.summary["clean"] == 7
    assert report_07.summary["quarantined"] == 1
    assert report_07.summary["flagged"] == 2
    # 2 R002 corrections expected
    correction_rows = [r for r in report_07.corrections_summary
                       if r["rule_id"] == "R002"]
    assert correction_rows and correction_rows[0]["count"] == 2, \
        f"expected 2 R002 corrections, got {report_07.corrections_summary}"
    print("    Test 1 assertions: OK")

    # ---- Test 2: empty-file fast path on demo_05 ----
    fp_05 = Path("samples/demo_05_edge_empty.csv")
    assert fp_05.exists(), f"missing {fp_05}"
    print()
    print(f"  Test 2: {fp_05.name}  (empty-file fast path)")
    report_05 = run_agent(
        file_path=str(fp_05),
        settings=settings,
        rules_config=rules,
        audit_logger=audit,
    )
    print(f"    run_id:       {report_05.run_id}")
    print(f"    status:       {report_05.verdict['status']}")
    print(f"    duration:     {report_05.duration_seconds:.3f}s")
    print(f"    clean/quar/flag:  {report_05.summary['clean']} / "
          f"{report_05.summary['quarantined']} / "
          f"{report_05.summary['flagged']}")

    assert isinstance(report_05, Report)
    assert report_05.as_text
    assert report_05.summary["clean"] == 0
    assert report_05.summary["quarantined"] == 0
    assert report_05.summary["flagged"] == 0
    assert report_05.verdict["status"] == "ok"
    # Empty-file path should produce zero corrections and zero findings.
    assert report_05.corrections_summary == []
    assert report_05.findings_summary == []
    print("    Test 2 assertions: OK")

    # ---- Audit DB invariants for both runs ----
    print()
    print("  Audit DB checks:")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # Two file_runs rows, both finished, status matches the verdict.
        runs = {r["run_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM file_runs WHERE run_id IN (?, ?)",
            (report_07.run_id, report_05.run_id),
        ).fetchall()}
        assert runs[report_07.run_id]["status"] == report_07.verdict["status"]
        assert runs[report_05.run_id]["status"] == report_05.verdict["status"]
        assert runs[report_07.run_id]["finished_at"] is not None
        assert runs[report_05.run_id]["finished_at"] is not None
        print(f"    file_runs:    2 runs found, finished_at set on both")

        # demo_07: should have findings + decisions + corrections rows.
        f_count = conn.execute(
            "SELECT COUNT(*) FROM findings WHERE run_id = ?",
            (report_07.run_id,),
        ).fetchone()[0]
        d_count = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE run_id = ?",
            (report_07.run_id,),
        ).fetchone()[0]
        c_count = conn.execute(
            "SELECT COUNT(*) FROM corrections WHERE run_id = ?",
            (report_07.run_id,),
        ).fetchone()[0]
        print(f"    demo_07:      findings={f_count}  "
              f"decisions={d_count}  corrections={c_count}")
        assert f_count >= 4   # 2 R002 + 2 R003
        assert d_count >= 4
        assert c_count == 2

        # demo_05: no findings / decisions / corrections.
        f_05 = conn.execute(
            "SELECT COUNT(*) FROM findings WHERE run_id = ?",
            (report_05.run_id,),
        ).fetchone()[0]
        d_05 = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE run_id = ?",
            (report_05.run_id,),
        ).fetchone()[0]
        c_05 = conn.execute(
            "SELECT COUNT(*) FROM corrections WHERE run_id = ?",
            (report_05.run_id,),
        ).fetchone()[0]
        print(f"    demo_05:      findings={f_05}  "
              f"decisions={d_05}  corrections={c_05}")
        assert f_05 == 0 and d_05 == 0 and c_05 == 0

    # ---- Print demo_07's report for visual sanity check ----
    print()
    print("  Rendered Report.as_text for demo_07:")
    print(report_07.as_text)

    tmp.cleanup()
    print()
    print("Self-test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
