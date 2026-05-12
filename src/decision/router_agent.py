"""
Router Agent — Layer 3 (Decision)

Turns Findings (from Layer 2) into Decisions: one action per finding,
with a reasoning string for the audit log. This module is pure logic —
no I/O, no audit writes, no DataFrame mutation. The agent graph wires
it to the corrector / quarantine handler / notifier downstream.

Routing logic by rule
---------------------
  R001 (schema integrity)
    severity LOW   (trailing_comma_cosmetic)        -> AUTO_CORRECT
    severity HIGH  (extra_comma_breaks_schema)      -> QUARANTINE
    severity HIGH  (fewer_columns_missing_fields)   -> QUARANTINE

  R002 (trailing_negative_amount)
    Always                                          -> AUTO_CORRECT
    The corrector module derives the new value deterministically from
    the raw value; the router leaves corrected_value=None.

  R003 (invalid_cibil_comment)
    confidence >= router.quarantine_min_confidence  -> FLAG_FOR_REVIEW
    confidence <  threshold                         -> ACCEPT
    NEVER AUTO_CORRECT — CIBIL_COMMENTS require human business judgment,
    even at high LLM confidence. This is the explicit Rule 3 contract.

Thresholds come from config/settings.yaml -> router.

Run this file directly to self-test:
    python -m src.decision.router_agent
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

try:
    from src.models.schemas import Finding, Decision, Action, Severity
except ImportError:  # pragma: no cover
    from models.schemas import Finding, Decision, Action, Severity  # type: ignore


DEFAULT_QUARANTINE_MIN_CONFIDENCE = 0.70


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def route_findings(
    findings: list[Finding],
    settings: Optional[dict] = None,
) -> list[Decision]:
    """
    Decide an action for every finding.

    Args:
        findings:  Output of the validation layer (schema_validator +
                   rule_engine + anomaly_detector), aggregated.
        settings:  Parsed config/settings.yaml. Used to read router
                   thresholds. If None, sensible defaults are used.

    Returns:
        A list of Decision objects, one per Finding, in input order.
    """
    router_cfg = ((settings or {}).get("router") or {})
    quarantine_min = float(router_cfg.get(
        "quarantine_min_confidence", DEFAULT_QUARANTINE_MIN_CONFIDENCE,
    ))

    decisions: list[Decision] = []
    for f in findings:
        decisions.append(_route_one(f, quarantine_min))
    return decisions


# ---------------------------------------------------------------------------
# Per-rule routing
# ---------------------------------------------------------------------------
def _route_one(f: Finding, quarantine_min: float) -> Decision:
    if f.rule_id == "R001":
        return _route_r001(f)
    if f.rule_id == "R002":
        return _route_r002(f)
    if f.rule_id == "R003":
        return _route_r003(f, quarantine_min)
    # Unknown rule: route conservatively to FLAG_FOR_REVIEW so nothing
    # silently auto-corrects on a misconfigured rules.yaml.
    return Decision(
        finding=f,
        action=Action.FLAG_FOR_REVIEW,
        corrected_value=None,
        reasoning=(
            f"Unknown rule_id {f.rule_id!r}; routed to FLAG_FOR_REVIEW as a "
            "conservative default."
        ),
    )


def _route_r001(f: Finding) -> Decision:
    """Schema-integrity routing — split by severity."""
    if f.severity == Severity.LOW:
        # trailing_comma_cosmetic — the empty trailing cell was already
        # dropped by file_loader; auto-correct just records the fact.
        return Decision(
            finding=f,
            action=Action.AUTO_CORRECT,
            corrected_value=None,
            reasoning=(
                "R001 trailing-comma cosmetic: extra cell was empty, dropped "
                "during load. Safe to auto-correct."
            ),
        )
    # HIGH severity covers both extra_comma_breaks_schema and
    # fewer_columns_missing_fields — field positions are unreliable, so
    # the row cannot be safely auto-fixed.
    return Decision(
        finding=f,
        action=Action.QUARANTINE,
        corrected_value=None,
        reasoning=(
            f"R001 {f.rule_name}: column count off by more than a cosmetic "
            "trailing comma. Field positions unreliable — quarantine for "
            "human review."
        ),
    )


def _route_r002(f: Finding) -> Decision:
    """Trailing-negative amount — always auto-correct."""
    return Decision(
        finding=f,
        action=Action.AUTO_CORRECT,
        corrected_value=None,   # corrector module derives this deterministically
        reasoning=(
            "R002 trailing-negative amount is an unambiguous legacy-mainframe "
            "format. Corrector will move the trailing minus to the front."
        ),
    )


def _route_r003(f: Finding, quarantine_min: float) -> Decision:
    """CIBIL_COMMENTS judgment — never auto-correct."""
    if f.confidence >= quarantine_min:
        return Decision(
            finding=f,
            action=Action.FLAG_FOR_REVIEW,
            corrected_value=None,
            reasoning=(
                f"R003 CIBIL_COMMENTS judgment at confidence {f.confidence:.2f} "
                f">= flag threshold {quarantine_min:.2f}. NEVER auto-corrected; "
                "flagging for human review and email notification."
            ),
        )
    return Decision(
        finding=f,
        action=Action.ACCEPT,
        corrected_value=None,
        reasoning=(
            f"R003 CIBIL_COMMENTS judgment at confidence {f.confidence:.2f} "
            f"below flag threshold {quarantine_min:.2f}. Accepting the value "
            "as-is; no human action required."
        ),
    )


# ---------------------------------------------------------------------------
# Self-test — run with:  python -m src.decision.router_agent
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile
    from collections import Counter
    import yaml

    try:
        from src.ingestion.file_loader import load_csv
        from src.validation.schema_validator import validate_schema
        from src.validation.rule_engine import run_rules
        from src.validation.anomaly_detector import detect_cibil_anomalies
        from src.audit.audit_logger import AuditLogger
    except ImportError:  # pragma: no cover
        from ingestion.file_loader import load_csv  # type: ignore
        from validation.schema_validator import validate_schema  # type: ignore
        from validation.rule_engine import run_rules  # type: ignore
        from validation.anomaly_detector import detect_cibil_anomalies  # type: ignore
        from audit.audit_logger import AuditLogger  # type: ignore

    rules = yaml.safe_load(Path("config/rules.yaml").read_text())
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    # Force mock mode so the router self-test is fast + deterministic and
    # doesn't depend on Ollama being up.
    settings_mock = {**settings, "llm": {**settings.get("llm", {}), "enabled": False}}

    expected_cols = rules["schema"]["expected_columns"]
    fp = Path("samples/demo_07_showcase_synthetic.csv")
    if not fp.exists():
        print(f"FAIL: {fp} not found. Run from project root.")
        return 1

    print("=" * 72)
    print("RouterAgent self-test  —  end-to-end on demo_07_showcase_synthetic.csv")
    print("=" * 72)

    load_result = load_csv(fp, expected_cols)

    # Anomaly detector needs an AuditLogger to log mock calls; use a tempdir
    # DB so the real audit.db stays untouched (per the router contract:
    # the router itself writes nothing).
    tmp = tempfile.TemporaryDirectory()
    audit = AuditLogger(Path(tmp.name) / "router_self_test.db")
    run_id = "router-self-test"

    schema_findings = validate_schema(load_result, run_id=run_id)
    rule_findings = run_rules(load_result.dataframe, rules, run_id=run_id)
    anomaly_findings, anomaly_stats = detect_cibil_anomalies(
        df=load_result.dataframe,
        rules_config=rules,
        audit_logger=audit,
        run_id=run_id,
        settings=settings_mock,
    )

    all_findings: list = [*schema_findings, *rule_findings, *anomaly_findings]
    print(f"  total rows in file:        {load_result.total_rows}")
    print(f"  R001 schema findings:      {len(schema_findings)}")
    print(f"  R002 rule-engine findings: {len(rule_findings)}")
    print(f"  R003 anomaly findings:     {len(anomaly_findings)} "
          f"(fast={anomaly_stats.fastpath_matches}, "
          f"mock={anomaly_stats.mock_calls})")
    print(f"  total findings -> router:  {len(all_findings)}")

    decisions = route_findings(all_findings, settings)

    # Table: per-rule action breakdown
    print()
    print("  Decision breakdown:")
    print("  " + "-" * 70)
    print(f"  {'rule_id':8s} {'count':>5s}  {'AUTO_CORRECT':>13s} "
          f"{'QUARANTINE':>11s} {'FLAG_FOR_REVIEW':>16s} {'ACCEPT':>8s}")
    print("  " + "-" * 70)

    by_rule: dict[str, Counter] = {}
    for d in decisions:
        c = by_rule.setdefault(d.finding.rule_id, Counter())
        c[d.action.value] += 1

    for rid in sorted(by_rule.keys()):
        c = by_rule[rid]
        total = sum(c.values())
        print(
            f"  {rid:8s} {total:5d}  "
            f"{c.get('auto_correct', 0):>13d} "
            f"{c.get('quarantine', 0):>11d} "
            f"{c.get('flag_for_review', 0):>16d} "
            f"{c.get('accept', 0):>8d}"
        )

    # Per-decision detail so a human reader can verify the routing
    print()
    print("  Per-decision detail:")
    print("  " + "-" * 70)
    for d in decisions:
        f = d.finding
        raw = (f.raw_value or "")[:32]
        print(
            f"  row {f.row_index:2d}  {f.rule_id}  {f.severity.value:6s}  "
            f"conf={f.confidence:.2f}  {d.action.value:15s}  raw={raw!r}"
        )
        print(f"           reason: {d.reasoning[:96]}")

    # Sanity checks: invariants the spec requires
    print()
    print("  Invariant checks:")
    for d in decisions:
        if d.finding.rule_id == "R003":
            assert d.action != Action.AUTO_CORRECT, (
                "R003 must NEVER be auto-corrected"
            )
        if d.finding.rule_id == "R002":
            assert d.action == Action.AUTO_CORRECT, (
                "R002 must always be auto-corrected"
            )
    print("    R003 never AUTO_CORRECT: OK")
    print("    R002 always AUTO_CORRECT: OK")
    print(f"    one decision per finding ({len(decisions)} == {len(all_findings)}): "
          f"{'OK' if len(decisions) == len(all_findings) else 'FAIL'}")

    # demo_07 doesn't trigger R001, so synthesize one of each severity to
    # prove the schema-routing branch.
    print()
    print("  Synthetic R001 routing check (demo_07 has no schema anomalies):")
    synthetic = [
        Finding(run_id=run_id, rule_id="R001",
                rule_name="trailing_comma_cosmetic", row_index=99,
                severity=Severity.LOW,
                description="synthetic LOW", confidence=1.0),
        Finding(run_id=run_id, rule_id="R001",
                rule_name="extra_comma_breaks_schema", row_index=100,
                severity=Severity.HIGH,
                description="synthetic HIGH extra", confidence=1.0),
        Finding(run_id=run_id, rule_id="R001",
                rule_name="fewer_columns_missing_fields", row_index=101,
                severity=Severity.HIGH,
                description="synthetic HIGH fewer", confidence=1.0),
    ]
    syn_decisions = route_findings(synthetic, settings)
    for d in syn_decisions:
        print(f"    {d.finding.rule_name:32s} severity={d.finding.severity.value:6s} "
              f"-> {d.action.value}")
    assert syn_decisions[0].action == Action.AUTO_CORRECT, "R001 LOW must auto-correct"
    assert syn_decisions[1].action == Action.QUARANTINE,   "R001 HIGH extra must quarantine"
    assert syn_decisions[2].action == Action.QUARANTINE,   "R001 HIGH fewer must quarantine"
    print("    R001 severity-based routing: OK")

    tmp.cleanup()
    print()
    print("Self-test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
