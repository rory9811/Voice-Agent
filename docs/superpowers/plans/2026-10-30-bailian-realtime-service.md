# 百炼实时语音独立服务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先把 Venus 仓库的前端改成 MiniCPM-o demo 的设计，只保留视频全双工和音频双工；再从它复制出一份独立代码做成新服务，前台语音模型改为调用百炼 `qwen3.8-omni-flash-realtime`，后台任务系统照旧。

**Architecture:** 前端在 Venus 仓库改一次（Task 0），新服务分叉后直接继承，两份前端只靠 `/api/status` 的 `input_modes` 决定显示哪些模式。新服务不再有 GPU 模型服务器：浏览器仍连本机 Web 后端，Web 后端用一条 WebSocket 直连百炼。host 与模型之间的接口从“token 流”改为“语义事件”（说了什么文字、提交了什么任务、这段音频、用户开口了），token 解析、`<delegate>` 标签和 `<backend>` 注入在新服务里全部删除。

**Tech Stack:** Python 3.11、FastAPI、`websockets`（已在 `demos/constraints-web.txt`）、百炼 Realtime WebSocket API、原生 JS 前端。

**Spec:** 本计划无单独设计文档，决策见下方“决策记录”。接口依据：[Omni Realtime 文档](https://help.aliyun.com/zh/model-studio/realtime)、[客户端事件](https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events)、[服务端事件](https://help.aliyun.com/zh/model-studio/qwen-audio-realtime-server-events)。

## 决策记录

用户决定：
- 两份代码分开，每份代码是一个独立服务；切换模型不涉及权重。Venus 的模型和后端逻辑不动，只改前端及其配套接口。
- 两份代码的前端都参考 [MiniCPM-o-Demo](https://github.com/OpenBMB/MiniCPM-o-Demo) 的对应前端设计，最多只保留视频全双工和音频双工。
- 需要摄像头，因此不用文档未提及图像输入的 Qwen-Audio 系列，改用 Omni Realtime 系列。
- 语音可以上传阿里云；暂无 API Key，先用假服务端开发，拿到 Key 后实测。

实现方（Claude）的决定，可在审阅时推翻：
- 新仓库位置 `/Users/ray/Desktop/voice-bailian`，用 `git clone` 保留共同历史，后台任务系统的修复可以在两个仓库间 `cherry-pick`。
- 新服务从 Task 0 的 `duplex-frontend` 分支分叉，因此带上前端改动和 Venus 工作区里的诊断日志（`deploy/model/replay` 随模型服务器在 Task 4 删除）。
- `VenusOmni*` 等类名和 `harness/` 目录结构不重命名，只改语义变了的部分，让 `cherry-pick` 少冲突；页面文案改名。
- “两个功能”指交互模式：删除文件上传、半双工、轮次对话等其他模式；后台任务托盘和设置面板是委托能力的组成部分，保留。
- Venus 同一时间只部署一种权重，另一个模式的标签置灰；百炼两个模式都可用，音频双工只是不发画面。
- demo 仓库没有 LICENSE 文件，只参考布局和交互，不复制它的代码和资源。
- 被打断的进度和最终结果都不自动重念：系统分不清插话是不想听还是临时有事，贸然重念比少念一次更打扰；最终结果留在任务托盘。第一版不做“告诉模型用户没听完”。
- 断线不续接：百炼文档没有续接机制，断线即结束会话并提示。
- API Key 只放环境变量，不进 `harness.json`，也不下发给浏览器。

## Global Constraints

- 新仓库 Python `>=3.11`，不新增第三方依赖；只用已有的 `websockets==17.3`。
- 不写向后兼容：被替换的旧结构在同一任务内删除，不留开关、别名或旧测试。
- 环境变量：`DASHSCOPE_API_KEY`（必填）、`BAILIAN_REALTIME_URL`（必填，形如 `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime`）、`BAILIAN_MODEL`（默认 `qwen3.8-omni-flash-realtime`）、`BAILIAN_VOICE`（默认 `Tina`）。
- 音频：输入 16kHz/16bit/单声道 PCM，每次 `input_audio_buffer.append` 3200 字节（100 ms）；输出 24kHz/16bit/单声道 PCM。
- 画面：JPEG，原图不超过 190 KB，约每秒一帧，必须在第一段音频之后才发送。
- 不在任何日志中输出 API Key、音频内容或任务内容。
- Venus 仓库的提交只落在 `duplex-frontend` 分支，不动 `main`；不推送任何仓库。

## Review Focus

1. 用户打断正在播放的后台结果：已播部分保留，结果留在任务托盘，会话继续（测试见 Task 2）。
2. 打断后浏览器晚到的播放确认：静默忽略，不弹错误（测试见 Task 2）。
3. 回复已生成完但还在播放时用户开口：浏览器立刻停播，且不向百炼发 `response.cancel`（测试见 Task 2、Task 3）。
4. 后台结果回来时用户正在说话：等用户说完、模型空闲后才注入（测试见 Task 3）。
5. 云端断线或到达 120 分钟上限：页面显示明确原因，不会卡住（测试见 Task 3、Task 4）。

---

### Task 0: Venus 仓库前端改版（在 `/Users/ray/Desktop/voice` 执行）

**Files:**
- Modify: `demos/static/index.html`、`demos/static/style.css`、`demos/static/app.js`、`demos/static/i18n.js`、`demos/static/tasks.js`、`demos/server/app.py`、`demos/server/session.py`、`demos/server/resources.py`
- Delete: `demos/static/presence.js`、`demos/static/theme.js`、`demos/static/fonts/`、`demos/static/venus-logo*.gif`、`demos/server/media.py`
- Test: `tests/test_model_modes.py`（删除文件上传用例，改为下面的用例）

**Interfaces:**
- Produces（两份代码共用，Task 4 只改 `input_modes` 的取值）：
  - `GET /api/status` 返回 `model`、`input_modes`、`configured`、`busy`；`input_modes` 是 `["camera","audio"]` 的子集。删除 `model_type`、`upload_limit_mb`。前端只读 `input_modes`。
  - `WS /ws?mode=camera|audio`：`mode` 不在 `input_modes` 中时回 `fatal_error`，`code="input_mode"`。删除 `source` 参数。
  - 删除 `POST /api/sessions/{sid}/video`、`POST /api/sessions/{sid}/audio`。`upload_token` 保留，它还保护任务产物下载。

页面只有一个 `index.html`，用标签切换模式，避免维护两份几乎相同的页面：

| 区域 | 内容 | 对应现有代码 |
|---|---|---|
| 顶栏（白底） | 模型名、“视频全双工 / 音频双工”标签、状态徽章；右侧任务数、设置、语言 | 现有 `connection-state`、`tasks-toggle`、`settings-button`、`language-button` |
| 主区左列（占 1/3） | 媒体卡片（黑底，带 LIVE 灯和计时）：视频模式显示摄像头，音频模式显示波形；视频模式下方另有麦克风波形条；设置卡片（麦克风、摄像头、Venus 的回复长度）；状态卡片 | `preview-video`、`waveform.js`、`microphone-device`、`camera-device`、`length-penalty`、`control-hint` |
| 主区右列（占 2/3） | 对话记录卡片（可保存、可清空）；任务卡片 | `transcript`、`save-transcript`、`reset-button`、`tasks.js` |
| 底部控制条（通栏） | 开始、停止、麦克风静音、输出静音 | `start-button`、`stop-button`、`mute-mic`、`mute-output` |

视觉取自 demo 的 `duplex-shared.css`：背景 `#f5f5f0`，卡片白底圆角 `10px`，间距 `12px`，系统字体；主按钮 `#2d2d2d`，直播中 `#16a34a`，停止 `#cf222e`；宽度不超过 `768px` 时单列。只有浅色主题。设置面板只换样式，字段不变。

不做：文件模式、混音、录制与分享、系统提示词和参考音频编辑、指标面板、Force Listen、全屏字幕、镜像与翻转、扬声器选择、FAQ、移动端专页、管理页。

- [ ] **Step 1: 建分支并单独提交现有诊断改动**

```bash
git switch -c duplex-frontend
git add demos/model/adapter.py demos/model/server.py demos/server/cli.py demos/server/session.py demos/static/app.js tests/test_model_driver.py tests/test_web_link.py deploy/model/replay tests/test_replay_delegate.py docs/superpowers/plans/2026-10-30-bailian-realtime-service.md
git commit -m "chore: keep delegation diagnostics and the Bailian plan"
```

Expected: `git status --short` 无输出。此步需要用户先同意提交这些未提交改动。

- [ ] **Step 2: 写失败的测试**

```python
def test_status_lists_only_the_deployed_mode(self):
    self.assertEqual(status("omni")["input_modes"], ["camera"])
    self.assertEqual(status("audio")["input_modes"], ["audio"])
    self.assertNotIn("model_type", status("omni"))

def test_socket_rejects_a_mode_not_deployed(self):
    self.assertEqual(fatal(model_type="omni", mode="audio")["code"], "input_mode")

def test_upload_routes_are_gone(self):
    self.assertEqual(client.post("/api/sessions/x/video").status_code, 404)
    self.assertEqual(client.post("/api/sessions/x/audio").status_code, 404)
```

- [ ] **Step 3: 确认测试失败**

Run: `python -m pytest tests/test_model_modes.py -q`
Expected: FAIL

- [ ] **Step 4: 改后端**

`session.py` 删除 `input_source`、`upload_*`、`_upload_media`、`_feed_video`、`_video_idle`，`mode` 取值改为 `camera|audio`；`app.py` 按 Interfaces 修改，Venus 的 `input_modes` 由 `model_type` 得出（`omni → ["camera"]`，`audio → ["audio"]`）；`media.py` 整个删除，只为它服务的依赖一并删除（先 `grep` 确认）。

- [ ] **Step 5: 确认测试通过**

Run: `python -m pytest tests -q`
Expected: PASS

- [ ] **Step 6: 重写页面**

按上表改 `index.html`、`style.css`，从 `app.js`、`i18n.js` 删除文件模式、`presence`、主题、历史对话框的代码和文案。`state.mode` 只取 `camera|audio`，初始值取 `input_modes[0]`；不在 `input_modes` 中的标签置灰并提示“当前部署不支持”；会话进行中不能切换标签。

- [ ] **Step 7: 在浏览器验证**

用桩 tokenizer 直接启动 `create_app`（`model_type` 分别取 `omni`、`audio`，`model_server_url` 指向不可达地址），在内置浏览器打开。
Expected:
- 只有两个模式标签，部署之外的那个置灰；
- 两种宽度（1280、375）布局正常，无横向滚动，控制台无错误；
- 点开始后显示“模型不可达”的错误，而不是卡住；
- 设置面板能打开和保存，页面搜不到 `upload`、`video-file`、`presence` 相关元素。

真实模型的端到端验证需要 VPN 和 GPU 服务器，由用户运行；未运行时在交付说明里写明。

- [ ] **Step 8: Commit**

```bash
git add -A demos tests
git commit -m "feat(web): duplex-only frontend after the MiniCPM-o demo"
```

---

### Task 1: 分叉出新仓库

**Files:**
- Create: `/Users/ray/Desktop/voice-bailian/`（`git clone` 产物）

**Interfaces:**
- Consumes: Task 0 的 `duplex-frontend` 分支。
- Produces: 新仓库，后续所有路径都相对于它；远端名 `venus` 指向本地 Venus 仓库。

- [ ] **Step 1: 克隆并改远端名**

```bash
git clone --branch duplex-frontend /Users/ray/Desktop/voice /Users/ray/Desktop/voice-bailian
git -C /Users/ray/Desktop/voice-bailian remote rename origin venus
git -C /Users/ray/Desktop/voice-bailian switch -c bailian
```

Expected: `git -C /Users/ray/Desktop/voice-bailian log -1 --format=%s` 输出 Task 0 最后一条提交信息。

- [ ] **Step 2: 跑基线测试并记录失败项**

Run: `python -m pytest tests -q`
Expected: 记录通过数；已有失败写进 commit 说明，后续任务不为它们负责。

---

### Task 2: 接口改为语义事件，host 支持打断

这一步之后 `demos/server` 暂时不可用，只跑本任务列出的测试；Task 4 恢复。

**Files:**
- Modify: `harness/bridge/serving.py`、`harness/bridge/host.py`、`harness/bridge/runtime.py`、`harness/bridge/contracts.py`、`harness/core/harness.py`、`harness/core/session.py`
- Delete: `harness/bridge/boundary.py`、`harness/core/venus.py`、`harness/core/delegate_parser.py`
- Test: `tests/test_host_pipeline.py`、`tests/test_host_clock.py`（按新接口重写，不保留 token 用例）

**Interfaces:**
- Produces（`harness/bridge/serving.py`）：
  - `ModelOutputStep`：删除 `total_token_ids`，新增 `text: str = ""`、`delegates: tuple[str, ...] = ()`。未结束的步骤三者（text、delegates、audio）不能全空。`finish_reason` 取值 `"completed" | "interrupted" | "failed"`。
  - `UserSpeechStarted(session_id: str, incarnation: int, at_ms: int)`：新事件。
  - `ResultAppend(session_id, incarnation, work_id, attempt_id, text: str, final: bool)`（`final` 由 host 按 `injection.result.status != "pending"` 填写）、`ResultApplied(session_id, incarnation, work_id, attempt_id, generation_id, generation_epoch, applied_at_ms)`：替换 `PrefillAppend`、`PrefillApplied`；`PrefillVisibility` 删除。
  - `ServingEvent = InputAccepted | ModelOutputStep | ResultApplied | UserSpeechStarted`。
  - `SessionOpened`：删除 `capabilities` 和 `model_revision`；`SessionCapabilities`、`RawTokenMode`、`PROTOCOL_VERSION`、`VideoSegmentAppend` 删除。`OpenSession` 删除 `protocol_version`。
  - port 方法：`send_audio`、`send_video_frame`、`send_result(request: ResultAppend)`、`send_playback_ack`、`next_event`、`close_session`、`open_session`。
- Produces（`harness/bridge/host.py`）：
  - `PlaybackPort` 增加 `async def clear(self) -> None`。
  - `ProcessedOutput` 字段：`output`、`text: str`、`utterance_id`、`caused_by_work_id`、`queued_audio: bool`、`tail`。`AudioDisposition`、`mute_delegate_audio`、`tokenizer` 参数删除。
  - `VenusOmniServingHost.open(*, agent, serving, playback, session_id, model, identity=None, clock=_now_ms)`。
- Produces（`harness/bridge/runtime.py`）：
  - `VenusOmniSession.consume_model_step(text: str, delegates: tuple[str, ...], *, at_ms: int, cutoff_sequence: int) -> None`
  - `VenusOmniSession.delivery_interrupted(injection: BackendInjection) -> None`
  - `BackendInjection.text` 是纯文本，不含任何标签；`boundary.py` 里按 `feedback_id` 去重的逻辑移到 `next_backend_injection`。
- Produces（`harness/core`）：
  - `DelegateHarness.on_model_speech(session_id: str, text: str, *, at_ms: int) -> None`（替换 `on_model_text`，只记录模型说过的话，不做任何解析）。
  - `DelegateHarness.submit_delegate(...)` 增加关键字参数 `cutoff_sequence: int | None = None`，用于冻结任务对应的媒体范围。

- [ ] **Step 1: 按新接口写失败的测试**

在 `tests/test_host_pipeline.py` 用现有 `FakeServing` 写，playback 用记录调用的假对象：

```python
async def test_delegate_step_submits_with_input_cutoff(self):
    # step(text="", delegates=("查天气",), input_seq_cutoff=1)
    session.consume_model_step.assert_awaited_once_with(
        "", ("查天气",), at_ms=ANY, cutoff_sequence=1)

async def test_interrupting_result_does_not_poison(self):
    # 后台结果已入队 2 块、确认 1 块 → UserSpeechStarted → 带 finish_reason="interrupted" 的结束步
    playback.clear.assert_awaited_once()
    session.delivery_interrupted.assert_called_once_with(INJECTION)
    self.assertIsNone(host._backend_delivery)
    await host.append_audio(CHUNK)  # 会话仍可用

async def test_interrupted_progress_is_not_resent(self):
    # 进度（status="pending"）播放中 → UserSpeechStarted：标记 interrupted，之后 begin_backend_turn 不重发它
    session.delivery_interrupted.assert_called_once_with(PROGRESS)
    self.assertFalse(await host.begin_backend_turn())

async def test_interrupted_final_result_is_not_resent(self):
    # 最终结果同样不重念：用户可能就是不想听了，结果留在任务托盘
    session.delivery_interrupted.assert_called_once_with(INJECTION)
    self.assertFalse(await host.begin_backend_turn())

async def test_superseded_progress_is_discarded_before_sending(self):
    # injection_is_current 返回 False：不调用 send_result，调用 discard_injection
    session.discard_injection.assert_awaited_once_with(PROGRESS)
    self.assertFalse([r for r in serving.sent if isinstance(r, ResultAppend)])

async def test_result_append_marks_progress_and_final(self):
    self.assertEqual([r.final for r in sent_results], [False, True])

async def test_late_ack_after_interruption_is_ignored(self):
    # 打断后再对该 utterance 确认：不抛异常，也不转发给 serving
    self.assertEqual(await host.acknowledge_playback(utterance, 1), "")
    self.assertFalse([r for r in serving.sent if isinstance(r, PlaybackAck)])

async def test_speech_during_buffered_playback_clears_browser(self):
    # 生成已结束、播放未结束 → UserSpeechStarted
    playback.clear.assert_awaited_once()
    session.delivery_interrupted.assert_called_once_with(INJECTION)

async def test_result_turn_without_audio_keeps_session(self):
    # 后台结果这一轮只有工具调用、没有音频：不判协议错误，结果标记未确认
    session.feedback_failed.assert_called_once_with(INJECTION)
    await host.append_audio(CHUNK)

async def test_begin_backend_turn_sends_plain_text_result(self):
    self.assertEqual(serving.sent[-1].text, "done")
```

- [ ] **Step 2: 确认测试失败**

Run: `python -m pytest tests/test_host_pipeline.py tests/test_host_clock.py -q`
Expected: FAIL（`UserSpeechStarted` 等名字不存在）。

- [ ] **Step 3: 改 `serving.py` 和 `contracts.py`**

按 Interfaces 改写；`ModelStep` 删除。

- [ ] **Step 4: 改 `harness/core` 和 `runtime.py`**

删除解析器后，`finish_model_turn` 只调 `delivery.finish_frontend_turn("")`，返回 `""`。`consume_model_step` 先调 `on_model_speech`（text 非空时），再逐个调 `submit_delegate(cutoff_sequence=...)`。`delivery_interrupted`：取出并删除 receipt；若 `track_completed_work` 则 `await mark_delivered`；再 `feedback.delivered(work_id, feedback_id, "interrupted")`（因此 `work.delivery_status` 最终是 `interrupted`，结果留在托盘）。

- [ ] **Step 5: 改 `host.py`**

- `_process_output_locked` 不再有 token 累积和静音逻辑。
- 处理 `UserSpeechStarted`：先 `await playback.clear()`；所有未播完或未结束的 utterance 记入 `_interrupted`（有界 `deque` + `set`，上限 256，写法同 `_finished_generations`）；其中若含当前后台结果则 `delivery_interrupted` 并清空 `_backend_delivery` 与 `_attempts`。事件本身不产生 `ProcessedOutput`。
- `_acknowledge_playback_locked`：utterance 在 `_interrupted` 中时直接返回 `""`。
- `_finish_generation_locked`：删除“后台结果无音频即协议错误”。若 `total_chunks == 0` 且 `_backend_delivery` 还属于这一轮，则 `session.feedback_failed(injection)` 并清空，否则结果会一直占住队列。

- [ ] **Step 6: 确认测试通过并检查残留**

Run: `python -m pytest tests/test_host_pipeline.py tests/test_host_clock.py -q`
Expected: PASS

Run: `grep -rnE "total_token_ids|special_token|tokenizer|[Pp]refill|<delegate>|<backend>|DelegateParser" harness tests/test_host_pipeline.py tests/test_host_clock.py`
Expected: 无输出；若有，属于本任务的残留，一并删除或改写。

- [ ] **Step 7: Commit**

```bash
git add -A harness tests/test_host_pipeline.py tests/test_host_clock.py
git commit -m "refactor(bridge): semantic model events with interruption"
```

---

### Task 3: 百炼接入口与假服务端

**Files:**
- Create: `demos/bailian/__init__.py`、`demos/bailian/port.py`、`demos/bailian/prompts.py`
- Create: `tests/fake_bailian.py`
- Test: `tests/test_bailian_port.py`

**Interfaces:**
- Consumes: Task 2 的 `serving.py` 全部类型。
- Produces（`demos/bailian/port.py`）：
  - `class RemoteServingError(RuntimeError)`（不是 `TimeoutError` 子类）
  - `class RemoteSessionClosed(RemoteServingError)`
  - `class RemoteModelUnavailable(RemoteServingError)`，属性 `code: str`，取值 `model_auth | model_unreachable | model_link_lost`
  - `class BailianServingPort(url: str, *, api_key: str, model: str, voice: str, max_query_chars: int)`，实现 Task 2 的 port 方法，另有 `async def aclose() -> None`
- Produces（`tests/fake_bailian.py`）：`class FakeBailian`，异步上下文管理器，属性 `url`、`received: list[dict]`、`headers`；方法 `push(event: dict)`、`drop()`；可配置 `reject_token=True` 让握手返回 401。收到 `session.update` 自动回 `session.updated`。

端口状态规则（文档未写明的部分都按这里的保守做法，Task 7 实测后再校正）：

| 收到 / 调用 | 动作 |
|---|---|
| `open_session` | 连接（`Authorization: Bearer`，`ping_interval=5, ping_timeout=5, close_timeout=1`），等 `session.created`，发 `session.update`，等 `session.updated` |
| `send_audio` | 立即入队 `InputAccepted`；按 3200 字节切片发 `input_audio_buffer.append` |
| `send_video_frame` | 立即入队 `InputAccepted`；已发过音频才发 `input_image_buffer.append`，否则丢弃 |
| `speech_started` | 入队 `UserSpeechStarted`；有进行中的回复则发 `response.cancel`，并丢弃该回复后续音频和字幕 |
| `speech_stopped` | 记下当前 `input_seq`，作为下一轮任务的 `cutoff`；没有记录时用当前 `input_seq` |
| `response.created` | 分配 `generation_id`（用 `response.id`），`generation_epoch` 递增，`step_seq` 从 1 开始；若有已发出的结果在等待，入队 `ResultApplied` |
| `response.audio_transcript.delta` | 累积文字，附在下一块音频上 |
| `response.audio.delta` | 入队带音频的步骤，`audio_chunk_seq` 从 1 连续递增 |
| `response.function_call_arguments.done` | 校验（见 Step 3）：通过则入队 `delegates` 步骤，并记下 `call_id` |
| `response.done` | 入队结束步骤，带上剩余文字；`status` 映射为 `completed/interrupted/failed`（`cancelled` → `interrupted`）。对已接受或被拒的调用发 `function_call_output`；**仅当这一轮没有音频且接受了调用**时再发 `response.create`，让模型口头应答 |
| `send_result` | 放入待发队列；模型空闲时发 `conversation.item.create`（`role=system`，`input_text`，`final=True` 时前缀 `[后台结果] `，否则前缀 `[后台进度] `）和 `response.create` |
| `error`，`type=server_error` | 会话结束，抛 `RemoteServingError` |
| `error`，其他 type | 记日志；若有已发出未生效的结果，放回待发队列等下次空闲 |
| 连接断开 | `next_event` 和 `send_*` 抛 `RemoteModelUnavailable("model_link_lost", ...)` |

“模型空闲”的定义：没有进行中的回复，且用户不在说话（`speech_started` 之后、`speech_stopped` 之前）。

- [ ] **Step 1: 写假服务端和失败的测试**

`tests/test_bailian_port.py` 用真实 WebSocket 连 `FakeBailian`：

```python
async def test_open_configures_session(self):
    update = fake.received[0]["session"]
    self.assertEqual(update["voice"], "Tina")
    self.assertEqual(update["tools"][0]["name"], "delegate_task")
    self.assertEqual(update["turn_detection"]["type"], "semantic_vad")
    self.assertEqual(fake.headers["Authorization"], "Bearer k")

async def test_rejected_key_is_model_auth(self):
    self.assertEqual(ctx.exception.code, "model_auth")

async def test_audio_is_split_and_accepted_once(self):
    # 一次 send_audio 6400 字节 → 2 次 append，每次 3200 字节；1 个 InputAccepted
async def test_frame_before_first_audio_is_dropped_but_accepted(self):
async def test_transcript_attaches_to_next_audio_chunk(self):
    self.assertEqual((step.text, step.audio_chunk_seq, step.step_seq), ("你好", 1, 1))
async def test_delegate_uses_cutoff_from_speech_stopped(self):
    self.assertEqual((step.delegates, step.input_seq_cutoff), (("查天气",), 2))
async def test_silent_tool_turn_gets_spoken_followup(self):
    # function_call_output 之后紧跟 response.create
async def test_tool_turn_with_speech_has_no_followup(self):
async def test_bad_tool_arguments_create_no_task(self):
    # arguments 非 JSON / objective 为空 / 超过 max_query_chars：无 delegates 步骤，有错误 output
async def test_result_waits_while_user_speaks(self):
    # speech_started 后 send_result：不发；speech_stopped 且无回复后才发
async def test_result_waits_for_active_response(self):
async def test_rejected_result_is_retried_when_idle(self):
async def test_progress_and_final_use_different_prefixes(self):
    self.assertTrue(texts[0].startswith("[后台进度] "))
    self.assertTrue(texts[1].startswith("[后台结果] "))
async def test_result_applied_on_response_created(self):
async def test_speech_started_cancels_active_response(self):
    # 之后到达的 audio.delta 被丢弃；response.done(cancelled) → finish_reason == "interrupted"
async def test_speech_started_without_response_sends_no_cancel(self):
async def test_dropped_link_raises_link_lost(self):
    self.assertEqual(ctx.exception.code, "model_link_lost")
    self.assertNotIsInstance(ctx.exception, TimeoutError)
async def test_server_error_ends_session(self):
async def test_invalid_request_error_keeps_session(self):
async def test_close_makes_next_event_raise(self):
```

- [ ] **Step 2: 确认测试失败**

Run: `python -m pytest tests/test_bailian_port.py -q`
Expected: FAIL（`demos.bailian.port` 不存在）。

- [ ] **Step 3: 写 `prompts.py`**

`DELEGATE_TOOL` 为 function 类型工具：`name="delegate_task"`，唯一必填参数 `objective`（string），描述写明“交给后台执行的完整任务，把上下文指代还原成明确内容”。校验规则：能解析为 JSON 对象，`objective` 去空白后非空，长度不超过 `max_query_chars`。

`INSTRUCTIONS` 正文：

```text
# 角色与目标
你是实时语音助手，负责和用户自然对话，并把需要外部能力的请求交给后台执行。

# 输入说明
- 用户语音：当前对话内容。
- 摄像头画面：可能有，也可能没有。
- 以“[后台进度]”开头的系统消息：任务仍在进行，用户听不到原文。
- 以“[后台结果]”开头的系统消息：任务已经结束，内容是结果或失败原因，用户听不到原文。

# 生成规则
判断是否调用 delegate_task：
- 回答依赖实时信息（时间、天气、路况、票务、新闻）、联网查询、读写文件、运行代码，或需要多步执行时，调用。
- 用户明确要求交给后台时，调用。
- 闲聊、讲笑话、常识解释，以及根据当前画面可以直接描述的内容，直接回答，不调用。
- 请求含糊到无法确定任务内容时，先追问一个问题，不调用。
调用时：
- 先用一句话告诉用户已经开始处理，再调用工具；不要编造结果。
- objective 写完整任务，把“这个”“刚才那个”还原成明确内容。
收到后台消息时：
- 用口语转述要点，不提“后台进度”“后台结果”这些说法。
- 收到后台进度时，用一句话说明还在进行，不说成已完成，不推测最终结果。
- 结果表示失败时，直接说明没办成以及原因，不要猜测答案。
信息不足时：
- 听不清或没有画面时，如实说明并请用户重复或打开摄像头，不要编造内容。

# 输出格式
- 只输出适合朗读的纯文本，不用 Markdown、列表或表情。
- 每次回复不超过三句话，一次只问一个问题。

# 示例
用户：今天上海天气怎么样？
助手：好的，我查一下。（调用 delegate_task，objective 为“查询今天上海的天气”）
用户：讲个笑话吧。
助手：（直接讲一个简短的笑话，不调用工具）
```

- [ ] **Step 4: 实现 `port.py`**

按状态规则实现。`session.update` 字段：`modalities=["text","audio"]`、`voice`、`instructions`、`input_audio_format="pcm"`、`output_audio_format="pcm"`、`tools=[DELEGATE_TOOL]`、`turn_detection={"type":"semantic_vad","silence_duration_ms":800}`。握手被拒（`InvalidStatus` 且状态码 401/403）→ `model_auth`；其他连接失败 → `model_unreachable`。一个读循环任务把事件翻译进 `asyncio.Queue`，`send_*` 同步入发送队列，由一个写任务发出，保持“send 不等待模型”的约定。

- [ ] **Step 5: 确认测试通过**

Run: `python -m pytest tests/test_bailian_port.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add demos/bailian tests/fake_bailian.py tests/test_bailian_port.py
git commit -m "feat(bailian): realtime serving port over WebSocket"
```

---

### Task 4: Web 后端接入，删除模型服务器

**Files:**
- Modify: `demos/server/resources.py`、`demos/server/cli.py`、`demos/server/app.py`、`demos/server/session.py`、`demos/server/protocol.py`、`demos/server/configuration.py`、`harness/settings.py`（如需）
- Delete: `demos/model/`、`demos/settings.py`、`demos/variants.py`、`demos/requirements-model.txt`、`demos/constraints-model.txt`、`deploy/model/`、`download_models.py`、`requirements.txt`、`frontend/`
- Delete tests: `test_checkpoint.py`、`test_download_models.py`、`test_fetch_model.py`、`test_model_auth.py`、`test_model_driver.py`、`test_model_errors.py`、`test_model_link.py`、`test_model_server.py`、`test_model_stats.py`、`test_model_wire.py`、`test_web_link.py`、`test_replay_delegate.py`、`link_proxy.py`、`link_soak.py`
- Modify tests: `test_model_modes.py`（只留状态和模式校验，`input_modes` 改为两个模式都有）
- Test: `tests/test_web_session.py`（新建）、`tests/test_web_configuration.py`、`tests/test_web_settings.py`

**Interfaces:**
- Consumes: `BailianServingPort`、`RemoteModelUnavailable`（Task 3）；`VenusOmniServingHost.open`、`ProcessedOutput`、`PlaybackPort.clear`（Task 2）。
- Produces:
  - `real_resources(*, mode, settings_path) -> WebResources`；`WebResources(agent, serving)`，`model` 取 `BAILIAN_MODEL`。
  - `create_app(*, settings_path)`；`/api/status` 固定返回 `input_modes=["camera","audio"]`，`model` 为实际模型 ID。
  - 浏览器新消息 `{"type": "playback_clear"}`；`fatal_error.code` 新增 `model_auth`。
  - 删除 `model_link` 消息和 `ui_state` 中的 `delegate_audio_*` 字段。

- [ ] **Step 1: 写失败的测试**

`tests/test_web_session.py`：用假 websocket、假 factory，端口用 `FakeBailian` 上的 `BailianServingPort`。

```python
async def test_link_lost_reaches_browser_with_code(self):
    self.assertEqual(sent_fatal["code"], "model_link_lost")

async def test_rejected_key_reaches_browser_with_code(self):
    self.assertEqual(sent_fatal["code"], "model_auth")

async def test_speech_started_tells_browser_to_clear(self):
    self.assertIn({"type": "playback_clear"}, sent)

async def test_audio_mode_rejects_video_frames(self):
    self.assertEqual(sent_error["command"], "video_frame")

async def test_missing_api_key_fails_startup(self):
    # 未设 DASHSCOPE_API_KEY：cli main() 退出并提示变量名
    self.assertIn("DASHSCOPE_API_KEY", stderr)
```

- [ ] **Step 2: 确认测试失败**

Run: `python -m pytest tests/test_web_session.py -q`
Expected: FAIL

- [ ] **Step 3: 实现后端改动**

- `resources.py`：删除 `check_model`、`fetch_model`、`ModelInfo`、`_model_client`、`unreachable_message`、tokenizer。
- `cli.py`：删除 `--model-server`、`--model-timeout`；启动时检查两个必填环境变量，缺失时打印变量名并退出。
- `session.py`：`BrowserPlayback` 增加 `clear`；删除 Venus 型号校验、`_link_changed`、`_link_up`、断线丢音频逻辑。错误提示改为不提 Venus 的通用文案。
- `app.py`：删除 `model_type`、`tokenizer` 等参数。
- `configuration.py` 和测试：改用 `harness.settings` 的 `HarnessSetup`、`load_setup`、`save_setup`、`config_path`，删除 `duplex` 配置。

- [ ] **Step 4: 确认全部测试通过并检查残留**

Run: `python -m pytest tests -q`
Expected: 除 Task 1 记录的既有失败外全部 PASS。

Run: `grep -rnE "demos\.model|demos\.variants|demos\.settings|model_server|VENUS_MODEL_TOKEN|length_penalty" demos harness tests`
Expected: 无输出（`demos/static` 的残留在 Task 5 处理，此时可列出但不应出现在 Python 文件中）。

- [ ] **Step 5: Commit**

```bash
git add -A demos deploy tests harness download_models.py requirements.txt frontend
git commit -m "feat(web): serve through Bailian; remove the model server"
```

---

### Task 5: 浏览器改动

**Files:**
- Modify: `demos/static/capture.js`、`demos/static/audio.js`、`demos/static/app.js`、`demos/static/index.html`、`demos/static/i18n.js`

**Interfaces:**
- Consumes: 消息 `{"type":"playback_clear"}`、`fatal_error.code == "model_auth"`（Task 4）。

- [ ] **Step 1: 改采集节奏**

`capture.js`：音频切片从 1 秒改为 100 ms（`Math.round(context.sampleRate / 10)`），否则用户说完最多还要多等 1 秒才发出去。视频模式下每 10 个音频块取一帧：`queue(pcm, generation, withFrame)`，其余块不编码 JPEG；音频模式从不取帧。背压上限从 `3` 改为 `30`，仍对应约 3 秒积压。`video.readyState < 2` 时不再丢这段音频，只跳过画面。这一改动只在新服务做：Venus 仍按 1 秒一个单元工作。

- [ ] **Step 2: 改播放**

`app.js` 处理 `playback_clear`：调 `player.clear()`。`audio.js` 把 `leadSeconds` 默认值改为 `0.2`，并删除针对 VPN 步长测量的注释；实际取值在 Task 7 校正。

- [ ] **Step 3: 清理界面**

删除 `length-penalty` 输入框及其读写、`model_link` 处理。错误码映射增加 `model_auth`，文案：`API Key 被拒绝，请检查 DASHSCOPE_API_KEY` / `The API key was rejected. Check DASHSCOPE_API_KEY.`。页面标题和文案里的 “Venus” 改为 “Voice Agent”。

- [ ] **Step 4: 用假服务端手动验证**

用 `FakeBailian` 起一个脚本化服务端（`BAILIAN_REALTIME_URL` 指向它），在内置浏览器打开页面。
Expected:
- 两个模式标签都可用，都能开始会话，无控制台错误；
- 视频模式下假服务端收到每次 3200 字节的 `append`，以及约每秒一次的 `input_image_buffer.append`；
- 音频模式下假服务端收不到任何 `input_image_buffer.append`，也不申请摄像头权限；
- 推送 `speech_started` 后播放立即停止；
- 设置面板能打开和保存。

- [ ] **Step 5: Commit**

```bash
git add demos/static
git commit -m "feat(web): 100 ms capture and interruptible playback"
```

---

### Task 6: 部署与文档

**Files:**
- Modify: `deploy/web/compose.yaml`、`deploy/web/.env.example`、`deploy/web/Dockerfile`、`demos/requirements-web.txt`、`demos/constraints-web.txt`、`README.md`、`README_ZH.md`、`demos/README.md`、`demos/README_ZH.md`

- [ ] **Step 1: 改 compose 和 env**

`compose.yaml`：把项目名改为 `voice-bailian`，删除 `--model-server`、`--model-timeout` 和 `VENUS_MODEL_TOKEN`；传入 `DASHSCOPE_API_KEY: ${DASHSCOPE_API_KEY:?Set DASHSCOPE_API_KEY in .env}`、`BAILIAN_REALTIME_URL`（同样必填）、`BAILIAN_MODEL`、`BAILIAN_VOICE`。`.env.example` 对应更新。

- [ ] **Step 2: 改依赖**

`requirements-web.txt`：删除 `transformers`，把 `websockets` 显式列出。构建镜像后按该文件注释的办法重新生成约束：

```bash
docker compose -f deploy/web/compose.yaml build
docker run --rm --entrypoint pip realtime-venus-web freeze --all
```

把输出写入 `demos/constraints-web.txt`。Expected: 不含 `transformers`、`tokenizers`；含 `websockets==17.3`。

- [ ] **Step 3: 改文档**

README 只描述本服务：从配置 Key 到启动的步骤，语音和画面会发送到阿里云，费用按量计算，会话上限 120 分钟。删除 GPU、VPN、权重下载相关段落。

- [ ] **Step 4: 验证容器能启动**

Run: `docker compose -f deploy/web/compose.yaml up -d --build`（用假 Key 和指向假服务端的地址）
Expected: `http://localhost:8032/health` 返回 `{"status":"ok",...}`；未设置 Key 时 compose 报出变量名。

- [ ] **Step 5: Commit**

```bash
git add deploy demos/requirements-web.txt demos/constraints-web.txt README.md README_ZH.md demos/README.md demos/README_ZH.md
git commit -m "docs(deploy): run the Bailian service"
```

---

### Task 7: 拿到 Key 后实测与验收

**阻塞条件：** 需要用户把 `DASHSCOPE_API_KEY` 和 `BAILIAN_REALTIME_URL` 写入 `deploy/web/.env`（Key 不要发到对话里）。之前的任务都不依赖它。

**Files:**
- Create: `scripts/bailian_probe.py`
- Modify: `demos/bailian/port.py`、`demos/bailian/prompts.py`、`tests/fake_bailian.py`、`tests/test_bailian_port.py`（按实测结果）
- Create: `docs/bailian-probe.md`（实测记录）

- [ ] **Step 1: 写探测脚本**

`python scripts/bailian_probe.py --wav DIR --runs 5`：直接用真实接口，把结果写成 Markdown，回答以下问题：

| 编号 | 问题 | 结果影响 |
|---|---|---|
| 1 | `session.updated` 回显的格式、音色、`turn_detection` 是否与发送一致（文档有 `audio.output.voice` 新写法） | `session.update` 字段 |
| 2 | 工具结果延后返回时，期间能否继续对话 | 结果回传方式 |
| 3 | 模型回复中或用户说话中发 `response.create` 的返回 | “空闲”判定和重试 |
| 4 | 注入 `role=system` 后 `response.create` 是否念出内容 | 结果注入角色 |
| 5 | 音频块大小，字幕和音频的先后 | 文字附着规则 |
| 6 | `response.done.usage`：静音是否计费，一轮成本多少 | 费用说明；是否启用视频聚合 `representation_compact=normal` |
| 7 | `semantic_vad` 对比 `semantic_vad_ec`：外放时是否被自己的声音打断 | `turn_detection` 取值 |

- [ ] **Step 2: 运行探测并记录**

Run: `python scripts/bailian_probe.py --wav /Users/ray/Desktop/voice/deploy/model/replay --runs 5 > docs/bailian-probe.md`
Expected: 7 个问题都有明确答案。

- [ ] **Step 3: 校正实现**

只改 `demos/bailian/`；把真实行为同步进 `FakeBailian`，并让对应测试先失败再通过，使假服务端不再和真实接口有已知差异。

Run: `python -m pytest tests -q`
Expected: PASS

- [ ] **Step 4: 委托准确率验收**

用同一批录音跑 `delegate_task` 调用次数，每条 5 次。
Expected（实现方定的门槛，可调整）：
- `1_time`、`2_weather`、`4_train`、`7_explicit_zh` 合计至少 18/20 次调用工具；
- `5_chat`、`6_joke` 合计至多 1/10 次调用工具。

未达标时按全局提示词规范归类并改写现有规则，不为单条录音加特例。

- [ ] **Step 5: 端到端手测**

Run: `docker compose -f deploy/web/compose.yaml up -d --build`，浏览器打开 `http://localhost:8032`。
Expected:
- 视频模式下问画面内容，模型能答对；音频模式下问画面内容，模型如实说看不到；
- 两个模式下说“查一下今天天气”，先听到应答，稍后听到结果，任务托盘有记录；
- 模型说话时开口，声音立刻停止；
- 打断后台结果的播报，会话继续，托盘里结果仍在；
- 外放时不会被自己的声音打断（否则按问题 7 的结果调整）；
- 从说完到听见回复的延迟写入 `docs/bailian-probe.md`，并据此校正 `leadSeconds`。

- [ ] **Step 6: Commit**

```bash
git add scripts docs demos/bailian tests demos/static/audio.js
git commit -m "feat(bailian): calibrate against the live API"
```
