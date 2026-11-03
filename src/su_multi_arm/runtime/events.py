"""Streaming answer events (plan §3.15; execution-log default #12).

``Engine.answer_stream`` returns an ``AnswerStream``: an iterator of typed events in a fixed order

    decision → arm_result* → [review_round* → arm_result*] → evidence → token* → contract → done
                                                                            (or error, terminal)

``token`` events are **provisional**: they are raw fragments of the generator's reply (JSON text in
structured-output modes) emitted before the reply is validated and before the ``AnswerContract``
runs. The ``contract`` event states the contract outcome and whether the streamed text was
superseded; the ``done`` event carries the same ``AnswerResult`` the non-streaming
``Engine.answer`` returns, and it is the only authoritative answer. Exactly one terminal event
(``done`` or ``error``) ends every stream.

The run executes on a worker thread (the engine is a threaded sync core, default #9). ``cancel()``
(or a client disconnect in a serving layer) cancels the run's budget, so every further model
reservation raises ``RunCancelled``; the worker also stops at the next stage boundary or streamed
token, and an in-flight provider stream is closed. Usage already spent is committed as usual.
"""

from __future__ import annotations

import queue
import threading
from typing import Annotated, Any, Callable, Literal, Union

from pydantic import Field, TypeAdapter

from ..core.errors import AccessDenied, BackendError, BudgetExhausted, DeadlineExceeded, GenerationDisagreement, IndexNotReady, RunCancelled, SessionNotFound
from ..models import AnswerResult, StrictModel, UnsupportedConstraint, Violation


class _Event(StrictModel):
    seq: int = 0  # 1-based position in the stream; assigned when the event is emitted


class DecisionEvent(_Event):
    event: Literal["decision"] = "decision"
    pipeline: str
    mode: str
    agent: str
    outcome: str
    policy_applied: str | None = None
    failure: str | None = None
    intents: list[str] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_constraints: list[UnsupportedConstraint] = Field(default_factory=list)


class ArmResultEvent(_Event):
    event: Literal["arm_result"] = "arm_result"
    round: int  # 1 = first pass; review hops are 2, 3, ...
    arm: str
    record_ids: list[str] = Field(default_factory=list)
    aggregates: int = 0


class ReviewRoundEvent(_Event):
    event: Literal["review_round"] = "review_round"
    round: dict[str, Any]


class EvidenceEvent(_Event):
    event: Literal["evidence"] = "evidence"
    ids: list[str]
    omitted_ids: list[str] = Field(default_factory=list)
    truncated_ids: list[str] = Field(default_factory=list)


class TokenEvent(_Event):
    event: Literal["token"] = "token"
    stage: str = "generator"
    text: str
    provisional: Literal[True] = True  # never authoritative; see ``contract`` and ``done``


class ContractEvent(_Event):
    event: Literal["contract"] = "contract"
    status: Literal["passed", "abstained"]
    violations: list[Violation] = Field(default_factory=list)
    unsupported_constraints: list[UnsupportedConstraint] = Field(default_factory=list)
    tokens_streamed: int = 0
    # True when tokens were streamed: their text is a raw provisional reply and the final answer
    # (possibly an abstention) in ``done`` replaces it.
    tokens_superseded: bool = False


class DoneEvent(_Event):
    event: Literal["done"] = "done"
    result: AnswerResult


class ErrorEvent(_Event):
    event: Literal["error"] = "error"
    category: str
    message: str


AnswerEvent = Annotated[
    Union[DecisionEvent, ArmResultEvent, ReviewRoundEvent, EvidenceEvent, TokenEvent, ContractEvent, DoneEvent, ErrorEvent],
    Field(discriminator="event"),
]
ANSWER_EVENT = TypeAdapter(AnswerEvent)
EVENT_NAMES: tuple[str, ...] = ("decision", "arm_result", "review_round", "evidence", "token", "contract", "done", "error")
TERMINAL = frozenset({"done", "error"})


def error_category(exc: BaseException) -> str:
    """Stable, machine-readable category of a failed run (serving layers map it to a status)."""
    from ..pipeline.spec import RequestRejected

    if isinstance(exc, RunCancelled):
        return "cancelled"
    if isinstance(exc, BudgetExhausted):
        return "budget_exhausted"
    if isinstance(exc, DeadlineExceeded):
        return "deadline_exceeded"
    if isinstance(exc, SessionNotFound):
        return "session_not_found"
    if isinstance(exc, AccessDenied):
        return "access_denied"
    if isinstance(exc, RequestRejected):
        return "rejected"
    if isinstance(exc, (BackendError, GenerationDisagreement, IndexNotReady)):
        return "unavailable"
    category = getattr(exc, "category", None)
    if isinstance(category, str):
        return f"provider_{category}" if category != "schema_violation" else "schema_violation"
    if isinstance(exc, ValueError):
        return "invalid_request"
    return "internal"


Emit = Callable[[Any], None]


class AnswerStream:
    """Iterator of ``AnswerEvent``s for one answer run; ``cancel()`` stops the run and its budget.

    ``run(emit, on_delta)`` executes the answer and returns the ``AnswerResult``; ``emit`` raises
    ``RunCancelled`` after cancellation. ``next_event(timeout)`` returns ``None`` when no event
    arrived within ``timeout`` (serving layers send a heartbeat then)."""

    def __init__(self, run: Callable[[Emit, Callable[[str], None]], AnswerResult], cancel_budget: Callable[[str], None] | None = None):
        self._run = run
        self._cancel_budget = cancel_budget
        self._queue: queue.Queue = queue.Queue()
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._tokens = 0
        self.finished = False  # the consumer has received the terminal event
        self.worker_done = threading.Event()

    # --- producer side (worker thread) -----------------------------------------------------------

    def _put(self, event: Any) -> None:
        with self._lock:
            self._seq += 1
            event.seq = self._seq
        self._queue.put(event)

    def _emit(self, event: Any) -> None:
        if self._cancelled.is_set():
            raise RunCancelled("run cancelled by the caller")
        if isinstance(event, ContractEvent):
            event.tokens_streamed = self._tokens
            event.tokens_superseded = self._tokens > 0
        self._put(event)

    def _delta(self, text: str) -> None:
        if self._cancelled.is_set():
            raise RunCancelled("run cancelled by the caller")
        if text:
            self._tokens += 1
            self._put(TokenEvent(text=text))

    def _work(self) -> None:
        try:
            result = self._run(self._emit, self._delta)
            self._put(DoneEvent(result=result))
        except BaseException as exc:  # every failure becomes the terminal ``error`` event
            self._put(ErrorEvent(category=error_category(exc), message=str(exc)))
        finally:
            self.worker_done.set()

    def start(self) -> "AnswerStream":
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="answer-stream", daemon=True)
                self._thread.start()
        return self

    # --- consumer side ---------------------------------------------------------------------------

    def next_event(self, timeout: float | None = None) -> Any | None:
        """The next event, ``None`` on timeout; ``StopIteration`` after the terminal event."""
        if self.finished:
            raise StopIteration
        self.start()
        try:
            event = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if event.event in TERMINAL:
            self.finished = True
        return event

    def __iter__(self) -> "AnswerStream":
        return self

    def __next__(self) -> Any:
        event = self.next_event()
        assert event is not None  # no timeout: blocks until an event arrives
        return event

    def cancel(self, reason: str = "cancelled") -> None:
        """Stop the run: no-op after the terminal event was delivered."""
        if self.finished or self._cancelled.is_set():
            return
        self._cancelled.set()
        if self._cancel_budget is not None:
            self._cancel_budget(reason)

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def close(self) -> None:
        self.cancel("closed")

    def __enter__(self) -> "AnswerStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def result(self) -> AnswerResult:
        """Drain the stream; the final ``AnswerResult`` or a ``RuntimeError`` carrying the error event."""
        for event in self:
            if isinstance(event, DoneEvent):
                return event.result
            if isinstance(event, ErrorEvent):
                raise RuntimeError(f"{event.category}: {event.message}")
        raise RuntimeError("stream ended without a terminal event")  # pragma: no cover
