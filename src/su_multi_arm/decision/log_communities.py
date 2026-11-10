"""Recognise explicit shared-error/community requests without guessing arbitrary topics."""
import re


def shared_error_query(query: str) -> bool:
    return bool(
        re.search(r"\b(?:errors?|issues?|failures?|patterns?)\b", query, re.I)
        and re.search(r"\b(?:shared|common|cross[- ]tenant|across (?:clients|tenants|services)|communities)\b", query, re.I)
    )
