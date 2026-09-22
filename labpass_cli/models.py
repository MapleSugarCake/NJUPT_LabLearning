"""定义本轮课程执行的汇总数据及派生统计。

本文件定义：
    RunSummary：汇集发现课程数、已完成课程数及本轮处理结果。
    RunSummary.succeeded：计算本轮结果中成功完成的课程数量。
    RunSummary.failed：计算本轮结果中未成功的课程数量。
"""

from dataclasses import dataclass, field

from njupt_safetylabpass.models import CourseResult


# 作用：汇集发现课程数、已完成课程数及本轮处理结果。
# 说明：使用冻结且带 slots 的数据类，results 默认空元组，总耗时默认零。
# 说明：成功和失败数只统计本轮处理结果，已完成而跳过的课程单独记录。
@dataclass(frozen=True, slots=True)
class RunSummary:
    discovered: int
    already_finished: int
    results: tuple[CourseResult, ...] = field(default_factory=tuple)
    elapsed_seconds: float = 0.0

    # 作用：计算本轮结果中成功完成的课程数量。
    # 参数：
    #     self：当前实例。
    # 返回：成功结果的整数数量。
    @property
    def succeeded(self) -> int:
        return sum(result.succeeded for result in self.results)

    # 作用：计算本轮结果中未成功的课程数量。
    # 参数：
    #     self：当前实例。
    # 返回：结果总数减去成功数所得的整数。
    @property
    def failed(self) -> int:
        return len(self.results) - self.succeeded
