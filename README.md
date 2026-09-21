# LabPass

南京邮电大学实验室安全教育课程辅助工具，要求 Python 3.13+。版本以 `pyproject.toml` 的 `project.version` 为唯一来源，启动时自动显示。

本工具用于本人账号的课程学习与课程答题，不能完成考试。考试仍需本人手动完成。本项目坚持免费。

## 安装与启动

```powershell
uv sync
uv run python main.py
```

也可使用安装后的控制台入口：

```powershell
uv run labpass
```

程序完全通过控制台交互配置，不再解析命令行参数，也不提供旧包、旧参数兼容层。

需要浏览器兜底时，安装可选依赖，并确保本机已安装 Microsoft Edge：

```powershell
uv sync --extra browser
uv run --extra browser python main.py
```

浏览器组件使用 Playwright 驱动本机 Edge，不读取日常浏览器配置，不在运行时自动下载或安装浏览器。未安装可选依赖时，账号密码和校园网 Token 登录仍可使用。

Windows 用户可以直接运行构建好的 `labpass.exe`。EXE 保持控制台模式，退出前等待回车；源码运行不暂停。

## 控制台交互

1. 首先显示版本号。
2. 第一次输入询问“是否自定义设置？[y/N]”。回车或 `n` 使用默认值：debug 关闭、课程线程数 4。
3. 选择 `y` 后，依次选择是否开启 debug 和课程线程数。线程数只接受 1–4，回车默认 4；非法输入重新询问。
4. 每次登录前选择网络环境：`1` 为校外 VPN（默认），`2` 为校园网直连。不会自动探测网络。
5. 输入学号和密码。**密码明文显示**，输入两侧空格也会作为密码的一部分保留。
6. 默认尝试账号密码登录；失败后可选 `1` 浏览器、`2` 校园网 Token、`0` 退出（默认）。失败请求不会自动重新提交。
7. 显示课程进度、失败原因及最终汇总，自动跳过已完成课程。

`y/n` 不区分大小写。本次设置不保存到磁盘。Token 仍通过隐藏输入获取。

## 登录方式与网络要求

| 方式 | 网络 | 行为 |
| --- | --- | --- |
| 账号密码 | 校外 | 保留原有 VPN、统一认证、映射 CAS 和实验室资源 Token 链路 |
| 账号密码 | 校园网 | 直连 CAS，按本次 service 获取应用配置和实验室资源 Token |
| 浏览器兜底 | 校内或校外 | 在新的临时 Edge 会话中由用户完成登录、验证码等操作，再交接已验证的 Session |
| 手动 Token | 仅校园网 | 隐藏输入实验室资源 Token，然后直连验证权限 |

所有方式都先读取实验室权限接口验证认证结果，再读取课程列表。身份门户 Token 与实验室资源 Token 不可混用。校外访问还依赖 VPN 会话，单独粘贴 Token 不能替代 VPN 上下文。

浏览器兜底仅监听本次会话中准确匹配实验室 `validateLogin` 地址、请求方法和目标 service 的成功响应，从中取得资源 Token，复制适用于资源地址的 Cookie，并使用 `requests.Session` 进行只读鉴权。不会猜测 VPN 页面存储键，也不会将门户响应当成实验室认证结果。等待上限 5 分钟，关闭窗口、超时或 Edge 不可用时可以重新选择登录方式。

手动 Token 获取步骤：

1. 连接校园网，在浏览器中登录 `http://10.22.192.38:9092/`。
2. 打开浏览器开发者工具的网络面板，查找实验室权限等已存在的只读请求，例如 `getUserPermissionByToken`。
3. 在该请求的请求头中复制 `X-Access-Token`，不要复制智慧校园门户的 Token。
4. 运行程序，在账号密码登录失败后的回退菜单选择校园网 Token，并在隐藏提示中粘贴。

无需为了获取 Token 提交题目或调用课程完成接口。不要分享真实 Token、Cookie 或抓包文件。

## 日志与隐私

debug 关闭时不创建或检查日志文件，不输出 debug 信息；保留版本、必要提示、课程进度、失败原因和汇总。

debug 开启时同时写入控制台和 UTF-8 文件 `labpass_log.txt`。日期时间包含在日志内容中，文件名不加时间戳：

- 源码运行：保存在启动时的工作目录。
- EXE 运行：保存在 EXE 所在目录，与当前终端目录、解压临时目录无关。
- 同名文件已存在：立即报错并退出，不覆盖、不追加。请自行移动或删除旧日志后再运行。
- 目录不可写：报错并退出，不静默切换目录或关闭 debug。

密码明文输入仅改变终端回显，不会把输入内容录入日志。日志、异常消息和调试堆栈会脱敏密码、Token、Cookie、`tgc`、CAS ticket、sessionId、完整学号及运行中登记的秘密值。程序不持久化认证信息；浏览器使用临时上下文，不保存认证状态、HAR、trace 或截图。提交日志前仍应检查个人信息。

## 项目职责

```text
main.py                    # 唯一脚本入口，只调用 CLI entrypoint
njupt_auth/                # 认证、加密、资源 Session、HTTP 策略和脱敏
njupt_safetylabpass/        # 课程 API、模型、单课程业务与写入协调器
labpass_cli/               # 交互、配置、日志、线程调度、进度和汇总
tests/                     # 默认阻断网络的模拟回归测试
main.spec                  # Windows 控制台 EXE 配置
pyproject.toml             # 唯一版本源、依赖和构建配置
uv.lock                    # 依赖锁文件
```

认证包不依赖业务包或 CLI；业务包不依赖 CLI。业务包复用认证包的基础超时常量和脱敏工具，登录流程不进入业务层。

认证结果包含实际 `requests.Session`、资源 API 基址及独立 Session 工厂。CLI 将它们传给 `SafetyLabClient`。返回的资源 Session 是 `requests.Session` 子类，负责将认证头限制在资源 API 范围内，并保留 VPN 访问参数。

主 Session 用于读取课程列表。每个课程任务从认证快照创建自己的 Session、CookieJar 和连接适配器，并在任务结束时关闭。主客户端与全部课程副本共享一个运行级 `MutationCoordinator`。所有线程结束后，再关闭主客户端和认证工厂。

单课程流程在 `njupt_safetylabpass.run_course` 内执行；`labpass_cli.runner.CourseRunner` 只管理线程池与结果回调；控制台文本由 CLI 渲染。

## 业务、并发与错误处理

- 只能并发不同课程，默认 4 个线程，任何配置不得超过 4。
- 每门课程按返回顺序逐题提交；全部成功后才标记完成，无题课程可直接完成。
- 课程 GET 可以并行。所有答题和课程完成 POST 共用一个写入锁，最大同时执行数为 1。
- 题目响应 `id` 对应提交 `questionId`；响应 `courseId` 对应提交 `id`；响应 `questionId` 仅作为 `source_question_id` 保存。
- 仅包含逗号的字符串答案拆分成列表；无逗号的 `AB` 保持字符串。课程完成使用课程列表 `Course.id`。
- 连接/读取超时默认 10/30 秒。GET 最多三次尝试，仅重试连接/读取故障及 429、500、502、503、504；认证失败、证书错误、413 不重试。
- 底层适配器不自动重试。POST 不自动重试，也不通过 307/308 重放；ticket 消费和 Token 交换只尝试一次。
- POST 超时或连接中断报告结果不确定，请先到网页核对。`acquire lock fail` 和 `aquire lock fail` 均报告锁冲突且不自动重试。
- 单课程失败后继续其他课程。HTTP/业务认证失效会取消待执行任务，阻止排队写入继续发送；已经发出的请求需等待结束。
- 不自动刷新 Token、不在课程线程中重新登录、不重放失败的写入。

| 退出码 | 含义 |
| --- | --- |
| 0 | 所有待处理课程成功，或没有待处理课程 |
| 1 | 至少一门课程失败，已完成结果汇总 |
| 2 | 配置、输入、日志初始化、认证、课程列表或全局认证失败 |
| 130 | 用户通过 Ctrl+C 中断 |

## 开发、测试与构建

```powershell
uv sync --extra browser --group dev --group build
uv run python -m compileall -q main.py njupt_auth njupt_safetylabpass labpass_cli tests
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
uv run --extra browser --group build pyinstaller --clean --noconfirm main.spec
```

EXE 位于 `dist/labpass.exe`。构建会包含由项目元数据生成的版本信息、Playwright Python 组件和驱动，但不包含 Edge 浏览器本体。`build/`、`dist/` 和调试日志不提交到仓库。

依赖变更后使用 `uv lock` 更新锁文件，不手工修改。测试参考历史有效基线重新组织，并覆盖认证、浏览器响应、重试、敏感信息、交互、题目载荷、课程顺序、Session 隔离、并发写入及退出码。测试默认阻断 HTTP 和 socket 网络出口；浏览器测试使用模拟对象，不启动学校页面。

构建后分别验证 wheel/sdist 安装和三包导入，以及 EXE 的无参数启动、版本首行、输入结束退出、源码/EXE 日志路径和日志冲突行为。程序不再提供 `--help`、`--version`；无凭据启动检查应在网络选择或凭据输入前结束，不能以真实账号执行自动验收。

## 验证边界

现有校外流程以维护者此前确认可用的实现为迁移基线。本次新增和重构的校内外 HTTP、浏览器能力，需要维护者使用本人账号分别进行真实环境验收；模拟测试、离线构建和 EXE 启动检查不能证明真实认证已成功。

维护者应分别确认校内外登录、课程列表及状态、课程并发上限、单课程顺序和网页最终状态。自动测试、构建验证、真实账号测试必须分别记录，未执行的项目明确标为未验证。

## 作者与反馈

- Author: MapleCake（NJUPT 2025届）
- GitHub: https://github.com/MapleSugarCake/LabLearningAutoPass
- QQ: 292441165
