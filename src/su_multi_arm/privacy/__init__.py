"""Safe redaction (addendum A1 §3.2.3, slice B11): named built-in redactors, user redactors on the
pattern engine, and a content-free leak scan."""

from .redactors import BUILTIN, RedactionFailed, SafeRedactor, builtin_redactors, safe_redactor
from .scan import ScanReport, scan_paths, scan_text

__all__ = ["BUILTIN", "RedactionFailed", "SafeRedactor", "ScanReport", "builtin_redactors", "safe_redactor", "scan_paths", "scan_text"]
