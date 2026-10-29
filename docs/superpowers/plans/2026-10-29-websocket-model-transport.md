# 网页服务 ↔ 模型服务改用 WebSocket（输入流水线 + 输出推送 + 断线续接）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把网页服务与模型服务之间每秒好几次、一个等一个的 HTTP 往返，换成一条常驻 WebSocket：输入发出就走，不等回执；模型每算完一步就把输出推过来；连接被 VPN 掐断后自动重连，接回同一个模型会话，消息不丢也不重。验收标准：摄像头模式下，同一段回答里相邻语音块的到达间隔稳定在约 1.0 s。

**Architecture:** 分两层。下层是"可靠有序流"：两个方向上每条必须送达的消息都带会话内序号 `seq`，每条消息还捎带"我已连续收到对方的最大序号" `ack`；对方没确认的消息留在发送方的重放缓冲里，重连后从对方的 `ack` 之后按原顺序重发，接收方按序号去重。上层是 host 与模型服务之间的新约定，从"请求等回复"改成"发送不等待 + 一条有序事件流"：输入回执、模型输出、prefill 确认都按模型发出的顺序从同一条流到达，host 逐条校验，栅栏规则不变。模型侧所有推理由 driver 一个协程串行执行，不再等拉取；"拉一次走一步"的背压改成信用额度：已推送但 host 还没取走的输出最多 4 步。

**Tech Stack:** Python 3.11、FastAPI/uvicorn 0.54（服务端 WebSocket）、websockets 17.3 asyncio 客户端（`websockets.asyncio.client.connect`）、unittest 风格测试，用 pytest 运行。

**Spec:** 本计划的「设计决定」一节（无单独 spec 文档）。需求、约束和测量数据来自发起会话的简报。

## 现状：为什么摄像头模式每 2–3 s 才出一段

测量结果：每个 HTTP 请求往返约 345 ms，其中 TCP 建连只占 1–2 ms，其余是本机 aTrust 客户端中转造成的。模型单步推理 p50 约 400 ms、p95 约 890 ms、最长 1.36 s。摄像头模式下浏览器每秒送"1 帧 JPEG + 1 秒音频"，语音输出却每 2.0–3.1 s 才到一段（每段 1 s）；只送音频时约 1.0 s 一段。网页播放本身已核实没问题。

慢的原因不是带宽，而是下面三处等待互相咬住，把往返时间串进了每一步的周期：

| # | 等待点 | 位置 | 后果 |
|---|---|---|---|
| 1 | 浏览器消息逐条处理，每条都要等模型服务的 HTTP 回复 | `demos/server/session.py:234-291` | 摄像头模式每秒 1 帧 + 1 段音频，播放时再加 1 条播放回执，共 2–3 次串行往返，每次至少 345 ms |
| 2 | host 发 HTTP 期间一直持有 `_owner_lock`，处理输出也要这把锁 | `harness/bridge/host.py:357,382,445,649` | 有输入在路上时，已经到达的输出不能处理，下一次拉取也发不出去 |
| 3 | 模型只在有 `/output` 拉取挂着时才推理一步；append 要先拿 `_turn_lock`，而这把锁在整步推理期间都被占着 | `demos/model/adapter.py:420,442,470-472,652-664` | 每走一步之前先要等一次拉取往返；每次 append 的回复还要等当前这一步算完，最长 1.36 s |

用一步的周期串起来看：输出返回网页要半个往返，处理它要等正在进行的 append 先回来（又是一次往返，外加模型那边等锁）；处理完才发出下一次拉取，拉取到达模型又要半个往返；模型这才开始算下一步。所以周期约等于"一次拉取往返 + 一步推理 + 等 append"，摄像头模式 append 多，就到了 2–3 s。只送音频时 append 少，周期勉强压在 1 s 左右。

根治思路是把往返时间从"周期"里拿出来，变成一个固定延迟：模型凑满 1 s 输入就算，算完就推；输入发出不等回复。这样周期等于输入周期 1 s，只要单步推理小于 1 s（p95 890 ms）就跟得上；往返只让每段语音整体晚到约 345 ms，不再拉长间隔。之前的分析已经说明，只把 HTTP 换成 WebSocket、仍然一条等一条，省不了时间，因为等待在约定里，不在传输协议里。所以本计划同时改约定。

## 设计决定

- **D1 一条 WebSocket，二进制帧。** 模型服务新增 `WS /session`。每条消息的格式是：4 字节大端头长度 + UTF-8 JSON 头 + 原始负载（PCM 或 JPEG 字节，不再用 base64）。头字段与对应 DTO（`harness/bridge/serving.py`）同名，DTO 里的 `bytes` 字段改放在负载里。HTTP 只保留 `/healthz`、`/tokenizer`、`/stats`。`PROTOCOL_VERSION` 升为 `realtime-venus-harness/3`，并由 `/healthz` 返回；网页启动时核对，两边版本不一致就拒绝启动，并说明要更新哪一边。

- **D2 消息类型。** "可靠"一列为"是"的消息带 `seq`，进入重放缓冲；其余消息只属于当前这条连接。所有消息都带 `ack`，客户端发出的消息还带 `consumed`（见 D5）。

  | 方向 | type | 可靠 | 头字段（除 type/seq/ack/consumed） | 负载 |
  |---|---|---|---|---|
  | 网页→模型 | `open` | 否，新连接的第一条 | `protocol_version, session_id, model`，可选 `length_penalty` | — |
  | 网页→模型 | `resume` | 否，新连接的第一条 | `session_id, incarnation` | — |
  | 网页→模型 | `audio` | 是 | `AudioAppend` 去掉 `data` | PCM16 |
  | 网页→模型 | `video_frame` | 是 | `VideoFrameAppend` 去掉 `data` | JPEG |
  | 网页→模型 | `prefill` | 是 | `PrefillAppend` 全部字段 | — |
  | 网页→模型 | `playback_ack` | 是 | `PlaybackAck` 全部字段 | — |
  | 网页→模型 | `close` | 是 | `reason` | — |
  | 网页→模型 | `state` | 否 | 无（只为送出最新的 `ack`/`consumed`） | — |
  | 模型→网页 | `opened` | 否 | `SessionOpened` 全部字段 | — |
  | 模型→网页 | `resumed` | 否 | 无（`ack` 即模型已收到的最大序号） | — |
  | 模型→网页 | `input_accepted` | 是 | `InputAccepted` 全部字段 | — |
  | 模型→网页 | `output` | 是 | `ModelOutputStep` 去掉音频字节；`audio` 为 `{sample_rate_hz, channels, sample_width_bytes, format}` 或 `null` | PCM16，无音频时为空 |
  | 模型→网页 | `prefill_applied` | 是 | `PrefillApplied` 全部字段 | — |
  | 模型→网页 | `closed` | 是 | `SessionClosed` 全部字段 | — |
  | 模型→网页 | `error` | 否，发完即断开 | `code, error` | — |

  `error.code` 只有五种取值：`model_busy`、`model_mismatch`、`session_lost`（要接回的会话已不存在）、`protocol`（帧格式、序号或栅栏错误）、`model_error`（模型侧异常）。WebSocket 关闭码：1000 正常关闭；1008 令牌错误；4001 被新连接取代；1011 发生错误。

- **D3 可靠有序流：序号、确认、重放边界。**
  - 两个方向各自从 1 开始连续编号，编号属于会话，重连后不重置。
  - `ack` 和 `consumed` 在消息写出的那一刻填当前值，不在入队时填。发送方收到 `ack` 后，删掉重放缓冲里序号 ≤ `ack` 的消息。
  - 重连握手时双方交换 `ack`，各自从对方的 `ack` 之后按原顺序重发。接收方遇到 `seq` ≤ 已收到的最大序号就丢弃（去重）；遇到跳号算协议错误。所以 host 看到的事件流即使中间断过线，也仍然是"每条恰好一次、按顺序"，host 不需要知道断过线。
  - 每个方向的重放缓冲上限 32 MiB，超出就判定这个会话接不回来了。
  - 应用层原有的序号（`event_seq`、`input_seq`、`step_seq`、`audio_chunk_seq`）全部保留。传输层的 `seq` 负责送达，应用层序号负责栅栏校验，两层互不替代。

- **D4 host 约定：发送不等待，事件按序消费。**
  - 新的 serving port 接口：`send_audio`、`send_video_frame`、`send_video_segment`、`send_prefill`、`send_playback_ack` 都是同步函数，只把消息放进发送队列；`next_event()` 按模型发出的顺序返回 `InputAccepted | ModelOutputStep | PrefillApplied`。同步函数里没法 await 网络，所以"拿着锁等网络"这件事在类型层面就不可能再出现。
  - 锁：`_owner_lock` 只保护本地状态，持有期间不再等待模型。`_begin_lock`、`_active_pull_task`、`_pending_pulled_output`、`_deferred_injection`、`process_output` 一并删除。它们原本是为了防止"prefill 和一个正在路上的输出抢跑"；现在 prefill 的确认也在同一条有序流里到达，不存在抢跑。
  - 输入栅栏：发送时分配 `event_seq`，同时在本地登记 `(event_seq, harness_sequence)`。回执到达时按先进先出对号，要求三点同时成立：`event_seq` 一致；`input_seq` 等于上一个加 1；`input_seq` 等于登记的 `harness_sequence`。输出的 `input_seq_cutoff` 仍然只能引用已经回执过的输入（原有检查不变）。这条规则能成立，靠的是模型侧的一个保证：接收输入和把回执放进发送队列，在同一段不让出事件循环的代码里完成，所以回执一定排在任何"吃掉了这个输入"的输出前面；而单条有序连接保证到达顺序就是发出顺序。
  - 出错即作废：下面三种情况，host 都立即进入 poison 状态，之后 append、播放回执、`begin_backend_turn` 全部抛错，网页会话以 `fatal_error` 结束。（a）任何一条事件校验失败；（b）模型发来 `error`；（c）重连超过期限。已经发出但还没收到回执的输入不需要撤回，因为整个会话都会关闭，不留任何状态。
  - 播放回执：浏览器报的"这一块播完了"本身就是事实，host 在发出 `playback_ack` 的同时推进本地播放账本。`PlaybackAccepted` 回执删除，它只是把数字原样回显，不带任何新信息。模型侧发现回执往回退，就发 `error`，走上面的作废流程。
  - prefill：host 只在自己判断空闲时发出 prefill；模型在下一个生成边界（当前前台生成结束后）应用它，再回 `prefill_applied`。`PrefillDeferred` 删除：以前的"模型说忙→host 稍后重发同一个 attempt"，和"模型等到边界再应用"效果一样，后者还少一个来回。"同一个 attempt_id 重试一次"的歧义处理也删除，因为可靠流里不会出现"回复丢了、不知道生效没有"。prefill 发出后，如果 `feedback_timeout_s`（默认 60 s）内还没收到确认，就作废会话；这条规则取代原来两次 `wait_for` 的写法。

- **D5 模型侧 driver：不再等拉取，用信用额度做背压。**
  - driver 是唯一调用模型的协程：前台推理、后台续写、把 prefill 写进 KV，全都在它里面依次执行，所以 `_turn_lock` 删除。append、播放回执、信用更新都只是同步记账，不等推理。
  - 信用：客户端在每条消息头里报 `consumed`，即 host 至今从 `next_event` 取走的输出累计条数。driver 只在 `emitted - consumed < OUTPUT_WINDOW`（= 4）时才走下一步。和旧方案的关系：旧方案相当于窗口为 1，代价是每一步都多等一次往返。要不让窗口成为瓶颈，需要窗口 ≥ (单步时长 + 往返) / 单步时长；按 400 ms 单步、1 s 的往返尖峰算是 3.5，所以取 4。另一方面，网页卡住或断线时，模型最多多算 4 步，服务端缓冲有上限。后台续写不等输入、会用零音频垫步，照样受这个窗口约束。
  - 前台的步仍然由输入驱动：凑满 1 s 音频才走一步。

- **D6 断线重连与接回会话。**
  - 客户端：aTrust 主动掐断时立即就能发现；如果是无声的黑洞，由 5 s ping、5 s 超时发现。发现后依次等 0.5、1、2、2、2… s 重试，总期限 `RECONNECT_DEADLINE_S = 45`。每次重试：建新连接 → 发 `resume{session_id, incarnation, ack, consumed}` → 模型回 `resumed{ack}` → 双方重放。之所以每 2 s 就试一次：aTrust 掐线后的约 16 s 里，新连接会在 17–64 ms 内被关掉，试一次几乎没有成本，而且能在它恢复后 2 s 内接上。
  - 服务端：连接断开后，会话转为"离线"状态，保留 `RESUME_WINDOW_S = 60` s。这个时长大于客户端的 45 s 期限，保证客户端还在重试时会话一定还在。`resume` 把会话挂到新连接上；如果服务端还以为旧连接在线（半开连接），新连接取代旧连接（旧连接以 4001 关闭）。
  - `open` 遇到一个离线会话：立即关闭它，把名额让给新会话，这样网页服务重启后不用等。`open` 遇到一个在线会话：返回 `model_busy`。这套规则取代 15 s 会话租约，`SESSION_LEASE_S` 和 `last_seen` 删除。
  - 45 s 小于 `max_playback_wait_ms`（60 s），所以单纯的断线不会让一条正在播报的任务结果被判成超时。
  - 开会话本身不自动重试：握手失败就报 `model_unreachable`，由用户再点一次开始；服务器上可能残留的离线会话会在下一次 `open` 时让位。

- **D7 约 16 s 的中断，用户看到什么。**
  - 断开的那一刻：网页服务给浏览器发 `{"type":"model_link","state":"down"}`，页面显示一条不会自动消失的提示："与模型服务器的连接中断，正在重连；这段时间你说的话模型听不到。"实时麦克风和摄像头的数据在网页服务这一层直接丢弃，不进入 host，所以 `event_seq` 和 harness 媒体序号都保持连续。浏览器里已经缓冲的语音会继续播完。
  - 为什么丢弃而不是缓存：如果缓存这 16 s 的输入，恢复后模型会一口气处理这些旧输入，回答滞后十几秒，播放队列也越积越长。丢弃的含义很简单——"这 16 s 模型没听到"，可以直接告诉用户。上传文件模式例外：文件是内容，不是实时输入，照常缓冲，恢复后补发。
  - 恢复：发送 `state: "up"`，页面提示"已重新连接模型服务器"。会话、KV、正在进行的后台任务都接着用；断开前已经发出但没确认的输入和输出，会按顺序补送。
  - 超过 45 s 仍未恢复：发 `fatal_error`，使用新错误码 `model_link_lost`，提示"与模型服务器的连接中断超过 45 秒，本次会话已结束，请重新开始。"

- **D8 不合并 `a9db774`（分支 `claude/quizzical-austin-7e7b76`）。** 这个提交修的是 HTTP 空闲连接复用时的撞车：uvicorn 的 keep-alive 比客户端早几百毫秒关闭空闲连接，客户端恰好在这时复用它，请求就丢了。WebSocket 方案落地后，会话流量全部走一条从不空闲的连接（每秒都有数据，另外还有 5 s 一次的 ping），而 keep-alive 超时本来就不作用于 WebSocket。剩下的 HTTP 请求只有网页启动时的 `/healthz` 和 `/tokenizer`（`resources.py` 里每个 `httpx.Client` 只发一个请求就关闭，不会复用连接），以及手工查询的 `/stats`。撞车需要"复用一条已空闲 4–5 s 的连接"，这个条件不再出现，所以不合并这个修复，删掉那个分支即可。另一种断连（aTrust 自己掐断，即模式 2）由 D6 的重连覆盖。
- **D9 撤掉 `377ce60`（`PUBLIC_HOST` / `WEB_BIND_IP`）。** 它是为"把网页搬到 GPU 服务器"做的，这条路已经放弃。连同它改的文档和测试一起撤掉；`253cb73`（浏览器固定提前 1 s 起播）保留。
- **D10 浏览器 ↔ 网页服务这一段不改。** 它走本机回环，base64 的开销与 VPN 延迟无关。

## 删除清单

删除前用 `grep -rn` 确认调用点为零（本仓库没有 codegraph 索引），然后连同测试、文档一起删。

| 位置 | 删除 | 由什么取代 | Task |
|---|---|---|---|
| `demos/model/server.py` | `POST /sessions`、`DELETE /sessions/{sid}`、`/sessions/{sid}/audio、video_frame、video_segment、prefill、playback_ack、output`，以及 `_body_with_session`、`_err`、`default_timeout_s` / `--default-timeout-s` | `WS /session` | 3、4 |
| `demos/model/adapter.py` | `SESSION_LEASE_S`、`_Session.last_seen/queue/output_requested/output_inflight`、`next_output_step`、`append_video_segment`、`_turn_lock`、`_require*`、`OutputTimeout`、`UnknownSession`、`IncarnationMismatch`、`SessionClosedError`、所有 `status_code` | 离线/接回窗口、信用额度、driver 独占模型、`SessionLost`、`code` | 3 |
| `demos/model/wire.py` | `b64`、`unb64`、`parse_audio_append`、`parse_frame_append`、`generated_audio_dict`、`playback_accepted`，以及 docstring 里的 base64 规则 | 二进制帧、`audio_header` | 3 |
| `demos/model/client.py` | 整个 httpx 实现：`_request`、`_b64`、`_ub64`、`RemoteOutputTimeout`、HTTP 状态码映射、旧的 8 个方法 | WebSocket 版 `RemoteOmniServingPort` | 5 |
| `demos/server/resources.py` | `check_model` 里"缺少 `token_ok` 就是旧服务端"的判断 | 比较 `protocol_version` | 5 |
| `harness/bridge/serving.py` | `PlaybackAccepted`、`PrefillDeferred`、`append_*` / `next_output_step` / `append_prefill` / `acknowledge_playback` 这几个 Protocol 方法 | `send_*` + `next_event` | 2 |
| `harness/bridge/host.py` | `process_output`、`active_backend_turn`、`queued_backend_results`、`_begin_lock`、`_active_pull_task`、`_pending_pulled_output`、`_model_owner_busy`、`_deferred_injection`、`_validate_playback_accepted`、prefill 歧义重试、`_last_harness_sequence` | D4 | 2 |
| `demos/server/session.py` | `output()` 里的 `queued_backend_results` / `active_backend_turn` 判断和 `TimeoutError` 分支 | `begin_backend_turn() -> bool` | 6 |
| 测试 | 租约测试、HTTP MockTransport 测试、`/sessions` 401 断言、httpx 客户端的 token/代理测试、`process_output` 的用法 | 各 Task 中的新测试 | 2–5 |
| 文档 | "15 秒释放名额"、"每秒 3 次请求加 1 次输出拉取"、"HTTP 模型服务"、`MODEL_TIMEOUT_S` 的旧说明 | Task 8 | 8 |
| git | `377ce60`（撤销）；`claude/quizzical-austin-7e7b76` 分支上的 `a9db774`（不合并） | D8、D9 | 0、9 |

## Global Constraints

- 网页服务留在 Mac 上（Docker），模型服务在 GPU 服务器 `10.194.99.8:8031`。Mac 无法 SSH 到服务器，服务器上的命令只能由用户手动执行，再把输出贴回来。
- 不新增依赖：服务端 WebSocket 用 `uvicorn[standard]` 已经带的 `websockets==17.3`，客户端用 `websockets.asyncio.client.connect`；两端的 requirements 和 constraints 文件都不改。
- 保留 harness 的硬性要求：原始 token id 与特殊 token 表、`same_kv_prefill`、有序输入栅栏、按音频块确认播放。`serving.py` 里 `SessionCapabilities` 的 hard flags 不变。
- 不写向后兼容（CLAUDE.md 第 1 条）：旧 HTTP 会话路由、会话租约、`PrefillDeferred`、`PlaybackAccepted`、base64 编解码，以及它们的测试和文档，在同一个分支里删除，不留开关。
- 常量：`OUTPUT_WINDOW = 4`；`RESUME_WINDOW_S = 60.0`；`RECONNECT_DEADLINE_S = 45.0`；`RECONNECT_DELAYS_S = (0.5, 1.0, 2.0)`，最后一项重复使用；`CLOSE_TIMEOUT_S = 10.0`；两端 ping 间隔 5 s、超时 5 s；`MAX_FRAME_BYTES = 1 << 20`；重放缓冲上限 `32 << 20` 字节/方向。
- websockets 客户端必须传 `proxy=None`。websockets 15 起默认会读取系统和环境变量里的代理，这与原 httpx 客户端设 `trust_env=False` 的理由相同。
- 认证：WebSocket 握手带 `Authorization: Bearer <VENUS_MODEL_TOKEN>`。FastAPI 的 `@app.middleware("http")` 不会拦截 WebSocket，所以 `/session` 必须自己用 `token_ok` 校验，不通过就以 1008 关闭。
- 中间提交不能部署：Task 2–6 期间，网页和模型两侧暂时对不上；只有 Task 9 时才部署。
- 测试命令（仓库根目录）：`python3 -m pytest -q tests --ignore=tests/test_checkpoint.py --ignore=tests/test_model_modes.py`。浸泡测试要单独跑：`python3 -m pytest -q tests/link_soak.py`（文件名不匹配 `test_*.py`，默认不会被收集）。本机 python3 的 websockets 是 16.0、uvicorn 是 0.46；本计划用到的 API（`connect` 的 `additional_headers`、`proxy`、`open_timeout`、`ping_interval`、`ping_timeout`、`max_size`，以及 uvicorn 的 `ws_ping_interval`、`ws_ping_timeout`、`ws_max_size`）在这两个版本里都存在。

## Review Focus

1. Mac 或网页容器里设了 `HTTPS_PROXY` / `ALL_PROXY` → 连接模型仍然直连，不经过代理。（Task 5 `test_link_ignores_proxy_env`）
2. aTrust 掐断连接，但服务端没察觉（半开连接）→ 客户端的 `resume` 取代旧连接，而不是收到 `model_busy`。（Task 4 `test_resume_supersedes_a_live_connection`）
3. 网页服务崩溃后重启（无法 resume）→ 新的 `open` 立即成功，旧的离线会话让位。（Task 3 `test_open_preempts_a_detached_session`）
4. 断线期间用户点"停止"→ `host.close()` 最多 `CLOSE_TIMEOUT_S` 就返回，网页会话能结束，不会卡死。（Task 5 `test_close_during_outage_gives_up`）
5. 断线期间浏览器继续送实时音频和画面，恢复后第一段输入的 `event_seq` 和 harness 序号仍然连续，host 不会进入 poison。（Task 6 `test_live_input_is_dropped_while_link_down`，Task 7 `test_link_drop_mid_conversation_resumes`）

---

### Task 0: 撤掉 377ce60，记录基线

**Files:** 无新改动；撤销 `377ce60` 改过的 8 个文件（`demos/README*.md`、`demos/server/app.py`、`demos/server/cli.py`、`deploy/web/.env.example`、`deploy/web/compose.yaml`、`tests/test_local_host.py`、`tests/test_web_settings.py`）。

- [ ] **Step 1:** 在 `fix/web-playback-and-remote-page` 上执行 `git revert --no-edit 377ce60`。用 revert 而不是改写历史，因为它不会丢东西，而且同样能把文档和测试一起恢复原样。
- [ ] **Step 2: 核对。** `grep -rn -E "PUBLIC_HOST|WEB_BIND_IP|public_host" demos deploy tests` 应无输出；`git log --oneline -3` 应依次是 Revert、`377ce60`、`253cb73`。
- [ ] **Step 3: 基线。** 运行测试命令，记下通过数（撤销前是 149 passed）。以后各 Task 都和这个数比较。
- [ ] **Step 4:** 从这里切出 `feat/ws-model-link`，之后的 Task 都在这个分支上做。

### Task 1: 帧格式与可靠流的两个基本部件

**Files:**
- Modify: `demos/model/wire.py`（新增下列接口；把 `StepStats` 从 `adapter.py` 移到这里，供两端共用）
- Modify: `demos/model/adapter.py`（删除 `StepStats`，改为从 `wire` 导入）、`tests/test_model_stats.py`（改导入路径）
- Test: `tests/test_model_wire.py`

**Interfaces — Produces（`demos/model/wire.py`）:**
- `PROTOCOL_VERSION = "realtime-venus-harness/3"`、`MAX_FRAME_BYTES = 1 << 20`
- `class FrameError(ValueError)`
- `encode_frame(header: dict[str, Any], payload: bytes = b"") -> bytes`：头部用 `json.dumps(header, separators=(",", ":"))` 序列化
- `decode_frame(data: bytes) -> tuple[dict[str, Any], bytes]`：以下情况都抛 `FrameError`——数据不足 4 字节、头长度越界、头不是 JSON object、`type` 不是 str、整帧超过 `MAX_FRAME_BYTES`
- `class Outbox(max_bytes: int = 32 << 20)`：`push(header, payload=b"") -> int`（写入 `header["seq"]` 并返回它；累计未确认字节超过上限时抛 `OverflowError`）；`confirm(ack: int) -> None`；`after(seq: int) -> list[tuple[dict, bytes]]`（序号大于 `seq` 的未确认帧，按顺序）；`last_seq: int`；`ready: asyncio.Event`（每次 `push` 时置位，写出协程靠它唤醒）
- `class Inbox`：`last: int`（初始为 0）；`accept(seq: int) -> bool`——`seq == last + 1` 时推进并返回 True，`seq <= last` 时返回 False（表示重复），`seq > last + 1` 时抛 `FrameError`
- `StepStats`（原样移入）

- [ ] **Step 1: 写失败测试。**
  - `test_frame_round_trip_keeps_raw_payload`：`header={"type":"audio","seq":1,"ack":0}`、`payload=b"\x01\x02"*8000`，解码后头和负载都与原来相同；`len(frame) == 4 + len(json.dumps(header, separators=(",",":")).encode()) + 16000`，即负载没有经过 base64。
  - `test_decode_rejects_bad_frames`：`b"\x00\x00"`、头长度声明为 100 但实际只有 10 字节、头是 `[]`、头里没有 `type`、超过 `MAX_FRAME_BYTES` 的帧，每种都抛 `FrameError`。
  - `test_outbox_keeps_frames_until_confirmed`：连续 push 3 帧，返回 1、2、3；`confirm(1)` 之后 `after(0)` 的序号是 `[2, 3]`、`after(2)` 是 `[3]`；`confirm(3)` 之后 `after(0) == []`，`last_seq == 3`。
  - `test_outbox_limit`：`Outbox(max_bytes=10)`，push 一个 8 字节负载成功，再 push 一个 8 字节负载抛 `OverflowError`。
  - `test_inbox_dedups_and_rejects_gaps`：依次 `accept(1)` 返回 True、`accept(1)` 返回 False、`accept(2)` 返回 True、`accept(4)` 抛 `FrameError`；最后 `last == 2`。
- [ ] **Step 2:** 运行 `python3 -m pytest -q tests/test_model_wire.py`，期望全部 FAIL（ImportError）。
- [ ] **Step 3:** 实现上面的接口，移动 `StepStats`。
- [ ] **Step 4:** 运行全量测试，期望与基线持平，外加新增的 5 个通过。提交 `feat(model): binary frames and a replayable outbox for the model link`。

### Task 2: serving 约定与 host：发送不等待，事件按序消费

**Files:**
- Modify: `harness/bridge/serving.py`、`harness/bridge/host.py`
- Modify: `tests/test_host_clock.py`（不再调用 `process_output`，改为向假 port 的事件队列放一条输出，再调用 `next_output`）
- Test: `tests/test_host_pipeline.py`（在文件内定义 `FakeServing`：`send_*` 把请求追加到 `sent` 列表；`next_event` 从 `asyncio.Queue` 里取，取到异常实例就抛出；`close_session` 返回 `SessionClosed`，并往队列里放一个 `RuntimeError("closed")`）

**Interfaces:**
- Produces（`serving.py`）：`PROTOCOL_VERSION = "realtime-venus-harness/3"`；`ServingEvent = InputAccepted | ModelOutputStep | PrefillApplied`；`VenusOmniServingPort` 的方法改为：
  - `async open_session(request: OpenSession) -> SessionOpened`
  - `send_audio(request: AudioAppend) -> None`、`send_video_frame(request: VideoFrameAppend) -> None`、`send_video_segment(request: VideoSegmentAppend) -> None`、`send_prefill(request: PrefillAppend) -> None`、`send_playback_ack(request: PlaybackAck) -> None`（都是同步函数）
  - `async next_event(*, timeout_s: float | None = None) -> ServingEvent`（超时抛内置 `TimeoutError`）
  - `async close_session(request: CloseSession) -> SessionClosed`（在 `next_event` 阻塞时也能调用，并让 `next_event` 抛错）
  - Protocol 的 docstring 写明 D3–D5 的四条保证：发送有序、每条恰好送达一次；事件按模型发出的顺序到达；回执排在引用它的输出之前；取走一条输出就算消费，消费量决定模型还能走几步。删除 `PlaybackAccepted`、`PrefillDeferred`。
- Produces（`host.py` 的 `VenusOmniServingHost`）：
  - `async append_audio(chunk) -> None`、`append_video_frame(frame) -> None`、`append_video_segment(segment) -> None`：在 `_owner_lock` 内依次做可用性检查、分配 `event_seq`、把媒体交给 harness、检查 harness 序号连续、登记 `(event_seq, harness_sequence)` 到 `_unacked_inputs: deque`、调用 `serving.send_*`。整个过程不等待模型。
  - `async next_output(*, timeout_s=None) -> ProcessedOutput`：循环 `await serving.next_event()`（不持锁），然后在 `_owner_lock` 内处理这条事件——`InputAccepted` 按 D4 对号，`PrefillApplied` 建立后台生成状态（沿用现有的"应用之后"那段逻辑，另外要求此时 `_generation is None`，否则视为协议错误），`ModelOutputStep` 走原有的处理流程并返回。超时只针对"等到一条输出"。
  - `async begin_backend_turn() -> bool`：没有可用结果、已有生成、已有投递、或者已有 prefill 在途时，返回 False；否则取出下一个结果（过期的 feedback 丢弃后继续取下一个），发出 `PrefillAppend`，把 `_pending_prefill`（新增字段 `sent_at: float`）记为在途，返回 True。删除 `timeout_s` 参数。
  - `async acknowledge_playback(utterance_id, cumulative_played_chunks, *, at_ms=None) -> str`：本地校验 → `send_playback_ack` → `session.acknowledge_playback_chunks`，任何一步出错都作废会话。
  - `_monitor_feedback`：发出 prefill 时就启动；确认到达之前，以 `_pending_prefill.sent_at` 判断超时，到达之后以 `last_activity` 判断。
  - 删除：`process_output`、`active_backend_turn`、`queued_backend_results`、`_begin_lock`、`_active_pull_task`、`_pending_pulled_output`、`_model_owner_busy`、`_deferred_injection`、`_validate_playback_accepted`、prefill 的歧义重试、`_last_harness_sequence`（由 `_last_sent_harness_sequence` 和 `_unacked_inputs` 取代）。`attach_file` 的前置条件改为 `self._event_seq == 0 and self._generation is None`。

- [ ] **Step 1: 写失败测试**（`test_host_pipeline.py`；假 harness session 的 `ingest_user_audio` 每次返回下一个整数，从 1 开始）：
  - `test_appends_do_not_wait_for_acks`：连续两次 `append_audio` 都立即完成，此时事件队列为空；`fake.sent` 中依次是 `event_seq` 为 1、2 的两个 `AudioAppend`。
  - `test_acks_are_matched_in_order`：两次 append 后，依次放入 `InputAccepted(event_seq=1,input_seq=1)`、`(2,2)` 和一条 `input_seq_cutoff=2` 的输出；`next_output` 返回这条输出。
  - `test_out_of_order_ack_poisons`：放入 `InputAccepted(event_seq=2,input_seq=1)`，`next_output` 抛 `HostProtocolError`；之后 `append_audio` 抛 `RuntimeError`，消息含 "failed state"。
  - `test_output_cannot_cite_unacked_input`：只 append 一次、不放回执，放入一条 `cutoff=1` 的输出 → 抛 `HostProtocolError`。
  - `test_prefill_applies_in_stream_order`：假 session 的后台结果队列里有一个 `BackendInjection`。`begin_backend_turn()` 返回 True，且 `fake.sent[-1]` 是 `PrefillAppend`；紧接着再调一次返回 False。然后依次放入：一条前台输出（`turn_finished=True`）、`PrefillApplied(generation_id="backend-s:2", generation_epoch=2)`、一条该 generation 带音频的输出。两次 `next_output` 的返回值，`caused_by_work_id` 依次为 `""` 和这个 work 的 id。
  - `test_unanswered_prefill_times_out`：`feedback_timeout_s=0.2`，发出 prefill 后等 0.6 s → host 已 poison，并且调用过 `session.feedback_failed`。
  - `test_playback_ack_sent_without_waiting`：处理完一条带音频的后台输出后，`acknowledge_playback(utt, 1)` 立即返回；`fake.sent[-1]` 是 `PlaybackAck(cumulative_played_chunks=1)`。
  - `test_close_unblocks_next_event`：在另一个 task 里调用 `next_output()`；执行 `host.close()` 后，那个 task 在 1 s 内结束并抛错。
- [ ] **Step 2:** 运行这两个测试文件，期望 FAIL。
- [ ] **Step 3:** 按 Interfaces 实现。harness 文档里关于 host 方法的描述（`harness/README*.md` 第 81–84 行）仍然成立，不用改。
- [ ] **Step 4:** 运行全量测试，期望无新增失败。提交 `refactor(host): send without waiting and consume serving events in order`。

### Task 3: 模型会话核心：无锁记账、driver 独占模型、信用、离线与接回

**Files:**
- Modify: `demos/model/adapter.py`
- Modify: `demos/model/server.py`（删除全部 `/sessions...` 路由、`_body_with_session`、`_err`、`_ServerConfig.default_timeout_s`、`--default-timeout-s`。它们的调用对象在本 Task 里消失了；WebSocket 入口由 Task 4 加上）
- Modify: `demos/model/wire.py`（删除 `b64`、`unb64`、`parse_audio_append`、`parse_frame_append`、`generated_audio_dict`、`playback_accepted`；新增 `audio_header(sample_rate_hz: int = 24_000) -> dict`；其余 builder 返回的 dict 加上 `type` 字段）
- Modify: `tests/test_model_server.py`（`SessionOwnershipTests` 移到 `test_model_driver.py` 并改用新签名；依赖租约的 `test_requests_keep_session_alive`、`test_stale_session_is_replaced` 删除，由下面的生命周期测试取代）、`tests/test_model_stats.py`（`test_each_model_step_is_timed` 改为用 `receive` 喂原始 PCM，在 outbox 里等输出出现）、`tests/test_model_auth.py`（删除 `client.post("/sessions")` 那一行，Task 4 补上 WebSocket 的认证测试）
- Test: `tests/test_model_driver.py`

**Interfaces — Produces（`demos/model/adapter.py`）:**
- `OUTPUT_WINDOW = 4`、`RESUME_WINDOW_S = 60.0`
- 错误类型：`AdapterError.code = "protocol"`、`SessionBusy.code = "model_busy"`、`ModelMismatch.code = "model_mismatch"`、新增 `SessionLost.code = "session_lost"`、`ModelNotLoaded.code = "model_error"`。删除所有 `status_code`、`OutputTimeout`、`UnknownSession`、`IncarnationMismatch`、`SessionClosedError`（会话查找只剩 resume 一处，统一归为 `SessionLost`）。
- `_Session` 新增字段 `outbox: Outbox`、`inbox: Inbox`、`emitted: int`、`consumed: int`、`prefill_request: dict | None`、`link: asyncio.Event | None`、`expiry: asyncio.Task | None`、`failure: str | None`；删除 `queue`、`output_requested`、`output_inflight`、`last_seen`。
- `async open_session(req: dict, link: asyncio.Event) -> tuple[_Session, dict]`：在 `_open_lock` 内执行。已有会话在线 → `SessionBusy`；已有会话离线 → 先以 `"replaced"` 关闭它，再开新会话。
- `async resume_session(session_id: str, incarnation: int, *, ack: int, consumed: int, link: asyncio.Event) -> _Session`：会话不存在、incarnation 不符或已关闭 → `SessionLost`。若旧 `link` 仍在，对它 `set()`（让旧连接退出），取消 `expiry`，执行 `outbox.confirm(ack)`，更新 `consumed`。
- `detach(sess, link) -> None`：仅当 `sess.link is link` 时生效；置 `sess.link = None`，启动 `expiry` 任务（`RESUME_WINDOW_S` 后若仍离线，就以 `"link_lost"` 关闭会话）。
- `receive(sess, header: dict, payload: bytes) -> None`（同步函数）：先 `outbox.confirm(header["ack"])`，再取 `consumed = max(consumed, header["consumed"])`；可靠帧先经过 `inbox.accept` 去重；然后按 type 分发：`audio`、`video_frame` 先做 `_fence_event`，再写入缓冲，`input_seq += 1`，紧接着 push `input_accepted`，中间不 await；`prefill` 若 attempt 已应用过，就立即 push `deduplicated=True` 的 `prefill_applied`，否则存入 `prefill_request`；`playback_ack` 检查单调不减；`close` 以 `"client_close"` 调度 `close_session`；`state` 只更新 ack 和 consumed。最后唤醒 driver。违反约定时抛 `AdapterError`。
- `async close_session(sess, reason: str) -> None`：幂等。置 closed，调用 `set_session_stop()`，**等 driver 自己退出**（不 cancel，因为 cancel 停不住正在线程里跑的推理），然后 teardown；`failure` 为空时 push `closed`；最后对 `link` 执行 `set()`。
- driver 循环（替换 `_driver_loop`/`_step_once` 的入口条件）：
  1. 若 `prefill_request` 存在，且 `fg_gen` 和 `resume_gen` 都为 None：应用它（即原 `append_prefill` 里写 KV 的那段），push `prefill_applied`，然后回到循环开头。
  2. 否则，若 `emitted - consumed < OUTPUT_WINDOW`，且有 `resume_gen` 或已凑满一个 bucket：走一步，push `output`，`emitted += 1`。
  3. 否则沿用现有的唤醒/20 ms 轮询。
  driver 崩溃时，记录 `sess.failure`（异常的类名和消息），另起一个任务执行 `close_session(sess, "model_error")`，然后退出。不能在 driver 里直接 await `close_session`，因为后者要等 driver 结束。

- [ ] **Step 1: 写失败测试**（`test_model_driver.py`。模型用 `MagicMock`，需要时给 `streaming_generate` 设置 `side_effect`；link 用 `asyncio.Event()`；从 `sess.outbox.after(0)` 读出已发帧并检查顺序）：
  - `test_append_is_acknowledged_during_a_running_step`：`streaming_generate` 阻塞在一个 `threading.Event` 上。先送 1 s 音频，让 driver 进入这一步；在这一步完成之前再送第 2 段音频，outbox 里应立刻出现 `event_seq=2` 的 `input_accepted`。
  - `test_steps_without_any_pull`：送 1 s 音频后，1 s 内 outbox 里出现一条 `output`。
  - `test_ack_precedes_the_output_that_consumes_it`：在 outbox 中，`input_accepted(input_seq=1)` 的位置排在 `output(input_seq_cutoff=1)` 之前。
  - `test_output_window_bounds_unconsumed_steps`：模型每步都返回 speak 且不结束。一次送 10 s 音频，等 0.5 s 后 outbox 里恰好有 4 条 `output`；再 `receive` 一条 `state{consumed:2}`，0.5 s 后有 6 条。
  - `test_prefill_waits_for_the_generation_boundary`：模型前两步 speak 不结束，第 3 步 `end_of_turn`。送 3 s 音频，在出现第 1 条输出后送 prefill。断言 `prefill_applied` 排在 `turn_finished=True` 那条输出之后，并且排在任何 `generation_id` 以 `backend-` 开头的输出之前。
  - `test_prefill_is_idempotent_by_attempt_id`：同一个 attempt 先后发两次（传输层 `seq` 不同），第二次的回复为 `deduplicated=True`，`generation_id` 与第一次相同；带 backend 文本的 `streaming_prefill` 只被调用过一次。
  - `test_open_refuses_an_attached_session`：A 在线时，打开 B 抛 `SessionBusy`。
  - `test_open_preempts_a_detached_session`：A 执行 `detach` 后，打开 B 成功；A 已关闭；B 的 `incarnation == A.incarnation + 1`。
  - `test_detached_session_expires`：patch `RESUME_WINDOW_S=0.1`，`detach` 后 0.3 s，会话已关闭。
  - `test_resume_reattaches_and_supersedes`：`detach` 后在窗口内 resume，窗口过后会话仍然打开；再用另一个 link resume 一次，第一个 link 已被 `set()`。
  - `test_resume_rejects_unknown_session`：会话 id 不对、incarnation 不对，都抛 `SessionLost`。
  - `test_playback_ack_moving_backwards_is_rejected`：先回执 2，再回执 1 → 抛 `AdapterError`。
- [ ] **Step 2:** 运行，期望 FAIL。
- [ ] **Step 3:** 按 Interfaces 实现，并删除上面列出的旧代码。
- [ ] **Step 4:** 运行全量测试，期望无新增失败。提交 `refactor(model): driver owns the model, credits replace output pulls`。

### Task 4: 模型服务的 `WS /session` 入口

**Files:** Modify `demos/model/server.py`；Test：`tests/test_model_server.py`（新增 `SessionSocketTests`）、`tests/test_model_auth.py`

**Interfaces:**
- Consumes：Task 1 的帧函数、Task 3 的 adapter 方法。
- Produces：
  - `GET /healthz` 新增 `"protocol_version": wire.PROTOCOL_VERSION`。
  - `WS /session`：先用 `token_ok(websocket)` 校验，失败以 1008 关闭；accept 后读第一帧，`open` → `adapter.open_session`、回 `opened`；`resume` → `adapter.resume_session`、回 `resumed{ack: sess.inbox.last}`；其他 type → `error{code:"protocol"}`。握手阶段的 `AdapterError` → 发送 `error{code: exc.code}` 后以 1011 关闭。随后并行运行读取和写出两个协程。读取协程不断调用 `adapter.receive(...)`；抛 `AdapterError` 时发送 `error`，以 `"protocol_error"` 关闭会话。写出协程从对方的 `ack` 之后开始，依次写出 `sess.outbox.after(...)` 的帧，写出时把头里的 `ack` 填为 `sess.inbox.last`；没有新帧时等 `outbox.ready` 或 `link`。`link` 被置位后分三种情况：（a）会话仍然打开（说明被新连接取代）→ 立即停止，以 4001 关闭；（b）会话已关闭且 `failure` 非空 → 发送 `error{code:"model_error", error: failure}`，以 1011 关闭；（c）会话已关闭且 `failure` 为空 → 先写完剩余的帧（包括 `closed`），再以 1000 关闭。对方断开时两个协程都停止。无论哪种退出，最后都调用 `adapter.detach(sess, link)`。
  - `uvicorn.run(..., ws_ping_interval=5.0, ws_ping_timeout=5.0, ws_max_size=wire.MAX_FRAME_BYTES)`。

- [ ] **Step 1: 写失败测试**（用 FastAPI `TestClient.websocket_connect("/session", headers=...)`，配合 `send_bytes` / `receive_bytes`；adapter 用 `omni_adapter()`，并 patch `_build_adapter`）：
  - `test_session_socket_requires_token`：不带令牌、带错误令牌，都以 1008 断开；带正确令牌能完成 open。
  - `test_open_returns_capabilities_and_acks_input`：`opened` 的 `incarnation == 1`、`capabilities.raw_token_mode == "delta"`；送一帧 `audio`（seq=1）后收到 `input_accepted`，其中 `seq == 1`、`ack == 1`、`input_seq == 1`。
  - `test_resume_replays_unconfirmed_frames`：连接 A 执行 open、送 audio 并收到 `input_accepted`（服务端 seq=1），但之后发出的帧一直带 `ack=0`；关闭 A。连接 B 发 `resume{ack:0}`，收到 `resumed{ack:1}`，然后再次收到服务端 seq=1 的 `input_accepted`。
  - `test_resume_supersedes_a_live_connection`：A 保持连接，B 执行 resume；A 以 4001 断开，B 收到 `resumed`。
  - `test_handshake_errors_are_coded`：model 名不对 → `model_mismatch`；A 在线时 B 执行 open → `model_busy`；resume 一个不存在的会话 → `session_lost`。
  - `test_health_reports_protocol_version`（`test_model_auth.py`）。
- [ ] **Step 2:** 运行，期望 FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 全量测试无新增失败。提交 `feat(model): serve the session over one WebSocket with resume`。

### Task 5: 网页侧 `RemoteOmniServingPort`：WebSocket、重连接回、到达间隔统计

**Files:**
- Modify: `demos/model/client.py`（整体重写为 WebSocket 实现，删除 httpx 版本：`_request`、`_b64`、`_ub64`、`RemoteOutputTimeout`、HTTP 状态码映射、旧的 8 个方法）
- Modify: `demos/server/resources.py`（`check_model` 原来用"`/healthz` 里有没有 `token_ok`"判断服务端是否太旧，改为比较 `protocol_version == wire.PROTOCOL_VERSION`；不一致时报 "The model server at … runs a different version of this code; update both sides to the same commit."）
- Modify: `tests/test_model_errors.py`（删除三个 HTTP MockTransport 测试和 `test_adapter_mismatch_type_name_matches_client`——错误映射改用 `code` 字段，不再靠类名；保留 `test_session_reports_model_code_to_browser`；新的错误映射测试放在 `test_model_link.py`）、`tests/test_fetch_model.py`（`test_model_client_sends_token`、`test_model_client_ignores_proxy_env` 删除，由下面的测试替代；`test_outdated_server_is_named` 改为检查 `protocol_version`）
- Create: `tests/link_proxy.py`（测试辅助，不会被收集）：`async serve_model(adapter, *, token=None)`，一个 async context manager，在当前事件循环里用 `uvicorn.Server` 起 `create_app`（patch `_build_adapter`），端口 0，返回 base_url；`class LatencyProxy(target_port: int, one_way_ms: float = 0)`，提供 `async start() -> int`、`drop_all()`（对所有现有连接执行 `transport.abort()`）、`refuse_for(seconds: float)`（这段时间内新连接 accept 后立刻关闭，模拟 aTrust）。每个方向的字节按到达时间加上 `one_way_ms` 后、按顺序转发。
- Test: `tests/test_model_link.py`

**Interfaces — Produces（`demos/model/client.py`）:**
- `RemoteOmniServingPort(base_url: str, *, timeout_s: float = 60.0, length_penalty: float | None = None, token: str | None = None)`：构造参数不变。WebSocket 地址由 `base_url` 推出（`http`→`ws`、`https`→`wss`，加上 `/session`）。`timeout_s` 用作握手（open 和 close）的等待上限。
- 实现 Task 2 的全部 port 方法。`send_video_segment` 直接抛 `ValueError("this model server does not accept video segments")`：服务端声明 `video_segments=False`，host 不会调用它。
- `next_event` 取走一条 `ModelOutputStep` 时，执行 `consumed += 1` 并唤醒写出协程（让它发一条 `state`）。
- `link_up: bool`；`on_link_change: Callable[[bool], Awaitable[None]] | None`（由网页会话设置；调用时出现的异常不能影响连接管理）。
- 连接管理按 D6 实现，使用常量 `RECONNECT_DEADLINE_S`、`RECONNECT_DELAYS_S`、`CLOSE_TIMEOUT_S`。`connect(..., additional_headers=…, proxy=None, open_timeout=10, ping_interval=5, ping_timeout=5, max_size=wire.MAX_FRAME_BYTES)`。
- 错误映射：握手收到 `model_busy` 或 `model_mismatch` → `RemoteModelUnavailable(同名 code)`；open 时连不上 → `model_unreachable`；会话中收到 `session_lost`、超过重连期限、重放缓冲溢出 → `model_link_lost`；以 1008 关闭 → `RemoteServingError("model server rejected VENUS_MODEL_TOKEN")`；其他 `error` → `RemoteServingError(error)`。`close_session` 之后，`next_event` 抛 `RemoteSessionClosed`。
- 到达间隔统计：记录每条 `output` 到达的单调时钟时间。`aclose()` 时在 `demos.model.client` logger 上以 INFO 输出一行：`model output gaps: steps <summary> speech <summary>`。summary 用 `StepStats.summary()`；"speech" 只统计同一 `generation_id` 内、相邻两条带音频输出之间的间隔。

- [ ] **Step 1: 写失败测试**（`test_model_link.py`；服务端是 `serve_model(omni_adapter())`；需要断线的测试，port 连到 `LatencyProxy` 上）：
  - `test_events_arrive_in_emit_order`：open 后送 3 段 1 s 音频，取出的事件里 `InputAccepted` 依次为 1、2、3，每条输出的 `input_seq_cutoff` 都不超过它之前已经取到的最大 `input_seq`；音频事件的 `data` 是原始字节，长度为偶数。
  - `test_open_errors_map_to_codes`：第一个 port 在线时第二个 port open → `model_busy`；model 名不对 → `model_mismatch`；目标端口上没有服务 → `model_unreachable`。
  - `test_wrong_token_is_named`：服务端 `token="secret"`，port 用 `"wrong"` → `RemoteServingError`，消息含 `VENUS_MODEL_TOKEN`。
  - `test_link_ignores_proxy_env`：设置 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 为 `socks5://127.0.0.1:1`，open 仍然成功。
  - `test_reconnect_resumes_without_loss`：送 2 段音频 → `proxy.drop_all()` → 再送 2 段 → 取到的 `InputAccepted` 恰好是 1..4，各出现一次、按顺序；`on_link_change` 依次收到 `False`、`True`。
  - `test_reconnect_deadline_ends_session`：patch `RECONNECT_DEADLINE_S=1.0`，执行 `drop_all()` 并 `refuse_for(3)` → `next_event` 抛 `RemoteModelUnavailable`，`code == "model_link_lost"`。
  - `test_close_during_outage_gives_up`：patch `CLOSE_TIMEOUT_S=0.5`，断线并拒绝重连期间调用 `close_session`，1 s 内抛 `RemoteServingError`；正在等待的 `next_event` 抛 `RemoteSessionClosed`。
  - `test_gaps_are_logged_at_close`：`assertLogs("demos.model.client", "INFO")` 中有一行包含 `model output gaps`。
- [ ] **Step 2:** 运行，期望 FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 全量测试无新增失败。提交 `feat(web): model link over WebSocket with resume`。

### Task 6: 网页会话与页面：断线提示、实时输入丢弃、新的后台轮次调用

**Files:** Modify `demos/server/session.py`、`demos/static/app.js`、`demos/static/i18n.js`；Test：`tests/test_web_link.py`

**Interfaces:**
- Consumes：Task 2 的 `begin_backend_turn() -> bool`，Task 5 的 `on_link_change`。
- Produces：
  - `WebAgentSession.run()`：打开 host 之前设置 `self.resources.serving.on_link_change = self._link_changed`。
  - `async WebAgentSession._link_changed(up: bool) -> None`：更新 `self._link_up`（初始为 True），并发送 `{"type": "model_link", "state": "up" | "down"}`。
  - `handle()`：在现有的 input_source 检查之后，如果 `command in {"audio", "video_frame"} and not self._link_up`，直接 return，不报错。文件模式（`_feed_video`、`_video_idle`）直接调用 host，不经过这里，所以照常缓冲。
  - `output()`：`if finished and await self.host.begin_backend_turn(): finished = False`。删除对 `queued_backend_results`、`active_backend_turn` 的判断，以及捕获 `TimeoutError` 的那段。
  - `app.js`：`notify(message, error = false, sticky = false)`，sticky 为真时不设自动隐藏计时器。收到 `model_link` 且为 down → `notify(t("modelReconnecting"), true, true)`；为 up → `notify(t("modelReconnected"))`。`fatal_error` 的已知 code 表里加上 `model_link_lost: "modelLinkLost"`。
  - `i18n.js`（中/英）：
    - `modelReconnecting`：「与模型服务器的连接中断，正在重连；这段时间你说的话模型听不到。」/ "Lost the connection to the model server; reconnecting. The model can't hear you until it's back."
    - `modelReconnected`：「已重新连接模型服务器。」/ "Reconnected to the model server."
    - `modelLinkLost`：「与模型服务器的连接中断超过 45 秒，本次会话已结束，请重新开始。」/ "The connection to the model server was down for more than 45 seconds, so this conversation ended. Start a new one."
    - `modelBusy` 改为：「模型正被另一个连接使用。如果刚重启过网页服务，请约 10 秒后重试。」/ "The model is in use by another connection. If you just restarted the web service, try again in about 10 seconds."

- [ ] **Step 1: 写失败测试：**
  - `test_live_input_is_dropped_while_link_down`：`host` 用 MagicMock，其中 `append_audio` 是 AsyncMock。在 `_link_up=False` 时，`handle("audio", 合法的 pcm16 消息)` 不会调用 `append_audio`，也不抛错；改为 `_link_up=True` 后再调用一次，`append_audio` 被 await 了一次。
  - `test_link_change_is_reported`：`await session._link_changed(False)` 后，`websocket.send_json` 收到 `{"type": "model_link", "state": "down"}`。
- [ ] **Step 2:** 运行，期望 FAIL。
- [ ] **Step 3:** 实现。
- [ ] **Step 4:** 全量测试无新增失败。提交 `feat(web): show model link outages and drop live input meanwhile`。

### Task 7: 在 Mac 上模拟 345 ms 往返和断线的浸泡测试

**Files:** Create `tests/link_soak.py`（复用 `tests/link_proxy.py`）

**Interfaces — Consumes：** Task 2 的 host、Task 3 的 adapter、Task 5 的 port 和 `link_proxy`。

- 假模型 `PacedModel`：`streaming_generate` 按 `(0.4, 0.4, 0.9, 0.4, 1.36)` s 循环 `time.sleep`，这组时长对应测得的 p50/p95/最大值。每步返回 speak，带 1 s 的 24 kHz 非静音波形，每 10 步 `end_of_turn` 一次。`total_ids` 每步增加 3 个 id。还要提供 `prepare`、`streaming_prefill`；`official_duplex` 指向自己。
- 假 harness session：ingest 返回连续整数；`consume_total_ids` 返回 `ModelStep("x", (), False, 1)`；其余方法用 AsyncMock。playback sink 记录每块音频的到达时间。
- 流程：`LatencyProxy(one_way_ms=172)`；`port.open_session` 后，按 `test_host_clock.py` 的方式直接构造 host。按事件循环时钟每 1.0 s 送 1 帧 JPEG（用 PIL 生成 16×16）和 1 s 音频，同时另一个 task 持续调用 `host.next_output`。

- [ ] **Step 1: 写测试：**
  - `test_camera_pace_over_a_345_ms_link`：运行 30 s。同一 generation 内相邻语音块到达间隔的中位数在 0.9–1.1 s 之间，p95 ≤ 1.4 s（1.36 s 那一步本身会出现在 p95 里）；收到的输出数 ≥ 送出的输入数 − 3，说明没有积压；`host._poisoned is None`。
  - `test_link_drop_mid_conversation_resumes`：常量 `OUTAGE_S = 3.0`。第 10 s 时执行 `proxy.drop_all()` 并 `refuse_for(OUTAGE_S)`，模拟 aTrust。结束时 host 没有 poison（任何丢失或重复的事件都会触发 poison，所以这一条本身就是完整性检查）；拒绝结束后 `OUTAGE_S + 2 + 1` s 内重新收到输出；`on_link_change` 依次收到 `False`、`True`。
- [ ] **Step 2:** 运行 `python3 -m pytest -q tests/link_soak.py`，期望两项都 PASS。如果 Task 2–5 正确，这里不需要再改产品代码；如果失败，回到对应的 Task 修。
- [ ] **Step 3:** 把 `OUTAGE_S` 临时改为 16 再跑一次（对应实测的 16 s），期望 PASS，然后改回 3.0。
- [ ] **Step 4:** 提交 `test: soak the model link at 345 ms with an aTrust-style outage`。

### Task 8: 文档

**Files:** Modify `demos/README.md`、`demos/README_ZH.md`、`harness/README.md`、`harness/README_ZH.md`、`deploy/web/.env.example`

- [ ] **Step 1:** `demos/README*`：
  - 第 24 行附近：模型接口由 HTTP 改为"一条 WebSocket（`/session`），另有 `/healthz`、`/tokenizer`、`/stats` 三个 HTTP 接口"。
  - "一个活跃会话"那一段改为："网页服务断开后，模型保留这个会话 60 秒，等它重连接回；这期间如果网页服务重启后新开会话，旧会话立即让位。"
  - "延迟"一段改为："输入不等回复、输出由模型推送，网络往返只让回复整体晚到一个往返的时间，不影响说话的节奏。连接中断时页面会提示，45 秒内恢复就接着原来的对话。"保留 curl 测往返的那句。
  - `MODEL_TIMEOUT_S` 一行改为"开会话、关会话时等待模型的上限（秒）"。
- [ ] **Step 2:** `harness/README*` 第 33 行：把 "HTTP model service" 改为 "WebSocket model service"。
- [ ] **Step 3:** `deploy/web/.env.example` 中 `MODEL_TIMEOUT_S` 的注释，与上面的说法保持一致。
- [ ] **Step 4:** 执行 `grep -rn -E "15 秒|15 seconds|/sessions|output pull|输出拉取" demos harness deploy`，期望无输出（`docs/superpowers` 下的历史计划不改）。提交 `docs: model link over WebSocket`。

### Task 9: 部署与验收（服务器上的命令由用户执行）

- [ ] **Step 1:** 在 Mac 上全量测试和 `tests/link_soak.py` 都通过。
- [ ] **Step 2: 同步代码到服务器**（用你平时的方式）。需要的文件是：`demos/__init__.py`、`demos/variants.py`、`demos/model/`、`demos/requirements-model.txt`、`demos/constraints-model.txt`、`deploy/model/`。依赖文件没有变，所以镜像的 pip 层会命中缓存，重建时不需要联网。如果服务器上的构建缓存已经清过，就在 `deploy/model/.env` 里设 `PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`。
- [ ] **Step 3: 用户在服务器的 `deploy/model` 目录执行**：`docker compose up -d --build`，然后 `docker compose logs --tail 20 model` 和 `curl -s http://127.0.0.1:8031/healthz`，并把输出贴回来。期望日志里有 `adapter ready`，healthz 输出里有 `"protocol_version":"realtime-venus-harness/3"`。从这一步开始，旧网页服务就连不上新模型了，要尽快做 Step 4。
- [ ] **Step 4:** Mac 上在 `deploy/web` 目录执行 `docker compose up -d --build`；`docker compose logs --tail 20 web` 里不应有启动错误（如果有版本不一致，会明确报出来）。
- [ ] **Step 5: 验收 A1（摄像头模式）。** 打开页面，摄像头模式下对话至少 3 分钟，让模型回答多次（累计不少于 60 个语音块），然后停止会话，执行 `docker compose logs web | grep "model output gaps"`。期望 speech 的 p50 在 0.9–1.1 s 之间、p95 ≤ 1.3 s；steps 的 p50 在 0.9–1.1 s 之间。再在 Mac 上执行 `curl -s -H "Authorization: Bearer $VENUS_MODEL_TOKEN" http://10.194.99.8:8031/stats`，期望 steps 的 p95 < 1000 ms，用来确认瓶颈不在模型。
- [ ] **Step 6: 验收 A2（纯语音模式）。** 同上，期望值相同。
- [ ] **Step 7: 验收 A3（断线演练，可选）。** 对话中在 Mac 上断开 aTrust 约 15 s 再连上。期望：页面出现"正在重连"提示 → 恢复后提示"已重新连接"；问"我刚才问了什么"，模型能答出断线前的话题。
- [ ] **Step 8: 验收 A4。** 连续对话 15 分钟，`docker compose logs web | grep -i -E "disconnected|fatal"` 无输出。
- [ ] **Step 9: 清理（由用户决定）。** 删除分支 `claude/quizzical-austin-7e7b76` 及其 worktree `.claude/worktrees/quizzical-austin-7e7b76`（见 D8）。断连调查留下的 `probe.py`、`race.py` 针对的是旧 HTTP 路由，从此不再适用。
