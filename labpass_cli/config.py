"""Console defaults and validated per-run settings."""

from dataclasses import dataclass

DEFAULT_WORKERS = 4
MAX_WORKERS = 4
LOG_FILENAME = "labpass_log.txt"


@dataclass(frozen=True, slots=True)
class RunSettings:
    debug: bool = False
    workers: int = DEFAULT_WORKERS
    force_resubmit: bool = False

    def __post_init__(self) -> None:
        if type(self.workers) is not int or not 1 <= self.workers <= MAX_WORKERS:
            raise ValueError("课程线程数必须是 1–4 的整数")
