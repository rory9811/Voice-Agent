# 模型服务独立部署（GPU 服务器）+ 其余本地部署 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `demos/model/server.py`（语音模型）单独跑在 GPU 服务器上，网页服务、Harness、Codex 跑在本地电脑（macOS 或 Linux），两者经 SSH 隧道通信。

**Architecture:** 两个进程现在已经通过 HTTP/JSON 通信（`demos/model/client.py` ↔ `demos/model/server.py`），拆分的主体是部署层：启动器、安装器、配置按"模型 / 网页"两个角色拆开。另外要修四个"同机部署时被掩盖、拆开后会出错"的问题：web 端依赖本地权重目录里的 tokenizer；model type 两边各配一份；模型单会话在客户端失联后永久占用；服务器时钟被当成本地时钟用。

**Tech Stack:** Python 3.11–3.12、FastAPI/uvicorn、httpx、transformers 4.51.3（本地只用 tokenizer）、uv、OpenSSH 端口转发。

**Spec:** 本计划的「设计决定」一节（无单独 spec 文档）。

## 现状：拆开后会坏在哪

| # | 现状 | 拆开后的后果 | 位置 |
|---|---|---|---|
| 1 | 一个启动器在同一台机器上拉起 model + web，先查 CUDA 和权重，再查 Codex | 本地没有 GPU 和权重，起不来 | `demos/launcher/cli.py:122-239` |
| 2 | web 进程用 `--tokenizer-path <权重目录>` 加载 tokenizer。用途有两个：开会话时核对 12 个协议 token 的 ID，以及把模型输出的 token ID 解码成文字 | 本地没有权重目录 | `demos/server/resources.py:31-44`、`harness/bridge/host.py:285`、`harness/core/venus.py:215` |
| 3 | model type 在 web 和 model 两边各传一次，服务器不校验客户端声明的模型名（只是原样回显） | 两台机器配置不一致时，UI 模式会和实际模型对不上 | `demos/model/adapter.py:258-300` |
| 4 | 模型只允许 1 个会话。web 进程退出时，supervisor 会连带杀掉 model 进程 | 独立部署后，本地 web 崩溃或断网会让服务器上的会话永远不关，之后每次开会话都返回 409 | `adapter.py:272-273`、`supervisor.py:120-129` |
| 5 | 模型输出里的 `at_ms` 由服务器时钟生成，harness 拿它当截止时间，去筛选本地时钟打戳的音视频 | 两台机器时钟有偏差时，委托任务拿到的画面或音频会丢失或错位（偏差 >5 s 丢画面，≥12 s 快照为空） | `host.py:793,936` → `harness/core/session.py:209` → `harness/core/policy.py:31-50` |
| 6 | 判断进程存活靠读 `/proc` | macOS 上 `--status`/`--stop` 失效 | `demos/launcher/supervisor.py:18-24` |
| 7 | 安装器只允许 Linux，一次装全套 torch，Codex 只下载 Linux 版 | 本地装不上 | `demos/install.py:35-40,103,160,184` |
| 8 | httpx 默认读取环境变量或 macOS 系统代理。本地为了访问 Codex/Gemini 常开代理 | 发往 `127.0.0.1:8031` 的模型请求会被代理接管 | `demos/model/client.py:78` |
| 9 | 界面和文档写"在服务器上登录 Codex / 填服务器上的目录" | 文案和实际部署不符 | `demos/static/i18n.js`、`index.html`、`codex_login.py`、各 README |

## 设计决定

- **D1 连接方式：只走 SSH 隧道。** 模型服务继续只监听服务器的 `127.0.0.1:8031`。本地保持 `ssh -N -L 8031:127.0.0.1:8031 user@server` 运行，本地配置 `model.url = http://127.0.0.1:8031`。原因：模型 API 没有鉴权、只允许一个会话，音频和画面以 base64 明文传输。直接暴露端口需要另做 token + TLS，不在本计划范围内。
- **D2 本地只配一个 URL。** model type 和 tokenizer 都由 web 启动时向模型服务获取：`/healthz` 新增 `model_type`；新增 `GET /tokenizer`，只返回数据文件、不含 `.py`。本地用 `Qwen2TokenizerFast.from_pretrained` 加载，不执行远端代码；两个 checkpoint 的自定义 tokenizer 类只增加了几个属性，`decode` 和 `convert_tokens_to_ids` 与父类完全一致（已在 HF 仓库核对）。服务器在开会话时校验 `model` 名，不一致就拒绝。
- **D3 启动器按角色拆分。** 服务器运行 `bash start.sh model`，本地运行 `bash start.sh web`，各自有独立的状态文件和锁。删除"一条命令起两个进程"；同机部署就在同一台机器上跑这两条命令。
- **D4 配置拆分。** 服务器用新文件 `model_server.json`，本地沿用 `config.json`，但其中 `model` 一节只描述"怎么连模型"。
- **D5 会话租约。** 开会话时，如果已有会话超过 `SESSION_LEASE_S = 15` 秒没收到任何请求，就判定客户端已失联，先关掉旧会话再开新的。这个阈值成立的前提是：会话进行中，web 至少每 ~1 s 拉一次输出（`demos/server/session.py:201-217`）。
- **D6 时钟以本地为准。** host 收到模型输出时用自己的时钟打时间戳；服务器的 `at_ms` 只保留在 DTO 里供诊断。修在 host 这一层，是因为两个时钟只在这里交汇，下游 4 个过滤点（policy / media / buffers / vision）一次就都对了。
- **D7 安装器按角色拆分。** `bash install.sh model`：Linux + CUDA，安装 `requirements-model.txt`，不装 Codex。`bash install.sh web`：macOS 或 Linux，安装 `requirements-web.txt`（不含 torch），Codex 支持 darwin 版本（资产已确认存在）。
- **D8 模型客户端不走代理。** `httpx.AsyncClient(..., trust_env=False)`。

## Global Constraints

- web 端 Python 3.11–3.12（harness 用到 `enum.StrEnum`）；安装器两种角色都用 uv 建 3.11/3.12 的 venv（`runtime/venv`）。
- 两端 `transformers==4.51.3`。
- 模型服务只监听 `127.0.0.1`，不新增鉴权。
- 不保留兼容路径（CLAUDE.md 规则 1）：被替换的 CLI 参数（`--tokenizer-path`、web 的 `--model-type`）、配置键、双进程 supervisor、对应测试在同一任务里删除。
- Codex 版本保持 `0.153.4`，资产名 `codex-{aarch64|x86_64}-apple-darwin.tar.gz` / `codex-{aarch64|x86_64}-unknown-linux-musl.tar.gz`。
- 测试命令（仓库根目录）：`runtime/venv/bin/python -m unittest discover -s tests -v`；单文件加 `-p '<file>.py'`。

## Review Focus

1. 本地启动 web 时隧道没建或模型服务还在加载 → `start.sh web` 直接失败，并打印要运行的 ssh 命令。不能等起来后才在每次会话里报"请检查服务端日志"。（Task 6 测试 `test_web_role_fails_fast_without_model`）
2. 本地 web 被 kill -9 或断网后重启 → 15 s 内重开返回 busy，超过 15 s 能正常开新会话。（Task 3 测试 `test_stale_session_is_replaced`、`test_live_session_stays_busy`）
3. 服务器从 omni 换成 audio，本地 web 没重启 → 开会话明确报模型不一致，不会出现半可用状态。（Task 3 `test_open_rejects_other_model`）
4. 本地设置了 `ALL_PROXY` 或系统代理 → 模型请求仍然直连隧道。（Task 5 `test_model_client_ignores_proxy_env`）
5. 同一台机器同时跑两个角色 → 状态文件、锁、端口检查互不冲突；web 角色不检查 8031 是否空闲，因为它被隧道或模型占用。（Task 6 `test_roles_use_separate_state`、`test_web_role_does_not_bind_model_port`）

---

### Task 1: 依赖与安装器按角色拆分

**Files:**
- Create: `demos/requirements-model.txt`、`demos/requirements-web.txt`
- Delete: `demos/requirements.txt`
- Modify: `requirements.txt`（改为 `-r demos/requirements-model.txt`，它是推理示例的依赖入口）、`demos/install.py`
- Test: `tests/test_install_roles.py`

**Interfaces:**
- Produces: `python -m demos.install {model|web} [--python X]`（`install.sh` 原样透传 `"$@"`，无需改动）；`codex_asset(system: str, machine: str) -> str`

- [ ] **Step 1: 记录基线。** 在本机用现有安装器装不了（只允许 Linux），所以先完成本任务再跑全量测试，记下当前失败数，作为之后对比的基线。
- [ ] **Step 2: 写失败测试** `test_codex_asset`：`("Darwin","arm64")→"codex-aarch64-apple-darwin"`、`("Darwin","x86_64")→"codex-x86_64-apple-darwin"`、`("Linux","x86_64")→"codex-x86_64-unknown-linux-musl"`、`("Linux","aarch64")→"codex-aarch64-unknown-linux-musl"`，`("Windows","AMD64")` 抛 `RuntimeError`。
- [ ] **Step 3: 写依赖文件。**
  - `requirements-model.txt` = 现有 `demos/requirements.txt` 的模型段（minicpmo-utils[tts]、torch/torchaudio/torchvision、transformers、accelerate、numpy、sentencepiece、safetensors、resampy、setuptools）+ `Pillow>=10.4,<12`（`wire.decode_frames` 用到）+ `fastapi>=0.115,<1` + `uvicorn[standard]>=0.30,<1`。不含 harness 和 httpx。
  - `requirements-web.txt` = `-r ../harness/requirements.txt` + `transformers==4.51.3` + `httpx[socks]>=0.28,<0.29` + `fastapi` + `uvicorn[standard]`（版本范围同上）。不含 torch。
- [ ] **Step 4: 改 `install.py`。** 新增位置参数 `role`。平台限制：model 只允许 Linux，web 允许 Linux 和 Darwin。只有 web 角色调用 `install_codex()`，资产名改用 `codex_asset(platform.system(), platform.machine())`。按角色安装对应的 requirements 文件。导入自检：model 角色 `import demos.model.server, torch, torchaudio, transformers, stepaudio2, librosa, PIL`，并打印 CUDA 可用性；web 角色 `import demos.server.app, transformers`。结尾提示按角色给出下一步命令。
- [ ] **Step 5: 在本机验证。** 运行 `bash install.sh web`，期望输出含 `Application imports: OK`；再运行 `runtime/venv/bin/python -c "import torch"`，期望 `ModuleNotFoundError`。
- [ ] **Step 6:** 运行 `test_install_roles.py`，期望 PASS。提交 `feat(install): split model and web installer roles`。

### Task 2: 进程身份跨平台

**Files:** Modify `demos/launcher/supervisor.py:18-24`；Test `tests/test_supervisor.py`

**Interfaces:** Produces `process_start(pid: int) -> str | None`（签名不变）

- [ ] **Step 1: 写失败测试。** `test_process_start_is_stable`：对 `os.getpid()` 连续两次调用，结果非空且相等。`test_reaped_child_has_no_start`：启动 `sys.executable -c pass` 并 `wait()` 后，对其 pid 调用返回 `None`。
- [ ] **Step 2:** 在 macOS 上运行，期望第一个测试 FAIL（`/proc` 不存在，返回 None）。
- [ ] **Step 3: 实现。** 改用 `ps -o stat=,lstart= -p <pid>`：输出为空，或 stat 以 `Z` 开头，返回 `None`；否则返回 lstart 字符串。删除 `/proc` 实现。
- [ ] **Step 4:** 测试 PASS，提交 `fix(launcher): portable process identity via ps`。

### Task 3: 模型服务自描述、下发分词器、会话校验与租约

**Files:** Modify `demos/model/server.py`、`demos/model/adapter.py`；Test `tests/test_model_server.py`

**Interfaces:**
- Produces（HTTP，供 Task 5/6 使用）：
  - `GET /healthz` → 就绪时 `{"ready": true, "model_type": "omni"|"audio", "model": "Realtime-Venus-Omni"|"Realtime-Venus-Audio", ...原有字段}`
  - `GET /tokenizer` → `{"files": {"<name>": "<utf-8 text>"}}`。只包含 `TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "added_tokens.json", "vocab.json", "merges.txt")` 中在 `model_path` 下实际存在的文件
  - `POST /sessions`：`model` 与服务端不一致 → 400 `{"type": "AdapterError"}`；已有活会话 → 409（不变）
- Produces（Python）：`adapter.SESSION_LEASE_S = 15.0`；`_Session.last_seen: float`（`time.monotonic()`），每次 `_require_by_id` 时刷新；`RealtimeVenusOmniAdapter._close_locked(sess, reason: str) -> dict`（原 `close_session` 在 `_open_lock` 内的主体，`close_session` 改为加锁后调用它）

- [ ] **Step 1: 写失败测试。** fake model 用 `MagicMock()`；按 `decoding.configure_length_penalty` 的分支，length_penalty=1.0 时 MagicMock 会走 `"unchanged"`，不需要额外桩。
  - `test_open_rejects_other_model`：adapter `model_name="Realtime-Venus-Omni"`，以 `model="Realtime-Venus-Audio"` 开会话，抛 `AdapterError`。
  - `test_live_session_stays_busy`：开 A，立即开 B，抛 `SessionBusy`。
  - `test_stale_session_is_replaced`：开 A，把 `A.last_seen` 减去 20，开 B 成功；断言 `A.closed is True`，`adapter._session.session_id == "B"`，B 的 `incarnation == A.incarnation + 1`。
  - `test_tokenizer_endpoint_serves_only_data_files`：临时目录放 `tokenizer.json`、`tokenizer_config.json`、`vocab.json`、`tokenization_x_fast.py`、`model.safetensors`；用 `patch("demos.model.server._build_adapter", AsyncMock(return_value=stub))` 建 `create_app(_ServerConfig(model_path=tmp, model_type="audio"))`。`GET /tokenizer` 返回的 `files` 键恰好是三个数据文件；`GET /healthz` 里 `model_type == "audio"`。
- [ ] **Step 2:** 运行，期望 4 个 FAIL。
- [ ] **Step 3: 实现。** 先改 adapter：在 `open_session` 的 `_open_lock` 内，先校验 model 名；如果旧会话未关且 `time.monotonic() - last_seen > SESSION_LEASE_S`，调用 `_close_locked(old, "lease_expired")`，否则抛 `SessionBusy`。再改 server：`health()` 合并 `config.model_type` 和 `config.model_name`；新增 `/tokenizer` 路由，用 `Path.read_text(encoding="utf-8")` 读文件。
- [ ] **Step 4:** 运行 `test_model_server.py` 和 `test_model_modes.py`，全部 PASS。提交 `feat(model): self-describing server, tokenizer download, session lease`。

### Task 4: host 用本地时钟标记模型输出

**Files:** Modify `harness/bridge/host.py:793,936`；Test `tests/test_host_clock.py`

**Interfaces:** 无对外变化。`ModelOutputStep.at_ms` 仍做协议校验，但不再传给 harness session。

- [ ] **Step 1: 写失败测试** `test_output_uses_local_clock`：直接构造 `VenusOmniServingHost(session=fake, serving=fake, playback=fake, opened=SessionOpened(...), clock=lambda: 5_000)`。`fake.consume_total_ids` 和 `fake.finish_model_turn` 为 `AsyncMock`。喂一个 `at_ms=10**12`、`turn_finished=True`、带非空 `total_token_ids` 的 `ModelOutputStep`（走 `process_output`），断言两者收到的 `at_ms == 5_000`。
- [ ] **Step 2:** 运行，期望 FAIL（收到 `10**12`）。
- [ ] **Step 3: 实现。** 两处 `at_ms=output.at_ms` 改为 `at_ms=self._clock()`。
- [ ] **Step 4:** 运行 PASS，再跑全量测试，与基线对比无新增失败。提交 `fix(host): stamp model output with the local clock`。

### Task 5: web 端从模型服务获取模型类型与分词器

**Files:**
- Modify: `demos/server/resources.py`（删除 `load_tokenizer`）、`demos/server/cli.py`、`demos/server/app.py`、`demos/model/client.py:78`
- Modify tests: `tests/test_codex_login.py`、`test_web_configuration.py`、`test_workspace_settings.py`、`test_model_modes.py` 中的 `create_app(..., tokenizer_path='/unused')` 改为 `create_app(..., model_type=..., tokenizer=None, model_server_url="http://unused")`
- Test: `tests/test_fetch_model.py`

**Interfaces:**
- Consumes: Task 3 的 `/healthz`、`/tokenizer`
- Produces:
  - `resources.ModelInfo(model_type: str, tokenizer: Any)`（frozen dataclass）
  - `resources.fetch_model(server_url: str, timeout_s: float = 30.0) -> ModelInfo`：同步调用，使用 `httpx.Client(trust_env=False)`；把文件写进 `TemporaryDirectory` 后用 `Qwen2TokenizerFast.from_pretrained(dir)` 加载。连接失败或 `/healthz` 未就绪时抛 `RuntimeError`，消息包含 `ssh -N -L {port}:127.0.0.1:{port} user@server` 和 `bash start.sh model`（port 取自 URL）
  - `create_app(*, settings_path, model_type, tokenizer, model_server_url, demo_path=None)`
  - `real_resources(*, mode, settings_path, server_url, tokenizer, model_type, demo_path=None)`
  - `python -m demos.server.cli --model-server URL --config HARNESS [--demo-config P] [--host H] [--port N]`：启动时调用 `fetch_model`，失败就以退出码 1 退出并打印该消息。删除 `--tokenizer-path` 和 `--model-type`

- [ ] **Step 1: 写失败测试。**
  - `test_fetch_model_reads_type_and_files`：`httpx.MockTransport` 让 `/healthz` 返回 `{"ready":true,"model_type":"audio"}`，`/tokenizer` 返回两个文件；patch `Qwen2TokenizerFast.from_pretrained`，断言收到的目录里恰好有这两个文件，返回值 `model_type == "audio"`。
  - `test_fetch_model_unreachable_names_tunnel`：transport 抛 `httpx.ConnectError`，期望 `RuntimeError`，消息含 `ssh -N -L 8031:127.0.0.1:8031`。
  - `test_model_client_ignores_proxy_env`：在 `ALL_PROXY=socks5://127.0.0.1:1` 环境下构造 `RemoteOmniServingPort("http://127.0.0.1:8031")`，断言 `port._client._trust_env is False`。
- [ ] **Step 2:** 运行，期望 FAIL。
- [ ] **Step 3: 实现上述接口。** `WebAgentSession` 的 factory 改为 partial 绑定 `tokenizer=`。
- [ ] **Step 4: 真实 tokenizer 等价性核对（需联网，一次性）。** 用 `huggingface_hub.hf_hub_download` 把 `Realtime-Venus-Omni/` 下的 6 个数据文件和 `tokenization_realtime_venus_omni_fast.py` 下到临时目录。分别用 `Qwen2TokenizerFast.from_pretrained(d)` 和 `AutoTokenizer.from_pretrained(d, trust_remote_code=True)` 加载，断言两者的 `venus_special_token_ids` 相等，且对 `encode("你好<delegate>查天气</delegate><|turn_eos|>")` 的 `decode(ids, skip_special_tokens=False)` 结果相等。Audio 目录（`tokenization_minicpmo_fast.py`）同样做一遍。
- [ ] **Step 5:** 跑全量测试，与基线对比无新增失败。提交 `feat(web): fetch model type and tokenizer from the model server`。

### Task 6: 启动器按角色拆分 + 配置拆分

**Files:**
- Modify: `demos/launcher/options.py`、`cli.py`、`config.py`、`supervisor.py`、`demos/deployment.py`、`config.json`
- Create: `model_server.json`
- Tests: 重写 `tests/test_deployment_config.py` 的 `DeploymentConfigTests` 和 `tests/test_model_modes.py::test_launcher_forwards_variant_to_both_services`（改为 `test_model_role_forwards_variant`）

**Interfaces:**
- Consumes: Task 2 `process_start`；Task 3 `/healthz`；Task 5 的 `demos.server.cli` 参数
- Produces:
  - `python -m demos model [--config P] [--type audio|omni|video] [--model-path] [--ref-audio] [--port N] [--memory-minutes N] [--startup-timeout S] [--check|--detach|--stop|--status]`，默认配置 `<root>/model_server.json`
  - `python -m demos web [--config P] [--model-url URL] [--harness-config P] [--host H] [--port N] [--startup-timeout S] [--no-login] [--check|--detach|--stop|--status]`，默认配置 `<root>/config.json`
  - `model_server.json`：`{"type":"video","path":"Realtime-Venus-Omni","reference_audio":"","port":8031,"memory_minutes":40,"startup_timeout":600}`
  - `config.json`：`{"model":{"url":"http://127.0.0.1:8031","length_penalty":0.8,"timeout_s":180},"harness":{"config":"harness/config.json"},"web":{"host":"127.0.0.1","port":8032},"startup_timeout":600}`
  - `options.load_model_options(path) -> dict`（键：type/path/reference_audio/port/memory_minutes/startup_timeout）；`options.load_web_options(path) -> dict`（展平为 model_url/length_penalty/model_timeout_s/harness_config/web_host/web_port/startup_timeout）。未知键、缺 `harness.config`、`model.url` 不是 http(s) 时都抛 `ValueError`。删除 `load_deployment`、扁平格式支持和 `runtime/harness.json` 回退（见「待你确认」）
  - 环境变量覆盖。model 角色：`VENUS_MODEL_TYPE`、`{TYPE}_MODEL_PATH`/`MODEL_PATH`、`REF_AUDIO`、`VENUS_MODEL_PORT`、`VENUS_MEMORY_MINUTES`、`VENUS_STARTUP_TIMEOUT`。web 角色：`VENUS_MODEL_URL`、`HARNESS_CONFIG`、`VENUS_WEB_HOST`、`VENUS_WEB_PORT`、`VENUS_STARTUP_TIMEOUT`。优先级：命令行 → 环境变量 → 配置 → 默认
  - `config.ModelLaunch` / `config.WebLaunch`（frozen dataclass，替代 `LaunchConfig`）
  - `supervisor.StackSupervisor(runtime: Path, name: str, command: list[str], health_url: str, startup_timeout: float, state: dict)`：只管一个子进程；状态 `runtime/{name}.json`，锁 `runtime/{name}.lock`，日志 `runtime/logs/{name}.log`。`OMP_NUM_THREADS` 等环境变量的默认值保留
  - `deployment.frontend_settings` 改用 `load_web_options`；`updated_frontend_document` 仍写 `data["model"]["length_penalty"]`
- 行为：
  - model 角色：选 checkpoint → 校验参考音频 → 检查 `127.0.0.1:port` 是否可绑定 → CUDA 检查 → `--check` 到此结束 → 启动 `demos.model.server --host 127.0.0.1` → 等 `/healthz` 就绪 → 打印本地要运行的 `ssh -N -L {port}:127.0.0.1:{port} {user}@{server}`
  - web 角色：只检查 web 端口是否可绑定 → `setup_backend`（Codex 登录）→ 用 `supervisor.healthy("{model_url}/healthz")` 探测模型，失败则报错退出（消息同 Task 5）→ `--check` 到此结束 → 启动 `demos.server.cli` → 等 `/health` → 打印 `Open http://localhost:{port}`

- [ ] **Step 1: 写失败测试**（全部用 `patch` 隔离子进程、端口和 Codex）：
  - `test_model_role_forwards_variant`：audio/omni 两种情况下，supervisor 收到的 command 含 `demos.model.server`、`--model-type <mode>`、`--host 127.0.0.1`。
  - `test_web_role_command`：command 含 `--model-server http://127.0.0.1:8031`、`--config <harness>`、`--demo-config <config.json>`，不含 `--tokenizer-path` 和 `--model-type`。
  - `test_web_role_fails_fast_without_model`：`healthy` 返回 False 时，`main(['web'])` 以 `SystemExit(1)` 退出，stderr 含 `ssh -N -L`。
  - `test_web_role_does_not_bind_model_port`：`ensure_ports_available` 只收到 `(8032,)`。
  - `test_roles_use_separate_state`：两个角色的 supervisor 分别写 `runtime/model.json` 和 `runtime/web.json`。
  - `test_detach_argv_round_trip`（每个角色各一次）：detach 时冻结的 argv 在另一个 cwd 下重新解析，得到相同的路径。
  - `test_old_formats_rejected`：`load_web_options` 对 `{"model_type":"audio"}` 和 `{"model":{"path":"w"},"harness":{"config":"h.json"}}` 抛 `ValueError`。
  - 保留并改写：`test_missing_invalid_unknown_and_wrong_types_fail`、`test_harness_reference_must_exist`、`test_shell_preserves_callers_directory`（改为 `start.sh web --config ...`）。
- [ ] **Step 2:** 运行，期望 FAIL。
- [ ] **Step 3: 实现上述接口**，同时删除双子进程流程和 `ensure_ports_available` 里的"两端口必须不同"分支。
- [ ] **Step 4: 本机冒烟测试（无 GPU）。** `bash start.sh web --check --no-login` 在隧道未建时报错并给出 ssh 命令；`bash start.sh web --status` 与 `bash start.sh model --status` 都输出 `"alive": false`。
- [ ] **Step 5:** 全量测试与基线对比无新增失败。提交 `feat(launcher): separate model and web roles`。

### Task 7: 文案与文档

**Files:** `demos/server/codex_login.py`（MESSAGES）、`demos/static/i18n.js`（en 19-40,108,110,131,138,163；zh 219-240,308,310,331,338）、`demos/static/index.html`（393,395,402,457,468,554）、`README.md` / `README_ZH.md`（快速开始 §1 安装、§3 实时对话）、`demos/README.md` / `README_ZH.md`（准备、启动、在本地电脑打开、配置、服务管理、日志路径）、`harness/README.md` / `README_ZH.md`（第 120 行附近 Codex 登录位置）

- [ ] **Step 1: 文案规则。** 凡指 Codex 登录、任务目录、Gemini 环境变量的"服务器"，一律改为"本机"。`network` 提示改为"连接中断。请确认本地网页服务仍在运行，以及到模型服务器的 SSH 隧道仍然连通。"（英文对应翻译）。`effortDefault` 改为"使用 Codex 默认值"。
- [ ] **Step 2: 文档结构。** 部署分三步：① 服务器 `bash install.sh model` → 填 `model_server.json` → `bash start.sh model --detach`；② 本地 `ssh -N -L 8031:127.0.0.1:8031 user@server`；③ 本地 `bash install.sh web` → 填 `config.json` → `bash start.sh web` → 打开 `http://localhost:8032`。删除"网页端口 SSH 转发"的旧段落。补充「已知限制」（见下文）和「同机部署 = 两条命令」。
- [ ] **Step 3: 验证。** `grep -n "on the server\|服务器上" demos/static demos/server` 只剩与模型服务相关的行。提交 `docs: document split model/web deployment`。

### Task 8: 端到端验证（需要 GPU 服务器，手动）

- [ ] 服务器：`bash install.sh model`；`bash start.sh model --detach`；`curl -s 127.0.0.1:8031/healthz` 返回 `"ready": true` 和正确的 `model_type`。
- [ ] 本地：建隧道；`curl -s -o /dev/null -w '%{time_total}\n' 127.0.0.1:8031/healthz` 记录 RTT（< 0.15 s 体验正常）。
- [ ] 本地：`bash install.sh web`；`bash start.sh web`；浏览器完成一轮语音对话，并发起一个委托任务，确认结果被播报。
- [ ] 对话中对本地 web 进程 `kill -9`，然后重启：15 s 内开会话提示 busy，15 s 后能正常开始。
- [ ] 服务器切换为 audio 并重启模型、本地不重启：开会话时报模型不一致；重启本地 web 后 UI 切换为麦克风模式。
- [ ] 本地开启系统代理后重复一轮对话，仍然正常。

## 已知限制（写进文档，不在本计划内解决）

- **往返延迟上限。** host 在持有 `_owner_lock` 时依次发出音频、画面、播放确认的请求，每秒约 3 次往返，外加 1 次输出拉取，而模型每一步生成都要等一次拉取请求。RTT ≲ 100 ms 时体验与同机部署接近；> 200 ms 时回复会变慢，可能出现积压。升级方案：给 `VenusOmniServingPort` 实现一个 WebSocket 或流式传输。
- **结果播报的 2.5 s 反应窗口**（`result_management.frontend_reaction_timeout_ms`）需要覆盖"prefill 往返 + 一次生成 + 一次拉取"，RTT 越高余量越小。

## 待你确认

1. **旧配置格式直接删除？**（CLAUDE.md 规则 1：已发布的数据格式需你决定）计划默认删除扁平格式的 `config.json` 支持和 `runtime/harness.json` 默认回退，已有用户需要按新格式重写 `config.json`，并新建 `model_server.json`。
2. 删除"一条命令同机起两个进程"，同机部署改为两条命令（计划默认如此）。
