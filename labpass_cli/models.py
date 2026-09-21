"""Run-level summary, owned by the console application."""

from dataclasses import dataclass, field

from njupt_safetylabpass.models import CourseResult


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
