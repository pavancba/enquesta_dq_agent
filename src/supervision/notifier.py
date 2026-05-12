"""
Notifier — Layer 4 (Supervision)

Delivers one consolidated email per run plus an always-on console summary.

Two delivery modes (settings.yaml -> email.mode):
  * "smtp" (default) — real send over STARTTLS using credentials from
    environment variables. Same connection pattern proven by
    scripts/test_smtp.py.
  * "mock"           — write the message as a .eml file under
    data/sent_emails/ (filename: {run_id_first_8}_{file_stem}.eml) so the
    output can be opened in Mail.app or parsed by stdlib email.

If mode="smtp" and the send fails for ANY reason (auth, connect, TLS,
recipient refused, timeout), the notifier silently falls back to mock
mode and captures the SMTP error in NotificationResult.error — the run
itself never crashes on email infrastructure problems.

Subject is chosen from SupervisorVerdict.status:
    ok            -> "[Enquesta DQ] {flagged} rows flagged for review — {file}"
    elevated      -> "[ELEVATED] [Enquesta DQ] ..."
    held_for_hitl -> "[HOLD — ACTION REQUIRED] {file} held for review — ..."

Email is skipped entirely when flagged_count == 0 AND
verdict.should_notify is False — console block still prints.

Run this file directly to self-test (mock mode):
    python -m src.supervision.notifier

Opt-in real SMTP send (will hit pavan.gali@accelance.io):
    python -m src.supervision.notifier --send-real-email
"""
from __future__ import annotations

import os
import smtplib
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Literal, Optional

import pandas as pd

try:
    from src.audit.audit_logger import AuditLogger
    from src.decision.quarantine_handler import SplitResult
    from src.models.schemas import Action
    from src.supervision.supervisor_agent import SupervisorVerdict
except ImportError:  # pragma: no cover
    from audit.audit_logger import AuditLogger  # type: ignore
    from decision.quarantine_handler import SplitResult  # type: ignore
    from models.schemas import Action  # type: ignore
    from supervision.supervisor_agent import SupervisorVerdict  # type: ignore


EmailMode = Literal["smtp", "mock", "skipped"]


@dataclass
class NotificationResult:
    """Outcome of one notify() call."""
    console_printed: bool
    email_sent: bool                 # True if an email was actually delivered
    email_mode: EmailMode            # smtp | mock | skipped
    email_path: Optional[Path] = None     # mock mode .eml file path
    error: Optional[str] = None          # captured SMTP error if fell back


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def notify(
    verdict: SupervisorVerdict,
    split_result: SplitResult,
    run_id: str,
    file_name: str,
    settings: dict,
    audit_logger: AuditLogger,
    df: Optional[pd.DataFrame] = None,
) -> NotificationResult:
    """
    Print a console summary and (conditionally) send the run's email.

    `df` is optional. When provided, per-row sections of the email body
    include the ACCOUNTNUMBER for each flagged row. Without it the email
    still ships, but rows show only the row index. The signature in the
    module-8 spec didn't include df; it's added here so the email body
    can satisfy the spec's Section 3 ("Row N (account_number=...)")
    without exposing internal model state via Findings.
    """
    email_cfg = (settings or {}).get("email") or {}
    mode_setting = str(email_cfg.get("mode", "smtp")).lower()
    recipients = list(email_cfg.get("recipients") or [])

    flagged_count = verdict.flagged_count
    should_skip = flagged_count == 0 and not verdict.should_notify

    # Build the message regardless of mode, so the console block can
    # quote the subject too.
    subject = _build_subject(verdict, file_name)
    body = _build_body(verdict, split_result, run_id, file_name, df)

    # ---- email delivery ---------------------------------------------------
    email_mode: EmailMode
    email_path: Optional[Path] = None
    email_sent = False
    error: Optional[str] = None

    if should_skip:
        email_mode = "skipped"
    elif not recipients:
        # Nothing to send to. Fall back to mock so there's still an artifact.
        email_mode = "mock"
        email_path = _write_mock(subject, body, email_cfg, run_id,
                                 file_name, from_addr=None, to_addrs=[])
        email_sent = True
        error = "no recipients configured; wrote .eml to mock dir"
    elif mode_setting == "mock":
        from_addr = _from_address(email_cfg) or "noreply@enquesta-dq.local"
        email_path = _write_mock(subject, body, email_cfg, run_id,
                                 file_name, from_addr=from_addr,
                                 to_addrs=recipients)
        email_mode = "mock"
        email_sent = True
    else:
        # mode == "smtp" — try real, fall back to mock on any failure.
        try:
            _send_smtp(subject, body, email_cfg, recipients)
            email_mode = "smtp"
            email_sent = True
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            from_addr = _from_address(email_cfg) or "noreply@enquesta-dq.local"
            email_path = _write_mock(subject, body, email_cfg, run_id,
                                     file_name, from_addr=from_addr,
                                     to_addrs=recipients)
            email_mode = "mock"
            email_sent = True   # we did write the mock file

    # ---- console block (always) ------------------------------------------
    _console_print(verdict, run_id, email_mode, email_path, recipients)

    # ---- audit ------------------------------------------------------------
    # Record the notification attempt as an llm_call-style row? No — that's
    # the wrong table. The decisions table is the right home conceptually,
    # but a "notify event" doesn't tie to a single finding. For demo scope
    # we don't add a new table; finish_run captures the run-level status
    # already, and the .eml / SMTP error string lives on NotificationResult
    # for the caller to log. (Mentioned for the reader's benefit; no action
    # needed here.)
    _ = audit_logger  # accepted for future expansion / interface stability

    return NotificationResult(
        console_printed=True,
        email_sent=email_sent,
        email_mode=email_mode,
        email_path=email_path,
        error=error,
    )


# ---------------------------------------------------------------------------
# Subject + body builders
# ---------------------------------------------------------------------------
def _build_subject(v: SupervisorVerdict, file_name: str) -> str:
    if v.status == "held_for_hitl":
        return (f"[HOLD — ACTION REQUIRED] {file_name} held for review — "
                f"{v.quarantined_count} quarantined")
    if v.status == "elevated":
        return (f"[ELEVATED] [Enquesta DQ] {v.flagged_count} flagged, "
                f"{v.quarantined_count} quarantined — {file_name}")
    return f"[Enquesta DQ] {v.flagged_count} rows flagged for review — {file_name}"


def _build_body(
    v: SupervisorVerdict,
    sr: SplitResult,
    run_id: str,
    file_name: str,
    df: Optional[pd.DataFrame],
) -> str:
    lines: list[str] = []

    # Section 1 — Run summary
    lines.append("Run summary")
    lines.append("-" * 60)
    lines.append(f"File:               {file_name}")
    lines.append(f"Run ID:             {run_id}")
    lines.append(f"Total rows:         {v.total_rows}")
    lines.append(f"Clean:              {len(sr.clean_rows)}")
    lines.append(f"Quarantined:        {v.quarantined_count}")
    lines.append(f"Flagged for review: {v.flagged_count}")
    lines.append("")

    # Section 2 — Supervisor verdict
    lines.append("Supervisor verdict")
    lines.append("-" * 60)
    lines.append(f"Status:           {v.status}")
    lines.append(f"Quarantine ratio: {v.quarantine_ratio:.2%}")
    lines.append(f"Flag ratio:       {v.flag_ratio:.2%}")
    if v.reasons:
        lines.append("Reasons:")
        for r in v.reasons:
            lines.append(f"  - {r}")
    lines.append("")

    # Section 3 — Flagged rows
    if v.flagged_count > 0:
        lines.append("Flagged rows (require human judgment)")
        lines.append("-" * 60)
        for row_idx in sr.flagged_rows:
            account = _account_for_row(df, row_idx)
            acct_str = f", account_number={account}" if account else ""
            lines.append(f"Row {row_idx}{acct_str}:")
            decisions = sr.row_to_decisions.get(row_idx, [])
            # Only show R003 / FLAG_FOR_REVIEW decisions for this row;
            # other decisions on the same row aren't the email's subject.
            for d in decisions:
                if d.action != Action.FLAG_FOR_REVIEW:
                    continue
                f = d.finding
                lines.append(f"  Suspicious value: {f.raw_value!r}")
                lines.append(f"  LLM verdict:      {_verdict_word(d.reasoning, f)}")
                lines.append(f"  Confidence:       {f.confidence:.2f}")
                lines.append(f"  Reasoning:        {f.llm_reasoning or '(none)'}")
            lines.append("  " + "-" * 30)
        lines.append("")

    # Section 4 — Footer
    lines.append("Footer")
    lines.append("-" * 60)
    lines.append(f"Full audit trail: audit.db / run_id = {run_id}")
    lines.append(f"Generated at:     "
                 f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")

    return "\n".join(lines) + "\n"


def _verdict_word(reasoning: str, finding) -> str:
    """Pull the verdict label out of the finding description, falling back."""
    desc = finding.description or ""
    for word in ("invalid", "suspicious", "valid"):
        if word in desc.lower():
            return word
    return "suspicious"


def _account_for_row(df: Optional[pd.DataFrame], row_idx: int) -> Optional[str]:
    if df is None or "ACCOUNTNUMBER" not in df.columns:
        return None
    try:
        return str(df.at[row_idx, "ACCOUNTNUMBER"])
    except (KeyError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Delivery helpers
# ---------------------------------------------------------------------------
def _from_address(email_cfg: dict) -> Optional[str]:
    """Read SMTP_FROM_ADDRESS (or whichever env var the config names)."""
    smtp_cfg = email_cfg.get("smtp") or {}
    env_name = smtp_cfg.get("from_address_env", "SMTP_FROM_ADDRESS")
    return os.environ.get(env_name)


def _send_smtp(
    subject: str,
    body: str,
    email_cfg: dict,
    recipients: list[str],
) -> None:
    """Open STARTTLS, login, send. Raises on any error — caller catches."""
    smtp_cfg = email_cfg.get("smtp") or {}
    host = os.environ.get(smtp_cfg.get("host_env", "SMTP_HOST"))
    port_raw = os.environ.get(smtp_cfg.get("port_env", "SMTP_PORT"))
    username = os.environ.get(smtp_cfg.get("username_env", "SMTP_USERNAME"))
    password = os.environ.get(smtp_cfg.get("password_env", "SMTP_PASSWORD"))
    from_addr = os.environ.get(
        smtp_cfg.get("from_address_env", "SMTP_FROM_ADDRESS"))

    if not all([host, port_raw, username, password, from_addr]):
        missing = [n for n, v in (
            ("SMTP_HOST", host), ("SMTP_PORT", port_raw),
            ("SMTP_USERNAME", username), ("SMTP_PASSWORD", password),
            ("SMTP_FROM_ADDRESS", from_addr),
        ) if not v]
        raise RuntimeError(f"missing SMTP env vars: {', '.join(missing)}")

    port = int(port_raw)  # type: ignore[arg-type]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=20) as server:  # type: ignore[arg-type]
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(username, password)  # type: ignore[arg-type]
        server.send_message(msg)


def _write_mock(
    subject: str,
    body: str,
    email_cfg: dict,
    run_id: str,
    file_name: str,
    from_addr: Optional[str],
    to_addrs: list[str],
) -> Path:
    """Serialize the message as a .eml so Mail.app can open it."""
    mock_cfg = email_cfg.get("mock") or {}
    out_dir = Path(mock_cfg.get("output_dir", "data/sent_emails"))
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = Path(file_name).stem
    fname = f"{(run_id or 'norunid')[:8]}_{stem}.eml"
    out_path = out_dir / fname

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr or "noreply@enquesta-dq.local"
    if to_addrs:
        msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)

    out_path.write_bytes(bytes(msg))
    return out_path


def _console_print(
    v: SupervisorVerdict,
    run_id: str,
    email_mode: EmailMode,
    email_path: Optional[Path],
    recipients: list[str],
) -> None:
    if email_mode == "smtp":
        email_target = ", ".join(recipients) if recipients else "(none)"
    elif email_mode == "mock":
        email_target = str(email_path) if email_path else "(no path)"
    else:
        email_target = "(skipped — no notification required)"

    print("[Enquesta DQ] Run complete")
    print(
        f"Status: {v.status}  |  "
        f"Total: {v.total_rows}  |  "
        f"Clean: {v.total_rows - v.quarantined_count - v.flagged_count}  |  "
        f"Quar: {v.quarantined_count}  |  "
        f"Flag: {v.flagged_count}"
    )
    print(f"Quarantine ratio: {v.quarantine_ratio:.2%}  |  "
          f"Flag ratio: {v.flag_ratio:.2%}")
    print(f"Email: {email_mode} -> {email_target}")
    print(f"Run ID: {run_id}")
    print("Full audit: audit.db")


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import io
    import tempfile
    from contextlib import redirect_stdout
    from email.parser import BytesParser
    from email import policy
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
    except ImportError:  # pragma: no cover
        from ingestion.file_loader import load_csv  # type: ignore
        from validation.schema_validator import validate_schema  # type: ignore
        from validation.rule_engine import run_rules  # type: ignore
        from validation.anomaly_detector import detect_cibil_anomalies  # type: ignore
        from decision.router_agent import route_findings  # type: ignore
        from decision.corrector import apply_corrections  # type: ignore
        from decision.quarantine_handler import split_and_write  # type: ignore
        from supervision.supervisor_agent import evaluate_run  # type: ignore

    rules = yaml.safe_load(Path("config/rules.yaml").read_text())
    settings = yaml.safe_load(Path("config/settings.yaml").read_text())
    settings_mock_llm = {**settings,
                        "llm": {**settings.get("llm", {}), "enabled": False}}

    expected_cols = rules["schema"]["expected_columns"]
    fp = Path("samples/demo_07_showcase_synthetic.csv")
    if not fp.exists():
        print(f"FAIL: {fp} not found.")
        return 1

    send_real = "--send-real-email" in sys.argv

    print("=" * 72)
    print("Notifier self-test")
    print(f"  sample file:        {fp.name}")
    print(f"  Phase B real SMTP:  {'YES (--send-real-email)' if send_real else 'no'}")
    print("=" * 72)

    # ---- Run the pipeline once; reuse outputs for both phases ----
    tmp = tempfile.TemporaryDirectory()
    audit = AuditLogger(Path(tmp.name) / "notif_audit.db")
    run_id = "notif-self-test-abcdefgh"

    load_result = load_csv(fp, expected_cols)
    schema_findings = validate_schema(load_result, run_id=run_id)
    rule_findings = run_rules(load_result.dataframe, rules, run_id=run_id)
    anomaly_findings, _ = detect_cibil_anomalies(
        df=load_result.dataframe, rules_config=rules,
        audit_logger=audit, run_id=run_id, settings=settings_mock_llm,
    )
    findings = [*schema_findings, *rule_findings, *anomaly_findings]
    decisions = route_findings(findings, settings_mock_llm)
    corrected_df, _ = apply_corrections(load_result.dataframe, findings)
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
    print(f"  pipeline produced: "
          f"clean={len(split.clean_rows)} "
          f"quar={len(split.quarantined_rows)} "
          f"flag={len(split.flagged_rows)} "
          f"status={verdict.status}")
    print(f"  should_notify={verdict.should_notify}")

    # ---- Phase A: mock mode ----
    print()
    print("-" * 72)
    print("Phase A: mock mode")
    print("-" * 72)
    mock_settings = {
        **settings,
        "email": {
            **(settings.get("email") or {}),
            "mode": "mock",
            "mock": {"output_dir": str(Path(tmp.name) / "sent_emails")},
        },
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        result_a = notify(
            verdict=verdict, split_result=split,
            run_id=run_id, file_name=fp.name,
            settings=mock_settings, audit_logger=audit,
            df=corrected_df,
        )
    console_text = buf.getvalue()
    print(console_text.rstrip())
    print()
    print(f"  email_mode:  {result_a.email_mode}")
    print(f"  email_sent:  {result_a.email_sent}")
    print(f"  email_path:  {result_a.email_path}")
    print(f"  error:       {result_a.error}")

    assert result_a.email_mode == "mock"
    assert result_a.email_sent is True
    assert result_a.email_path is not None and result_a.email_path.exists()

    # Parse the .eml with stdlib (mailparser isn't a dep; stdlib does it)
    with result_a.email_path.open("rb") as f:
        parsed = BytesParser(policy=policy.default).parse(f)
    subject = parsed["Subject"]
    body_text = parsed.get_body(preferencelist=("plain",)).get_content()
    print()
    print(f"  parsed Subject: {subject}")
    # Subject for "ok" status (demo_07's actual outcome)
    assert subject.startswith("[Enquesta DQ]"), f"bad subject: {subject!r}"
    assert "2 rows flagged" in subject, f"flagged count missing: {subject!r}"
    assert fp.name in subject, "file name missing from subject"

    # Body invariants
    assert "Run summary" in body_text
    assert "Supervisor verdict" in body_text
    assert "Flagged rows" in body_text
    # The two flagged values from demo_07
    assert "VP BILL #: 27099999" in body_text
    assert "penalty removal" in body_text
    assert run_id in body_text
    # Account-number lookup worked
    assert "account_number=" in body_text
    # Console block printed
    assert "[Enquesta DQ] Run complete" in console_text
    assert "Email: mock ->" in console_text
    print("  Phase A invariants: OK")

    # ---- Phase B: real SMTP (opt-in) ----
    print()
    print("-" * 72)
    print(f"Phase B: real SMTP {'(executing)' if send_real else '(skipped — pass --send-real-email to enable)'}")
    print("-" * 72)
    if send_real:
        # Load .env so SMTP_* vars are available
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        smtp_settings = {
            **settings,
            "email": {
                **(settings.get("email") or {}),
                "mode": "smtp",
            },
        }
        start = time.perf_counter()
        result_b = notify(
            verdict=verdict, split_result=split,
            run_id=run_id, file_name=fp.name,
            settings=smtp_settings, audit_logger=audit,
            df=corrected_df,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        print()
        print(f"  email_mode:  {result_b.email_mode}")
        print(f"  email_sent:  {result_b.email_sent}")
        print(f"  error:       {result_b.error}")
        print(f"  real email sent in {latency_ms}ms")
        assert result_b.email_mode == "smtp", \
            f"expected smtp, got {result_b.email_mode} (error={result_b.error})"
        assert result_b.error is None
        assert result_b.email_sent is True
        print("  Phase B invariants: OK")
    else:
        print("  (use `python -m src.supervision.notifier --send-real-email` to send)")

    tmp.cleanup()
    print()
    print("Self-test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
