"""Versioned, hidden evaluation contracts; never included in agent prompts."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCENARIOS = {"basic": 0.6, "boundary": 0.2, "adversarial": 0.1, "regression": 0.1}
RUBRIC_DIMENSIONS = ("correctness", "evidence", "completeness", "safety", "efficiency", "process")
SCORER_VERSION = "four-dimensions-v1"
PositiveInt = Annotated[int, Field(strict=True, ge=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Rule(Contract):
    id: str = Field(min_length=1)
    kind: Literal[
        "answer_contains", "answer_excludes", "tool_sequence", "tool_argument", "max_steps"
    ]
    dimension: Literal["correctness", "evidence", "completeness", "safety", "efficiency", "process"]
    hard: bool = True
    text: str | None = None
    sequence: list[list[str]] | None = None
    call_index: Annotated[int, Field(strict=True, ge=0)] | None = None
    arguments: dict | None = None
    limit: PositiveInt | None = None

    @model_validator(mode="after")
    def check_payload(self):
        if self.kind.startswith("answer_") and not self.text:
            raise ValueError("answer rules require nonempty text")
        if self.kind == "tool_sequence" and (
            not self.sequence or any(not g or any(not n for n in g) for g in self.sequence)
        ):
            raise ValueError("tool_sequence requires nonempty alternative tool groups")
        if self.kind == "tool_argument" and (self.call_index is None or not self.arguments):
            raise ValueError("tool_argument requires call_index and nonempty arguments")
        if self.kind == "max_steps" and self.limit is None:
            raise ValueError("max_steps requires a positive limit")
        return self


class EvaluationSpec(Contract):
    scenario: Literal["basic", "boundary", "adversarial", "regression"] = "basic"
    task_type: Literal["text", "tool", "end_to_end", "mixed"] = "end_to_end"
    split: Literal["evaluation", "holdout"] = "evaluation"
    smoke: bool = False
    critical: bool = False
    long_task: bool = False
    fault_recovery: bool = False
    badcase: str | None = None
    # One group of acceptable tools per call position, authored by the dataset owner.
    # Absent annotations mean unknown selection accuracy, not inferred correctness.
    expected_tool_sequence: list[list[str]] | None = None
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_annotations(self):
        if self.expected_tool_sequence is not None and (
            not self.expected_tool_sequence
            or any(not g or any(not n for n in g) for g in self.expected_tool_sequence)
        ):
            raise ValueError("expected_tool_sequence requires nonempty tool groups")
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError("duplicate rule IDs")
        return self


class SuiteSpec(Contract):
    id: str = "xclaw-synthetic"
    version: str = "1"
    kind: Literal["benchmark", "product_regression"] = "product_regression"


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def evaluation_hash(spec: EvaluationSpec) -> str:
    return digest({"scorer": SCORER_VERSION, "spec": spec.model_dump()})
