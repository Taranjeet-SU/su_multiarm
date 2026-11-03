"""Compatibility shim: predicate support moved to ``su_multi_arm.extraction.predicate_support``."""

from .extraction.predicate_support import PredicateSupport, Support, languages

__all__ = ["PredicateSupport", "Support", "languages"]
