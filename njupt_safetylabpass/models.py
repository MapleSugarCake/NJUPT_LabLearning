"""定义课程、题目、进度事件和执行结果的数据模型。

本文件定义：
    Course：表示课程列表中的一门课程及其完成状态。
    Question：保存一条课程题目关系及其提交答案。
    CourseProgress：向调用方传递单课程开始和答题进度。
    CourseStatus：用字符串枚举表示已返回课程结果的成功或失败。
    CourseStatus.SUCCESS：课程全部处理成功的状态成员。
    CourseStatus.FAILED：课程处理失败的状态成员。
    CourseResult：记录单门课程的执行状态、计数和安全错误说明。
    CourseResult.succeeded：判断结果是否为课程成功状态。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


# 作用：表示课程列表中的一门课程及其完成状态。
# 说明：使用冻结且带 slots 的数据类；id 来自课程列表，供查询题目和标记完成使用。
# 说明：名称供显示，完成标记默认 False，分类说明可以缺省。
@dataclass(frozen=True, slots=True)
class Course:
    id: str
    name: str
    finished: bool = False
    type_name: str | None = None


# 作用：保存一条课程题目关系及其提交答案。
# 说明：使用冻结且带 slots 的数据类，提交方法接收完整对象以保持标识语义。
# 说明：响应 id 保存为 submission_id，提交时用作 questionId；
#     响应 courseId 保存为 course_id，提交时用作 id。
# 说明：响应 questionId 仅保留为 source_question_id 供诊断；答案为字符串或字符串列表，题型可缺省。
@dataclass(frozen=True, slots=True)
class Question:
    submission_id: str
    course_id: str
    answer: str | list[str]
    source_question_id: str | None = None
    kind: str | None = None


# 作用：向调用方传递单课程开始和答题进度。
# 说明：冻结数据类关联课程、阶段及题目计数，调用方自行决定显示方式。
# 说明：stage 为 started 或 answered；计数默认零，业务层不输出控制台文本。
@dataclass(frozen=True, slots=True)
class CourseProgress:
    """向调用方传递单课程开始和答题进度。"""

    course: Course
    stage: Literal["started", "answered"]
    answered_count: int = 0
    question_count: int = 0


# 作用：用字符串枚举表示已返回课程结果的成功或失败。
# 说明：继承 StrEnum，供结果模型和调度汇总共同使用；运行取消通过异常传播。
class CourseStatus(StrEnum):
    # 课程全部处理成功的状态成员。
    # 字符串值为 success，表示答题及标记完成均已成功。
    SUCCESS = "success"
    # 课程处理失败的状态成员。
    # 字符串值为 failed，具体原因及提交结果是否不确定由结果模型保存。
    FAILED = "failed"


# 作用：记录单门课程的执行状态、计数和安全错误说明。
# 说明：冻结数据类默认题目与答题计数为零，错误说明可缺省。
# 说明：uncertain 用于标识写入可能已被服务器处理，汇总时提示先核对网页状态。
@dataclass(frozen=True, slots=True)
class CourseResult:
    course: Course
    status: CourseStatus
    question_count: int = 0
    answered_count: int = 0
    error: str | None = None
    uncertain: bool = False

    # 作用：判断结果是否为课程成功状态。
    # 参数：
    #     self：当前实例。
    # 返回：status 等于 CourseStatus.SUCCESS 时为 True，否则为 False。
    @property
    def succeeded(self) -> bool:
        return self.status is CourseStatus.SUCCESS
