# CLAUDE.md — Project context for Claude Code

> This file briefs you (Claude Code) on the project so you can pick up exactly
> where the previous session left off. **Read this first** before touching code.

## What this project is

An on-prem **Data Quality Agent** for processing daily Enquesta Bill Print files
before they corrupt Salesforce records. Built for a municipal billing
department. Demo scope: Layer 1 (Data Quality) only.

**Architecture: 5 layers, 12 modules.** All wired by a LangGraph state machine.

## Stack — non-negotiable choices

- **Python 3.13** (already configured in `.venv`)
- **LangGraph 1.0+** for agent orchestration
- **Ollama with `llama3.2:3b`** for local LLM (on-prem story is core to the pitch)
- **SQLite** for the audit ledger (`audit.db`)
- **Streamlit** for the demo UI
- **Pandas + Pydantic** for data + schemas
- **smtplib + python-dotenv** for email notifications

Target machine: **MacBook Air M2, 8 GB RAM** — memory budget matters.

## Where we are right now

| Module | File | Status |
|---|---|---|
| 1. Audit logger | `src/audit/audit_logger.py` | ✅ Done + self-test green |
| 2. File loader | `src/ingestion/file_loader.py` | ✅ Done + self-test green |
| 2. Schema validator (Rule 1) | `src/validation/schema_validator.py` | ✅ Done + self-test green |
| 3. Rule engine (Rule 2) | `src/validation/rule_engine.py` | ✅ Done + self-test green |
| 3. Corrector | `src/decision/corrector.py` | ✅ Done + self-test green |
| **4. Anomaly detector (Rule 3 + Ollama)** | `src/validation/anomaly_detector.py` | ⏸ **TODO — START HERE** |
| 5. Router agent | `src/decision/router_agent.py` | Stub only |
| 6. Quarantine handler | `src/decision/quarantine_handler.py` | Stub only |
| 7. Supervisor agent | `src/supervision/supervisor_agent.py` | Stub only |
| 8. Notifier (SMTP email) | `src/supervision/notifier.py` | Stub only |
| 9. Report generator | `src/audit/report_generator.py` | Stub only |
| 10. Agent graph (LangGraph) | `src/graph/agent_graph.py` | Stub only |
| 11. Streamlit UI | `ui/app.py` | Stub only |

## The 3 rules (v1 scope)

| Rule | What it catches | Action |
|---|---|---|
| **R001** | Extra comma in data adds an extra column | Quarantine (cosmetic trailing → auto-correct) |
| **R002** | Trailing-negative amount (`2000.00-` → `-2000.00`) | Auto-correct |
| **R003** | Invalid / suspicious CIBIL_COMMENTS | **Flag + Email** (NEVER auto-correct) |

Rules live in `config/rules.yaml`. Runtime config in `config/settings.yaml`.

## Critical design decisions already made

1. **Audit log is the source of truth.** Every finding, decision, correction,
   and LLM call goes to SQLite. Reviewer can audit anything.

2. **CIBIL_COMMENTS is never auto-corrected.** Even high-confidence LLM
   verdicts → flag for human review, not auto-fix. Business judgment required.

3. **Email to `pavan.gali@accelance.io`** for every flagged row batch.
   Real SMTP via Gmail App Password (config in `.env`, never committed).
   `.env.example` is the template.

4. **Four known-good CIBIL_COMMENT code families** (from analysis of 206
   production files, 4,457 rows): IT*, VO-*, PM-*, VAC-*. ~80% of comments
   match these. The LLM only needs to judge the residual ~20%.

5. **Two-tier detection in Rule 3** (decision for anomaly_detector.py):
   - First check deterministically against IT/VO/PM/VAC patterns — if match → ACCEPT
   - Only call LLM for residual non-matching values
   - Saves ~80% of LLM calls

6. **Verdict vocabulary**: `valid | suspicious | invalid` with `confidence: 0.0..1.0`

7. **Demo-day safety**: every module has a mock/fallback mode so demo doesn't
   crash if Ollama or SMTP misbehaves. Configurable in `settings.yaml`.

## Module patterns to follow

Every module so far has:
- A docstring header explaining purpose + layer + interaction
- Dataclass or Pydantic-based return types (in `src/models/schemas.py`)
- A `_self_test()` function and `if __name__ == "__main__"` block
- Self-test is runnable: `python -m src.<package>.<module>`
- Self-test exercises every public function with real data from `samples/`

When you finish a module, run its self-test BEFORE saying you're done.

## What's in `samples/`

7 curated CSVs (6 real production files + 1 synthetic). Each has a specific
demo purpose. Full documentation in `samples/README.md`.

The synthetic showcase file `demo_07_showcase_synthetic.csv` is built to
trigger every rule and is the best file to test new modules against.

## What's in the audit DB right now

Run `sqlite3 audit.db "SELECT run_id, file_name, total_rows, auto_corrected FROM file_runs;"`
to see prior test runs. Modules 1-3 wrote real data here during their tests.

## Next module: `anomaly_detector.py`

Implements Rule 3. Key requirements:

- Input: a pandas DataFrame (the well-formed rows from file_loader), the
  rules config dict, the audit logger instance, and the run_id
- Output: list of `Finding` objects for CIBL_COMMENTS values that look
  suspicious or invalid

Steps inside:
1. Iterate CIBL_COMMENTS column
2. Skip empty cells? (decide based on rules.yaml — empty IS a known-bad pattern)
3. For each non-empty value: regex check against IT*, VO-*, PM-*, VAC-* patterns
4. If matches → continue (no LLM call, no finding)
5. If doesn't match → build prompt with known-good + known-bad examples from
   rules.yaml, call Ollama with `format="json"` for structured output
6. Parse JSON response into LLMVerdict dataclass
7. Log every LLM call to `audit.log_llm_call(...)` with prompt, response, latency
8. Convert verdict into a Finding with `confidence` and `llm_reasoning` filled in
9. Apply threshold: if confidence >= `flag_for_review` threshold → produce
   Finding; else skip
10. Include a `--mock` mode controlled by `settings.yaml -> llm.enabled = false`
    that returns deterministic verdicts based on simple patterns (so demo
    can run if Ollama is dead)

Self-test should:
- Test against all 7 demo files
- Show: which comments were skipped by pattern-match (fast path)
- Show: which comments were sent to LLM (slow path)
- Show: each LLM verdict with reasoning
- Confirm: every LLM call appears in the `llm_calls` audit table

## Reference: the data
- 206 production files analyzed (Jun 2025 – May 2026)
- 4,457 total rows
- 48% of files have Rule 2 hits
- 20% of files have Rule 3 (freeform comments)
- 6.4% of files have Rule 1 (real extra column)

## How to talk to the user
- Pavan is a project owner, not a Python expert
- Show your work but don't over-explain Python internals
- Always run the self-test and paste the output before declaring done
- Commit each module with `feat(<layer>): <what>` message format
- If something fails, debug it; don't punt back to Pavan unless stuck
