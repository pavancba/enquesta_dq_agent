"""
Anomaly Detector — Layer 2 (Validation), LLM-powered

Implements Rule 3 (invalid_cibil_comment): calls the local Ollama
model to judge whether each CIBL_COMMENTS value looks legitimate.

The prompt provides the LLM with:
  - The value being checked
  - Known-good examples from rules.yaml
  - Known-bad examples from rules.yaml
  - Row context (account, class, amount) for correlation

Returns a structured verdict:
  {
    "verdict": "valid" | "suspicious" | "invalid",
    "confidence": 0.0..1.0,
    "reason": "...",
    "recommended_action": "auto_correct" | "quarantine" | "flag" | "accept"
  }

This is the "agentic" part of the agent — judgment, not just rules.

TODO (next milestone): implement Ollama call + JSON parsing + fallback
to mock_llm mode when Ollama is unavailable.
"""
