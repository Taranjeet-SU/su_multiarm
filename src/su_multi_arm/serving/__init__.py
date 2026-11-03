"""Serving (plan §3.15): ``http`` (FastAPI app, SSE in ``sse``), ``mcp`` (stdio and streamable HTTP),
``auth`` (``PrincipalResolver`` hook), ``discovery`` and ``parity`` (request-model parity check every
transport must pass). ``su_multi_arm.api``/``su_multi_arm.mcp`` keep their v1 import paths."""

from .parity import REQUEST_FIELDS, normalise_schema, request_from_arguments, tool_differences

__all__ = ["REQUEST_FIELDS", "normalise_schema", "request_from_arguments", "tool_differences"]
