# Enquesta Data Quality Agent

A local, on-prem agentic AI system that catches and corrects data quality issues
in daily Enquesta billing files before they corrupt downstream Salesforce records.

**Demo scope:** Layer 1 (Data Quality) only. Process monitoring (Layer 2) is
deliberately out of scope for the first demo.

---

## Architecture (5 layers, 12 modules)

```
┌─────────────────────────────────────────────────────────────────┐
│                   ENQUESTA DQ AGENT — DEMO                       │
│                  one agent, one audit log                        │
└─────────────────────────────────────────────────────────────────┘

LAYER 1  INGESTION       file_watcher  -> file_loader
LAYER 2  VALIDATION      schema_validator -> rule_engine -> anomaly_detector
LAYER 3  DECISION        router_agent -> corrector -> quarantine_handler
LAYER 4  SUPERVISION     supervisor_agent -> notifier
LAYER 5  AUDIT & OUTPUT  audit_logger -> report_generator
```

All layers are wired together by `src/graph/agent_graph.py` (LangGraph).

---

## Rules implemented in the demo

| Rule | What it catches | Type | Action |
|---|---|---|---|
| **R001** | Extra comma in data adds an extra column | Deterministic | Quarantine |
| **R002** | Trailing-negative amount (`2000.00-` → `-2000.00`) | Deterministic | Auto-correct |
| **R003** | Invalid / suspicious CIBIL Comment | **LLM judgment (Ollama)** | **Flag + Email** |

**Why no auto-correct for Rule 3:** CIBIL Comments require business judgment.
The agent surfaces the issue with reasoning; humans decide the fix.
Flagged rows trigger an email to the billing team (configured in `settings.yaml`).

Rules live in `config/rules.yaml` — tunable without code changes.

---

## What the data actually looks like

The rules above were derived from analyzing a **206-file production archive**
(Jun 2025 – May 2026) containing **4,457 rows** of real Enquesta Bill Print data.

### File-level health

```
Out of 206 files:
  Clean (no issues):         79 files   38.7%
  Has at least one issue:   114 files   55.3%
  Empty / header-only:       13 files    6.3%
```

### Issue frequency (real production rates)

| Issue | Files affected | % | In demo? |
|---|---|---|---|
| Trailing-negative amounts (`50.00-`) | 98 | 48.0% | ✅ Rule 2 |
| Internal duplicate rows | 73 | 35.8% | 🗺 Roadmap |
| Freeform "BILL #" comments | 41 | 20.1% | ✅ Rule 3 |
| Non-standard `CLASS1` | 14 | 6.9% | 🗺 Roadmap |
| Real extra column in data | 13 | 6.4% | ✅ Rule 1 |
| Invalid state code | 13 | 6.4% | 🗺 Roadmap |

### CIBIL_COMMENTS distribution (4,457 rows analyzed)

```
IT########  (invoice codes)   2,509   56%   ── known valid
VO-#####    (voucher codes)     828   19%   ── known valid
PM-#####    (payment codes)     193    4%   ── known valid
VAC-####    (vacancy codes)      20    0%   ── known valid
                                       ─────────────
                              3,550   80%   "happy path"

VP BILL #: …  (freeform)        129    3%   ── needs LLM judgment
Mixed / lowercase / empty       150    3%   ── needs LLM judgment
Other patterns                  628   14%   ── needs LLM judgment
```

**The four code families (IT/VO/PM/VAC) cover ~80% of comments.** The LLM only
judges the remaining ~20% — the residual cases where deterministic rules don't
apply. This is the right division of labor: cheap rules first, expensive judgment
second.

---

## Stack

| Layer | Choice |
|---|---|
| Runtime | Python 3.11 |
| Agent orchestration | LangGraph |
| LLM | Ollama with `llama3.2:3b` |
| Data | Pandas + Pydantic |
| Storage | SQLite (audit) + local folders (data) |
| UI | Streamlit |
| Target machine | MacBook Air M2, 8 GB RAM |

---

## Setup (one-time, on your Mac)

```bash
# 1. Clone and enter
git clone <this-repo>
cd enquesta_dq_agent

# 2. Python virtual environment
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Install Ollama (one-time)
brew install ollama

# 4. Pull the model (one-time, ~2 GB download)
ollama pull llama3.2:3b

# 5. Configure email notifications
cp .env.example .env
# Edit .env with your SMTP credentials (Gmail app password recommended)
```

### Gmail SMTP setup (recommended for the demo)

The demo sends flagged-row alerts to **pavan.gali@accelance.io** via SMTP.

1. Use a Gmail sender account (or any account you control)
2. Enable 2-factor authentication on that account
3. Generate an App Password: https://myaccount.google.com/apppasswords
4. Put the credentials in `.env`:
   ```
   SMTP_USERNAME=your-sender@gmail.com
   SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx
   SMTP_FROM_ADDRESS=your-sender@gmail.com
   ```

**Demo-day safety:** if SMTP fails, the agent automatically falls back to
mock mode and writes the email as a `.eml` file in `data/sent_emails/`. The
demo continues without interruption.

---

## Run the demo

```bash
./run_demo.sh
```

This script verifies Ollama, pre-warms the model, and opens the Streamlit UI
at `http://localhost:8501`.

Then:
1. Drag a CSV from `samples/` into the UI's upload box
2. Watch the agent process it in real time
3. Inspect the audit log and download the clean / quarantine / flagged files

---

## Demo day checklist

- [ ] Mac plugged into power (battery throttles Ollama)
- [ ] Quit Slack, Teams, Chrome (free up RAM on 8 GB Air)
- [ ] Verify Ollama is running: `ollama list`
- [ ] Pre-warm: `ollama run llama3.2:3b "ready"`
- [ ] Have backup mode ready: set `llm.enabled: false` in settings.yaml if needed

---

## Future: Cloud Deployment

The architecture is **on-prem-first by design** — no cloud egress required
for any component. The City's IT team can deploy this to any internal
server (Windows Server 2022 / RHEL 8+) without changing the codebase.

If a cloud-hosted variant is later requested (e.g., for staging or DR):
- AWS GovCloud / Azure Government are both supported (the stack is
  vendor-neutral Python)
- Ollama can be swapped for AWS Bedrock or Azure OpenAI without
  changing the agent logic (only `src/validation/anomaly_detector.py`)
- SQLite audit DB can be swapped for managed Postgres
- Streamlit can be replaced with a hardened internal web portal

---

## Demo dataset

The `samples/` folder contains 7 curated CSVs, each demonstrating a specific
agent behavior. See `samples/README.md` for full details and demo flow.

| File | Purpose |
|---|---|
| `demo_01_clean.csv` | Happy path — agent says "all clean" |
| `demo_02_rule2_only.csv` | Trailing-negative auto-correction |
| `demo_03_rule3_freeform.csv` | LLM judgment + email alert |
| `demo_04_multi_issue.csv` | Multiple rules firing together |
| `demo_05_edge_empty.csv` | Empty-file edge case |
| `demo_06_duplicates.csv` | Duplicate-row detection |
| `demo_07_showcase_synthetic.csv` | Every rule, one file |

---

## Roadmap

**v1 (this demo):** Rules 1, 2, 3 — schema integrity, trailing-negative correction,
CIBIL Comment LLM judgment with email alerts.

**v1.1 (scoped from real data, ready to enable):**
- Rule 4 — duplicate row detection (35.8% of files affected)
- Rule 5 — invalid state code (6.4% of files affected)
- Rule 6 — CLASS1 format validation (6.9% of files affected)

**v2 (Layer 2 — process monitoring):**
- File arrival monitoring (Watcher)
- Pipeline health metrics
- Cross-file pattern detection

---

## Status

| Phase | Status |
|---|---|
| Discovery & data analysis (206 files, 4,457 rows) | ✅ Complete |
| Architecture (5 layers, 12 modules) | ✅ Complete |
| Scaffold + configs + curated demo data | ✅ Complete |
| Module implementation | ⏳ In progress |
| End-to-end test | ⏳ Pending |
| Demo rehearsal | ⏳ Pending |

```
.
├── config/             # rules.yaml + settings.yaml
├── data/               # inbox / clean / quarantine / flagged / sent_emails
├── samples/            # 7 curated demo CSVs + README
├── src/                # 12 modules across 5 layers
│   ├── ingestion/
│   ├── validation/
│   ├── decision/
│   ├── supervision/
│   ├── audit/
│   ├── graph/
│   └── models/
├── ui/                 # Streamlit app
├── tests/              # pytest suite (TODO)
├── requirements.txt
├── .env.example
├── run_demo.sh
└── README.md
```
