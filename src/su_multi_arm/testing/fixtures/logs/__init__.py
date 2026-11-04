"""Synthetic log fixtures (addendum A1 §4 L0): the deterministic generator, the fake source cluster
and the ReDoS corpus. Synthetic only; nothing here is derived from a real system."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from .fake_source import FakeLogCluster
from .generator import SERVICES, SyntheticDay, SyntheticLogSpec, generate_day, index_name

REDOS_CORPUS = Path(__file__).with_name("redos_corpus.yaml")


@dataclass(frozen=True)
class RedosCase:
    id: str
    cls: Literal["nested_quantifier", "overlapping_alternation", "polynomial", "backreference", "lookaround"]
    pattern: str
    prefix: str
    pump: str
    repeat: int
    suffix: str
    re2: bool
    expect: Literal["linear", "refuse"]
    lint: bool

    def attack(self, repeat: int | None = None) -> str:
        return self.prefix + self.pump * (self.repeat if repeat is None else repeat) + self.suffix


def redos_cases() -> list[RedosCase]:
    data = yaml.safe_load(REDOS_CORPUS.read_text())
    out = []
    for case in data["cases"]:
        attack = case["attack"]
        out.append(RedosCase(case["id"], case["class"], case["pattern"], attack["prefix"], attack["pump"], int(attack["repeat"]), attack["suffix"], bool(case["re2"]), case["expect"], bool(case["lint"])))
    return out


def fake_cluster_for(spec: SyntheticLogSpec, **options) -> tuple[FakeLogCluster, SyntheticDay]:
    day = generate_day(spec)
    return FakeLogCluster(indices=day.indices, mappings=day.mappings, **options), day


__all__ = ["FakeLogCluster", "RedosCase", "REDOS_CORPUS", "SERVICES", "SyntheticDay", "SyntheticLogSpec", "fake_cluster_for", "generate_day", "index_name", "redos_cases"]
