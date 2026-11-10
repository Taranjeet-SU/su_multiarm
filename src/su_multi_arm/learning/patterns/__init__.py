"""Pattern learning (addendum A1 §3.6).

- L4a: the deterministic miner (``mining``), the regex synthesiser (``synthesis``) and bootstrap
  coverage (``bootstrap``); the validation gate (``gate``) and the ``auto | manual`` promotion
  policy (``promotion``).
- L4b: the LLM proposer (``propose``), the egress policy (``egress``), run and day spend caps
  (``spend``) and the loop mine → propose → gate → write → reparse (``loop``).
"""

from .bootstrap import BootstrapReport, LogLine, bootstrap
from .egress import EgressPolicy, EgressRefused, LineRedactor, RedactedLine
from .gate import GateEvidence, GateResult, run_configured_gate, run_gate
from .loop import LearnReport, LearningSession, learn, session_from_config
from .mining import Cluster, MinerConfig, TemplateMiner
from .promotion import DecisionRefused, PromotionOutcome, human_decision, submit
from .propose import LlmProposer, PatternProposalReply
from .spend import DaySpendLedger, LearnerMeter
from .synthesis import synthesise

__all__ = [
    "BootstrapReport",
    "Cluster",
    "DaySpendLedger",
    "DecisionRefused",
    "EgressPolicy",
    "EgressRefused",
    "GateEvidence",
    "GateResult",
    "LearnReport",
    "LearnerMeter",
    "LearningSession",
    "LineRedactor",
    "LlmProposer",
    "LogLine",
    "MinerConfig",
    "PatternProposalReply",
    "PromotionOutcome",
    "RedactedLine",
    "TemplateMiner",
    "bootstrap",
    "human_decision",
    "learn",
    "run_configured_gate",
    "run_gate",
    "session_from_config",
    "submit",
    "synthesise",
]
