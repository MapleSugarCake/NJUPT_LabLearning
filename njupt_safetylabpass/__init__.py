"""集中导出实验室课程业务接口及领域模型。

公开接口：
    SafetyLabClient：使用已认证会话访问课程 API。
    MutationCoordinator：共享写入锁及运行级取消状态。
    run_course：按题目顺序执行单门课程。
    Course：课程列表中的课程信息。
    Question：题目提交所需标识和答案。
    CourseProgress：单课程的结构化进度事件。
    CourseResult：单课程执行结果。
    CourseStatus：课程成功或失败的状态枚举。
这些接口均从所属模块导入，包初始化不登录或执行课程。

本文件定义：
    __all__：课程业务包公开导出名称的字符串列表。
"""

from .client import SafetyLabClient
from .coordination import MutationCoordinator
from .course import run_course
from .models import Course, CourseProgress, CourseResult, CourseStatus, Question

# 课程业务包公开导出名称的字符串列表。
# 提供客户端、协调器、单课程入口和领域类型的明确导出范围。
__all__ = [
    "Course",
    "CourseProgress",
    "CourseResult",
    "CourseStatus",
    "MutationCoordinator",
    "Question",
    "SafetyLabClient",
    "run_course",
]
