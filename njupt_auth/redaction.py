"""提供贯穿单次运行的秘密值登记与安全诊断文本。

本文件定义：
    _KEYS：敏感字段名称的正则候选模式。
    _FIELD：识别键值形式敏感信息的预编译正则表达式。
    _COOKIE_LINE：匹配整行 Cookie 或 Set-Cookie 内容的正则表达式。
    _BEARER：匹配 Bearer 认证凭据的正则表达式。
    _STUDENT：识别完整学号形态的正则表达式。
    Redactor：保存运行期秘密值并生成安全日志文本。
    Redactor.__init__：初始化秘密值集合及可重入锁。
    Redactor.remember：登记非空字符串秘密值及其常见编码形式。
    Redactor.redact：先替换已登记值，再过滤结构化敏感字段与学号。
    Redactor.excerpt：生成已脱敏、单行且长度有界的诊断摘要。
    Redactor.clear：清空当前运行登记的秘密值引用。
    safe_excerpt：在没有运行级上下文时生成结构化脱敏摘要。
"""

import json
import re
import threading
from urllib.parse import quote, quote_plus

# 敏感字段名称的正则候选模式。
# 覆盖密码、认证头、Token、票据、Cookie、学号和各类会话标识。
_KEYS = (
    r"password|passwd|x-access-token|access[-_]token|authorization|token|entoken|ticket|"
    r"cookie|set-cookie|tgc|jsessionid|enssessionid|guestsessionid|username|sessionid"
)
# 识别键值形式敏感信息的预编译正则表达式。
# 保留字段名及分隔符，替换被引号包围或未加引号的字段值。
_FIELD = re.compile(
    rf"(?i)(\b(?:{_KEYS})\b[\"']?\s*[:=]\s*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}}]+)"
)
# 匹配整行 Cookie 或 Set-Cookie 内容的正则表达式。
# 不区分大小写并按多行处理，避免仅遮盖一部分 Cookie。
_COOKIE_LINE = re.compile(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]+")
# 匹配 Bearer 认证凭据的正则表达式。
# 保留认证方案名称，将后续凭据替换为固定占位符。
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
# 识别完整学号形态的正则表达式。
# 匹配可带字母前缀的 8–12 位数字，使用单词边界限制避免截取更长标识。
_STUDENT = re.compile(r"(?<![\w])(?:[A-Za-z]?\d{8,12})(?![\w])")


# 作用：保存运行期秘密值并生成安全日志文本。
# 说明：使用可重入锁保护登记集合，可供认证、并发课程及日志格式器共享。
# 说明：秘密值仅在内存登记；完成日志输出并关闭处理器后由拥有者清理。
class Redactor:
    """保存运行期秘密值并生成安全日志文本。"""

    # 作用：初始化秘密值集合及可重入锁。
    # 参数：
    #     self：当前实例。
    # 返回：无返回值（None）。
    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.RLock()

    # 作用：登记非空字符串秘密值及其常见编码形式。
    # 参数：
    #     self：当前实例。
    #     *values：任意数量的候选值；非字符串及空字符串被忽略。
    # 返回：无返回值（None）；更新当前脱敏集合。
    # 说明：同时登记 URL 编码、表单编码和 JSON 转义形式，以覆盖不同诊断路径。
    def remember(self, *values: object) -> None:
        """登记非空字符串秘密值及其常见编码形式。"""
        with self._lock:
            for value in values:
                if not isinstance(value, str) or not value:
                    continue
                self._values.update(
                    (value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1])
                )

    # 作用：先替换已登记值，再过滤结构化敏感字段与学号。
    # 参数：
    #     self：当前实例。
    #     value：需转换为字符串并脱敏的对象。
    # 返回：敏感内容被固定占位符替换的字符串。
    # 说明：先按长度降序替换已知值，减少较短秘密值抢先匹配而留下尾部的风险。
    def redact(self, value: object) -> str:
        """先替换已登记值，再过滤结构化敏感字段与学号。"""
        text = str(value)
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for secret in values:
            text = text.replace(secret, "***")
        text = _COOKIE_LINE.sub(r"\1***", text)
        text = _FIELD.sub(r"\1***", text)
        text = _BEARER.sub("Bearer ***", text)
        return _STUDENT.sub("***", text)

    # 作用：生成已脱敏、单行且长度有界的诊断摘要。
    # 参数：
    #     self：当前实例。
    #     value：需转换为字符串并脱敏的对象。
    #     limit：截断前允许保留的正文字符数，超限时另外追加省略号。 默认值为 500。
    # 返回：去除首尾空白的摘要；超过 limit 时保留前缀并追加省略号。
    # 说明：先脱敏再合并换行和截断，避免先截断导致秘密值不能完整匹配。
    def excerpt(self, value: object, limit: int = 500) -> str:
        """生成已脱敏、单行且长度有界的诊断摘要。"""
        text = self.redact(value).replace("\r", " ").replace("\n", " ").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    # 作用：清空当前运行登记的秘密值引用。
    # 参数：
    #     self：当前实例。
    # 返回：无返回值（None）。
    # 说明：应在诊断处理器关闭后调用；释放引用不等于保证底层内存擦除。
    def clear(self) -> None:
        """清空当前运行登记的秘密值引用。"""
        with self._lock:
            self._values.clear()


# 作用：在没有运行级上下文时生成结构化脱敏摘要。
# 参数：
#     value：需转换为字符串并脱敏的对象。
#     limit：截断前允许保留的正文字符数，超限时另外追加省略号。 默认值为 500。
# 返回：使用临时 Redactor 得到的有界单行文本。
# 说明：没有预先登记的秘密值，只能依赖内置字段、认证头及学号匹配规则。
def safe_excerpt(value: object, limit: int = 500) -> str:
    """在没有运行级上下文时生成结构化脱敏摘要。"""
    return Redactor().excerpt(value, limit)
