"""
Anomaly Detector — Layer 2 (Validation), LLM-powered

Implements Rule 3 (invalid_cibil_comment): judges whether each CIBIL_COMMENTS
value looks legitimate. This is the "agentic" part of the agent — judgment,
not just rules.

Design: two-tier detection
--------------------------
  Tier 1 (fast path, ~80% of rows):
    Regex against the four known-good code families (IT*, VO-*, PM-*, VAC-*)
    from rules.yaml. A match -> ACCEPT immediately, no LLM call, no Finding.

  Tier 2 (LLM path, ~20% of rows):
    For values that don't match any known-good pattern, build a prompt with
    the known-good + known-bad examples from rules.yaml and call the local
    Ollama llama3.2:3b model with format="json" for structured output.
    Empty values are handled deterministically (always invalid) to avoid
    burning an LLM call on a known-bad pattern.

Verdict vocabulary
------------------
    valid       — looks like a legitimate (but unrecognized) comment
    suspicious  — could be valid, could be garbage; needs human eyes
    invalid     — clearly bad (gibberish, placeholder, English freeform, etc.)
  All verdicts carry a confidence in [0.0, 1.0]. A Finding is produced when
  the verdict is suspicious or invalid AND confidence >= the rule's
  flag_for_review threshold (from rules.yaml).

Mock mode
---------
  Controlled by settings.yaml -> llm.enabled. When false, _judge_mock() is
  used instead of Ollama — deterministic pattern-based verdicts so the demo
  still runs if Ollama is down. Mock calls are still logged to audit DB
  with model="mock:llama3.2:3b" so the audit trail is complete.

Audit
-----
  Every LLM invocation (real or mock) is logged via AuditLogger.log_llm_call
  with prompt, response, latency, and row_index. This is non-negotiable —
  every CIBIL_COMMENTS judgment must be reviewable.

CIBIL_COMMENTS is NEVER auto-corrected. The only possible outcomes for a
Finding produced here are flag_for_review (-> email) or accept (no action).

Run this file directly to self-test:
    python -m src.validation.anomaly_detector
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    from src.models.schemas import Finding, Severity
    from src.audit.audit_logger import AuditLogger
except ImportError:  # pragma: no cover
    from models.schemas import Finding, Severity  # type: ignore
    from audit.audit_logger import AuditLogger  # type: ignore


# ---------------------------------------------------------------------------
# Verdict + per-row routing classification
# ---------------------------------------------------------------------------
VALID = "valid"
SUSPICIOUS = "suspicious"
INVALID = "invalid"
VALID_VERDICTS = {VALID, SUSPICIOUS, INVALID}


@dataclass
class LLMVerdict:
    """Structured judgment for one CIBIL_COMMENTS value."""
    verdict: str                 # valid | suspicious | invalid
    confidence: float            # 0.0..1.0
    reason: str                  # short human-readable rationale
    raw_response: str = ""       # exact text returned by the model (for audit)
    latency_ms: int = 0          # round-trip latency for this call
    source: str = "ollama"       # ollama | mock | fastpath


@dataclass
class DetectionStats:
    """Counters returned alongside findings, mainly for the self-test / UI."""
    total_rows: int = 0
    fastpath_matches: int = 0    # matched a known-good regex
    empty_values: int = 0        # empty cells flagged deterministically
    llm_calls: int = 0           # real Ollama invocations
    mock_calls: int = 0          # mock-mode invocations
    findings_produced: int = 0
    per_value_verdicts: list[tuple[str, LLMVerdict]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def detect_cibil_anomalies(
    df: pd.DataFrame,
    rules_config: dict,
    audit_logger: AuditLogger,
    run_id: str,
    settings: Optional[dict] = None,
) -> tuple[list[Finding], DetectionStats]:
    """
    Run Rule 3 (CIBIL Comment judgment) over a DataFrame.

    Args:
        df:            DataFrame of well-formed rows (from file_loader).
        rules_config:  Parsed config/rules.yaml.
        audit_logger:  Active AuditLogger — every LLM call is logged here.
        run_id:        Active run_id from audit_logger.start_run().
        settings:      Parsed config/settings.yaml. If None, mock mode is used.

    Returns:
        (findings, stats) where findings is the list of CIBIL_COMMENTS issues
        worth flagging for human review, and stats are counters useful for
        the UI / self-test.
    """
    rule_cfg = _find_rule(rules_config, "R003")
    if rule_cfg is None:
        return [], DetectionStats()

    columns = rule_cfg.get("applies_to_columns", ["CIBL_COMMENTS"])
    detection = rule_cfg.get("detection", {})
    known_good_patterns = [
        re.compile(p["pattern"])
        for p in detection.get("known_good_patterns", [])
    ]
    known_good_examples = detection.get("known_good_examples", [])
    known_bad_examples = detection.get("known_bad_examples", [])
    model_name = detection.get("model", "llama3.2:3b")
    thresholds = rule_cfg.get("confidence_thresholds", {})
    flag_threshold = float(thresholds.get("flag_for_review", 0.5))
    severity = Severity(rule_cfg.get("severity", "medium"))
    rule_id = rule_cfg["rule_id"]
    rule_name = rule_cfg["name"]

    llm_enabled = bool(((settings or {}).get("llm") or {}).get("enabled", False))
    mock_cfg = (settings or {}).get("mock_llm", {}) or {}

    findings: list[Finding] = []
    stats = DetectionStats(total_rows=len(df))

    for col in columns:
        if col not in df.columns:
            continue

        for row_idx, value in df[col].items():
            sval = "" if value is None else str(value).strip()

            # Deterministic: empty IS a known-bad pattern per rules.yaml.
            if sval == "":
                stats.empty_values += 1
                verdict = LLMVerdict(
                    verdict=INVALID,
                    confidence=0.95,
                    reason="Empty CIBIL_COMMENTS value (matches known-bad pattern).",
                    source="fastpath",
                )
                stats.per_value_verdicts.append((sval, verdict))
                if verdict.confidence >= flag_threshold:
                    findings.append(_to_finding(
                        run_id=run_id, rule_id=rule_id, rule_name=rule_name,
                        row_idx=int(row_idx), column=col, value=sval,
                        severity=severity, verdict=verdict,
                    ))
                    stats.findings_produced += 1
                continue

            # Tier 1 — fast path: regex against known-good families
            if _matches_known_good(sval, known_good_patterns):
                stats.fastpath_matches += 1
                continue

            # Tier 2 — LLM (or mock) judgment
            row_ctx = _row_context(df, int(row_idx))
            if llm_enabled:
                verdict = _judge_ollama(
                    value=sval,
                    row_ctx=row_ctx,
                    model=model_name,
                    settings=settings or {},
                    known_good_examples=known_good_examples,
                    known_bad_examples=known_bad_examples,
                )
                if verdict.source == "ollama":
                    stats.llm_calls += 1
                else:
                    # Ollama failed mid-flight and fell back to mock.
                    stats.mock_calls += 1
            else:
                verdict = _judge_mock(sval, mock_cfg)
                stats.mock_calls += 1

            # Audit every judgment — real or mock.
            audit_logger.log_llm_call(
                run_id=run_id,
                model=f"{verdict.source}:{model_name}",
                prompt=_audit_prompt(sval, row_ctx),
                response=verdict.raw_response or _verdict_to_json(verdict),
                latency_ms=verdict.latency_ms,
                row_index=int(row_idx),
            )

            stats.per_value_verdicts.append((sval, verdict))

            # Only flag verdicts at/above threshold and not "valid".
            if verdict.verdict != VALID and verdict.confidence >= flag_threshold:
                findings.append(_to_finding(
                    run_id=run_id, rule_id=rule_id, rule_name=rule_name,
                    row_idx=int(row_idx), column=col, value=sval,
                    severity=severity, verdict=verdict,
                ))
                stats.findings_produced += 1

    return findings, stats


# ---------------------------------------------------------------------------
# Tier-1 matcher
# ---------------------------------------------------------------------------
def _matches_known_good(value: str, patterns: list[re.Pattern]) -> bool:
    """True if `value` matches any known-good code-family regex."""
    return any(p.match(value) for p in patterns)


# ---------------------------------------------------------------------------
# Tier-2: Ollama judgment
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a data quality auditor for a municipal billing system. "
    "Your job is to judge whether a CIBIL_COMMENTS value on a billing row "
    "looks like a legitimate code or like a data-entry error / freeform note. "
    "Respond ONLY with strict JSON in this exact shape:\n"
    '{"verdict": "valid|suspicious|invalid", '
    '"confidence": 0.0-1.0, "reason": "short rationale"}\n'
    "Do not include any other keys. Do not include prose outside the JSON."
)


def _build_user_prompt(
    value: str,
    row_ctx: dict,
    known_good_examples: list[str],
    known_bad_examples: list[str],
) -> str:
    """Render the user prompt with examples + row context."""
    good = ", ".join(repr(x) for x in known_good_examples) or "(none)"
    bad = ", ".join(repr(x) for x in known_bad_examples) or "(none)"
    ctx_lines = [f"  {k}: {v!r}" for k, v in row_ctx.items()]
    ctx = "\n".join(ctx_lines) if ctx_lines else "  (no extra context)"
    return (
        f"VALUE TO JUDGE: {value!r}\n\n"
        f"KNOWN-GOOD EXAMPLES: {good}\n"
        f"KNOWN-BAD EXAMPLES: {bad}\n\n"
        f"ROW CONTEXT:\n{ctx}\n\n"
        "Return your JSON verdict now."
    )


def _audit_prompt(value: str, row_ctx: dict) -> str:
    """A compact prompt summary stored in the audit log."""
    return f"value={value!r} ctx={row_ctx}"


def _judge_ollama(
    value: str,
    row_ctx: dict,
    model: str,
    settings: dict,
    known_good_examples: list[str],
    known_bad_examples: list[str],
) -> LLMVerdict:
    """Call Ollama for a structured verdict. Falls back to mock on any error."""
    llm_cfg = settings.get("llm") or {}
    temperature = float(llm_cfg.get("temperature", 0.1))
    timeout_s = float(llm_cfg.get("timeout_seconds", 30))

    user_prompt = _build_user_prompt(
        value, row_ctx, known_good_examples, known_bad_examples,
    )

    try:
        import ollama  # local import so the module loads even if pkg missing
    except ImportError as e:
        verdict = _judge_mock(value, settings.get("mock_llm") or {})
        verdict.reason = f"ollama package not installed; mock fallback: {verdict.reason}"
        return verdict

    start = time.perf_counter()
    try:
        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            format="json",
            options={"temperature": temperature},
            # The python-ollama client honors OLLAMA_HOST env; base_url is set
            # via env. We don't override it here — the default localhost:11434
            # matches settings.yaml.
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        # Ollama down, model not pulled, network error, timeout — never crash
        # the agent. Fall back to mock and tag the source so the audit row
        # shows what happened.
        verdict = _judge_mock(value, settings.get("mock_llm") or {})
        verdict.reason = f"ollama call failed ({type(e).__name__}: {e}); mock fallback"
        verdict.latency_ms = int((time.perf_counter() - start) * 1000)
        return verdict

    raw = (response.get("message") or {}).get("content", "") or ""
    parsed = _parse_verdict_json(raw)
    parsed.latency_ms = latency_ms
    parsed.raw_response = raw
    parsed.source = "ollama"
    return parsed


def _parse_verdict_json(raw: str) -> LLMVerdict:
    """Best-effort parse of the model's JSON. Tolerant of minor noise."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to scrape the first {...} block out of the response.
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return LLMVerdict(
                    verdict=SUSPICIOUS, confidence=0.5,
                    reason=f"unparseable model response: {raw[:120]!r}",
                )
        else:
            return LLMVerdict(
                verdict=SUSPICIOUS, confidence=0.5,
                reason=f"unparseable model response: {raw[:120]!r}",
            )

    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in VALID_VERDICTS:
        verdict = SUSPICIOUS
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason", "")).strip() or "(no reason supplied)"
    return LLMVerdict(verdict=verdict, confidence=confidence, reason=reason)


# ---------------------------------------------------------------------------
# Tier-2: deterministic mock (demo-day safety net)
# ---------------------------------------------------------------------------
_BILLNUM_RE = re.compile(r"^(VP|CIV)\s*BILL\s*#", re.IGNORECASE)
_PURE_DIGITS_RE = re.compile(r"^\d+$")
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]{4,}\s+[A-Za-z]{3,}")
_COMPOSITE_RE = re.compile(r"^(IT|VO|PM|VAC)[-\d]+\s*-\s*\d+")


def _judge_mock(value: str, mock_cfg: dict) -> LLMVerdict:
    """Pattern-based stand-in for Ollama. Deterministic, fast, never fails."""
    v = (value or "").strip()
    lower = v.lower()

    if v == "":
        return LLMVerdict(verdict=INVALID, confidence=0.95,
                          reason="empty value", source="mock")
    if lower in {"test", "n/a", "na", "none", "null"} or "asdf" in lower:
        return LLMVerdict(verdict=INVALID, confidence=0.9,
                          reason="placeholder/gibberish token", source="mock")
    if _BILLNUM_RE.match(v):
        return LLMVerdict(verdict=SUSPICIOUS, confidence=0.75,
                          reason="freeform 'BILL #' pattern (unrecognized family)",
                          source="mock")
    if _ENGLISH_WORD_RE.search(v):
        return LLMVerdict(verdict=SUSPICIOUS, confidence=0.7,
                          reason="freeform English text (not a code)",
                          source="mock")
    if _COMPOSITE_RE.match(v):
        return LLMVerdict(verdict=SUSPICIOUS, confidence=0.6,
                          reason="composite identifier with dash-suffix",
                          source="mock")
    if _PURE_DIGITS_RE.match(v):
        return LLMVerdict(verdict=SUSPICIOUS, confidence=0.6,
                          reason="pure digits — looks like a parcel ID, not a comment code",
                          source="mock")

    default_verdict = str(mock_cfg.get("default_verdict", SUSPICIOUS)).lower()
    if default_verdict not in VALID_VERDICTS:
        default_verdict = SUSPICIOUS
    default_conf = float(mock_cfg.get("default_confidence", 0.6))
    return LLMVerdict(verdict=default_verdict, confidence=default_conf,
                      reason="no rule matched; mock default", source="mock")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_rule(rules_config: dict, rule_id: str) -> Optional[dict]:
    for r in rules_config.get("rules", []):
        if r.get("rule_id") == rule_id:
            return r
    return None


def _row_context(df: pd.DataFrame, row_idx: int) -> dict:
    """Pull a few extra columns to give the model billing-row context."""
    wanted = ["ACCOUNTNUMBER", "CIBL_CLASS1", "CIBL_AMT_5", "CIBL_AMT_95"]
    ctx: dict = {}
    for c in wanted:
        if c in df.columns:
            try:
                ctx[c] = str(df.at[row_idx, c])
            except Exception:
                pass
    return ctx


def _to_finding(
    run_id: str, rule_id: str, rule_name: str,
    row_idx: int, column: str, value: str,
    severity: Severity, verdict: LLMVerdict,
) -> Finding:
    return Finding(
        run_id=run_id,
        rule_id=rule_id,
        rule_name=rule_name,
        row_index=row_idx,
        column=column,
        raw_value=value,
        severity=severity,
        description=(
            f"CIBIL_COMMENTS value {value!r} judged {verdict.verdict} "
            f"(confidence {verdict.confidence:.2f}) by {verdict.source}."
        ),
        confidence=verdict.confidence,
        verdict=verdict.verdict,
        llm_reasoning=verdict.reason,
    )


def _verdict_to_json(v: LLMVerdict) -> str:
    return json.dumps({
        "verdict": v.verdict,
        "confidence": v.confidence,
        "reason": v.reason,
        "source": v.source,
    })


# ---------------------------------------------------------------------------
# Self-test — run with:  python -m src.validation.anomaly_detector
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile
    import yaml

    try:
        from src.ingestion.file_loader import load_csv
    except ImportError:
        from ingestion.file_loader import load_csv  # type: ignore

    config_path = Path("config/rules.yaml")
    settings_path = Path("config/settings.yaml")
    if not config_path.exists() or not settings_path.exists():
        print("FAIL: run from project root (need config/rules.yaml + settings.yaml).")
        return 1

    with config_path.open() as f:
        rules = yaml.safe_load(f)
    with settings_path.open() as f:
        settings = yaml.safe_load(f)

    expected_cols = rules["schema"]["expected_columns"]
    samples = sorted(Path("samples").glob("demo_*.csv"))

    # Use a temp DB so the self-test stays clean and reproducible.
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "self_test_audit.db"
    audit = AuditLogger(db_path)

    print("=" * 72)
    print("AnomalyDetector self-test  —  Rule 3 (CIBIL_COMMENTS judgment)")
    print(f"  llm.enabled in settings.yaml: {settings.get('llm', {}).get('enabled')}")
    print(f"  model:                        "
          f"{settings.get('llm', {}).get('model', '?')}")
    print(f"  audit DB:                     {db_path}")
    print(f"  sample files:                 {len(samples)}")
    print("=" * 72)

    # Run twice — once forced-mock, once with whatever settings.yaml says.
    # If settings.yaml has llm.enabled=true and Ollama is up, this exercises
    # the real model path too.
    for mode_label, mode_settings in [
        ("MOCK MODE (llm.enabled=False)", {**settings, "llm": {**settings.get("llm", {}), "enabled": False}}),
        (f"LIVE MODE (llm.enabled={settings.get('llm', {}).get('enabled')})", settings),
    ]:
        print()
        print("-" * 72)
        print(mode_label)
        print("-" * 72)

        grand = DetectionStats()
        all_llm_verdicts: list[tuple[str, str, LLMVerdict]] = []

        for fp in samples:
            load_result = load_csv(fp, expected_cols)
            run_id = audit.start_run(fp.name, str(fp))
            findings, stats = detect_cibil_anomalies(
                df=load_result.dataframe,
                rules_config=rules,
                audit_logger=audit,
                run_id=run_id,
                settings=mode_settings,
            )
            # Every R003 finding must carry the LLM verdict word so the
            # router can route on it without parsing prose.
            for f in findings:
                assert f.verdict in VALID_VERDICTS, (
                    f"R003 finding missing verdict (got {f.verdict!r}) "
                    f"row={f.row_index} value={f.raw_value!r}"
                )
            audit.finish_run(
                run_id=run_id,
                total_rows=stats.total_rows,
                auto_corrected=0,
                quarantined=0,
                flagged=stats.findings_produced,
            )

            grand.total_rows += stats.total_rows
            grand.fastpath_matches += stats.fastpath_matches
            grand.empty_values += stats.empty_values
            grand.llm_calls += stats.llm_calls
            grand.mock_calls += stats.mock_calls
            grand.findings_produced += stats.findings_produced
            for val, v in stats.per_value_verdicts:
                all_llm_verdicts.append((fp.name, val, v))

            print(
                f"  {fp.name:42s}  rows={stats.total_rows:3d}  "
                f"fast={stats.fastpath_matches:3d}  empty={stats.empty_values:2d}  "
                f"llm={stats.llm_calls:2d}  mock={stats.mock_calls:2d}  "
                f"flagged={stats.findings_produced:2d}"
            )

        print()
        print("  Totals:")
        print(f"    rows examined:    {grand.total_rows}")
        print(f"    fast-path skips:  {grand.fastpath_matches}")
        print(f"    empty values:     {grand.empty_values}")
        print(f"    real LLM calls:   {grand.llm_calls}")
        print(f"    mock LLM calls:   {grand.mock_calls}")
        print(f"    findings flagged: {grand.findings_produced}")

        if all_llm_verdicts:
            print()
            print("  Per-value verdicts (LLM-path values only):")
            seen: set[str] = set()
            for fname, val, v in all_llm_verdicts:
                key = (val, v.source, round(v.confidence, 2), v.verdict)
                if key in seen:
                    continue
                seen.add(key)
                tag = f"[{v.source}]"
                print(f"    {tag:10s} {val!r:40s} -> {v.verdict:10s} "
                      f"conf={v.confidence:.2f}  {v.reason}")

    # Confirm every LLM/mock call landed in the audit DB.
    print()
    print("-" * 72)
    print("Audit-DB integrity check")
    print("-" * 72)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        n_calls = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        n_runs = conn.execute("SELECT COUNT(*) FROM file_runs").fetchone()[0]
        sample = conn.execute(
            "SELECT model, latency_ms, substr(response, 1, 80) "
            "FROM llm_calls ORDER BY call_id DESC LIMIT 3"
        ).fetchall()
    print(f"  file_runs rows:  {n_runs}")
    print(f"  llm_calls rows:  {n_calls}")
    if sample:
        print("  most recent llm_calls:")
        for model, lat, resp in sample:
            print(f"    {model:25s}  {lat:5d}ms  {resp}")

    tmp.cleanup()
    print()
    print("Self-test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
