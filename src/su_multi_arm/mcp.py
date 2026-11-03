"""MCP server entry point kept at its v1 import path; the implementation is ``serving/mcp.py``."""

from __future__ import annotations


def create_mcp(config=None, **kwargs):
    from .serving.mcp import create_mcp as build

    return build(config, **kwargs)


__all__ = ["create_mcp"]
