# AGENTS.md

本文件适用于整个仓库，供后续开发者和编码代理在修改 LabPass 时遵循。

## 项目概览

LabPass 是面向南京邮电大学实验室安全教育系统的 Python 3.13+ 控制台工具。项目支持自动 SSO/VPN 登录、校园网手动 Token 登录、动态课程发现、顺序答题，以及最多 4 个课程任务并发。

项目版本以 `pyproject.toml` 中的 `[project].version` 为唯一来源。不要在其他源码文件中重复维护版本常量。

## 目录与职责

- `main.py`：兼容入口，只调用 `labpass.cli.entrypoint`，不要在这里增加业务逻辑。
- `labpass/cli.py`：参数解析、交互输入、顶层异常处理、退出码和结果汇总。
- `labpass/auth.py`：自动认证流程与手动 Token Session 创建。
- `labpass/client.py`：业务 API、HTTP/业务响应校验、课程和题目解析。
- `labpass/runner.py`：课程级线程池、单课程处理顺序和结果聚合。
- `labpass/models.py`：领域 dataclass 和状态类型。
- `labpass/config.py`：URL、超时、并发上限、请求头和其他静态配置。
- `labpass/http.py`：Session 公共配置和 GET-only 重试策略。
- `labpass/crypto.py`：与学校前端兼容的凭据加密。
- `labpass/logging_utils.py`：控制台日志配置与敏感信息脱敏。
- `tests/`：完全模拟 HTTP 的单元测试，不得访问或修改真实学校系统。
- `main.spec`：Windows 控制台 EXE 的 PyInstaller 配置。

若新增行为，优先放入职责对应的现有模块。只有在职责明显独立且现有模块会变得混乱时才新增模块。

## 不可破坏的行为约束

### 并发

- 只能并发处理不同课程，使用 `ThreadPoolExecutor`。
- 默认并发数为 4，用户可调整为 1–4，任何路径都不得突破 4。
- 单门课程内必须保持：获取题目 → 按返回顺序逐题提交 → 标记课程完成。
- 只有全部题目成功提交后才可调用课程完成接口；没有题目的课程可以直接完成。
- 不得在线程间共享同一个 `requests.Session`。每个课程任务必须使用独立 Session，并在任务结束后关闭。

### 网络与重试

- 所有请求必须设置连接和读取超时，当前默认值为 10/30 秒。
- 自动重试只允许用于 GET，当前最多尝试 3 次，并仅针对连接问题及 429、500、502、503、504。
- POST 不得自动重试。POST 超时必须报告为“结果不确定”，提示用户先到网页核对。
- 每个响应都要检查 HTTP 状态；JSON 业务响应还要检查 `success`、`code`、`message` 和需要的 `result`。
- 单课程错误应记录失败并继续其他课程；401/403 或业务层认证失效属于全局错误，应取消待执行任务并返回退出码 2。

### 登录与敏感信息

- 密码和 Token 必须通过 `getpass` 或等价隐藏输入获取，不得使用普通 `input`。
- 不得把账号密码、Token、Cookie、CAS ticket、完整学号或原始认证请求体写入控制台、异常消息、测试快照或文件。
- 新增日志内容必须经过敏感性审查；异常响应只能输出截断且脱敏的摘要。
- 项目默认不创建日志文件，也不持久化任何认证信息。
- 手动 Token 模式使用校内直连 API，相关提示和 README 必须明确其校园网限制。

### 退出码

- `0`：所有待处理课程成功，或没有待处理课程。
- `1`：至少一门课程失败，但整体流程已完成汇总。
- `2`：配置、登录、课程列表或全局认证失败。
- `130`：用户通过 `Ctrl+C` 中断。

修改顶层流程时必须保持这些含义稳定。

## 代码规范

- 目标运行时为 Python 3.13+，使用现代类型注解和标准库能力。
- 领域数据优先使用带 `slots=True` 的 dataclass，不使用无结构的并行列表传递数据。
- 捕获最具体的异常；仅可在课程任务和 CLI 顶层设置兜底异常边界。
- 用户可恢复的错误应给出简洁中文消息；详细堆栈只在 `--debug` 下显示。
- 不使用裸 `except`，不静默吞掉业务错误，不通过全局变量共享运行状态。
- 不新增 Rich 等界面依赖，除非项目需求明确改变。
- 使用 Ruff 保持格式、导入顺序和静态规范，不手工引入与现有格式冲突的风格。

## 测试要求

任何涉及认证、API、解析、并发、错误处理或 CLI 的修改，都必须增加或更新测试。

测试必须遵守：

- 使用模拟 `requests.Session` 或假客户端，不得请求真实 NJUPT 域名和内网地址。
- 不得在 fixture 中使用真实账号、密码、Cookie、Ticket 或 Token。
- 并发测试需要验证最大活跃任务数不超过 4，并验证每个任务使用独立客户端或 Session。
- 请求测试需要验证 GET 重试边界和 POST 不重试/结果不确定行为。
- 日志相关修改必须验证敏感字段脱敏。
- CLI 修改必须覆盖参数校验和相关退出码。

提交修改前运行：

```powershell
uv run python -m compileall -q main.py labpass tests
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## 依赖与构建

- 运行依赖声明在 `pyproject.toml` 的 `project.dependencies`。
- 测试和静态检查依赖放入 `dependency-groups.dev`。
- PyInstaller 等构建依赖放入 `dependency-groups.build`。
- 依赖发生变化后必须执行 `uv lock` 并提交更新后的 `uv.lock`。
- 不直接编辑 `uv.lock`。

Windows EXE 构建命令：

```powershell
uv run --group build pyinstaller --clean --noconfirm main.spec
```

构建后至少验证：

```powershell
.\dist\labpass.exe --help
.\dist\labpass.exe --version
```

`main.spec` 必须保持 `console=True`，否则隐藏输入、进度输出和结束暂停均无法可靠工作。`build/` 与 `dist/` 是生成目录，不应提交。

## README 同步

当以下内容变化时，必须同步更新 `README.md`：

- CLI 参数、默认值或退出码；
- 登录方式、网络要求或 Token 获取流程；
- 项目目录结构；
- 并发、重试、错误处理或隐私行为；
- 安装、测试或 EXE 构建命令；
- Python 最低版本或项目版本。

README 中不得承诺脚本可以完成考试，也不得声称未经真实账号验证的接口行为已经验证。

## 人工验收边界

自动化检查不得使用真实凭据。若修改学校认证或业务接口，最终需要项目维护者使用本人账号人工确认：

1. 自动登录成功；
2. 课程列表和完成状态正确；
3. 并发课程不超过 4；
4. 单课程题目按顺序提交；
5. 网页最终状态与控制台汇总一致。

没有真实凭据时，应明确报告“自动测试和构建已通过，但未执行真实账号烟雾测试”，不得伪造验证结果。
