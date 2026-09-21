"""Typed domain models used by the client and runner."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


@dataclass(frozen=True, slots=True)
class Course:
    id: str
    name: str
    finished: bool = False
    type_name: str | None = None


@dataclass(frozen=True, slots=True)
class Question:
    submission_id: str
    course_id: str
    answer: str | list[str]
    source_question_id: str | None = None
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class CourseProgress:
    """Business progress data; the caller decides how to display it."""

    course: Course
    stage: Literal["started", "answered"]
    answered_count: int = 0
    question_count: int = 0


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
