"""Pattern engine, validator and ReDoS lint (addendum §3.4.1).

Two engines, chosen explicitly by configuration and never substituted for each other:

- ``re2`` (``google-re2``, extra ``patterns``): linear-time matching, the multi-pattern ``Set``
  prefilter, and the only engine whose results are deterministic under every input.
- ``regex`` (the ``regex`` module, extra ``patterns-lite``): a development fallback. Every pattern is
  compiled ASCII-only (``regex.ASCII``, the ``(?a)`` semantics of RE2's ``\\w``/``\\d``/``\\s``) and
  every search runs under a per-call timeout. A timeout makes the line's result non-deterministic: it is
  reported as ``timeout`` and the engine records ``deterministic: False`` in the provenance.

A missing engine is a typed ``PatternEngineUnavailable``; there is no automatic fallback.

The validator applies under both engines. It parses a pattern with the standard library's regular
expression parser (the only AST available without another dependency) and refuses anything outside
the RE2 subset, then applies the ReDoS rules on that AST:

1. star height <= 1: no repeat with an upper bound >= 2 contains another such repeat when either is
   unbounded (``(a+)+``, ``(\\w+\\s?)*``);
2. no quantified alternation whose branches can start with a common character (``(a|ab)+``), and no
   quantified alternation with an empty branch;
3. no two adjacent unbounded repeats over intersecting classes (``\\S+\\S+``, ``.*x?.*``);
4. every unbounded repeat is followed by an anchor or by a character it cannot consume (``[^"]*"``,
   ``\\S+ ``, ``[0-9]+$``); reaching the end of an unanchored pattern after it is refused.

When ``google-re2`` is importable the validator also compiles the pattern with RE2, so the RE2 subset
check is definitive there; without it the subset is enforced by the AST rules above.
"""

from __future__ import annotations

import importlib.metadata
import re
import re._constants as sre_c  # stdlib parser internals: the AST the lint walks
import re._parser as sre_parse
from dataclasses import dataclass, field
from typing import Any, Literal

from ...core.errors import SuError

EngineKind = Literal["re2", "regex"]
VALIDATOR_VERSION = "1"
MAX_UNICODE = 0x10FFFF
RE2_MAX_REPEAT = 1000
_ALLOWED_FLAGS = set("ims-")
# Escapes accepted inside a library pattern (RE2 and the stdlib parser agree on all of them).
_ALLOWED_ESCAPE_LETTERS = set("dDwWsSbBAntrfvx")


class PatternEngineUnavailable(SuError, RuntimeError):
    """The configured engine's package is not installed. Never replaced by the other engine."""


class PatternTimeout(SuError, RuntimeError):
    """A ``regex``-engine search exceeded its per-call timeout (non-deterministic result)."""


@dataclass(frozen=True)
class LintIssue:
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    pattern_id: str | None = None
    source: str | None = None  # file name (or "config") the entry came from

    def render(self) -> str:
        where = ":".join(p for p in (self.source, self.pattern_id) if p)
        return f"{self.severity} {self.code}{' [' + where + ']' if where else ''}: {self.message}"


# --- character sets as sorted, disjoint inclusive intervals -------------------------------------

Charset = tuple[tuple[int, int], ...]
EMPTY: Charset = ()
ANY: Charset = ((0, MAX_UNICODE),)


def _norm(intervals: list[tuple[int, int]]) -> Charset:
    out: list[list[int]] = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return tuple((a, b) for a, b in out)


def _union(*sets: Charset) -> Charset:
    return _norm([iv for s in sets for iv in s])


def _complement(s: Charset) -> Charset:
    out, start = [], 0
    for lo, hi in s:
        if lo > start:
            out.append((start, lo - 1))
        start = hi + 1
    if start <= MAX_UNICODE:
        out.append((start, MAX_UNICODE))
    return tuple(out)


def _intersects(a: Charset, b: Charset) -> bool:
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i][1] < b[j][0]:
            i += 1
        elif b[j][1] < a[i][0]:
            j += 1
        else:
            return True
    return False


def _fold(s: Charset) -> Charset:
    """Add ASCII case counterparts (case-insensitive patterns)."""
    extra = []
    for lo, hi in s:
        for base, other in ((ord("a"), ord("A")), (ord("A"), ord("a"))):
            a, b = max(lo, base), min(hi, base + 25)
            if a <= b:
                extra.append((a - base + other, b - base + other))
    return _union(s, tuple(extra))


_DIGIT: Charset = ((48, 57),)
_WORD: Charset = _norm([(48, 57), (65, 90), (95, 95), (97, 122)])
_SPACE: Charset = _norm([(9, 13), (32, 32)])
_CATEGORIES = {
    sre_c.CATEGORY_DIGIT: _DIGIT,
    sre_c.CATEGORY_NOT_DIGIT: _complement(_DIGIT),
    sre_c.CATEGORY_WORD: _WORD,
    sre_c.CATEGORY_NOT_WORD: _complement(_WORD),
    sre_c.CATEGORY_SPACE: _SPACE,
    sre_c.CATEGORY_NOT_SPACE: _complement(_SPACE),
}
_REPEATS = (sre_c.MAX_REPEAT, sre_c.MIN_REPEAT)
_FORBIDDEN_OPS = {
    sre_c.GROUPREF: "backreference",
    sre_c.GROUPREF_EXISTS: "conditional group",
    sre_c.ASSERT: "lookahead/lookbehind",
    sre_c.ASSERT_NOT: "negative lookahead/lookbehind",
    sre_c.ATOMIC_GROUP: "atomic group",
    sre_c.POSSESSIVE_REPEAT: "possessive quantifier",
}


# --- AST analysis -------------------------------------------------------------------------------


@dataclass
class _Follow:
    """What may come after a position: characters, and whether the pattern end is reachable
    without consuming a character or passing an anchor."""

    chars: Charset = EMPTY
    can_end: bool = False


@dataclass
class _Analysis:
    issues: list[tuple[str, str]] = field(default_factory=list)  # (code, message)
    literal_chars: int = 0
    anchored_start: bool = False
    anchored_end: bool = False
    groups: list[str] = field(default_factory=list)


class _Walker:
    def __init__(self, flags: int):
        self.issues: list[tuple[str, str]] = []
        self.flags = flags

    # characters ------------------------------------------------------------------------------
    def _chars(self, op: Any, av: Any, icase: bool) -> Charset:
        if op == sre_c.LITERAL:
            s: Charset = ((av, av),)
        elif op == sre_c.NOT_LITERAL:
            s = _complement(((av, av),))
        elif op == sre_c.ANY:
            s = ANY
        elif op == sre_c.IN:
            parts, negate = [], False
            for iop, iav in av:
                if iop == sre_c.NEGATE:
                    negate = True
                elif iop == sre_c.LITERAL:
                    parts.append(((iav, iav),))
                elif iop == sre_c.RANGE:
                    parts.append(((iav[0], iav[1]),))
                elif iop == sre_c.CATEGORY:
                    parts.append(_CATEGORIES.get(iav, ANY))
                else:  # unknown class member: assume it may match anything (conservative)
                    parts.append(ANY)
            s = _union(*parts)
            if icase:
                s = _fold(s)
            return _complement(s) if negate else s
        else:
            return EMPTY
        return _fold(s) if icase else s

    def nullable(self, items: Any) -> bool:
        return all(self._item_nullable(op, av) for op, av in items)

    def _item_nullable(self, op: Any, av: Any) -> bool:
        if op == sre_c.AT:
            return True
        if op == sre_c.SUBPATTERN:
            return self.nullable(av[3])
        if op == sre_c.BRANCH:
            return any(self.nullable(b) for b in av[1])
        if op in _REPEATS:
            return av[0] == 0 or self.nullable(av[2])
        return False

    def first(self, items: Any, icase: bool, follow: _Follow) -> _Follow:
        """First characters of ``items`` followed by ``follow``; an anchor stops the path."""
        chars: list[Charset] = []
        for op, av in items:
            if op == sre_c.AT:
                return _Follow(_union(*chars), False)
            item = self._item_first(op, av, icase)
            chars.append(item.chars)
            if not self._item_nullable(op, av):
                return _Follow(_union(*chars), False)
            if not item.can_end:  # nullable but every empty path passes an anchor
                return _Follow(_union(*chars), False)
        return _Follow(_union(*chars, follow.chars), follow.can_end)

    def _item_first(self, op: Any, av: Any, icase: bool) -> _Follow:
        """First characters of one item; ``can_end`` when it can be passed without an anchor."""
        if op == sre_c.SUBPATTERN:
            return self.first(av[3], self._icase(icase, av), _Follow(EMPTY, True))
        if op == sre_c.BRANCH:
            parts = [self.first(b, icase, _Follow(EMPTY, True)) for b in av[1]]
            return _Follow(_union(*(p.chars for p in parts)), any(p.can_end for p in parts))
        if op in _REPEATS:
            body = self.first(av[2], icase, _Follow(EMPTY, True))
            return _Follow(body.chars, av[0] == 0 or body.can_end)
        return _Follow(self._chars(op, av, icase), False)

    def alphabet(self, items: Any, icase: bool) -> Charset:
        out: list[Charset] = []
        for op, av in items:
            if op == sre_c.SUBPATTERN:
                out.append(self.alphabet(av[3], self._icase(icase, av)))
            elif op == sre_c.BRANCH:
                out.extend(self.alphabet(b, icase) for b in av[1])
            elif op in _REPEATS:
                out.append(self.alphabet(av[2], icase))
            else:
                out.append(self._chars(op, av, icase))
        return _union(*out)

    def literal_count(self, items: Any) -> int:
        total = 0
        for op, av in items:
            if op == sre_c.LITERAL:
                total += 1
            elif op == sre_c.SUBPATTERN:
                total += self.literal_count(av[3])
            elif op == sre_c.BRANCH:
                total += min(self.literal_count(b) for b in av[1])
            elif op in _REPEATS:
                total += av[0] * self.literal_count(av[2])
        return total

    @staticmethod
    def _icase(icase: bool, av: Any) -> bool:
        add, remove = av[1], av[2]
        if add & sre_c.SRE_FLAG_IGNORECASE:
            return True
        if remove & sre_c.SRE_FLAG_IGNORECASE:
            return False
        return icase

    # rules -----------------------------------------------------------------------------------
    def walk(self, items: Any, icase: bool, follow: _Follow, quantified: bool = False) -> None:
        """Check every item with the follow set of its position."""
        items = list(items)
        follows: list[_Follow] = [follow] * len(items)
        current = follow
        for index in range(len(items) - 1, -1, -1):
            follows[index] = current
            current = self.first([items[index]], icase, current)
        previous_unbounded: Charset | None = None
        for index, (op, av) in enumerate(items):
            here = follows[index]
            if op in _FORBIDDEN_OPS:
                self.issues.append(("re2_subset", f"{_FORBIDDEN_OPS[op]} is outside the RE2 subset"))
                continue
            if op == sre_c.AT and av == sre_c.AT_END_STRING:
                self.issues.append(("re2_subset", "\\Z is outside the RE2 subset; use $ (lines carry no newline)"))
            if op == sre_c.SUBPATTERN:
                if av[1] & ~(sre_c.SRE_FLAG_IGNORECASE | sre_c.SRE_FLAG_MULTILINE | sre_c.SRE_FLAG_DOTALL) or av[2] & ~(
                    sre_c.SRE_FLAG_IGNORECASE | sre_c.SRE_FLAG_MULTILINE | sre_c.SRE_FLAG_DOTALL
                ):
                    self.issues.append(("re2_subset", "only the i, m and s inline flags are in the RE2 subset"))
                self.walk(av[3], self._icase(icase, av), here, quantified)
            elif op == sre_c.BRANCH:
                if quantified:
                    self._check_alternation(av[1], icase)
                for branch in av[1]:
                    self.walk(branch, icase, here, quantified)
            elif op in _REPEATS:
                low, high, body = av
                if high != sre_c.MAXREPEAT and high > RE2_MAX_REPEAT or low > RE2_MAX_REPEAT:
                    self.issues.append(("re2_subset", f"repeat count above {RE2_MAX_REPEAT} is outside the RE2 subset"))
                unbounded = high == sre_c.MAXREPEAT
                repeats = high >= 2
                if repeats:
                    self._check_star_height(body, unbounded)
                alphabet = self.alphabet(body, icase)
                if unbounded:
                    if previous_unbounded is not None and _intersects(previous_unbounded, alphabet):
                        self.issues.append(("adjacent_unbounded", "two adjacent unbounded repeats over intersecting classes"))
                    if here.can_end or _intersects(here.chars, alphabet):
                        self.issues.append(
                            (
                                "unbounded_follow",
                                "an unbounded repeat must be followed by an anchor or by a character it cannot consume",
                            )
                        )
                inner = _Follow(_union(self.first(body, icase, _Follow()).chars, here.chars), here.can_end) if repeats else here
                self.walk(body, icase, inner, quantified or repeats)
                if unbounded:
                    previous_unbounded = alphabet
                    continue
                if low > 0 and not self.nullable(body):
                    previous_unbounded = None
                continue
            if not self._item_nullable(op, av):
                previous_unbounded = None

    def _check_star_height(self, body: Any, outer_unbounded: bool) -> None:
        for op, av in body:
            if op in _REPEATS:
                if av[1] >= 2 and (outer_unbounded or av[1] == sre_c.MAXREPEAT):
                    self.issues.append(("star_height", "a repeated group contains an unbounded repeat (star height > 1)"))
                    return
                self._check_star_height(av[2], outer_unbounded)
            elif op == sre_c.SUBPATTERN:
                self._check_star_height(av[3], outer_unbounded)
            elif op == sre_c.BRANCH:
                for branch in av[1]:
                    self._check_star_height(branch, outer_unbounded)

    def _check_alternation(self, branches: Any, icase: bool) -> None:
        firsts = []
        for branch in branches:
            if self.nullable(branch):
                self.issues.append(("quantified_alternation", "a quantified alternation has an empty branch"))
                return
            firsts.append(self.first(branch, icase, _Follow()).chars)
        for i in range(len(firsts)):
            for j in range(i + 1, len(firsts)):
                if _intersects(firsts[i], firsts[j]):
                    self.issues.append(
                        ("quantified_alternation", "a quantified alternation has branches that can start with the same character")
                    )
                    return


def _scan_text(pattern: str) -> list[tuple[str, str]]:
    """Syntax the stdlib parser accepts but RE2 reads differently (or refuses)."""
    issues = []
    i, in_class = 0, False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if nxt.isalnum() and nxt not in _ALLOWED_ESCAPE_LETTERS:
                issues.append(("re2_subset", f"escape \\{nxt} is outside the RE2 subset"))
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
            if pattern[i + 1 : i + 2] == "]" or pattern[i + 1 : i + 3] == "^]":
                i += 2 if pattern[i + 1] == "]" else 3
                continue
        elif ch == "{" and pattern[i + 1 : i + 2] == ",":
            issues.append(("re2_subset", "{,n} is a literal in RE2; write {0,n}"))
        elif pattern.startswith("(?", i):
            rest = pattern[i + 2 :]
            if rest.startswith(("P<", ":")):
                pass
            elif rest.startswith(("<", "P=", "P>", "=", "!", ">", "#", "(")):
                issues.append(("re2_subset", "only (?P<name>...), (?:...) and i/m/s flag groups are in the RE2 subset"))
            else:
                match = re.match(r"([A-Za-z-]*)[:)]", rest)
                if match is None or not set(match.group(1)) <= _ALLOWED_FLAGS:
                    issues.append(("re2_subset", "only the i, m and s inline flags are in the RE2 subset"))
        i += 1
    return issues


def analyse(pattern: str) -> _Analysis:
    """Parse ``pattern`` (ASCII semantics) and apply the RE2-subset and ReDoS rules."""
    result = _Analysis()
    if "\n" in pattern:
        result.issues.append(("syntax", "a pattern cannot contain a newline"))
        return result
    result.issues.extend(_scan_text(pattern))
    try:
        parsed = sre_parse.parse(pattern, sre_c.SRE_FLAG_ASCII)
    except re.error as exc:
        result.issues.append(("syntax", f"invalid pattern: {exc}"))
        return result
    flags = parsed.state.flags
    if flags & (sre_c.SRE_FLAG_VERBOSE | sre_c.SRE_FLAG_LOCALE):
        result.issues.append(("re2_subset", "only the i, m and s inline flags are in the RE2 subset"))
    walker = _Walker(flags)
    walker.walk(parsed.data, bool(flags & sre_c.SRE_FLAG_IGNORECASE), _Follow(EMPTY, True))
    result.issues.extend(walker.issues)
    result.literal_chars = walker.literal_count(parsed.data)
    items = list(parsed.data)
    result.anchored_start = bool(items) and items[0] == (sre_c.AT, sre_c.AT_BEGINNING) or bool(items) and items[0] == (sre_c.AT, sre_c.AT_BEGINNING_STRING)
    result.anchored_end = bool(items) and items[-1] == (sre_c.AT, sre_c.AT_END)
    result.groups = sorted(parsed.state.groupdict, key=lambda name: parsed.state.groupdict[name])
    if parsed.state.groups - 1 != len(parsed.state.groupdict):
        result.issues.append(("unnamed_group", "capture groups must be named (?P<name>...); use (?:...) otherwise"))
    seen = []
    for issue in result.issues:  # one message per rule
        if issue not in seen:
            seen.append(issue)
    result.issues = seen
    return result


def validate_regex(pattern: str, *, min_literal_chars: int = 0, pattern_id: str | None = None, source: str | None = None) -> list[LintIssue]:
    """Validator + lint for one expanded pattern: errors for the RE2 subset and ReDoS rules,
    warnings for too few literal characters and for a pattern unanchored at both ends."""
    analysis = analyse(pattern)
    issues = [LintIssue("error", code, message, pattern_id, source) for code, message in analysis.issues]
    if not any(i.code == "syntax" for i in issues) and re2_available():
        try:
            _re2_compile(pattern)
        except Exception as exc:  # re2 raises its own ``re2.error``
            issues.append(LintIssue("error", "re2_compile", f"RE2 refuses the pattern: {exc}", pattern_id, source))
    if not issues:
        if analysis.literal_chars < min_literal_chars:
            issues.append(
                LintIssue("warning", "few_literals", f"{analysis.literal_chars} literal characters (< {min_literal_chars})", pattern_id, source)
            )
        if not analysis.anchored_start and not analysis.anchored_end:
            issues.append(LintIssue("warning", "unanchored", "pattern is unanchored at both ends", pattern_id, source))
    return issues


def group_names(pattern: str) -> list[str]:
    try:
        parsed = sre_parse.parse(pattern, sre_c.SRE_FLAG_ASCII)
    except re.error:
        return []
    return sorted(parsed.state.groupdict, key=lambda name: parsed.state.groupdict[name])


def literal_chars(pattern: str) -> int:
    return analyse(pattern).literal_chars


# --- engines ------------------------------------------------------------------------------------


def re2_available() -> bool:
    try:
        import re2  # noqa: F401
    except ImportError:
        return False
    return True


def _re2_options() -> Any:
    import re2

    options = re2.Options()
    options.log_errors = False
    return options


def _re2_compile(pattern: str) -> Any:
    import re2

    return re2.compile(pattern, _re2_options())


@dataclass(frozen=True)
class Hit:
    """One match: whole span and named-group spans (``None`` when the group did not take part)."""

    start: int
    end: int
    groups: dict[str, tuple[int, int] | None]


class PatternEngine:
    kind: EngineKind
    version: str
    deterministic: bool

    def compile(self, pattern: str) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def search(self, compiled: Any, text: str) -> Hit | None:  # pragma: no cover - interface
        raise NotImplementedError

    def build_set(self, patterns: list[str]) -> Any:
        return None

    def prefilter(self, prefilter: Any, text: str, count: int) -> set[int]:
        """Indexes of the patterns that may match ``text``; every index without a ``Set``."""
        return set(range(count))

    def identity(self) -> dict[str, Any]:
        return {"engine": self.kind, "engine_version": self.version, "validator_version": VALIDATOR_VERSION}


class Re2Engine(PatternEngine):
    kind = "re2"
    deterministic = True

    def __init__(self) -> None:
        try:
            import re2
        except ImportError:
            raise PatternEngineUnavailable(
                "pattern engine 're2' needs google-re2 (pip install 'su-multi-arm[patterns]'); "
                "the 'regex' engine is a separate, explicit development choice"
            ) from None
        self._re2 = re2
        self.version = importlib.metadata.version("google-re2")

    def compile(self, pattern: str) -> Any:
        compiled = self._re2.compile(pattern, _re2_options())
        return compiled, dict(compiled.groupindex)

    def search(self, compiled: Any, text: str) -> Hit | None:
        regex, index = compiled
        match = regex.search(text)
        if match is None:
            return None
        groups = {}
        for name, number in index.items():
            start, end = match.span(number)  # google-re2 needs integer group indexes
            groups[name] = None if start < 0 else (start, end)
        start, end = match.span(0)
        return Hit(start, end, groups)

    def build_set(self, patterns: list[str]) -> Any:
        if not patterns:
            return None
        prefilter = self._re2.Set.SearchSet(_re2_options())
        for pattern in patterns:
            prefilter.Add(pattern)
        prefilter.Compile()
        return prefilter

    def prefilter(self, prefilter: Any, text: str, count: int) -> set[int]:
        if prefilter is None:
            return set()
        return set(prefilter.Match(text) or ())  # None when nothing matches


class RegexEngine(PatternEngine):
    kind = "regex"
    deterministic = False  # per-call timeouts make results input-and-machine dependent

    def __init__(self, timeout_ms: int = 100) -> None:
        try:
            import regex
        except ImportError:
            raise PatternEngineUnavailable(
                "pattern engine 'regex' needs the regex package (pip install 'su-multi-arm[patterns-lite]')"
            ) from None
        self._regex = regex
        self.timeout = timeout_ms / 1000
        self.version = importlib.metadata.version("regex")

    def compile(self, pattern: str) -> Any:
        return self._regex.compile(pattern, self._regex.ASCII | self._regex.VERSION0)

    def search(self, compiled: Any, text: str) -> Hit | None:
        try:
            match = compiled.search(text, timeout=self.timeout)
        except TimeoutError:
            raise PatternTimeout(f"regex search exceeded {self.timeout * 1000:.0f} ms") from None
        if match is None:
            return None
        groups = {}
        for name in compiled.groupindex:
            start, end = match.span(name)
            groups[name] = None if start < 0 else (start, end)
        return Hit(match.start(), match.end(), groups)


def select_engine(kind: EngineKind, *, timeout_ms: int = 100) -> PatternEngine:
    """The configured engine, or ``PatternEngineUnavailable``. Never the other engine."""
    if kind == "re2":
        return Re2Engine()
    if kind == "regex":
        return RegexEngine(timeout_ms)
    raise PatternEngineUnavailable(f"unknown pattern engine {kind!r}")
