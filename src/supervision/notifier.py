"""
Notifier — Layer 4 (Supervision)

Delivers run summaries and HITL alerts via two channels:

  1. Console / UI summary
     - Shown after every run
     - Structured ("12 auto-corrected, 0 quarantined, 2 flagged")

  2. EMAIL ALERTS
     - Triggered when Rule 3 flags one or more CIBL_COMMENTS values
     - ONE consolidated email per file run (not per-row spam)
     - Recipients from config/settings.yaml: email.recipients
     - Subject: "[Enquesta DQ] N rows flagged for review — <filename>"
     - Body includes for each flagged row:
         * Account number
         * Suspicious CIBIL_COMMENTS value
         * LLM verdict + confidence + reasoning
         * Audit log reference for drill-down

Two delivery modes (configurable in settings.yaml -> email.mode):
  - "smtp": real send via SMTP (credentials from env vars only)
  - "mock": write .eml file to data/sent_emails/ for inspection

SMTP secrets are NEVER stored in YAML — they come from environment
variables (SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, etc.). The
settings.yaml file only references the variable NAMES.

TODO (next milestone): implement.
"""
