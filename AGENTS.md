# AGENTS.md

本文件适用于整个仓库，供开发者和编码代理修改 LabPass 时遵循。

## 项目与职责

LabPass 是 Python 3.13+ 控制台工具。一个发行项目包含 `njupt_auth`、`njupt_safetylabpass`、`labpass_cli` 三个包，不保留旧 `labpass` 包或旧参数兼容层。版本以根目录 `pyproject.toml` 的 `project.version` 为唯一来源，不在源码中维护版本常量或硬编码回退值。

- `main.py`：仅调用 `labpass_cli.cli.entrypoint`。
- `njupt_auth`：校内外 SSO/CAS/VPN、加密、浏览器认证、Token 导入、只读鉴权、Session 工厂、传输策略和脱敏基础能力；不包含课程业务、线程调度或控制台交互。
- `njupt_safetylabpass/client.py`：业务 API、HTTP/业务校验、课程和题目解析。
- `njupt_safetylabpass/course.py`：单课程顺序执行；只返回结果和进度事件，不创建线程池或输出控制台文本。
- `njupt_safetylabpass/models.py`、`coordination.py`：领域模型、共享写入锁及取消信号。
- `labpass_cli`：交互、运行配置、版本读取、日志配置、线程池、进度显示、退出码和汇总。
- `tests/`：完全模拟请求，默认阻断 HTTP 和 socket 网络出口。
- `main.spec`：控制台 EXE，包含发行元数据和 Playwright 驱动，调用本机 Edge。

优先使用现有职责模块。认证包不得依赖业务包或 CLI；业务包不得依赖 CLI。业务包可以复用认证包的超时常量和脱敏基础工具，但不得执行登录。

## 认证与 Session 契约

- 公共认证入口为 `authenticate`、`authenticate_in_browser`、`import_token`。
- 认证结果交接实际 `requests.Session`、资源 API 基址和独立 Session 工厂。资源 Session 可以是 `requests.Session` 子类；不得仅交接 Token 或要求业务重新建立认证。
- Session 工厂保存本轮认证快照，为每门课程创建独立 Session、CookieJar、Cookie 对象和连接适配器，保留 Cookie domain/path/secure/expires 等属性。
- 主客户端拥有主 Session；课程任务各自关闭课程 Session。线程全部结束后关闭认证工厂，已关闭工厂不得继续创建会话。
- 认证失败、中断、浏览器取消时必须释放已创建的资源。
- 账号密码认证显式选择网络，不用请求超时推断校园网或校外网络。校外链路以原有可用 VPN/SSO/映射 CAS 流程为基线，不擅自移除网关 Cookie 和参数。
- 按本次 service 查询应用配置，使用 `loginAppId or appId`，不硬编码 HAR 中的临时标识。
- 获取非空 Token 不代表成功；所有方式必须通过资源只读权限探测。身份门户 Token 不得用于实验室业务。
- 手动 Token 独立导入仅限校园网直连，提示及 README 必须明确限制。
- 浏览器是可选 Playwright 适配器，使用本机 Edge、临时非持久化上下文，由用户完成凭据、验证码等交互。
- 浏览器仅捕获本次上下文中准确匹配实验室 validateLogin 地址、GET 方法、成功状态和目标 service 的响应；校内外均不猜测存储键，不导入门户响应。
- 浏览器材料仅在内存中交接并再次探测，不保存 storage_state、HAR、trace 或截图，不读取日常浏览器配置，不自动下载浏览器。
- 无证据时不实现 Token 刷新；课程线程不重新登录，不重放失败 POST。

## 并发与业务不可破坏的约束

- 只能使用 `ThreadPoolExecutor` 并发不同课程；默认 4，只允许整数 1–4，任何路径不得超过 4。
- 单课程顺序为：获取题目 → 按返回顺序逐题提交 → 标记完成。全部提交成功才完成，无题课程可以直接完成。
- 主业务客户端和所有课程副本共享同一个运行级 `MutationCoordinator`。认证包不创建或持有业务写入协调器。
- GET 可以并行，答题和课程完成等状态变更 POST 必须串行，同时最多 1 个。
- 全局认证失效或用户中断后取消待执行任务；取得写入锁后再次检查取消状态，防止排队 POST 继续发送。已发出请求不承诺撤回，应等待资源释放。
- 响应 `id` → `Question.submission_id` → 提交 `questionId`。
- 响应 `courseId` → `Question.course_id` → 提交 `id`。
- 响应 `questionId` → `Question.source_question_id`，仅供诊断，不提交。
- 答题接口接收完整 Question；三个标识语义不得合并，不从课程列表 ID 重建答题载荷。
- 字符串答案仅在含逗号时拆分并去除两侧空白；无逗号的 `AB` 不拆分。现有列表答案保持列表校验。
- 完成接口继续使用课程列表 `Course.id`，没有新证据不得替换。

## 网络和异常

- 所有请求设置连接/读取超时，默认 10/30 秒。
- 底层 HTTPAdapter 自动重试禁用；传输层显式重试 GET，最多三次，仅连接/读取故障和 429、500、502、503、504。
- 证书错误、认证失败、格式错误和 413 不重试；Retry-After 不得扩大允许状态集合。
- POST 不自动重试，307/308 不得重放 POST。超时或连接中断报告结果不确定，提示先到网页核对。
- CAS ticket 消费和 Token 交换即使使用 GET，也只尝试一次。
- 认证重定向逐跳校验目标，最多 10 跳；资源 Session 限定 API 范围，不携带资源 Token 跟随登录跳转。
- 每个响应检查 HTTP 状态，JSON 检查 success、code、message 和需要的 result。
- `acquire lock fail` 和 `aquire lock fail` 均转换为 LockConflictError，说明未自动重试。
- 单课程业务失败继续其他课程；HTTP/业务 401/403 及认证跳转属于全局错误，终止本轮并返回 2。

## 控制台、日志与敏感信息

- 不引入 argparse 或其他命令行参数解析。启动首先打印版本，第一次输入固定为是否自定义设置的 y/n 选择。
- 默认 debug 关闭、线程数 4；自定义时只接受有效线程数，非法输入重新询问。设置不持久化。
- 每次登录前询问网络环境，回车默认校外 VPN；账号密码失败后提供浏览器、校园网 Token、退出选择，不自动重新提交凭据。
- **密码使用普通 input 明文回显，不去除密码两侧空格。Token 继续使用 getpass 或等价隐藏输入。** 密码回显仅限用户输入时，不能将密码记录进日志或异常。
- 禁止在日志、异常、测试快照或文件中保存真实密码、Token、Cookie、tgc、CAS ticket、sessionId、完整学号或原始认证请求体。
- debug 关闭时不创建或检查日志文件，不输出 debug；保留版本、必要提示、课程进度、失败原因和汇总。
- debug 开启时控制台和 UTF-8 日志文件同时输出，日期时间放在内容中；文件名固定 `labpass_log.txt`。
- 源码日志目录为启动时工作目录；EXE 日志目录为 `sys.executable` 所在目录，不能使用临时解压目录。
- 文件必须原子独占创建。同名文件存在或目录不可写时，在认证前报错并退出 2；不得覆盖、追加、自动改名或静默关闭 debug。
- 所有应用日志出口使用同一个运行级脱敏上下文，包含异常堆栈；只输出安全且有界的服务端消息，不输出任意原始错误页。
- 正常退出和异常退出均关闭日志 handler；不通过全局变量保存认证状态或秘密值。
- EXE 结束时等待回车，源码不暂停；保持 `main.spec` 的 console=True。

## 退出码和代码规范

- 0：全部待处理课程成功或无待处理课程。
- 1：至少一门课程失败，已完成汇总。
- 2：配置、输入、日志初始化、认证、课程列表或全局认证失败。
- 130：Ctrl+C 中断。

使用现代类型注解、带 slots=True 的 dataclass 和具体异常。只有课程任务和 CLI 顶层允许未预期异常兜底；清理资源优先 finally/上下文管理器。不得裸 except、静默吞掉业务错误或新增 Rich。可恢复错误用简洁中文，脱敏堆栈仅在 debug 下显示。使用 Ruff 格式和静态检查。

## 测试要求

任何认证、API、解析、并发、日志或交互变更必须增加或更新测试：

- 使用假 Session、模拟传输及模拟浏览器事件；不得请求真实学校域名或内网地址，不得使用真实凭据。
- 覆盖校内外认证、动态应用选择、响应来源校验、资源权限探测及失败/中断后的资源关闭。
- 验证 GET 重试精确边界、POST 连接失败/超时/307/308 不重发，以及 ticket 交换不重放。
- 载荷测试为响应 id、courseId、questionId 使用三个不同假值，课程列表 ID 也单独设置，并精确断言 Payload。
- 验证单课程顺序、失败不完成、无题完成、单课失败不影响其他课程。
- 并发测试验证活跃任务不超过 4、GET 重叠、POST 最大并发 1、Session/Cookie/adapter 独立及协调器共享。
- 覆盖认证失效后的排队写入取消和待执行任务取消。
- 锁错误覆盖两种拼写，并断言失败 POST 仅发送一次。
- CLI 覆盖提示顺序、默认/自定义设置、非法输入、明文密码、隐藏 Token、登录回退和全部退出码。
- 日志覆盖源码/EXE 路径、已存在文件不变、关闭 debug 不落盘、双出口与异常堆栈脱敏。

提交前运行：

```powershell
uv run python -m compileall -q main.py njupt_auth njupt_safetylabpass labpass_cli tests
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## 依赖、打包和文档

核心运行依赖在 project.dependencies；Playwright 在 project.optional-dependencies.browser；测试/静态检查在 dependency-groups.dev；PyInstaller 在 dependency-groups.build。更改依赖后执行 uv lock，不手工编辑 uv.lock。

```powershell
uv sync --extra browser --group dev --group build
uv build
uv run --extra browser --group build pyinstaller --clean --noconfirm main.spec
```

wheel 应包含三个包，sdist 不得包含 Temp/HAR、认证材料或构建目录。EXE 必须包含由唯一版本源生成的 labpass 发行元数据及 Playwright 驱动，不包含 Edge 本体。build、dist 和日志不提交。

删除参数后，EXE 验收不再使用 --help/--version。用无凭据且不触发网络的输入检查版本首行、首个提示、EOF 退出、EXE 暂停、日志路径和文件冲突；另验证 wheel/sdist 安装及三包导入。

接口、目录、交互、网络要求、隐私、并发、重试、退出码、安装测试或构建命令变化时同步 README。README 不得承诺完成考试，也不得把模拟测试描述为真实账号成功。

## 人工验收边界

自动检查不得使用真实凭据。维护者使用本人账号分别验证校内外账号密码和浏览器认证、课程列表和状态、课程并发上限、逐题顺序以及网页最终状态。

交付分别报告自动测试、构建验证、真实账号测试的实际执行情况。只有前两项确实通过时才能写“自动测试和构建已通过，但未执行真实账号烟雾测试”。未验证的认证分支不得声称已在真实环境可用。
