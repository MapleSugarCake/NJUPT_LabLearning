# AGENTS.md

本文件适用于整个仓库，供开发者和编码代理修改 LabPass 时遵循。

“必须”“不得”等要求是项目规范；标为“当前实现”的内容用于定位代码和理解调用链，不自动覆盖规范。发现两者冲突时记录为“已知偏差”，修复代码后再更新记录，不把实现偏差改写为新的规范。

## 项目与职责

LabPass 是 Python 3.13+ 控制台工具。一个发行项目包含 `njupt_auth`、`njupt_safetylabpass`、`labpass_cli` 三个包，不保留旧 `labpass` 包或旧参数兼容层。版本以根目录 `pyproject.toml` 的 `project.version` 为唯一来源，不在源码中维护版本常量或硬编码回退值。

- `main.py`：仅调用 `labpass_cli.cli.entrypoint`。
- `njupt_auth`：校内外 SSO/CAS/VPN、加密、浏览器认证、Token 导入、只读鉴权、Session 工厂、传输策略和脱敏基础能力；不包含课程业务、线程调度或控制台交互。
- `njupt_safetylabpass`：使用已认证资源会话执行课程业务，不负责登录、交互或线程池；`client.py` 负责业务 API、HTTP/业务校验、课程和题目解析。
- `njupt_safetylabpass/course.py`：单课程顺序执行；只返回结果和进度事件，不创建线程池或输出控制台文本。
- `njupt_safetylabpass/models.py`、`coordination.py`：领域模型、共享写入锁及取消信号。
- `labpass_cli`：交互、运行配置、版本读取、日志配置、线程池、进度显示、退出码和汇总。
- `tests/`：完全模拟请求，默认阻断 HTTP 和 socket 网络出口。
- `main.spec`：控制台 EXE，包含发行元数据和 Playwright 驱动，调用本机 Edge。

优先使用现有职责模块。认证包不得依赖业务包或 CLI；业务包不得依赖 CLI。业务包可以复用认证包的超时常量和脱敏基础工具，但不得执行登录。

### 认证包模块边界

| 模块 | 职责与边界 |
| --- | --- |
| `njupt_auth/auth.py` | 编排账号密码认证、动态应用查询、受控跳转、票据交换、Token 导入和权限探测；内部 `create_result` 是各认证方式建立资源会话的共同收尾入口。 |
| `njupt_auth/browser.py` | 可选 Playwright 适配器，在调用浏览器认证时才导入 Playwright；捕获本次浏览器上下文的有效资源响应后交给共同收尾流程。 |
| `njupt_auth/transport.py` | `HttpSession` 管理超时、显式 GET 重试和禁止自动跳转；`ResourceSession` 限制认证材料的发送范围；`ResourceSessionFactory` 创建独立资源会话。 |
| `njupt_auth/models.py` | 网络环境枚举及认证结果、会话生命周期；不包含课程模型。 |
| `njupt_auth/crypto.py`、`config.py` | 分别实现认证协议使用的加密和协议地址/默认传输常量；不承载课程逻辑或 CLI 运行设置。加密实现仅服务于当前协议，不作为通用密码学接口。 |
| `njupt_auth/errors.py`、`redaction.py` | 分别定义认证异常和可共享的线程安全脱敏上下文；不选择登录回退方式，不配置控制台或日志文件。 |

### 业务与 CLI 模块边界

- 业务包根导出 `SafetyLabClient`、`MutationCoordinator`、`run_course` 和 `Course`、`Question`、`CourseProgress`、`CourseResult`、`CourseStatus`；导入包不触发认证或课程请求。异常从 `exceptions.py` 导入。
- CLI 的 `cli.py` 编排交互和调用；`config.py` 保存本轮设置；`runner.py` 管理课程线程池和任务资源；`models.py` 汇总结果；`logging_utils.py` 管理日志出口与脱敏格式；`version.py` 读取发行元数据。
- 当前调用链：`entrypoint` → `run_cli` 输出版本、读取设置、建立日志上下文、选择网络 → `execute` 认证、建立客户端、读取并筛选课程 → `CourseRunner.run` → 每门课程 `client.clone()` + `run_course` → 收集结果、释放会话、打印汇总并返回退出码。
- `CourseRunner` 按完成顺序收集结果；进度回调在课程工作线程执行，完成回调在收集结果的线程执行。回调调用方须考虑此线程归属，业务层不直接输出控制台文本。

## 认证与 Session 契约

### 公共接口与模型

包根公开三个认证入口及 `check_access`、`AuthenticationResult`、`NetworkEnvironment`、`AuthError`、`AuthExpiredError`；`create_result` 等内部辅助函数不是包根公共接口。

| 接口 | 输入、返回与边界 |
| --- | --- |
| `authenticate(username, password, *, environment, redactor=None, session_factory=HttpSession)` | 显式选择网络，返回通过权限探测的 `AuthenticationResult`。拒绝空白账号和空密码，保留密码两侧空格。这里的 `session_factory` 是认证引导会话的注入点，不是结果中的资源会话工厂。 |
| `authenticate_in_browser(*, environment, redactor=None)` | 使用指定网络的临时 Edge 上下文，返回同一种已验证认证结果；缺少可选组件或 Edge 时报告认证异常，不自动安装。 |
| `import_token(token, *, redactor=None)` | 仅将实验室资源 Token 导入校园网直连会话，从空 Cookie 集合开始，不建立 VPN；探测成功后返回认证结果。 |
| `check_access(session, api_base_url, *, redactor)` | 使用现有会话 GET 资源权限接口，成功返回 `None`；校验 HTTP、业务结果及 `result.menu` 为列表。HTTP 401/403 或跳转视为认证失效，不刷新 Token、不重新登录。传入普通 Session 的策略缺口见网络章节。 |

认证失败以 `AuthError` 体系报告，具体分类见网络和异常章节。`NetworkEnvironment` 是 `StrEnum`，成员为 `INTRANET="intranet"` 和 `EXTRANET="extranet"`。

- `AuthenticationResult` 是带 `slots=True` 的数据类，交接 `session: requests.Session`、`api_base_url: str`、`session_factory: ResourceSessionFactory`；会话与工厂不进入自动生成的 repr。资源 Session 可以是 `requests.Session` 子类；不得仅交接 Token 或要求业务重新建立认证。
- 结果支持上下文管理，`close()` 依次关闭主会话和资源工厂。CLI 中主客户端也关闭同一主会话，项目管理的 Session 支持重复关闭；不得因此省略认证结果的生命周期管理。
- Session 工厂保存本轮 Token、资源主机相关 Cookie 和附加请求头快照，为每门课程创建独立 Session、请求头、CookieJar、Cookie 对象和连接适配器，保留 Cookie domain/path/secure/expires 等属性及适用于 API 子路径的 Cookie。各会话共享 Redactor，不实时同步彼此或主会话后续的 Cookie/请求头变化。
- 课程任务各自关闭课程 Session；线程池结束并等待已启动任务释放后，再关闭主会话与认证工厂。工厂关闭清空快照并禁止继续创建会话（当前抛出 `RuntimeError`），不负责关闭已经交出的会话。
- 认证引导会话无论成功、失败或中断均关闭；共同收尾流程探测失败时释放新建主会话和工厂。浏览器取消或认证结束必须释放临时浏览器资源，成功后仅交接资源会话所需的内存材料。

### 认证流程与约束

当前实现的关键流程如下；跳转、重试和 Token 发送范围仍受网络章节约束。

- 校园网账号密码：实验室 service 的 CAS 入口 → 必要时查询动态应用配置并提交加密凭据 → 获取实验室 ticket → `validateLogin` 一次交换资源 Token。
- 校外账号密码：VPN 预登录 → 以 VPN 回调为 service 的统一认证 → 确认 VPN/网关授权 → 映射身份服务上的实验室 CAS → 必要时完成该 service 的认证 → 一次交换实验室资源 Token。两个阶段各自使用对应 service 的应用配置，不混用门户 Token 与资源 Token。
- 浏览器：临时 Edge 上下文进入对应网络的实验室认证入口，由用户完成交互 → 仅捕获目标 `validateLogin` 的 GET、HTTP 200、准确 service 和唯一非空 ticket 对应的成功响应 → 复制适用 Cookie 与 User-Agent → 共同收尾。校外 VPN 回调完成后，适配器重新进入映射的实验室 CAS。
- Token 导入：校内资源 Token + 空 Cookie 集合 → 共同收尾。
- 共同收尾：`create_result` 创建资源快照工厂及主会话 → `check_access` 只读权限探测 → 返回 `AuthenticationResult`。获取非空 Token 不代表成功；身份门户 Token 不得用于实验室业务。

维护上述流程时须遵守以下规范：

- 账号密码认证显式选择网络，不用请求超时推断校园网或校外网络。校外链路以原有可用 VPN/SSO/映射 CAS 流程为基线，不擅自移除网关 Cookie 和参数。
- 按本次 service 查询应用配置，使用 `loginAppId or appId`，不硬编码 HAR 中的临时标识。
- 手动 Token 独立导入仅限校园网直连，提示及 README 必须明确限制。
- 浏览器是可选 Playwright 适配器，使用本机 Edge、临时非持久化上下文，由用户完成凭据、验证码等交互。
- 浏览器响应来源须准确匹配资源地址的协议、主机及路径；校内外均不猜测存储键，不导入门户响应。
- 浏览器材料仅在内存中交接并再次探测，不保存 storage_state、HAR、trace 或截图，不读取日常浏览器配置，不自动下载浏览器。
- 无证据时不实现 Token 刷新；课程线程不重新登录，不重放失败 POST。

## 并发与业务不可破坏的约束

- 只能使用 `ThreadPoolExecutor` 并发不同课程；默认 4，只允许整数 1–4，任何路径不得超过 4。
- 单课程顺序为：校验视频总时长 → 获取题目 → 按返回顺序逐题提交 → 一次上报视频总时长的整数秒 → 标记完成 → 回读课程列表核验。无题课程同样执行视频步骤，任一步失败不继续后续写入。
- 视频总秒数取课程列表 `duration`，用 Decimal 保留精度，提交时截取整数秒字符串作为 `finishRate` 的 `watchDuration`，不得超过总时长；不向上取整、不提交小数字符串、不分段定时发送。列表响应的 `watchDuration` 是百分比，不得作为秒数提交。缺失或非法时长为单课程失败，不猜测。
- 默认模式仅跳过 `video_finished` 为真的课程；成功判定始终要求 `isFinish` 完成且视频百分比恰好为 100。保留超 100 的实际百分比。首次 finish 成功后回读，若有效百分比不等于 100，最多追加一次 finish 并再次核验，不重复答题或 finishRate；百分比已为 100 但完成标记未达标时不补交。
- 请求失败、超时、锁错误、非法百分比、记录缺失或回读失败不得触发业务补交；仍未达标不报告成功。回读沿用 GET 传输重试，认证失效仍是全局错误。上述成功后补交是明确业务流程，不是失败 POST 的自动重试。
- 主业务客户端和所有课程副本共享同一个运行级 `MutationCoordinator`。认证包不创建或持有业务写入协调器。
- GET 可以并行，答题、视频进度和课程完成等状态变更 POST 必须串行，同时最多 1 个。
- 全局认证失效或用户中断后取消待执行任务；取得写入锁后再次检查取消状态，防止排队 POST 继续发送。已发出请求不承诺撤回，应等待资源释放。
- 响应 `id` → `Question.submission_id` → 提交 `questionId`。
- 响应 `courseId` → `Question.course_id` → 提交 `id`。
- 响应 `questionId` → `Question.source_question_id`，仅供诊断，不提交。
- 答题接口接收完整 Question；三个标识语义不得合并，不从课程列表 ID 重建答题载荷。
- 字符串答案仅在含逗号时拆分并去除两侧空白；无逗号的 `AB` 不拆分。现有列表答案保持列表校验。
- 完成接口继续使用课程列表 `Course.id`，没有新证据不得替换。

### 业务模型与接口

- `Course` 为冻结且带 slots 的数据类：`id`/`name` 为课程标识和名称，`type_name` 可缺省；`finished` 保留解析后的服务端 `isFinish` 标记，不单独代表视频完成。`duration_seconds: Decimal | None` 来自 `duration`；`video_percent: Decimal | None` 来自列表 `watchDuration`，不是秒数。
- 时长必须是有限正数，百分比必须是有限非负数；缺失、布尔值或非法值解析为 `None`。百分比大于 100 仍保留。`require_video_duration()` 在写入前拒绝无效时长；正数按 `ROUND_DOWN` 截取整数秒，当前小于一秒的正时长会提交字符串 `"0"`，不得擅自猜测补足时长。
- `submit_answer(question)` 接收完整 `Question`，其 `answer` 是字符串或字符串列表，ID 映射遵循上文。`submit_video_progress(course)` 向 `finishRate` 提交 `{"id": Course.id, "watchDuration": 整数秒字符串}`；`finish_course(course_id)` 向 `finish` 提交课程列表 ID。
- `read_video_status(course_id)` 每次读取完整课程列表，按 ID 返回当前 `Course`；缺失记录或普通读取失败报告“视频完成状态未确认”，认证失效及取消继续传播。`verify_video_finished(course_id)` 只读并核验，成功返回 `None`，自身不补交；最多一次 finish 补交由 `run_course` 控制，无业务轮询。
- `CourseProgress` 仅含 `started`、`answered` 两种阶段及课程、答题数、题目数，不代表视频已完成。`CourseResult` 保留课程、`CourseStatus.SUCCESS/FAILED`、题目计数和安全错误说明；`uncertain=True` 表示提交结果不确定，不能当作成功或重试依据。
- `run_course` 不关闭传入客户端；普通业务异常转为单课程失败，认证失效登记全局取消并继续抛出，取消异常同样向上传播。`CourseRunner` 为每项任务创建和关闭客户端副本；异常终止时取消待执行任务并等待已启动任务释放。

## 网络和异常

- 所有请求设置连接/读取超时，默认 10/30 秒。
- 底层 HTTPAdapter 自动重试禁用；传输层显式重试 GET，最多三次，仅连接/读取故障和 429、500、502、503、504。
- 证书错误、认证失败、格式错误和 413 不重试；Retry-After 不得扩大允许状态集合。
- POST 不自动重试，307/308 不得重放 POST。超时或连接中断报告结果不确定，提示先到网页核对。
- CAS ticket 消费和 Token 交换即使使用 GET，也只尝试一次。认证跳转流程共享一次性地址消费记录；网关授权也按一次性材料处理，不因进入下一认证阶段而重置消费限制。
- `HttpSession` 禁止 requests 自动跳转；认证编排逐跳校验协议、主机及路径，单条跳转链最多 10 跳，并检测循环。不得通过允许自动跳转来绕过票据和目标校验。
- `ResourceSession` 只向资源 API 基址的同协议、同主机和规范化子路径发送请求，拒绝片段、路径穿越及可疑编码。通过校验后逐请求附加 `X-Access-Token`，不将其放入通用默认头，不携带资源 Token 跟随登录跳转。
- 校外资源 GET 从适用的 `vpn_timestamp` Cookie 补充 `_t`（调用方已提供时保留）；POST 在查询串附加 `enlink-vpn`。这由资源 Session 负责，业务代码不得另建会话或重复实现网关认证。
- 每个响应检查 HTTP 状态，JSON 检查 success、code、message 和需要的 result。
- `acquire lock fail` 和 `aquire lock fail` 均转换为 LockConflictError，说明未自动重试。
- 单课程业务失败继续其他课程；HTTP/业务 401/403 及认证跳转属于全局错误，终止本轮并返回 2。

认证异常与业务异常是两个独立层次，不按名称相近互相替换：

- 认证侧 `AuthError` 的子类包括协议错误 `AuthProtocolError`、凭据错误 `InvalidCredentialsError`、需人工交互 `InteractionRequiredError`、网络/服务不可用 `AuthUnavailableError`、一次性请求结果不确定 `AuthOutcomeUncertainError`、认证失效 `AuthExpiredError` 和浏览器不可用 `BrowserUnavailableError`。CLI 决定登录回退，认证包不提示输入或自动重交凭据。
- 业务侧以 `LabPassError` 为基类；`AuthenticationExpiredError` 和 `RunCancelledError` 向调度层传播。读取网络错误用 `NetworkError`，服务端拒绝用 `ApiError`，其子类包括 `ResponseFormatError`、`LockConflictError`、`SubmissionUncertainError`；最后一种映射到课程结果的 `uncertain`。

**已知偏差：权限探测的通用 Session 契约。** 当前 `check_access` 注解允许 `requests.Session`，但直接调用 `session.get()`，未显式传入超时及 `allow_redirects=False`；项目认证流程使用的 `ResourceSession` 会施加这些策略，直接传入普通 Session 则无此保证。此处是待修复接口缺口，不是对“所有请求设置超时”和受控跳转规范的例外。

## 控制台、日志与敏感信息

- 不引入 argparse 或其他命令行参数解析。启动首先打印版本，第一次输入固定为是否自定义设置的 y/n 选择。
- `RunSettings` 默认 `debug=False`、`workers=4`、`force_resubmit=False`；自定义设置按 debug → 并发线程数 → 是否强制重新提交所有课程的顺序询问。线程数只接受整数 1–4，布尔值不作为整数接受，非法输入重新询问。选择默认设置不询问强制模式，设置不持久化。
- 强制模式只改变首次课程列表的筛选：按课程 ID 去重后全部处理，包括原本已完成的课程；每门仍执行完整答题、视频上报和完成核验。回读新增课程不加入本轮，不删除课程记录，也不承诺服务端覆盖旧结果。
- `RunSummary.discovered` 为首次去重后的课程数量，`results` 是本轮结果元组；`already_finished` 当前字段语义是实际跳过数量，强制模式为零，不等于首次列表中已完成数量。成功/失败只统计本轮执行结果。
- 每次登录前询问网络环境，回车默认校外 VPN；账号密码失败后提供浏览器、校园网 Token、退出选择，不自动重新提交凭据。
- **密码使用普通 input 明文回显，不去除密码两侧空格。Token 继续使用 getpass 或等价隐藏输入。** 密码回显仅限用户输入时，不能将密码记录进日志或异常。
- 禁止在日志、异常、测试快照或文件中保存真实密码、Token、Cookie、tgc、CAS ticket、sessionId、完整学号或原始认证请求体。
- debug 关闭时不创建或检查日志文件，不输出 debug；保留版本、必要提示、课程进度、失败原因和汇总。
- debug 开启时控制台和 UTF-8 日志文件同时输出，日期时间放在内容中；文件名固定 `labpass_log.txt`。
- 源码日志目录为启动时工作目录；EXE 日志目录为 `sys.executable` 所在目录，不能使用临时解压目录。
- 文件必须原子独占创建。同名文件存在或目录不可写时，在认证前报错并退出 2；不得覆盖、追加、自动改名或静默关闭 debug。
- 所有应用日志出口使用同一个运行级脱敏上下文，包含异常堆栈；只输出安全且有界的服务端消息，不输出任意原始错误页。`run_cli` 创建 Redactor 并交给认证、会话工厂及日志格式器；认证/传输层登记秘密值，`logging_utils.py` 过滤各日志出口及堆栈，CLI 最终在 `finally` 清空登记。关闭认证结果本身不会清空共享 Redactor；独立使用认证包时由调用方管理其生命周期。
- 正常退出和异常退出均关闭日志 handler；不通过全局变量保存认证状态或秘密值。
- EXE 结束时等待回车，源码不暂停；保持 `main.spec` 的 console=True。

**已知偏差：入口输出顺序。** 当前 `main.py` 在调用 `entrypoint` 前打印横幅，违反“入口仅调用 entrypoint”和“启动首先打印版本”的规范；不能因为 `run_cli` 自身先输出版本就认定源码入口合规。对应回归检查为 `tests/test_cli.py::test_source_entrypoint_version_first_and_eof_before_network`。此偏差修复前保留规范和检查，不把横幅首行写成新约定。

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
- 验证单课程顺序、失败不完成、无题视频上报及回读、单课失败不影响其他课程。
- 验证小数时长截取为不超过总时长的整数秒字符串且仅一次上报、秒数与百分比语义分离、无效时长不写入。覆盖超 100 实际进度保留、非 100 时仅一次 finish 补交、补交成功/仍失败/取消/认证失效及未知状态不补交。
- 并发测试验证活跃任务不超过 4、GET 重叠、POST 最大并发 1、Session/Cookie/adapter 独立及协调器共享。
- 覆盖认证失效后的排队写入取消和待执行任务取消。
- 锁错误覆盖两种拼写，并断言失败 POST 仅发送一次。
- CLI 覆盖提示顺序、默认/自定义设置、非法输入、明文密码、隐藏 Token、登录回退和全部退出码。
- 强制模式覆盖默认关闭、自定义提示顺序、空列表/全部完成/混合状态、去重和仅首次列表调度、跳过数为零、完整业务顺序，以及普通失败、取消、认证失效和未知完成状态的边界；不得绕过写入串行或核验成功条件。
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
