"""
Report Generator — Layer 5 (Audit & Output)

End-of-run summary rendered in three formats:

  * as_text     — plain text for stdout / email body
  * as_markdown — GitHub-flavored markdown for the Streamlit UI
  * as_json     — JSON for dashboards / API consumers

The report pulls per-run aggregates from the audit DB:
  * findings_summary  (per-rule + severity counts)
  * corrections_summary (per-rule + column counts)
  * llm_summary (call count, total/avg latency)

…and joins them with the SupervisorVerdict, SplitResult, and
NotificationResult already produced upstream. The single source of
truth for every number is the audit DB; the verdict / split / notify
objects are quoted for context, not authority.

Run this file directly to self-test:
    python -m src.audit.report_generator
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from src.audit.audit_logger import AuditLogger
    from src.decision.quarantine_handler import SplitResult
    from src.supervision.notifier import NotificationResult
    from src.supervision.supervisor_agent import SupervisorVerdict
except ImportError:  # pragma: no cover
    from audit.audit_logger import AuditLogger  # type: ignore
    from decision.quarantine_handler import SplitResult  # type: ignore
    from supervision.notifier import NotificationResult  # type: ignore
    from supervision.supervisor_agent import SupervisorVerdict  # type: ignore


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------
@dataclass
class Report:
    run_id: str
    file_name: str
    duration_seconds: float
    summary: dict = field(default_factory=dict)
    verdict: dict = field(default_factory=dict)
    notification: dict = field(default_factory=dict)
    findings_summary: list[dict] = field(default_factory=list)
    corrections_summary: list[dict] = field(default_factory=list)
    llm_summary: dict = field(default_factory=dict)
    as_json: str = ""
    as_text: str = ""
    as_markdown: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def generate_report(
    run_id: str,
    file_name: str,
    verdict: SupervisorVerdict,
    split_result: SplitResult,
    notification_result: NotificationResult,
    audit_logger: AuditLogger,
    started_at: datetime,
    finished_at: datetime,
) -> Report:
    """Assemble the end-of-run report. Pure aggregation; no DB writes."""
    duration = max(0.0, (finished_at - started_at).total_seconds())

    clean_n = len(split_result.clean_rows)
    quar_n = len(split_result.quarantined_rows)
    flag_n = len(split_result.flagged_rows)
    total = verdict.total_rows or (clean_n + quar_n + flag_n)

    summary = {
        "total_rows": total,
        "clean": clean_n,
        "quarantined": quar_n,
        "flagged": flag_n,
        "started_at": started_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "finished_at": finished_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }

    verdict_dict = {
        "status": verdict.status,
        "quarantine_ratio": verdict.quarantine_ratio,
        "flag_ratio": verdict.flag_ratio,
        "quarantined_count": verdict.quarantined_count,
        "flagged_count": verdict.flagged_count,
        "total_rows": verdict.total_rows,
        "reasons": list(verdict.reasons),
        "should_notify": verdict.should_notify,
    }

    notification_dict = {
        "email_mode": notification_result.email_mode,
        "email_sent": notification_result.email_sent,
        "email_path": str(notification_result.email_path)
                      if notification_result.email_path else None,
        "console_printed": notification_result.console_printed,
        "error": notification_result.error,
    }

    findings_summary = audit_logger.get_findings_summary(run_id)
    corrections_summary = audit_logger.get_corrections_summary(run_id)
    llm_summary = audit_logger.get_llm_summary(run_id)

    report = Report(
        run_id=run_id,
        file_name=file_name,
        duration_seconds=duration,
        summary=summary,
        verdict=verdict_dict,
        notification=notification_dict,
        findings_summary=findings_summary,
        corrections_summary=corrections_summary,
        llm_summary=llm_summary,
    )
    report.as_text = _render_text(report)
    report.as_markdown = _render_markdown(report)
    report.as_json = _render_json(report)
    return report


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator / denominator) * 100.0


def _findings_by_rule(findings_summary: list[dict]) -> dict[str, dict]:
    """Collapse the per-(rule,severity) rows into per-rule totals + a name."""
    by_rule: dict[str, dict] = {}
    for row in findings_summary:
        rid = row["rule_id"]
        entry = by_rule.setdefault(rid, {"name": row["rule_name"], "count": 0})
        entry["count"] += int(row["count"])
        # Prefer the first rule_name we see for that rule_id; if multiple
        # severities have different names (e.g. R001's three variants),
        # keep the original.
    return by_rule


def _render_text(r: Report) -> str:
    s = r.summary
    v = r.verdict
    n = r.notification
    total = s["total_rows"]

    lines: list[str] = []
    bar = "=" * 60
    lines.append(bar)
    lines.append("Enquesta DQ Agent — Run Report")
    lines.append(bar)
    lines.append(f"File:        {r.file_name}")
    lines.append(f"Run ID:      {r.run_id}")
    lines.append(f"Duration:    {r.duration_seconds:.2f}s")
    lines.append(f"Started:     {s['started_at']}")
    lines.append(f"Finished:    {s['finished_at']}")
    lines.append("")

    lines.append("--- Run Summary ---")
    lines.append(f"Total rows:    {total}")
    lines.append(f"Clean:         {s['clean']} ({_pct(s['clean'], total):.1f}%)")
    lines.append(f"Quarantined:   {s['quarantined']} "
                 f"({_pct(s['quarantined'], total):.1f}%)")
    lines.append(f"Flagged:       {s['flagged']} "
                 f"({_pct(s['flagged'], total):.1f}%)")
    lines.append("")

    lines.append("--- Supervisor Verdict ---")
    lines.append(f"Status:        {v['status']}")
    lines.append(f"Quarantine ratio: {v['quarantine_ratio'] * 100:.1f}%")
    lines.append(f"Flag ratio:    {v['flag_ratio'] * 100:.1f}%")
    if v["reasons"]:
        lines.append("Reasons:")
        for reason in v["reasons"]:
            lines.append(f"  - {reason}")
    lines.append("")

    lines.append("--- Findings by Rule ---")
    by_rule = _findings_by_rule(r.findings_summary)
    if by_rule:
        for rid in sorted(by_rule.keys()):
            entry = by_rule[rid]
            lines.append(f"{rid} ({entry['name']}):   {entry['count']}")
    else:
        lines.append("(no findings)")
    lines.append("")

    if r.corrections_summary:
        lines.append("--- Corrections Applied ---")
        for row in r.corrections_summary:
            lines.append(
                f"{row['rule_id']} / {row['column_name']}:  {row['count']}"
            )
        lines.append("")

    if r.llm_summary.get("count", 0) > 0:
        lines.append("--- LLM Activity ---")
        lines.append(f"Calls:         {r.llm_summary['count']}")
        lines.append(f"Total latency: {r.llm_summary['total_ms']} ms")
        lines.append(f"Avg latency:   {r.llm_summary['avg_ms']:.0f} ms")
        lines.append("")

    lines.append("--- Notification ---")
    lines.append(f"Email mode:   {n['email_mode']}")
    lines.append(f"Email sent:   {n['email_sent']}")
    if n["email_path"]:
        lines.append(f"Sent to:      {n['email_path']}")
    elif n["email_mode"] == "smtp":
        lines.append("Sent to:      (recipients from settings.yaml)")
    else:
        lines.append("Sent to:      (n/a)")
    if n["error"]:
        lines.append(f"Notes:        {n['error']}")
    lines.append("")

    lines.append(f"Full audit trail: audit.db (run_id = {r.run_id})")
    lines.append(bar)
    return "\n".join(lines) + "\n"


def _render_markdown(r: Report) -> str:
    s = r.summary
    v = r.verdict
    n = r.notification
    total = s["total_rows"]

    lines: list[str] = []
    lines.append(f"# Enquesta DQ Agent — Run Report")
    lines.append("")
    lines.append(f"**File:** `{r.file_name}`  ")
    lines.append(f"**Run ID:** `{r.run_id}`  ")
    lines.append(f"**Duration:** {r.duration_seconds:.2f}s  ")
    lines.append(f"**Started:** {s['started_at']}  ")
    lines.append(f"**Finished:** {s['finished_at']}")
    lines.append("")

    lines.append("## Run Summary")
    lines.append("")
    lines.append("| Bucket | Rows | Share |")
    lines.append("|---|---:|---:|")
    lines.append(f"| Total | **{total}** | 100.0% |")
    lines.append(f"| Clean | {s['clean']} | {_pct(s['clean'], total):.1f}% |")
    lines.append(f"| Quarantined | {s['quarantined']} | "
                 f"{_pct(s['quarantined'], total):.1f}% |")
    lines.append(f"| Flagged | {s['flagged']} | "
                 f"{_pct(s['flagged'], total):.1f}% |")
    lines.append("")

    lines.append("## Supervisor Verdict")
    lines.append("")
    lines.append(f"- **Status:** `{v['status']}`")
    lines.append(f"- **Quarantine ratio:** {v['quarantine_ratio'] * 100:.1f}%")
    lines.append(f"- **Flag ratio:** {v['flag_ratio'] * 100:.1f}%")
    if v["reasons"]:
        lines.append("- **Reasons:**")
        for reason in v["reasons"]:
            lines.append(f"  - {reason}")
    lines.append("")

    lines.append("## Findings by Rule")
    lines.append("")
    if r.findings_summary:
        lines.append("| Rule | Name | Severity | Count |")
        lines.append("|---|---|---|---:|")
        for row in r.findings_summary:
            lines.append(
                f"| `{row['rule_id']}` | {row['rule_name']} | "
                f"{row['severity']} | {row['count']} |"
            )
    else:
        lines.append("_No findings._")
    lines.append("")

    if r.corrections_summary:
        lines.append("## Corrections Applied")
        lines.append("")
        lines.append("| Rule | Column | Count |")
        lines.append("|---|---|---:|")
        for row in r.corrections_summary:
            lines.append(
                f"| `{row['rule_id']}` | `{row['column_name']}` | "
                f"{row['count']} |"
            )
        lines.append("")

    if r.llm_summary.get("count", 0) > 0:
        lines.append("## LLM Activity")
        lines.append("")
        lines.append(f"- **Calls:** {r.llm_summary['count']}")
        lines.append(f"- **Total latency:** {r.llm_summary['total_ms']} ms")
        lines.append(f"- **Avg latency:** {r.llm_summary['avg_ms']:.0f} ms")
        lines.append("")

    lines.append("## Notification")
    lines.append("")
    lines.append(f"- **Email mode:** `{n['email_mode']}`")
    lines.append(f"- **Email sent:** {n['email_sent']}")
    if n["email_path"]:
        lines.append(f"- **Sent to:** `{n['email_path']}`")
    elif n["email_mode"] == "smtp":
        lines.append("- **Sent to:** _recipients from settings.yaml_")
    else:
        lines.append("- **Sent to:** _n/a_")
    if n["error"]:
        lines.append(f"- **Notes:** {n['error']}")
    lines.append("")

    lines.append(f"_Full audit trail: `audit.db` (run_id = `{r.run_id}`)_")
    return "\n".join(lines) + "\n"


def _render_json(r: Report) -> str:
    payload = {
        "run_id": r.run_id,
        "file_name": r.file_name,
        "duration_seconds": r.duration_seconds,
        "summary": r.summary,
        "verdict": r.verdict,
        "notification": r.notification,
        "findings_summary": r.findings_summary,
        "corrections_summary": r.corrections_summary,
        "llm_summary": r.llm_summary,
    }
    return json.dumps(payload, indent=2, sort_keys=False, default=str)


# ---------------------------------------------------------------------------
# Self-test — run with:  python -m src.audit.report_generator
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile
    import yaml

    try:
        from src.ingestion.file_loader import load_csv
        from src.validation.schema_validator import validate_schema
        from src.validation.rule_engine import run_rules
        from src.validation.anomaly_detector import detect_cibil_anomalies
        from src.decision.router_agent import route_findings
        from src.decision.corrector import apply_corrections
        from src.decision.quarantine_handler import split_and_write
        from src.supervision.supervisor_agent import evaluate_run
        from src.supervision.notifier import notify
    except ImportError:  # pragma: no cover
        from ingestion.file_loader import load_csv  # type: ignore
        from validation.schema_validator import validate_schema  # type: ignore
        from validation.rule_engine import run_rules  # type: ignore
        from validation.anomaly_detector import detect_cibil_anomalies  # type: ignore
        from decision.router_agent import route_findings  # type: ignore
        from decision.corrector import apply_corrections  # type: ignore
        from decision.quarantine_handler import split_and_write  # type: ignore
        from supervision.supervisor_agent import evaluate_run  # type: ignore
        from supervision.notifier import notify  # type: ignore

    rules = yaml.safe_load(Path("config/rules.yaml").read_text())
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    settings_mock = {**settings,
                    "llm": {**settings.get("llm", {}), "enabled": False}}

    expected_cols = rules["schema"]["expected_columns"]
    fp = Path("samples/demo_07_showcase_synthetic.csv")
    if not fp.exists():
        print(f"FAIL: {fp} not found.")
        return 1

    print("=" * 72)
    print("ReportGenerator self-test  —  end-to-end pipeline on demo_07")
    print("=" * 72)

    tmp = tempfile.TemporaryDirectory()
    audit = AuditLogger(Path(tmp.name) / "report_audit.db")
    started_at = datetime.now(timezone.utc)
    run_id = audit.start_run(fp.name, str(fp))

    load_result = load_csv(fp, expected_cols)
    schema_findings = validate_schema(load_result, run_id=run_id)
    rule_findings = run_rules(load_result.dataframe, rules, run_id=run_id)
    anomaly_findings, _ = detect_cibil_anomalies(
        df=load_result.dataframe, rules_config=rules,
        audit_logger=audit, run_id=run_id, settings=settings_mock,
    )
    findings = [*schema_findings, *rule_findings, *anomaly_findings]
    # Log findings to audit so the per-rule aggregation actually has rows.
    for f in findings:
        audit.log_finding(f)

    decisions = route_findings(findings, settings_mock)
    corrected_df, corrections = apply_corrections(load_result.dataframe, findings)
    for c in corrections:
        audit.log_correction(
            run_id=run_id, row_index=c.row_index,
            column_name=c.column_name, rule_id=c.rule_id,
            value_before=c.value_before, value_after=c.value_after,
        )

    split = split_and_write(
        df=corrected_df, decisions=decisions, run_id=run_id,
        original_filename=fp.name,
        output_dirs={
            "clean": Path(tmp.name) / "clean",
            "quarantine": Path(tmp.name) / "quarantine",
            "flagged": Path(tmp.name) / "flagged",
        },
    )
    verdict = evaluate_run(split, total_rows=load_result.total_rows,
                          settings=settings)
    audit.finish_run(
        run_id=run_id,
        total_rows=load_result.total_rows,
        auto_corrected=len(corrections),
        quarantined=verdict.quarantined_count,
        flagged=verdict.flagged_count,
        status=verdict.status,
    )

    # Run notifier in mock mode so the report has a real notification dict
    notif_settings = {
        **settings,
        "email": {
            **(settings.get("email") or {}),
            "mode": "mock",
            "mock": {"output_dir": str(Path(tmp.name) / "sent_emails")},
        },
    }
    notif_result = notify(
        verdict=verdict, split_result=split,
        run_id=run_id, file_name=fp.name,
        settings=notif_settings, audit_logger=audit,
        df=corrected_df,
    )

    # Tiny sleep so duration_seconds is strictly > 0 regardless of clock granularity
    time.sleep(0.01)
    finished_at = datetime.now(timezone.utc)

    report = generate_report(
        run_id=run_id, file_name=fp.name,
        verdict=verdict, split_result=split,
        notification_result=notif_result,
        audit_logger=audit,
        started_at=started_at, finished_at=finished_at,
    )

    # ---- invariants ----
    print()
    print("Invariant checks:")
    assert report.as_text.startswith("=" * 60 + "\nEnquesta DQ Agent — Run Report")
    print("  as_text starts with header bar + title: OK")
    # The text format aligns labels with multiple spaces — match flexibly.
    import re
    assert re.search(r"^Status:\s+ok\b", report.as_text, re.MULTILINE), \
        f"expected 'Status: ok' line in text:\n{report.as_text}"
    print("  as_text contains a 'Status: ok' line: OK")
    # 2 flagged rows shown in the summary block
    assert "Flagged:       2" in report.as_text, \
        f"expected 'Flagged:       2' in text:\n{report.as_text}"
    print("  as_text shows 2 flagged rows: OK")
    # 2 corrections (R002 hits row 3 + row 4 on CIBL_AMT_5)
    # Look for the corrections section
    assert "--- Corrections Applied ---" in report.as_text
    # demo_07 has 2 R002 hits, both on CIBL_AMT_5
    assert "R002 / CIBL_AMT_5:  2" in report.as_text, \
        f"expected R002 / CIBL_AMT_5: 2 in text:\n{report.as_text}"
    print("  as_text shows 2 corrections (R002/CIBL_AMT_5): OK")

    # Markdown — quick structural check (no full md parse needed)
    assert report.as_markdown.startswith("# Enquesta DQ Agent")
    assert "## Run Summary" in report.as_markdown
    assert "| Rule | Name | Severity | Count |" in report.as_markdown
    print("  as_markdown has headers + tables: OK")

    # JSON — must round-trip
    decoded = json.loads(report.as_json)
    assert decoded["run_id"] == run_id
    assert decoded["summary"]["flagged"] == 2
    assert decoded["verdict"]["status"] == "ok"
    assert len(decoded["findings_summary"]) >= 2  # R002 + R003
    assert decoded["llm_summary"]["count"] >= 2   # 2 mock calls
    print(f"  as_json round-trips: OK ({len(report.as_json)} bytes)")

    assert report.duration_seconds > 0, "duration must be > 0"
    print(f"  duration_seconds > 0: OK ({report.duration_seconds:.3f}s)")

    # ---- print the text report for visual inspection ----
    print()
    print("Rendered as_text:")
    print(report.as_text)

    tmp.cleanup()
    print()
    print("Self-test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
