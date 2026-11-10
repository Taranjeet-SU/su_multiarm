"""Cross-encoder reranking of the ordered selection (``pipeline.selection.rerank``; opt-in).

The stage runs after ordering, diversity, source order and pins. It scores ``(query, title +
passage)`` pairs with the configured ``models.roles.reranker`` and reorders the first
``top_k_in`` unpinned records by that score; pinned records stay first and records past
``top_k_in`` keep their fusion order after the reranked head. ``top_k_out`` is an explicit cut of
the reranked selection (like ``max_documents``), reported in the trace with the dropped count.

Failure policy (``reranking.on_error``):

- ``fail`` (default): a model load error, a scoring error or a timeout raises ``RerankError``;
- ``fusion_order``: the selection keeps its fusion order unchanged and the trace records
  ``status: failed_open`` with the reason. This is an explicit, traced degradation chosen by the
  operator, never a silent one: ``SearchResult.trace.selection.rerank_detail`` carries it.

``timeout_ms`` bounds waiting for the scorer plus the scoring call. The first (cold) model load is
measured separately (``load_seconds``) and is not charged to the budget; a load failure follows
``on_error``. One scoring call runs at a time, in a daemon thread: a call that overran its budget
keeps running until it returns (PyTorch cannot be interrupted) and holds the scorer, so later calls
time out within their own budget rather than queueing unbounded work; nothing blocks process exit.

Device selection: ``auto`` uses CUDA when ``torch.cuda.is_available()`` and the CPU otherwise; an
explicit ``cuda`` that is unavailable is a load error (no silent CPU fallback). ``dtype: auto`` is
float16 on CUDA and float32 on the CPU.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Protocol, Sequence

from .types import Selected


class RerankError(RuntimeError):
    """The reranker could not score the selection under ``on_error: fail``."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(f"reranker {reason}: {message}")


class PairScorer(Protocol):
    """Anything that scores ``(query, passage)`` pairs; higher is more relevant."""

    def load(self) -> dict[str, Any]: ...

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]: ...


def resolve_device(device: str) -> str:
    """``auto`` -> ``cuda`` when available, else ``cpu``; explicit values are returned unchanged."""
    if device != "auto":
        return device
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:  # pragma: no cover - sentence-transformers requires torch
        return "cpu"


class CrossEncoderScorer:
    """``sentence_transformers.CrossEncoder`` behind ``PairScorer`` (lazy, loaded once)."""

    def __init__(self, role: Any):
        self.role = role
        self._model: Any = None
        self._lock = threading.Lock()
        self.device: str | None = None
        self.dtype: str | None = None

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._model is not None:
                return {"loaded": False, "device": self.device, "dtype": self.dtype}
            device = resolve_device(self.role.device)
            if device.startswith("cuda"):
                import torch

                if not torch.cuda.is_available():
                    raise RerankError("load_failed", f"device {device!r} requested but CUDA is not available")
            dtype = self.role.dtype if self.role.dtype != "auto" else ("float16" if device.startswith("cuda") else "float32")
            from sentence_transformers import CrossEncoder

            kwargs: dict[str, Any] = {"device": device, "trust_remote_code": False}
            if self.role.max_length is not None:
                kwargs["max_length"] = self.role.max_length
            if dtype == "float16":
                import torch

                kwargs["model_kwargs"] = {"torch_dtype": torch.float16}
            self._model = CrossEncoder(self.role.model, **kwargs)
            self.device, self.dtype = device, dtype
            return {"loaded": True, "device": device, "dtype": dtype}

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        scores = self._model.predict(list(pairs), batch_size=self.role.batch_size, show_progress_bar=False)
        return [float(s) for s in scores]


class _Timeout(Exception):
    pass


class Reranker:
    """One scorer, one scoring call at a time, each in a daemon thread bounded by the budget.

    A call waits for the scorer at most for its own budget; a call that overran keeps running in its
    daemon thread (PyTorch cannot be interrupted) and holds the scorer until it returns, so later calls
    fail open (or raise) within their budget instead of queueing unbounded work behind it, and an
    abandoned call never blocks interpreter exit."""

    def __init__(self, scorer: PairScorer, model: str = ""):
        self.scorer = scorer
        self.model = model
        self._busy = threading.Lock()

    def _score(self, pairs: list[tuple[str, str]], timeout: float | None) -> list[float]:
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._busy.acquire(timeout=-1 if timeout is None else timeout):
            raise _Timeout("the scorer is still busy with an earlier call that overran its budget")
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["scores"] = self.scorer.score(pairs)
            except BaseException as exc:  # re-raised in the caller's thread
                box["error"] = exc
            finally:
                self._busy.release()

        worker = threading.Thread(target=run, name="rerank", daemon=True)
        worker.start()
        worker.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            raise _Timeout("scoring exceeded the budget")
        if "error" in box:
            raise box["error"]
        return box["scores"]

    def rerank(
        self,
        query: str,
        ordered: list[Selected],
        passage: Callable[[str], str],
        options: Any,
        pins_first: bool,
    ) -> tuple[list[Selected], dict[str, Any]]:
        """Return the reordered (and optionally cut) selection and the ``rerank_detail`` trace."""
        detail: dict[str, Any] = {
            "model": self.model,
            "on_error": options.on_error,
            "top_k_in": options.top_k_in,
            "top_k_out": options.top_k_out,
            "timeout_ms": options.timeout_ms,
            "candidates": len(ordered),
        }
        pinned = [r for r in ordered if r.pinned and pins_first]
        rest = [r for r in ordered if not (r.pinned and pins_first)]
        head = rest if options.top_k_in is None else rest[: options.top_k_in]
        tail = [] if options.top_k_in is None else rest[options.top_k_in :]
        detail["scored"] = len(head)
        if not head:
            detail.update(status="skipped", reason="nothing_to_score", seconds=0.0)
            return self._cut(ordered, options, detail), detail
        try:
            started = time.perf_counter()
            loaded = self.scorer.load()
            detail["load_seconds"] = round(time.perf_counter() - started, 4)
            detail.update({k: v for k, v in loaded.items() if k in ("device", "dtype")})
            pairs = [(query, passage(r.record_id)) for r in head]
            started = time.perf_counter()
            scores = self._score(pairs, None if options.timeout_ms is None else options.timeout_ms / 1000.0)
            detail["seconds"] = round(time.perf_counter() - started, 4)
            if len(scores) != len(head):
                raise RerankError("bad_response", f"{len(scores)} scores for {len(head)} pairs")
        except _Timeout as exc:
            detail["seconds"] = round(time.perf_counter() - started, 4)
            return self._failed(ordered, options, detail, "timeout", f"{exc} ({options.timeout_ms} ms)")
        except RerankError as exc:
            return self._failed(ordered, options, detail, exc.reason, str(exc))
        except Exception as exc:  # model/library errors are categorised, never swallowed silently
            return self._failed(ordered, options, detail, "scoring_failed", f"{type(exc).__name__}: {exc}")
        for record, score in zip(head, scores, strict=True):
            record.score = float(score)
            record.reasons.append("rerank:cross_encoder")
        for record in tail:
            record.reasons.append("rerank:not_scored_past_top_k_in")
        head.sort(key=lambda r: (-r.score, r.record_id))
        detail["status"] = "reranked"
        return self._cut([*pinned, *head, *tail], options, detail), detail

    def _failed(self, ordered: list[Selected], options: Any, detail: dict[str, Any], reason: str, message: str) -> tuple[list[Selected], dict[str, Any]]:
        if options.on_error == "fail":
            raise RerankError(reason, message)
        detail.update(status="failed_open", reason=reason, message=message[:300])
        for record in ordered:
            record.reasons.append(f"rerank:failed_open:{reason}")
        return self._cut(ordered, options, detail), detail

    @staticmethod
    def _cut(ordered: list[Selected], options: Any, detail: dict[str, Any]) -> list[Selected]:
        if options.top_k_out is None or len(ordered) <= options.top_k_out:
            detail["dropped"] = 0
            return ordered
        detail["dropped"] = len(ordered) - options.top_k_out
        return ordered[: options.top_k_out]


def passage_text(title: str, text: str, limit: int) -> str:
    """The cross-encoder input for one record: title, newline, the retrieved passage (bounded)."""
    body = f"{title}\n{text}" if title else text
    return body[:limit]
