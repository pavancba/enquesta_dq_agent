"""
Router Agent — Layer 3 (Decision)

For each Finding produced by Layer 2, decides the action.

Routing logic by rule:
  - Rule 1 (extra comma)       -> always QUARANTINE
                                  (schema break, cannot determine right fix)

  - Rule 2 (trailing negative) -> AUTO_CORRECT
                                  Move minus from end to front:
                                    "2000.00-"  -> "-2000.00"
                                    "50.00-"    -> "-50.00"
                                    "12000.00-" -> "-12000.00"
                                  Numeric value unchanged; only the
                                  representation is normalized.

  - Rule 3 (CIBIL comment)     -> FLAG_FOR_REVIEW if LLM confidence >= 0.50
                                  else ACCEPT
                                  NEVER AUTO_CORRECT — CIBIL Comments
                                  require human business judgment.
                                  Flagged rows trigger an email to the
                                  billing team via Notifier.

This is what makes the system "agentic" — judgment per finding rather
than a fixed pipeline.

TODO (next milestone): implement routing logic.
"""
