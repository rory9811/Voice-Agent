# Claude Code 任务后端（订阅额度）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在设置页的“任务连接”里新增“任务执行器：Codex / Claude Code”开关。选 Claude Code 后，后台任务、任务路由、结果摘要、多模态直答、Skill 规划全部改由 Claude Code 命令行执行，不再启动任何 Codex 进程，而且只用 Claude 订阅额度，不走 API 计费。

**Architecture:** 不改 Codex 的代码路径，只新增一条平行的 Claude 路径。新增两个文件：`harness/llm/claude.py` 负责“启动 `claude -p`、读取事件流、确认用的是订阅登录”这些公共部分，以及一次性模型调用；`harness/agents/claude.py` 是后台任务执行器，继承 `CodexAgentProvider` 的任务生命周期（排队、追问、超时、结果文件），只替换和命令行打交道的三个方法。执行器、各环节提供方的选择集中在 `harness/settings.py`（校验）和 `harness/backends.py` / `harness/factory.py`（装配）。

**Tech Stack:** Python 3.11、asyncio 子进程、Claude Code CLI 2.1.309（`-p` 模式 + stream-json）、FastAPI 设置接口、原生 JS 设置页、Docker（bubblewrap + socat 沙箱）。

**Spec:** 本计划的「设计决定」一节（无单独 spec 文档），设计已在对话中确认。

## 现状：Codex 用在哪里

| 用途 | 现在的实现 | 位置 |
|---|---|---|
| 后台任务（General） | 常驻 `codex app-server`，JSON-RPC 协议；自定义工具 `context_fetch` 取任务开始时的上下文 | `harness/agents/codex.py`、`app_server.py` |
| 路由、结果摘要（Polish）、Skill 规划 | 每次调用一次 `codex exec`，只读沙箱 | `harness/llm/backends.py:CodexPlannerBackend` |
| 多模态直答 | `codex exec --image` | `harness/llm/delegate.py:CodexDirectAndPolish.execute` |
| 提供方选择 | 三个环节各自选 `codex` / `gemini`；后台任务固定 Codex | `harness/config.py:ModelCallConfig`、`harness/backends.py`、`harness/factory.py` |
| 登录检查 | `codex login status` 的退出码 | `demos/server/codex_login.py`、`configuration.py` |
| 镜像 | 下载固定版本 Codex 二进制；bubblewrap | `deploy/web/Dockerfile` |

## 已在本机验证的 Claude Code 行为（2.1.309）

- `claude auth status --json`：订阅登录时 `authMethod` 为 `"claude.ai"`；设了 `ANTHROPIC_API_KEY` 时为 `"api_key"`；设了 `ANTHROPIC_AUTH_TOKEN` 或 `CLAUDE_CODE_OAUTH_TOKEN` 时都显示 `"oauth_token"`，两者无法区分。
- `-p --output-format stream-json --verbose` 的第一条 `system/init` 消息带 `apiKeySource`，订阅登录时为 `"none"`。
- `--setting-sources ""` 与 `--strict-mcp-config` 可用：不加载任何设置文件，也不加载 claude.ai 账号里的 MCP 连接器。
- `--json-schema <schema>`：最终 `result` 消息里有 `structured_output`（已解析的对象）。
- `--input-format stream-json`：stdin 写一条 `{"type":"user","message":{"role":"user","content":[文本块, {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":…}}]}}` 即可传原生图片。
- `--session-id <uuid>` 预先指定会话 ID；之后 `--resume <uuid>` 能接上同一会话的记忆。会话按工作目录存放，所以两次的 cwd 必须相同。
- 沙箱：`--settings '{"sandbox":{"enabled":true,"network":{"allowedDomains":["*"]}}}'` 时沙箱内 `curl` 返回 200；`allowedDomains: []` 时被拦截。在默认权限模式加 `--permission-prompts none` 下，沙箱内的 Bash 命令自动放行。
- 文档说明：沙箱只约束 Bash 及其子进程。Read、Edit、Write、WebFetch、WebSearch 走权限规则，不受沙箱约束；`.claude/settings.json` 等受保护路径即使在可写目录内也禁止写入。Linux 上沙箱需要 `bubblewrap` 和 `socat`。

## 设计决定

- **D1 一个开关切换执行器。** 设置文件新增顶层 `"agent": "codex" | "claude"`。三个环节（`routing`、`responses`、`multimodal`）的 `provider` 取值扩为 `codex` / `claude` / `gemini`，并且必须是 `agent` 本身或 `gemini`，违反时 `HarnessSetup` 抛 `ValueError`。这条规则在服务端校验，不只靠页面：设置一旦能加载，就能保证选 Claude 时没有任何环节指向 Codex。Gemini 保留，因为它不属于 Codex，只有用户自己选才会用。
- **D2 设置文件格式。** 新增 `"claude": {"command": ["claude"], "model": null, "effort": "low"}`。两个执行器共用的运行参数（`vision`、各种超时、`network_access`）继续放在 `codex` 段，不改名。原因：不让 main 上已存的 `harness.json` 失效，也少和 main 冲突。`GeneralAgentConfig` 新增字段 `agent`；`agent == "claude"` 时，`command` / `model` / `effort` 取自 `claude` 段。
- **D3 推理强度按执行器校验。** Codex：`minimal low medium high xhigh`；Claude：`low medium high xhigh max`；Gemini 不发送强度，接受两者的并集。`null` 表示用命令行默认值（仅后台任务允许）。
- **D4 只走订阅：从源头去掉 API 计费的来源，再在三处核对。**
  1. 每次启动 `claude`（任务、一次性调用、登录检查），子进程环境里删掉所有 `ANTHROPIC_*`、`CLAUDE_CODE_USE_*` 和 `CLAUDE_CODE_OAUTH_TOKEN`，并固定带 `--setting-sources "" --strict-mcp-config`。设置文件里的 `apiKeyHelper` 也会让它改用 API key，所以设置文件一个都不加载。这两个参数是全局参数，登录检查必须放在子命令之前：`claude --setting-sources "" --strict-mcp-config auth status --json`（已验证；放在 `auth status` 之后会报 unknown option）。永不使用 `--bare`。
  2. 登录检查只有 `authMethod == "claude.ai"` 才算就绪；`loggedIn` 为真但方式不同时，给出单独状态 `not_subscription`，并阻止开始对话。
  3. 每次运行读到 `system/init` 时，`apiKeySource != "none"` 就立刻结束进程组并报错，这次运行不产生任何结果。
  4. 订阅账号可以开通“额外用量”（超出订阅额度后按量付费，计费方式和 API 一样）。事件流里每次请求都会带一条 `rate_limit_event`，其中 `rate_limit_info.isUsingOverage` 标明这次是否在用额外用量。一旦它为 `true`，立刻结束进程组，按“订阅额度已用完”报错，不消耗按量付费的额度。（本机实测：你的账号目前 `overageStatus` 为 `rejected`，也就是额外用量没有开通，这一条是防止以后开通后被悄悄计费。）
- **D5 后台任务：每轮启动一个 `claude -p` 进程。** 第一轮生成 uuid 作为会话 ID（`--session-id`），并写入 `history.thread_id`；追问时用 `--resume <同一 ID>`，cwd 不变。每轮都有自己的进程，结束后进程退出；`history.client` 只在这一轮运行期间指向它（继承来的 `forget_session` / `aclose` 会调用它的 `close()`）。
- **D6 后台任务的权限边界与 Codex 一致**（命令只能写任务工作目录，可联网）。参数：`--permission-mode acceptEdits --permission-prompts none --add-dir <工作目录根>`，再用 `--settings` 打开沙箱：`{"sandbox":{"enabled":true,"failIfUnavailable":true,"autoAllowBashIfSandboxed":true,"network":{"allowedDomains":["*"] 或 []}}}`。`network_access` 为真时追加 `--allowedTools WebFetch WebSearch`，为假时追加 `--disallowedTools WebFetch WebSearch`。沙箱不可用时让 Claude 直接报错（`failIfUnavailable`），不会在无沙箱状态下运行。Claude 执行器只支持 `sandbox == "workspace-write"`，配成 `read-only` 时 `GeneralAgentConfig` 直接报错。
- **D7 任务开始时的上下文改为文件。** 运行前写 `<任务目录>/inputs/<revision>-context.json`，内容为 `{"snapshot": 冻结上下文, "inputs": 输入文件清单}`，指令里告诉 Claude 去读这个文件。放在 revision 目录之外，是为了不和用户上传的同名文件冲突。
- **D8 输入与输出。** stdin 写一条 stream-json 用户消息，内容和 Codex 的输入一致：任务目标、`visual_evidence` 的 JSON、每张图的标注文本，以及原生 JPEG 图片块。指令用 `--append-system-prompt` 追加，保留 Claude Code 默认的工具使用提示。最终结果用 `--json-schema FINAL_SCHEMA`（复用 `codex.py` 里的常量），把 `structured_output` 交给现有的 `parse_result`。
- **D9 进度与取消。** 从事件流实时整理进度项，类型沿用 Codex 的名称：助手文本块记为 `publicCommentary`（截断到 800 字）；`Bash` 记为 `commandExecution`；`Edit` / `Write` / `NotebookEdit` 记为 `fileChange`（只保留文件名）；`WebSearch` / `WebFetch` 记为 `webSearch`；`mcp__*` 记为 `mcpToolCall`；其他工具不记录。状态先是 `inProgress`，收到对应的 `tool_result` 后改为 `completed` 或 `failed`（看 `is_error`）。取消时：进程组先 SIGTERM，3 秒后 SIGKILL。只有进程确认已退出，才把 `background_terminals_cleaned` 设为真；否则设为假，由现有逻辑标记为 `STOP_UNCONFIRMED`。
- **D10 一次性调用（路由、摘要、Skill 规划、多模态直答）。** 参数：`--tools "" --no-session-persistence --system-prompt <指令> --json-schema '{"type":"object"}'`，cwd 为临时目录。用自己的指令替换掉 Claude Code 的默认系统提示，可以少用额度。模型留空时依次继承任务模型、命令行默认模型；`effort` 用该环节自己的设置。
- **D11 额度用完或出错时，用户听到明确原因。** 现有的 `harness/jobs/faults.py:classify_fault` 不会把异常原文播报给用户，而是按异常上的 `status_code` / `code` 选择固定文案。所以 `ClaudeCliError` 要带上这两个字段，让现有映射直接命中，`faults.py` 不用改：
  - 运行中出现过 `rate_limit_info.status == "rejected"`，或触发了 D4.4 的额外用量拦截 → `code="quota_exceeded"` → 播报“后台服务额度不足，暂时无法继续，请检查服务额度。”
  - D4.3 的非订阅拦截 → `code="authentication_error"` → 播报“后台服务认证失败……请检查服务凭据或登录状态。”
  - 其他错误结果：`status_code` 取 `result.api_error_status`（例如 401、429、5xx），由现有映射处理。
  - 判断错误结果的条件：`is_error` 为真，或 `subtype` 不是 `success`。CLI 的原始错误文本只放进异常消息（写入日志），不播报。
  - 不重试，不切换到 Codex，也不切换到 API。
- **D12 登录在容器里完成。** 运行 `docker compose exec -it web claude auth login`；镜像设 `CLAUDE_CONFIG_DIR=/data/claude`，登录凭据和会话记录保存在 `deploy/web/data/claude/`。Claude Code 版本固定为 `2.1.309`，并设 `DISABLE_AUTOUPDATER=1`。
- **D13 便于合并 main。** 不修改 `harness/agents/codex.py`、`app_server.py`、`harness/llm/backends.py`、`harness/llm/delegate.py`。`demos/server/codex_login.py` 只抽出一个方法。Claude 执行器的结果收尾（媒体重试、写 `result.json`）和 Codex 版本有约 15 行重复；等 main 验证稳定后可以再提取到共用位置，本计划不做。

## Global Constraints

- Claude Code CLI 版本 `2.1.309`；不新增任何 Python 依赖。
- 子进程环境删掉的变量：所有 `ANTHROPIC_*`、所有 `CLAUDE_CODE_USE_*`、`CLAUDE_CODE_OAUTH_TOKEN`。保留其余变量（包括 `HTTPS_PROXY` 等代理变量）。
- 每次启动 `claude` 都必须带：`--setting-sources ""`、`--strict-mcp-config`，并且放在子命令之前；带 `-p` 时还要加 `--output-format stream-json --verbose`。
- 选 Claude 时，不启动任何 `codex` 进程：General、routing、responses、multimodal、Skill 规划都不构造 `CodexPlannerBackend`，General 不使用 `AppServer`。Polish 外层沿用 `CodexDirectAndPolish` 包装：它的 `oralize` 只调用传入后端的 `complete`，和 Codex 无关，Gemini 现在就是这样复用的。
- 不修改 D13 列出的 Codex 文件。
- 测试命令（worktree 根目录）：`python3 -m pytest -q tests --ignore=tests/test_checkpoint.py --ignore=tests/test_model_modes.py`。基线：122 passed（被排除的两个文件需要模型端依赖 `av`）。
- 提交信息沿用仓库风格 `feat(scope): …`，末尾带 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 用户可见文案同时提供中文和英文；服务端返回的提示保持 `English / 中文` 格式。

## Review Focus

1. 容器环境里设置了 `ANTHROPIC_API_KEY`（例如用户为别的用途写进了 `.env`）时，任务仍走订阅；登录检查也用清理后的环境判断，仍显示“已登录”而不是 API key。（Task 2 `test_cli_env_drops_api_billing_sources`；Task 4 `test_claude_login_ignores_api_key_in_environment`）
2. 对话进行中订阅额度用完时，用户听到“额度不足”，而不是笼统的“请调整请求后再试”；不会调用 Codex 或 API，也不会转用按量付费的额外用量。（Task 2 `test_error_result_carries_fault_codes`、`test_overage_is_refused`；Task 3 `test_agent_error_result_fails_run`）
3. 路由模式是 General（Auto 选项隐藏）时把执行器切到 Claude，保存必须成功：页面要同时翻转隐藏的 `routing.provider`。（Task 4 Step 6 浏览器手测）
4. 容器里沙箱起不来（缺 bwrap/socat，或 `/proc` 挂载被拒）时，任务明确报错，不会在无沙箱状态下执行。（Task 5 Step 5 容器实测）
5. 用户在 Claude 跑长命令时取消任务：整个进程组被结束，`cancelled` 事件的 `background_terminals_cleaned` 为真，不留下孤儿进程。（Task 3 `test_cancel_kills_process_group`）

---

### Task 1: 设置模型：执行器开关与按执行器校验

**Files:**
- Modify: `harness/config.py`（`REASONING_EFFORTS`、`validate_model_options`、`ModelCallConfig`）
- Modify: `harness/agents/config.py`（`GeneralAgentConfig`）
- Modify: `harness/settings.py`（`template`、`HarnessSetup.__init__`、`HarnessSetup.check`）
- Modify: `harness/config.json`（样例配置，手动补上 `agent` 和 `claude` 两段，与 `template()` 一致）
- Test: `tests/test_claude_settings.py`（新建）

**Interfaces:**
- Produces:
  - `harness.config.AGENTS = ("codex", "claude")`
  - `harness.config.REASONING_EFFORTS: dict[str, frozenset[str]]`，键为 `"codex"`、`"claude"`
  - `validate_model_options(model, effort, *, provider="codex", allow_inherit=False)`。`provider="gemini"` 时接受两套档位的并集。
  - `ModelCallConfig.provider ∈ {"codex", "claude", "gemini"}`
  - `GeneralAgentConfig.agent: str = "codex"`
  - `HarnessSetup.general.agent` 是“当前执行器”的唯一来源，后续任务都读它。

- [ ] **Step 1: 写失败测试** `tests/test_claude_settings.py`（unittest 风格，与 `test_harness_config.py` 一致）：
  - `test_claude_agent_uses_claude_section`：`template()` 改 `agent="claude"`、`claude={"command":["/opt/claude"],"model":"sonnet","effort":"max"}`，三个环节的 provider 都设为 `"claude"`。断言 `setup.general.agent == "claude"`、`setup.general.command == ("/opt/claude",)`、`setup.general.model == "sonnet"`、`setup.general.effort == "max"`，并且 `setup.general.execution_timeout_s` 仍取自 `codex` 段。
  - `test_role_provider_must_match_agent`：`agent="claude"` 且 `responses.provider="codex"` 时抛 `ValueError`；`agent="codex"` 且 `multimodal.provider="claude"` 时抛 `ValueError`；`agent="claude"` 且 `routing.provider="gemini"`（`gemini.api_key="k"`）时通过。
  - `test_effort_sets_follow_agent`：`agent="claude"`、`claude.effort="minimal"` 时抛 `ValueError`；`agent="codex"`、`codex.effort="max"` 时抛 `ValueError`；`ModelCallConfig(provider="gemini", effort="max")` 通过。
  - `test_claude_agent_rejects_read_only`：`GeneralAgentConfig(agent="claude", command=("claude",), sandbox="read-only")` 抛 `ValueError`。
  - `test_check_names_selected_executable`：`agent="claude"`，并 `patch('shutil.which', return_value=None)`，断言 `check()["problems"]` 包含 `"Claude Code executable not found / 找不到 Claude Code 程序"`。
  - `test_template_keeps_codex_default`：`HarnessSetup(template(), …).general.agent == "codex"`；`"agent" not in template()["codex"]`；`template()["claude"] == {"command": ["claude"], "model": None, "effort": "low"}`。
- [ ] **Step 2:** 运行 `python3 -m pytest -q tests/test_claude_settings.py`，期望全部 FAIL。
- [ ] **Step 3: 实现**，具体按 D1–D3。`claude` 段只允许 `command`、`model`、`effort` 三个键，`command` 必须是列表。三个环节的 provider 校验放在 `HarnessSetup.__init__` 里构造完 `self.general` 之后，错误文案为 `"Role providers must be the task agent or gemini / 各环节只能使用当前任务执行器或 Gemini"`。`check()` 的程序缺失提示按 `general.agent` 选择 `Codex` 或 `Claude Code`。同步更新 `harness/config.json`。
- [ ] **Step 4:** 跑新测试并 PASS，再跑全量测试，与基线相比没有新增失败。
- [ ] **Step 5:** 提交 `feat(settings): choose Codex or Claude Code as the task agent`。

### Task 2: Claude 命令行公共层与一次性调用

**Files:**
- Create: `harness/llm/claude.py`
- Modify: `harness/backends.py`（`model_backend`、`task_backends`）
- Create: `tests/claude_fake.py`（测试用的假 `claude` 程序）
- Test: `tests/test_claude_backend.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `ModelCallConfig.provider == "claude"`、`setup.general.agent`、`setup.general.command`。
- Produces（都在 `harness/llm/claude.py`）：
  - `def cli_env(environ: Mapping[str, str] | None = None) -> dict[str, str]`：返回删掉 D4 所列变量后的环境，默认取 `os.environ`。
  - `ISOLATION_ARGS: tuple[str, ...] = ("--setting-sources", "", "--strict-mcp-config")`
  - `class ClaudeCliError(RuntimeError)`：`__init__(self, message: str, *, code: str = "", status_code: int = 0)`，两个字段的取值见 D11。
  - `class CliRun`：
    - `__init__(self, command: list[str], *, cwd: str)`；`pid` 属性。
    - `async def run(self, stdin: bytes, on_message: Callable[[dict], Awaitable[None]] | None = None) -> dict`：启动进程（`start_new_session=True`、`env=cli_env()`、`limit=16 MiB`），写入并关闭 stdin，逐行解析 JSON，每条都先交给 `on_message`，最后返回 `result.structured_output`。途中按顺序检查：
      1. `system/init` 的 `apiKeySource != "none"` → `close()`，抛 `ClaudeCliError(code="authentication_error")`，文案为 `"Claude Code is not using the subscription login (credential source: {src}); refusing to run / Claude Code 当前未使用订阅登录（凭据来源：{src}），已拒绝运行"`。
      2. `rate_limit_event` 的 `isUsingOverage is True` → `close()`，抛 `ClaudeCliError(code="quota_exceeded")`，文案为 `"Claude subscription usage is exhausted; refusing paid extra usage / Claude 订阅额度已用完，不使用按量付费的额外用量"`。
      3. `rate_limit_event` 的 `status == "rejected"` → 记下，供下面的错误结果使用。
      4. `result` 为错误结果（判断条件和字段取值按 D11）→ 抛 `ClaudeCliError(result 文本或 subtype, code=…, status_code=api_error_status or 0)`。
      5. 成功结果但 `structured_output` 不是 dict → 抛 `ClaudeCliError`。
      6. 进程退出时还没收到过 `result` → 抛 `ClaudeCliError`，带上 stderr 最后 1200 个字符。
    - `async def close(self) -> bool`：先对进程组发 SIGTERM，3 秒后发 SIGKILL，返回进程是否已确认退出。
  - `def model_args(model: str | None, effort: str | None) -> list[str]`
  - `class ClaudeModelBackend`：`provider_name = "claude-code"`；`__init__(self, *, executable: str, model: str | None, effort: str | None, timeout_s: float)`；`model_name` 属性（`model or "claude-configured"`）；`async def generate(self, instructions: str, content: list[dict]) -> str`，返回 `json.dumps(structured_output, ensure_ascii=False)`；`async def complete(self, instructions: str, context: dict) -> str`，把 `harness.llm.backends._planner_request(context)` 作为文本块交给 `generate`。
  - `class ClaudeDirect`：`__init__(self, backend: ClaudeModelBackend)`；`async def execute(self, request, context) -> BackendResponse`，规则与 `CodexDirectAndPolish.execute` 相同（只有音频、没有画面时抛同样的 `ValueError`；每帧按 `frame.mime_type` 作为 base64 图片块；提示词与 Codex 直答相同；解析 `{"speech"}`）；返回 `BackendResponse(speech, "claude-code", backend.model_name, ms)`；`async def aclose(self)`。
- `harness/backends.py`：`model_backend` 在 `call.provider == "claude"` 时返回 `ClaudeModelBackend(executable=setup.general.command[0], model=call.model or setup.general.model, effort=call.effort, timeout_s=call.timeout_s)`。`task_backends` 里，direct 在 provider 为 `"claude"` 时用 `ClaudeDirect`；Skill 规划用 `model_backend(setup, setup.responses if setup.responses.provider == setup.general.agent else ModelCallConfig(provider=setup.general.agent))`；Polish 继续包在 `CodexDirectAndPolish(...)` 里（它的 `oralize` 只调用 `backend.complete`，Gemini 已经这样复用了）。

- [ ] **Step 1: 写假程序** `tests/claude_fake.py`：把 argv 和 stdin 以一行 JSON 追加到环境变量 `FAKE_CLAUDE_LOG` 指定的文件，同时记录收到的环境变量名；然后按 `FAKE_CLAUDE_SCRIPT`（JSON 文件）逐条输出消息。脚本里可以有 `{"sleep": 秒}` 和 `{"write": {"path": 相对 cwd 的路径, "text": …}}` 两种指令项，最后以 `FAKE_CLAUDE_EXIT`（默认 0）退出。测试通过在临时目录写一个以 `#!{sys.executable}` 开头、调用该脚本的可执行文件来使用它。
- [ ] **Step 2: 写失败测试** `tests/test_claude_backend.py`：
  - `test_cli_env_drops_api_billing_sources`：输入含 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`CLAUDE_CODE_USE_BEDROCK`、`CLAUDE_CODE_OAUTH_TOKEN`、`HTTPS_PROXY`、`PATH`，输出只剩 `HTTPS_PROXY` 和 `PATH`。
  - `test_complete_returns_structured_output`：假程序输出 init（`apiKeySource:"none"`）和 result（`structured_output: {"speech": "好"}`），`complete()` 返回的 JSON 解析后等于 `{"speech": "好"}`。日志里的 argv 包含 `-p`、`--tools ""`、`--no-session-persistence`、`--system-prompt <instructions>`、`--setting-sources ""`、`--strict-mcp-config`、`--json-schema '{"type":"object"}'`；进程环境里没有 `ANTHROPIC_API_KEY`（测试时用 `patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"})` 注入）。
  - `test_api_key_source_is_refused`：init 的 `apiKeySource: "ANTHROPIC_API_KEY"`，后面跟一个 `{"sleep": 30}`。`complete()` 在 5 秒内抛 `ClaudeCliError`，消息含 `subscription`，`classify_fault(exc).code == "AUTH_FAILED"`，并且假程序进程已退出。
  - `test_overage_is_refused`：init 正常，然后输出 `{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","isUsingOverage":true}}`，后面跟 `{"sleep": 30}`。5 秒内抛 `ClaudeCliError`，`classify_fault(exc).code == "QUOTA_EXHAUSTED"`，并且进程已退出。
  - `test_error_result_carries_fault_codes`：(a) 先输出 `rate_limit_event`，其 `status:"rejected"`；再输出 result `{"subtype":"success","is_error":true,"api_error_status":429,"result":"Claude usage limit reached"}`。抛出的异常消息含 `usage limit`，`classify_fault(exc).code == "QUOTA_EXHAUSTED"`。(b) 没有 rejected 事件、result 的 `api_error_status: 401`：`classify_fault(exc).code == "AUTH_FAILED"`。
  - `test_direct_sends_frames_as_images`：一帧 JPEG，日志里 stdin 的用户消息有一个 `type == "image"` 的块，其 `media_type == "image/jpeg"`；返回的 `BackendResponse.provider == "claude-code"`。
  - `test_claude_agent_builds_no_codex_backends`：`agent="claude"` 且三个环节都是 `claude` 时，`task_backends(setup)` 返回的对象树里（direct、polish 的 backend、planner 的 `_backend` 与 `_router_backend`）没有任何 `CodexPlannerBackend` 实例，direct 为 `ClaudeDirect`。
- [ ] **Step 3:** 运行，期望 FAIL。
- [ ] **Step 4: 实现** `harness/llm/claude.py` 与 `harness/backends.py` 的改动（按上面的 Interfaces 和 D4、D10、D11）。
- [ ] **Step 5:** 新测试全部 PASS，全量测试相对基线没有新增失败。
- [ ] **Step 6: 真机冒烟（一次性，用本机已登录的 Claude Code，消耗极少额度）。** 在 Python 里调用 `ClaudeModelBackend(executable=<本机 claude 路径>, model="haiku", effort="low", timeout_s=60).complete('Return {"speech":"ok"}.', {})`，期望返回 `{"speech": "ok"}` 一类的 JSON。这一步专门确认 `--json-schema '{"type":"object"}'` 在真实 CLI 上可用；如果不可用，就停下来回报，不改成解析文本。
- [ ] **Step 7:** 提交 `feat(llm): Claude Code model calls on the subscription login`。

### Task 3: Claude 后台任务执行器

**Files:**
- Create: `harness/agents/claude.py`
- Modify: `harness/agents/__init__.py`（导出 `ClaudeAgentProvider`）
- Modify: `harness/factory.py`（按 `setup.general.agent` 选择执行器）
- Modify: `harness/bridge/execution.py:369-370`（`provider` 写成 `f"{agent}_agent"`，`model` 留空时写成 `f"{agent}-configured"`，其中 `agent` 取 `self._general_config.agent`）
- Test: `tests/test_claude_agent.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `CliRun`、`model_args`、`ISOLATION_ARGS`、`ClaudeCliError`；`codex.py` 里的 `CodexAgentProvider`、`_Run`、`FINAL_SCHEMA`、`parse_result`；`media.py` 里的 `MEDIA_INSTRUCTIONS`、`MEDIA_RECOVERY`、`should_recover_media`；`vision.constrain_visual_result`。
- Produces：
  - `class ClaudeAgentProvider(CodexAgentProvider)`，覆盖以下三个方法：
    - `async def _execute(self, run: _Run, root: Path) -> AgentResult`
    - `async def _interrupt(self, run: _Run) -> None`
    - `async def read_progress(self, session_id: str, work_id: str) -> dict`：返回 `{"thread_id", "turn_id", "status": "running", "items": 最近 40 条进度项}`；没有正在运行的任务时抛 `RuntimeError("requested run is not active")`。
  - 模块常量 `INSTRUCTIONS`：内容按 `codex.py` 的 `INSTRUCTIONS` 改写，再拼上 `MEDIA_INSTRUCTIONS`。和 Codex 版本的区别只有三处：上下文来源改为“先读 `inputs/<revision>-context.json`”，不提 `context_fetch`；删掉 `request_user_input` 那句；最终结果改为“按给定 JSON schema 返回”。其余规则原样保留。
  - `run.turn_id` 使用 `request.work_id`；媒体重试那一轮使用 `request.work_id + ":media-recovery"`。

- [ ] **Step 1: 写失败测试** `tests/test_claude_agent.py`（使用 Task 2 的假程序，`GeneralAgentConfig(agent="claude", command=(fake,), workspace=tmp)`）：
  - `test_agent_returns_result_with_artifact`：假程序先写入 `out.txt`，再输出助手文本块、一个 Bash `tool_use` 与对应的 `tool_result`，最后输出 result，其 `structured_output` 为 `{"outcome":"completed","full_result":"done","artifacts":["out.txt"],"assumptions":[],"unresolved":[]}`。断言 `AgentResult.outcome == "completed"`；artifact 已被归档到 `results/<rev>/artifacts/out.txt`；argv 含 `--session-id <uuid>`、`--add-dir <root>`、`--permission-mode acceptEdits`、`--permission-prompts none`、`--json-schema`；`--settings` 解析后 `sandbox.enabled` 和 `sandbox.failIfUnavailable` 为真、`network.allowedDomains == ["*"]`；argv 不含 `--dangerously-skip-permissions`；`inputs/<rev>-context.json` 存在且含 `snapshot` 与 `inputs`。
  - `test_continuation_resumes_same_session`：第二次 `run(parent_work_id=第一次的 work_id)` 时，argv 含 `--resume <第一次的会话 ID>`、不含 `--session-id`，并且两次的 cwd 相同。
  - `test_progress_reports_commentary_and_tools`：假程序在 result 前 `sleep 2`。运行期间调用 `read_progress`，`items` 里有 `{"type":"publicCommentary"}` 和状态为 `completed` 的 `{"type":"commandExecution"}`。
  - `test_agent_error_result_fails_run`：先输出 `status:"rejected"` 的 `rate_limit_event`，再输出 result（`is_error: true`、`"Claude usage limit reached"`）。`run()` 抛出 `ClaudeCliError`，`classify_fault(exc).code == "QUOTA_EXHAUSTED"`，并且后续以这个任务为 parent 的追问会被拒绝（`history.valid` 为假）。
  - `test_cancel_kills_process_group`：假程序先启动一个 `sleep 60` 子进程，把它的 pid 写进日志，然后自己 `sleep 60`。取消 `run()` 的 task 后：子进程和假程序都已不存在；收到的 `cancelled` 事件里 `background_terminals_cleaned is True`、`process_stopped is True`。
  - `test_build_harness_uses_claude_agent`：`agent="claude"` 时，`build_harness(setup)` 传给 `VenusOmniAgentHarness` 的 `general_agent` 是 `ClaudeAgentProvider`（沿用 `test_harness_config.py` 里 patch 构造函数的写法）。
- [ ] **Step 2:** 运行，期望 FAIL。
- [ ] **Step 3: 实现**，按 D5–D9 和上面的 Interfaces：
  1. 先写 context 文件，并按 D6 拼好参数。stdin 用户消息的组成和 `codex.py:_execute` 的 `inputs` 列表一致，图片读取 `run.context.inputs[name]["path"]`，只接受 JPEG。
  2. 运行期间 `history.client = run.client = CliRun(...)`。通过 `CliRun.run` 的 `on_message` 逐条处理消息：收到 init 后发 `started` 事件（字段和 Codex 一致）；每个 `tool_use` 发一次 `tool` 事件；按 D9 维护 `run.observed_items`。
  3. 把 `CliRun.run` 的返回值交给 `parse_result(json.dumps(value), run.context, run.thread_id, run.turn_id)`。
  4. 媒体重试：条件和 Codex 一样（`should_recover_media` 加上 `workspace-write`），用 `--resume`、`MEDIA_RECOVERY` 文本，并关闭网络（`allowedDomains: []` 加 `--disallowedTools WebFetch WebSearch`）。
  5. 收尾：`constrain_visual_result`，写 `results/<rev>/result.json`，发 `completed` 事件，`history.client = None`。
  6. `_interrupt` 调用 `run.client.close()`，再发 `cancelled` 事件：`native_status=None`、`process_stopped=<close 的返回值>`、`background_terminals_cleaned=<同一个值>`、`interrupt_error` 和 `cleanup_error` 为 None，或者在进程没能确认退出时写原因。
- [ ] **Step 4:** 新测试全部 PASS，全量测试相对基线没有新增失败。
- [ ] **Step 5:** 提交 `feat(agents): Claude Code General worker`。

### Task 4: 登录检查与设置页

**Files:**
- Modify: `demos/server/codex_login.py`（把 `MESSAGES` 改为类属性，把 `subprocess.run` 那段抽成 `_state(self, resolved) -> str`；对外行为不变）
- Create: `demos/server/claude_login.py`
- Modify: `demos/server/configuration.py`（按执行器选择检查器；响应字段 `codex_login` 改名为 `agent_login`）
- Modify: `demos/static/index.html`、`demos/static/app.js`、`demos/static/i18n.js`
- Modify: `tests/test_codex_login.py`、`tests/test_gemini_provider.py`、`tests/test_web_configuration.py`、`tests/test_workspace_settings.py`（只改 `codex_login` → `agent_login` 字段名）
- Test: `tests/test_claude_login.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `cli_env`。
- Produces：
  - `class ClaudeLoginCheck(CodexLoginCheck)`，覆盖 `MESSAGES` 和 `_state`。`_state` 运行 `[resolved, *ISOLATION_ARGS, "auth", "status", "--json"]`，参数为 `env=cli_env()`、`timeout=5`、捕获 stdout、丢弃 stderr；结果映射：`loggedIn` 且 `authMethod == "claude.ai"` → `logged_in`；`loggedIn` 但方式不同 → `not_subscription`；未登录 → `not_logged_in`；JSON 无法解析或退出码非 0 → `error`；超时 → `timeout`。
  - 状态 `not_subscription` 的文案：`"Claude Code is logged in without a Claude subscription. In deploy/web run docker compose exec -it web claude auth login and sign in with your Claude account / Claude Code 当前不是订阅登录，请在 deploy/web 目录运行 docker compose exec -it web claude auth login，用 Claude 账号登录"`。
  - 状态 `not_logged_in` 的文案：`"Claude Code is not logged in. In deploy/web run docker compose exec -it web claude auth login, then recheck / Claude Code 未登录，请在 deploy/web 目录运行 docker compose exec -it web claude auth login，然后重新检查"`。
  - `WebConfiguration.check()` / `public()` 返回 `agent_login`（不再返回 `codex_login`）。
  - 页面：新增 `#task-agent` 下拉框（值为 `codex` / `claude`）；登录面板 id 改为 `agent-login-panel`、`agent-login-status`、`agent-login-help`、`recheck-login`；程序路径输入框改为 `#agent-binary`，原来的 `#backend-binary` 删除。

- [ ] **Step 1: 写失败测试** `tests/test_claude_login.py`：
  - `test_claude_login_states`：`patch('demos.server.claude_login.subprocess.run')` 依次返回四种 stdout：`{"loggedIn":true,"authMethod":"claude.ai"}`、`{"loggedIn":true,"authMethod":"api_key"}`、`{"loggedIn":false,"authMethod":"none"}`、`not json`，期望状态依次为 `logged_in`、`not_subscription`、`not_logged_in`、`error`。调用参数为 `[resolved, "--setting-sources", "", "--strict-mcp-config", "auth", "status", "--json"]`，`timeout=5`。
  - `test_claude_login_ignores_api_key_in_environment`：`patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"})` 后，传给 `subprocess.run` 的 `env` 里没有 `ANTHROPIC_API_KEY`。
  - `test_configuration_uses_selected_agent_login`：保存 `agent="claude"` 的设置，`patch` `ClaudeLoginCheck.check` 返回 `not_subscription`；`WebConfiguration.public()` 的 `agent_login.state == "not_subscription"`，`configured` 为假，并且 `CodexLoginCheck.check` 没有被调用。
- [ ] **Step 2:** 运行，期望 FAIL；然后把四个旧测试文件里的 `codex_login` 字段名改为 `agent_login`。
- [ ] **Step 3: 实现服务端。**
- [ ] **Step 4: 实现页面。**
  - `index.html`：在“任务连接”里，于“后端模型”之前加入“任务执行器”下拉框。登录面板和程序路径输入框按上面的 id 改名。三个环节的提供方下拉框中，第一个选项改由脚本填充。
  - `app.js`：
    - 新增 `agentName(agent)`（`codex` → `"Codex"`，`claude` → `"Claude Code"`）和 `ta(key)`（取 `t(key)` 后把 `{agent}` 替换为当前执行器名称）。
    - 切换执行器时：先把当前的模型、强度、程序路径存回 `data[旧执行器]`，再从 `data[新执行器]` 读入；后台任务强度和三个环节强度的可选项改为该执行器的档位，当前值不在新档位里时改为 `"low"`；三个环节提供方的第一个选项改为新执行器，原本等于旧执行器的改为新执行器；刷新登录面板。
    - 保存时：`data.agent`；`data[agent].command` 为 Codex 时是 `[bin, "app-server"]`，为 Claude 时是 `[bin]`；三个环节的 `provider` 无论路由模式如何都写入（`routing` 的其他字段仍只在 Auto 模式下写入）。
    - `settingsIssues` 里的 `/Codex executable/` 改为 `/executable not found/`，对应到 `agent-binary`。
  - `i18n.js`：把下列 key 的 “Codex” 改为 `{agent}` 占位，并改用 `ta()` 渲染：`codexLogin_*`（改名为 `agentLogin_*`）、`settingsSavedLoginNeeded`、`codexRequired`（改名为 `agentRequired`）、`codexAudioHint`（改名为 `agentAudioHint`）、`backendHint`、`modelInheritHint`、`effortDefault`、`codexPath`（改名为 `agentPath`）、`routingHint`、`geminiHint`。新增 key：`taskAgent`（中文“任务执行器”/英文 “Task agent”）、`agentLogin_not_subscription`（中文“不是订阅登录 · 请用 Claude 账号重新登录”/英文 “Not a subscription login · sign in with your Claude account”）、`agentLoginTitle`（“本机的 {agent} 登录状态”/“{agent} login on this computer”）、`agentLoginHelp_codex`（沿用原 `codexLoginHelp` 的文字）、`agentLoginHelp_claude`（中文“请在 deploy/web 目录运行 docker compose exec -it web claude auth login，用 Claude 账号完成登录后点击‘重新检查登录’。”/英文 “In deploy/web, run docker compose exec -it web claude auth login, sign in with your Claude account, then recheck here.”）。删除不再使用的旧 key。
- [ ] **Step 5:** 新测试和全量测试 PASS。
- [ ] **Step 6: 浏览器手测**（本机起 web 服务，可连一个假的模型地址，只看设置页）：
  1. 路由模式为 General 时把执行器切到 Claude Code 并保存，期望成功，而且保存后的 `harness.json` 里三个环节的 provider 都是 `claude`。
  2. 切回 Codex，原来的 Codex 模型、强度、路径都恢复。
  3. 选 Claude 时，强度下拉框没有 `minimal`、有 `max`；登录面板标题和提示都是 Claude Code 的。
  4. 中英文切换后，上面这些文案都正确。
- [ ] **Step 7:** 提交 `feat(web): pick the task agent in Settings and check its login`。

### Task 5: 镜像、文档与容器实测

**Files:**
- Modify: `deploy/web/Dockerfile`
- Modify: `deploy/web/compose.yaml`（只改注释）、`deploy/web/.env.example`（只改注释）
- Modify: `README.md`、`README_ZH.md`（Quick start ②）、`demos/README.md`、`demos/README_ZH.md`（登录、设置表、数据目录、沙箱说明）、`harness/README.md`（执行器表格）

- [ ] **Step 1: 改 Dockerfile。**
  - apt 安装列表加上 `socat`。
  - 新增 `ARG CLAUDE_CODE_VERSION=2.1.309`。以 root 运行 `curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION}`，把 `readlink -f /root/.local/bin/claude` 指向的文件以 755 权限复制到 `/usr/local/bin/claude`，删除 `/root/.local` 下的安装残留，最后执行 `claude --version`。
  - `ENV` 增加 `CLAUDE_CONFIG_DIR=/data/claude DISABLE_AUTOUPDATER=1`。
  - ENTRYPOINT 里的 `mkdir -p` 同时创建 `$CODEX_HOME` 和 `$CLAUDE_CONFIG_DIR`。
- [ ] **Step 2: 改文档。** 每处 Codex 登录说明旁边加上 Claude Code 的做法：`docker compose exec -it web claude auth login`；容器在重启循环中时，改用 `docker compose run --rm -it --entrypoint sh web -c 'mkdir -p "$CLAUDE_CONFIG_DIR" && claude auth login'`。在 Settings 里选择“任务执行器：Claude Code”。数据目录表格新增 `deploy/web/data/claude/`。沙箱一段说明两种执行器都用 bubblewrap，Claude 还需要 socat。说明“只用订阅额度、额度用完任务失败、不会切换到 API”。另外加一行：个人用自己的订阅没问题；如果要把这项功能提供给别人用，先查看 Anthropic 关于订阅登录用于第三方产品的条款。
- [ ] **Step 3: 构建并登录。** 在 worktree 的 `deploy/web` 目录：`cp ../../../voice/deploy/web/.env .env`（如果 main 那边还没有 `.env`，就按 `.env.example` 填写），然后 `docker compose up -d --build`。期望 `docker compose exec web claude --version` 输出 `2.1.309 (Claude Code)`。执行 `docker compose exec -it web claude auth login` 登录，之后 `docker compose exec web claude auth status --text` 显示订阅登录。（注意：这会替换掉 main 的同名容器；数据目录是 worktree 自己的 `deploy/web/data`。）
- [ ] **Step 4: 端到端实测。** 打开 http://localhost:8032，在 Settings 里选 Claude Code，保存后登录面板显示“已登录”。开始对话，下达任务“在任务目录里生成一个写着 hello 的 hello.txt”，期望任务完成、结果被播报、文件出现在 `deploy/web/data/<任务目录>/` 下。再追问一句“把内容改成 hi”，期望复用同一会话（日志里能看到 `--resume`）。
- [ ] **Step 5: 沙箱实测。** 下达任务“在 /data 根目录创建 x.txt”，期望写入被拒绝，任务以 partial 或 failed 报告这个限制，`deploy/web/data/x.txt` 不存在。如果所有 Bash 命令都因 `Can't mount proc` 失败：在 D6 的沙箱设置里加 `"enableWeakerNestedSandbox": true`（理由：外层容器就是隔离边界，Codex 在同一个容器里也依赖放开的 `/proc`），把这条记录到 D6，然后重新执行 Step 4 和 Step 5。
- [ ] **Step 6: 沙箱内联网实测。** 下达任务“用 curl 访问 https://example.com，告诉我 HTTP 状态码”，期望返回 200。沙箱里的命令联网时走 Claude 自己的沙箱代理，不一定会经过容器上的代理，所以需要单独测。如果 Claude 本身能正常对话（说明容器能连到 Anthropic）而这条命令失败，就停下来，把 `docker compose exec web env | grep -i proxy` 的输出和错误一起回报给用户，由用户决定容器的代理怎么配置。
- [ ] **Step 7: 订阅核对。** 执行 `docker compose exec web sh -c 'ANTHROPIC_API_KEY=x claude auth status --json'`，期望显示 `api_key`，以此确认这个变量确实会改变计费方式；同时确认 Settings 里的登录状态仍是“已登录”（因为检查用的是清理后的环境），任务也仍能正常完成。
- [ ] **Step 8:** 提交 `feat(deploy): Claude Code in the web image`。

---

## 合并 main 时的注意点

- main 的工作区目前有未提交的 `demos/README.md`、`demos/README_ZH.md` 改动。Task 5 也会改这两个文件，合并时可能在相同段落冲突，按段落手动合并即可。
- main 上对 `codex.py`、`app_server.py`、`llm/backends.py`、`llm/delegate.py` 的修复会直接合并进来，不会冲突；`ClaudeAgentProvider` 继承的生命周期也会自动得到这些修复。
- 如果 main 修改了 `CodexAgentProvider._execute` 结尾的媒体重试或 `result.json` 写法，需要同步检查 `ClaudeAgentProvider._execute` 里对应的约 15 行（D13）。
