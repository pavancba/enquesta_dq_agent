"""
Corrector — Layer 3 (Decision)

Applies deterministic fixes to rows the Router marked AUTO_CORRECT.

Currently handles ONE correction type:

  RULE 2 — Trailing-negative amount (legacy mainframe format)
  -------------------------------------------------------------
  Applies to: CIBL_AMT_5, CIBL_AMT_95
  Transformation: move trailing minus sign to the front
      "50.00-"    -> "-50.00"
      "2000.00-"  -> "-2000.00"
      "12000.00-" -> "-12000.00"

  Numeric value is unchanged — still negative two thousand, etc.
  Only the representation is normalized to standard "leading minus".

NOT auto-correctable (handled elsewhere):
  - Rule 1 (extra comma)         -> quarantined, humans decide
  - Rule 3 (invalid CIBIL Comment) -> flagged + emailed, humans decide

The original raw value is preserved in the audit log so every
correction is fully traceable and reversible.

TODO (next milestone): implement.
"""
