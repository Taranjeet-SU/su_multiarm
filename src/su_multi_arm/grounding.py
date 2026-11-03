"""Compatibility shim: grounding moved to ``su_multi_arm.extraction.grounding`` (plan §2.3)."""

from .extraction.grounding import Ambiguous, Located, MatchLevel, Step, contains, locate, locate_in_record, normalise

__all__ = ["Ambiguous", "Located", "MatchLevel", "Step", "contains", "locate", "locate_in_record", "normalise"]
