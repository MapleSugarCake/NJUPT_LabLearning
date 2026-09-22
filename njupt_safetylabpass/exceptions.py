"""定义课程 API、业务解析和运行取消的异常层次。

本文件定义：
    LabPassError：课程业务可报告错误的公共基类。
    AuthenticationExpiredError：表示实验室资源认证在业务运行期间失效。
    RunCancelledError：表示请求开始前已收到运行取消信号。
    NetworkError：表示读取业务资源时发生网络故障。
    ApiError：表示服务器拒绝请求或返回不可接受的 API 结果。
    LockConflictError：表示服务器未能取得业务写入锁。
    ResponseFormatError：表示业务 JSON 或字段类型不满足接口约定。
    SubmissionUncertainError：表示写入中断后无法确认服务器是否已完成处理。
"""


# 作用：课程业务可报告错误的公共基类。
# 说明：继承 Exception；错误消息应安全、简洁，可由课程任务或 CLI 显示。
class LabPassError(Exception):
    """课程业务可报告错误的公共基类。"""


# 作用：表示实验室资源认证在业务运行期间失效。
# 说明：继承 LabPassError；属于全局错误，协调器和调度层据此终止整轮任务。
class AuthenticationExpiredError(LabPassError):
    """表示实验室资源认证在业务运行期间失效。"""


# 作用：表示请求开始前已收到运行取消信号。
# 说明：继承 LabPassError；用于阻止待执行课程或排队写入继续访问资源。
class RunCancelledError(LabPassError):
    """表示请求开始前已收到运行取消信号。"""


# 作用：表示读取业务资源时发生网络故障。
# 说明：继承 LabPassError；只读请求失败与写入结果不确定使用不同异常区分。
class NetworkError(LabPassError):
    """表示读取业务资源时发生网络故障。"""


# 作用：表示服务器拒绝请求或返回不可接受的 API 结果。
# 说明：继承 LabPassError，也是锁冲突、响应结构错误和提交结果不确定的父类。
class ApiError(LabPassError):
    """表示服务器拒绝请求或返回不可接受的 API 结果。"""


# 作用：表示服务器未能取得业务写入锁。
# 说明：继承 ApiError；兼容两种服务端锁错误拼写，失败 POST 不自动重试。
class LockConflictError(ApiError):
    """表示服务器未能取得业务写入锁。"""


# 作用：表示业务 JSON 或字段类型不满足接口约定。
# 说明：继承 ApiError；包括课程、题目标识及答案格式校验失败。
class ResponseFormatError(ApiError):
    """表示业务 JSON 或字段类型不满足接口约定。"""


# 作用：表示写入中断后无法确认服务器是否已完成处理。
# 说明：继承 ApiError；课程结果应保留不确定标记，提示网页核对且不得自动重发。
class SubmissionUncertainError(ApiError):
    """表示写入中断后无法确认服务器是否已完成处理。"""
