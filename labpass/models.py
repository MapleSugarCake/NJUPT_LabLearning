"""Typed domain models used by the client and runner."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass(frozen=True, slots=True)
class Course:
    id: str
    name: str
    finished: bool = False
    type_name: str | None = None


@dataclass(frozen=True, slots=True)
class Question:
    id: str
    answer: Any


class CourseStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CourseResult:
    course: Course
    status: CourseStatus
    question_count: int = 0
    answered_count: int = 0
    error: str | None = None
    uncertain: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status is CourseStatus.SUCCESS


@dataclass(frozen=True, slots=True)
class RunSummary:
    discovered: int
    already_finished: int
    results: tuple[CourseResult, ...] = field(default_factory=tuple)
    elapsed_seconds: float = 0.0

    @property
    def succeeded(self) -> int:
        return sum(result.succeeded for result in self.results)

    @property
    def failed(self) -> int:
        return len(self.results) - self.succeeded
