"""
Data contracts used across the agent's modules.

Every module communicates via these typed objects. This makes the flow
explicit, testable, and easy to log to the audit DB.

TODO (next milestone): implement field validators where useful.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Action(str, Enum):
    AUTO_CORRECT = "auto_correct"
    QUARANTINE = "quarantine"
    FLAG_FOR_REVIEW = "flag_for_review"
    ACCEPT = "accept"


class Finding(BaseModel):
    """A single data-quality issue detected on one row."""
    run_id: str
    rule_id: str
    rule_name: str
    row_index: int
    column: Optional[str] = None
    raw_value: Optional[str] = None
    severity: Severity
    description: str
    confidence: float = 1.0
    # R003-only: the LLM's verdict word ("valid"|"suspicious"|"invalid").
    # The router routes on this, not on confidence — see router_agent._route_r003.
    verdict: Optional[str] = None
    llm_reasoning: Optional[str] = None   # filled in for R003 (LLM judgments)


class Decision(BaseModel):
    """Router's verdict for a single finding."""
    finding: Finding
    action: Action
    corrected_value: Optional[str] = None
    reasoning: str


class FileRun(BaseModel):
    """One end-to-end processing run for one input file."""
    run_id: str
    file_name: str
    file_path: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    total_rows: int = 0
    auto_corrected: int = 0
    quarantined: int = 0
    flagged: int = 0
    status: str = "in_progress"   # in_progress | ok | elevated | held_for_hitl | failed
