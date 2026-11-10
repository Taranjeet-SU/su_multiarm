"""Package answer stream → legacy SSE events, in the legacy order::

    plan → evidence → token* → final        (or error, terminal; ``: keep-alive`` comments between)

* ``decision`` → ``plan {modes, planner_model, mode}``.
* ``evidence`` → ``evidence {citations[{id: n, title, url}], modes}`` — numbered in packing order, the
  same numbers the ``final`` citations and ``[n]`` markers use.
* ``token`` → ``token {text}``: provisional answer text. When the generator replies in structured
  output (a JSON object), only the decoded ``answer`` string is forwarded; ``[evidence id]`` markers
  are renumbered to ``[n]`` and a possibly unfinished trailing marker is held back. The final answer
  replaces the provisional text (``answer_rewritten`` says whether it differs).
* ``done`` → ``final`` = the ``/api/qa`` envelope + ``answer_rewritten`` + ``stream{tokens, streamed_chars}``.
* ``error`` → ``error {status, detail{error, reason}}`` (public text only; status 500 only for
  internal failures, the cue for a legacy client to retry without streaming).
* ``arm_result``, ``review_round`` and ``contract`` have no legacy event; their content reaches the
  client through ``final`` (``compat.contract``, ``agentic``).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .envelope import renumber

REPLACEMENT = "\ufffd"
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_PEM_END = re.compile(r"-----END [A-Z ]*-----")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class AnswerTextExtractor:
    """Incrementally decodes the ``"answer"`` string of a streamed JSON reply; plain-text replies
    pass through unchanged. ``feed`` returns the decoded answer text so far."""

    KEY = '"answer"'

    def __init__(self) -> None:
        self.raw = ""
        self.mode: str | None = None  # "json" | "text"
        self.pos = 0  # next raw index to decode (json mode, inside the answer string)
        self.inside = False
        self.done = False
        self.text = ""

    def feed(self, chunk: str) -> str:
        self.raw += chunk
        if self.mode is None:
            stripped = self.raw.lstrip()
            if not stripped:
                return ""
            self.mode = "json" if stripped[0] in "{[" else "text"
        if self.mode == "text":
            self.text = self.raw
            return self.text
        if self.done:
            return self.text
        if not self.inside:
            at = self.raw.find(self.KEY)
            if at < 0:
                return self.text
            rest = self.raw[at + len(self.KEY) :]
            stripped = rest.lstrip()
            if not stripped.startswith(":"):
                return self.text
            after = stripped[1:].lstrip()
            if not after:
                return self.text
            if not after.startswith('"'):
                self.done = True  # not a string value: nothing to stream
                return self.text
            self.pos = len(self.raw) - len(after) + 1
            self.inside = True
        while self.pos < len(self.raw):
            ch = self.raw[self.pos]
            if ch == '"':
                self.done = True
                break
            if ch == "\\":
                if self.pos + 1 >= len(self.raw):
                    break  # escape split across chunks
                nxt = self.raw[self.pos + 1]
                if nxt == "u":
                    decoded = self._unicode_escape()
                    if decoded is None:
                        break  # escape (or the low half of a surrogate pair) split across chunks
                    self.text += decoded
                    continue
                self.text += _ESCAPES.get(nxt, nxt)
                self.pos += 2
                continue
            self.text += ch
            self.pos += 1
        return self.text

    def _hex4(self, at: int) -> int | None:
        digits = self.raw[at : at + 4]
        if len(digits) < 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
            return None
        return int(digits, 16)

    def _unicode_escape(self) -> str | None:
        """Decode the ``\\uXXXX`` escape at ``pos`` as ``json.loads`` does: a high surrogate followed
        by a ``\\uDC00``-``\\uDFFF`` escape is one character (a surrogate pair); a lone surrogate or a
        malformed escape becomes U+FFFD (never a lone surrogate, which UTF-8 cannot encode).
        ``None`` when more input is needed."""
        if self.pos + 6 > len(self.raw):
            return None
        code = self._hex4(self.pos + 2)
        if code is None:
            self.pos += 2
            return REPLACEMENT
        if 0xD800 <= code <= 0xDBFF:
            follow = self.raw[self.pos + 6 : self.pos + 8]
            if len(self.raw) < self.pos + 12 and (follow in ("", "\\") or follow == "\\u"):
                return None  # the low half may still arrive
            low = self._hex4(self.pos + 8) if follow == "\\u" else None
            if low is not None and 0xDC00 <= low <= 0xDFFF:
                self.pos += 12
                return chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00))
            self.pos += 6
            return REPLACEMENT
        self.pos += 6
        if 0xDC00 <= code <= 0xDFFF:
            return REPLACEMENT
        return chr(code)


class ProvisionalText:
    """Turns decoded provisional text into renumbered deltas, holding back an unfinished ``[...``.

    With ``redact`` (the profile's credential masking), text is released only up to the last
    whitespace (a token is masked only once it is complete) and never inside an unterminated
    private-key block, and every release is masked: a secret is never streamed in pieces."""

    def __init__(self, numbers: dict[str, int], redact: Callable[[str], str] | None = None):
        self.numbers = numbers
        self.redact = redact
        self.extractor = AnswerTextExtractor()
        self.emitted = ""
        self.tokens = 0

    def _settled(self, text: str) -> str:
        if self.extractor.done:
            return text
        cut = max(text.rfind(" "), text.rfind("\n"), text.rfind("\t"))
        text = text[: cut + 1] if cut >= 0 else ""
        begin = text.rfind("-----BEGIN")
        if begin >= 0 and not _PEM_END.search(text, begin):
            text = text[:begin]  # a key block is released only once its END line is complete
        return text

    def feed(self, chunk: str) -> str:
        decoded = self.extractor.feed(chunk)
        cut = decoded.rfind("[")
        safe = decoded if cut < 0 or "]" in decoded[cut:] else decoded[:cut]
        rendered = renumber(safe, self.numbers)
        if self.redact is not None:  # cut after renumbering: a complete ``[id, id]`` marker is never split
            rendered = self.redact(self._settled(rendered))
        if not rendered.startswith(self.emitted):
            return ""  # a marker resolved differently than streamed; the final answer replaces it
        delta = rendered[len(self.emitted) :]
        if delta:
            self.emitted = rendered
            self.tokens += 1
        return delta


def utf8_safe(text: str) -> str:
    """``text`` with every lone surrogate replaced by U+FFFD (encodable as UTF-8)."""
    return _LONE_SURROGATE.sub(REPLACEMENT, text)


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {utf8_safe(json.dumps(data, ensure_ascii=False, separators=(',', ':')))}\n\n"


KEEP_ALIVE = ": keep-alive\n\n"
