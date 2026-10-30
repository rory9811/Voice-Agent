import { AudioPlayer, encode } from "./audio.js";
import { MediaCapture } from "./capture.js";
import { enhanceSelects } from "./select.js";
import { TaskList } from "./tasks.js";
import { MicrophoneWaveform } from "./waveform.js";

const PRODUCT = "Realtime Venus";
const AGENTS = {
  codex: {
    name: "Codex",
    login: "docker compose exec web codex login --device-auth",
    help: "在本机的 deploy/web 目录运行下面的命令，完成登录后点“重新检查登录”：",
    command: (binary) => [binary, "app-server"],
    efforts: ["minimal", "low", "medium", "high", "xhigh"],
  },
  claude: {
    name: "Claude Code",
    login: "docker compose exec -it web claude auth login",
    help: "在本机的 deploy/web 目录运行下面的命令，用 Claude 账号登录后点“重新检查登录”：",
    command: (binary) => [binary],
    efforts: ["low", "medium", "high", "xhigh", "max"],
  },
};
const EFFORT_NAMES = { minimal: "极简", low: "低（较快）", medium: "中", high: "高", xhigh: "很高", max: "最高" };
const LOGIN_STATES = {
  logged_in: "已登录，可以执行后台任务",
  not_logged_in: "尚未登录",
  not_subscription: "不是订阅登录，请用 Claude 账号重新登录",
  missing: "找不到程序，请在“任务执行”里检查程序路径",
  timeout: "登录检查超时，请确认本机的执行器能正常运行后重试",
  error: "无法确认登录状态，请确认本机的执行器能正常运行后重试",
};
const DEVICE_ERRORS = {
  NotAllowedError: "请在浏览器中允许使用麦克风和摄像头，然后重试。",
  PermissionDeniedError: "请在浏览器中允许使用麦克风和摄像头，然后重试。",
  NotFoundError: "没有找到输入设备，请检查麦克风或摄像头。",
  NotReadableError: "设备正被其他程序占用，请关闭后重试。",
  InsecureContextError: "请通过 localhost 或 HTTPS 打开这个页面，才能使用设备。",
};
const CONNECTION_LOST = "连接断开了。请确认本地网页服务仍在运行、到模型服务器的 VPN 连接正常，然后重试。";
const FATAL_ERRORS = {
  busy: "已有一段对话在进行，请等它结束后再试。",
  configuration: "请先完成设置，再开始对话。",
  model_busy: "模型正被另一个连接使用。如果刚重启过网页服务，请约 10 秒后重试。",
  model_link_lost: "与模型服务器的连接中断，无法接回本次对话，请重新开始。",
  model_mismatch: "模型服务器已换成其他模型，请在 deploy/web 目录运行 docker compose restart。",
  model_unreachable: "连不上模型服务器，请检查 VPN 连接，并确认服务器上的模型仍在运行。",
};
const HEADSET_TIP = "voice-headset-tip-closed";

const $ = (id) => document.getElementById(id);
const state = {
  mode: "camera",
  inputModes: [],
  model: "",
  phase: "idle",
  connected: false,
  connecting: false,
  stopping: false,
  epoch: 0,
  socket: null,
  sessionId: null,
  token: null,
  startedAt: 0,
  configured: null,
  checkingSettings: false,
  muted: false,
  speakerMuted: false,
  transcript: new Map(),
  lastBackpressure: 0,
  online: true,
  configuration: null,
  agent: "codex",
  agentLogin: { agent: "", state: "" },
  roleModels: {},
  headsetTipClosed: false,
};
try {
  state.headsetTipClosed = localStorage.getItem(HEADSET_TIP) === "1";
} catch {}
const devices = { microphone: "", camera: "" };
try {
  Object.assign(devices, JSON.parse(localStorage.getItem("voice-devices") || "{}"));
} catch {}

enhanceSelects();
const waveform = new MicrophoneWaveform($("wave-canvas"));
const tasks = new TaskList({ send, download: downloadArtifact, onChange: () => render() });
const player = new AudioPlayer({
  ack: send,
  onText: appendText,
  onActivity: (active) => {
    if (state.connected) setPhase(active ? "speaking" : "listening");
  },
  onError: () => {
    notify("浏览器无法播放音频，请检查音频权限。", true);
    void stopSession().catch(() => {});
  },
});
const capture = new MediaCapture($("preview"), {
  onChunk: sendInput,
  onEnded: () => {
    notify("输入设备已断开，准备好后请重新开始。", true);
    void stopSession().catch(() => {});
  },
  onBackpressure: backpressure,
});

let toastTimer;
function notify(message, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  toastTimer = setTimeout(() => ($("toast").hidden = true), error ? 7000 : 4000);
}
function errorText(error) {
  return DEVICE_ERRORS[error?.name] || error?.message || "无法开始对话，请重试。";
}
function send(message) {
  if (state.socket?.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(message));
    return true;
  }
  return false;
}
function backpressure() {
  if (Date.now() - state.lastBackpressure > 20000) {
    state.lastBackpressure = Date.now();
    notify("网络跟不上，部分输入被跳过。");
  }
}
function sendInput(pcm, jpeg) {
  if (!state.connected) return;
  if (state.socket.bufferedAmount > 256 * 1024) {
    backpressure();
    return;
  }
  if (jpeg) send({ type: "video_frame", format: "jpeg", data: encode(jpeg) });
  send({
    type: "audio",
    format: "pcm16",
    sample_rate: 16000,
    data: encode(pcm),
    // Lets the server log whether model audio was audible while this was captured.
    playing: player.audibleSources.size > 0,
  });
}

// ---------- The page ----------

function clock() {
  const seconds = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function setControl(id, { label, icon, disabled, pressed }) {
  const button = $(id);
  button.querySelector("span").textContent = label;
  if (icon) button.querySelector("use").setAttribute("href", `#i-${icon}`);
  button.disabled = disabled;
  if (pressed !== undefined) button.setAttribute("aria-pressed", String(pressed));
}
function phaseText() {
  if (state.connecting) return "连接中";
  if (state.stopping) return "正在结束";
  if (state.phase === "speaking") return "正在回应";
  if (state.connected) return "正在听";
  return !state.online ? "服务不可用" : state.configured === false ? "设置未完成" : "空闲";
}
function render() {
  const busy = state.connected || state.connecting || state.stopping;
  const camera = state.mode === "camera";
  const open = Boolean(capture.stream) && !state.stopping;
  document.body.dataset.mode = state.mode;
  for (const button of document.querySelectorAll(".mode")) {
    const available = state.inputModes.includes(button.dataset.mode);
    button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
    button.disabled = busy || !available;
    button.title = available ? "" : "当前部署不支持这个模式";
  }
  $("preview").hidden = !(camera && open);
  $("screen-empty").hidden = camera && open;
  waveform.setState(open, state.muted);
  $("wave-canvas").hidden = !open;
  $("wave-empty").hidden = open;
  $("wave-empty-text").textContent = camera ? "麦克风波形" : "开始对话后打开麦克风";
  $("wave-canvas").parentElement.classList.toggle("muted", state.muted);
  $("lamp").hidden = !state.connected;
  if (state.connected) $("lamp-time").textContent = clock();
  $("camera-field").hidden = !camera;
  $("microphone-device").disabled = $("camera-device").disabled = busy;

  const start = {
    label: state.checkingSettings
      ? "检查中…"
      : state.connecting
        ? "连接中…"
        : state.connected
          ? "对话中"
          : state.stopping
            ? "正在结束…"
            : state.configured === false
              ? "完成设置"
              : "开始",
    disabled: busy || state.checkingSettings || !state.inputModes.length,
  };
  setControl("start", start);
  $("start").toggleAttribute("data-live", state.connected);
  setControl("stop", {
    label: state.connecting ? "取消" : "结束",
    disabled: !(state.connected || state.connecting),
  });
  setControl("mute-mic", {
    label: state.muted ? "取消麦克风静音" : "麦克风静音",
    icon: state.muted ? "mic-off" : "mic",
    disabled: !state.connected,
    pressed: state.muted,
  });
  setControl("mute-speaker", {
    label: state.speakerMuted ? "取消扬声器静音" : "扬声器静音",
    icon: state.speakerMuted ? "sound-off" : "sound",
    disabled: !state.connected,
    pressed: state.speakerMuted,
  });

  $("stat-phase").textContent = phaseText();
  $("stat-model").textContent = state.model || "—";
  $("stat-model").title = state.model;
  $("stat-time").textContent = state.connected ? clock() : "—";
  $("stat-mic").textContent = state.connected ? (state.muted ? "已静音" : "开启") : "—";
  $("stat-speaker").textContent = state.connected ? (state.speakerMuted ? "已静音" : "开启") : "—";
  $("stat-tasks").textContent = tasks.total ? `进行中 ${tasks.running} / 共 ${tasks.total}` : "0";

  const badge = $("service-state");
  const needsSetup = !busy && state.online && state.configured === false;
  badge.dataset.state = !state.online ? "offline" : state.connected ? "live" : needsSetup ? "setup" : "";
  badge.textContent = !state.online
    ? "服务不可用"
    : state.stopping
      ? "正在结束"
      : state.connecting
        ? "连接中"
        : state.connected
          ? "对话中"
          : needsSetup
            ? "设置未完成"
            : "就绪";

  $("notice").hidden = !needsSetup && state.headsetTipClosed;
  $("notice-text").textContent = needsSetup
    ? "还差几项设置才能开始对话。"
    : "外放时模型可能听到自己的声音并被打断，建议戴耳机。";
  $("notice-action").hidden = !needsSetup;
  $("notice-close").hidden = needsSetup;

  $("save-transcript").disabled = !state.transcript.size;
  $("clear-transcript").disabled =
    state.connecting || state.stopping || (!state.transcript.size && !state.connected);
}
function setPhase(phase) {
  state.phase = phase;
  document.body.dataset.phase = phase;
  render();
}
function setMode(mode) {
  if (state.connected || state.connecting || state.stopping || !state.inputModes.includes(mode)) return;
  state.mode = mode;
  render();
}
async function refreshStatus(fresh = false) {
  try {
    const response = await fetch(`/api/status${fresh ? "?fresh=true" : ""}`, { cache: "no-store" });
    if (!response.ok) throw Error();
    const status = await response.json();
    state.online = true;
    state.inputModes = status.input_modes;
    state.model = status.model;
    if (!state.inputModes.includes(state.mode) && !state.connected && !state.connecting)
      state.mode = state.inputModes[0];
    state.configured = status.configured;
    return status;
  } catch {
    state.online = false;
    return null;
  } finally {
    render();
  }
}

// ---------- The conversation ----------

async function startSession() {
  if (state.connected || state.connecting || state.stopping || state.checkingSettings) return;
  if (state.configured === false) {
    await openSettings(true);
    return;
  }
  // Unlock playback during the user's click, before any asynchronous network request.
  const primed = player.prime();
  state.checkingSettings = true;
  const epoch = ++state.epoch;
  render();
  try {
    await primed;
    const status = await refreshStatus(true);
    if (epoch !== state.epoch) return;
    if (!status) throw Error(CONNECTION_LOST);
    if (!status.configured) {
      state.checkingSettings = false;
      render();
      await openSettings(true);
      return;
    }
    if (status.busy) throw Error(FATAL_ERRORS.busy);
    state.checkingSettings = false;
    state.connecting = true;
    setPhase("connecting");
    tasks.clear();
    clearTranscript();
    state.muted = false;
    const started = await capture.start(state.mode, devices);
    if (!started || epoch !== state.epoch) return;
    render();
    void enumerateDevices();
    const ready = await connect(epoch);
    if (epoch !== state.epoch) return;
    state.sessionId = ready.session_id;
    state.token = ready.upload_token;
    state.connected = true;
    state.connecting = false;
    state.startedAt = Date.now();
    capture.active = true;
    setPhase("listening");
  } catch (error) {
    if (epoch !== state.epoch) return;
    await stopSession().catch(() => {});
    if (error.code === "configuration") {
      state.configured = false;
      await openSettings(true);
    } else notify(errorText(error), true);
  } finally {
    state.checkingSettings = false;
    render();
  }
}
function connect(epoch) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(
      `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?mode=${state.mode}`,
    );
    state.socket = socket;
    let settled = false;
    let serverRejected = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(Error("无法开始对话，请重试。"));
        socket.close();
      }
    }, 180000);
    socket.onmessage = (event) => {
      if (epoch !== state.epoch) return;
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.type === "ready") {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          resolve(message);
        }
      } else if (message.type === "fatal_error") {
        serverRejected = true;
        const error = Error(FATAL_ERRORS[message.code] || message.error || "无法开始对话，请重试。");
        error.code = message.code;
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(error);
        } else if (error.code === "configuration") {
          state.configured = false;
          void stopSession().then(() => openSettings(true)).catch(() => {});
        } else {
          notify(errorText(error), true);
          void stopSession().catch(() => {});
        }
      } else if (message.type === "playback") player.enqueue(message);
      else if (message.type === "text") appendText(message.text, message.utterance_id);
      else if (message.type === "works") tasks.update(message.works);
      else if (message.type === "model_link")
        notify(
          message.state === "down"
            ? "与模型服务器的连接中断，正在重连；这段时间你说的话模型听不到。"
            : "已重新连接模型服务器。",
          message.state === "down",
        );
      else if (message.type === "command_error") notify(message.error, true);
    };
    socket.onerror = () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(Error(CONNECTION_LOST));
      }
    };
    socket.onclose = () => {
      clearTimeout(timer);
      if (!settled) {
        settled = true;
        reject(Error(CONNECTION_LOST));
      }
      if (epoch === state.epoch && !state.stopping && !serverRejected) {
        notify(CONNECTION_LOST, true);
        void stopSession(false).catch(() => {});
      }
    };
  });
}
async function stopSession(notifyServer = true) {
  if (state.stopping) return state.stopPromise;
  state.stopping = true;
  state.connecting = false;
  state.connected = false;
  state.epoch++;
  setPhase("ending");
  player.clear();
  capture.active = false;
  const socket = state.socket;
  state.stopPromise = (async () => {
    const stopped = capture.stop();
    try {
      if (socket && socket.readyState !== WebSocket.CLOSED) {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            socket.close();
            reject(Error("上一段对话还在结束，请稍后再试。"));
          }, 45000);
          socket.addEventListener(
            "close",
            () => {
              clearTimeout(timer);
              resolve();
            },
            { once: true },
          );
          if (notifyServer && socket.readyState === WebSocket.OPEN)
            socket.send(JSON.stringify({ type: "stop_session" }));
          else socket.close();
        });
      }
      await stopped;
    } finally {
      state.socket = null;
      state.sessionId = null;
      state.token = null;
      state.stopping = false;
      tasks.end();
      setPhase("idle");
      void refreshStatus();
    }
  })();
  try {
    await state.stopPromise;
  } catch (error) {
    notify(errorText(error), true);
    throw error;
  } finally {
    state.stopPromise = null;
  }
}
function clearTranscript() {
  state.transcript.clear();
  $("transcript").replaceChildren($("transcript-empty"));
  $("transcript-empty").hidden = false;
  render();
}
function appendText(text, id) {
  if (!text) return;
  const key = id || `text-${state.transcript.size}`;
  // Follow new text only for someone already at the end, not someone reading back.
  const log = $("transcript");
  const following = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  let entry = state.transcript.get(key);
  if (!entry) {
    $("transcript-empty").hidden = true;
    const turn = document.createElement("article");
    turn.className = "turn";
    const mark = document.createElement("span");
    mark.className = "turn-icon";
    mark.innerHTML = '<svg class="icon"><use href="#i-wave" /></svg>';
    const body = document.createElement("div");
    body.className = "turn-body";
    const head = document.createElement("div");
    head.className = "turn-head";
    const name = document.createElement("strong");
    name.textContent = PRODUCT;
    const time = document.createElement("time");
    time.textContent = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    head.append(name, time);
    const paragraph = document.createElement("p");
    body.append(head, paragraph);
    turn.append(mark, body);
    log.append(turn);
    entry = { text: "", paragraph, time: Date.now() };
    state.transcript.set(key, entry);
    render();
  }
  entry.text += text;
  entry.paragraph.textContent = entry.text;
  if (following) log.scrollTop = log.scrollHeight;
}
function saveBlob(blob, name) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
async function downloadArtifact(workId, artifact) {
  const unavailable = "结果只能在对应的对话进行中下载。";
  if (!state.sessionId || !state.token) {
    notify(unavailable);
    return;
  }
  try {
    const response = await fetch(
      `/api/sessions/${encodeURIComponent(state.sessionId)}/works/${encodeURIComponent(workId)}/artifacts/${artifact.index}`,
      { headers: { "X-Venus-Session-Token": state.token } },
    );
    if (!response.ok) throw Error(unavailable);
    saveBlob(await response.blob(), artifact.name);
  } catch (error) {
    notify(errorText(error), true);
  }
}
async function enumerateDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  try {
    const available = await navigator.mediaDevices.enumerateDevices();
    for (const [key, kind, label] of [
      ["microphone", "audioinput", "麦克风"],
      ["camera", "videoinput", "摄像头"],
    ]) {
      const select = $(`${key}-device`);
      select.replaceChildren(new Option("系统默认", ""));
      available
        .filter((device) => device.kind === kind)
        .forEach((device, i) => select.add(new Option(device.label || `${label} ${i + 1}`, device.deviceId)));
      select.value = devices[key];
    }
  } catch {}
}

// ---------- Settings ----------

function fillRoleSelects(agent) {
  const { name, efforts } = AGENTS[agent];
  for (const select of document.querySelectorAll("select.provider")) {
    const gemini = select.value === "gemini";
    select.replaceChildren(new Option(name, agent), new Option("Gemini", "gemini"));
    select.value = gemini ? "gemini" : agent;
  }
  for (const select of document.querySelectorAll("select.effort")) {
    const current = select.value;
    const options = efforts.map((effort) => new Option(EFFORT_NAMES[effort], effort));
    if (select.id === "agent-effort") options.unshift(new Option(`${name} 默认`, ""));
    select.replaceChildren(...options);
    select.value = options.some((option) => option.value === current) ? current : "low";
  }
  $("agent-binary-label").textContent = `${name} 程序路径`;
}
function storeAgentFields(agent) {
  const section = state.configuration.data[agent];
  section.model = $("agent-model").value.trim() || null;
  section.effort = $("agent-effort").value || null;
  section.command = AGENTS[agent].command($("agent-binary").value.trim());
}
function loadAgentFields(agent) {
  const section = state.configuration.data[agent];
  $("agent-model").value = section.model || "";
  $("agent-effort").value = section.effort || "";
  $("agent-binary").value = section.command[0];
}
function updateRoleFields() {
  const busy = state.connected || state.connecting || state.stopping;
  $("routing-options").hidden = $("routing-mode").value !== "auto";
  for (const role of ["routing", "response", "multimodal"]) {
    const gemini = $(`${role}-provider`).value === "gemini";
    $(`${role}-effort`).closest("label").hidden = gemini;
    $(`${role}-effort`).disabled = busy || gemini;
    $(`${role}-model`).placeholder = gemini ? $("gemini-model").value : "留空使用任务模型";
  }
  $("multimodal-audio-hint").hidden = $("multimodal-provider").value === "gemini";
}
function renderLogin() {
  const agent = $("task-agent").value;
  const known = state.agentLogin.agent === agent;
  const login = known ? state.agentLogin.state : "";
  const unsaved = agent !== state.configuration?.data?.agent;
  $("login-state").textContent = !known
    ? `正在检查 ${AGENTS[agent].name} 登录…`
    : login === "logged_in" && unsaved
      ? "已登录，保存设置后生效"
      : LOGIN_STATES[login];
  $("login-state").dataset.state = login;
  $("login-steps").hidden = !["not_logged_in", "not_subscription"].includes(login);
  $("login-help").textContent = AGENTS[agent].help;
  $("login-command").textContent = AGENTS[agent].login;
}
async function checkLogin() {
  const agent = $("task-agent").value;
  state.agentLogin = { agent: "", state: "" };
  renderLogin();
  let result = { state: "error" };
  try {
    const response = await fetch(`/api/agent-login?agent=${agent}`, { cache: "no-store" });
    if (response.ok) result = await response.json();
  } catch {}
  // The user may have switched again while this was running.
  if ($("task-agent").value !== agent) return;
  state.agentLogin = { agent, state: result.state };
  renderLogin();
}
function settingsIssues(serverProblems = []) {
  const issues = [];
  const add = (id, message) => {
    if (!issues.some((issue) => issue.id === id)) issues.push({ id, message });
  };
  if (!$("workspace").value.trim()) add("workspace", "请填写任务目录。");
  if (!$("agent-binary").value.trim()) add("agent-binary", "请填写执行器的程序路径。");
  const providers = ["response", "multimodal"].map((role) => $(`${role}-provider`).value);
  if ($("routing-mode").value === "auto") providers.push($("routing-provider").value);
  const usesGemini = providers.includes("gemini");
  if (usesGemini && !state.configuration?.gemini_key_configured && !$("gemini-key").value.trim())
    add("gemini-key", "你选择了 Gemini，请填写 API Key。");
  for (const node of $("settings-form").querySelectorAll("input,select")) {
    if (node.disabled || node.closest("[hidden]") || (!usesGemini && node.id === "gemini-model")) continue;
    if (!node.checkValidity() && !issues.some((issue) => issue.id === node.id)) {
      const name = node.closest("label")?.querySelector("span")?.textContent || node.id;
      issues.push({ id: node.id, message: `${name}：请填写有效的值。` });
    }
  }
  // The panel at the top already says what to do about the login.
  const login = state.configuration?.agent_login;
  for (const message of serverProblems || []) {
    if (message === login?.message && login.state !== "missing") continue;
    if (message.includes("任务工作目录")) add("workspace", "请填写任务目录。");
    else if (message.includes("找不到")) add("agent-binary", "找不到执行器程序，请检查程序路径。");
    else if (message.includes("Gemini API Key")) add("gemini-key", "你选择了 Gemini，请填写 API Key。");
    else issues.push({ message: String(message) });
  }
  return issues;
}
function focusSetting(id) {
  const field = $(id);
  if (!field) return;
  const group = field.closest("details");
  if (group) group.open = true;
  field.focus({ preventScroll: true });
  field.scrollIntoView({ block: "center", behavior: "smooth" });
}
function clearIssues() {
  $("settings-issues-box").hidden = true;
  $("settings-issues").replaceChildren();
  for (const node of $("settings-form").querySelectorAll("[aria-invalid]")) {
    node.removeAttribute("aria-invalid");
    $(`${node.id}-error`)?.remove();
  }
}
function showIssues(issues, focusFirst = false) {
  clearIssues();
  if (!issues.length) return;
  $("settings-issues-box").hidden = false;
  for (const issue of issues) {
    const item = document.createElement("li");
    const text = document.createElement(issue.id ? "button" : "span");
    text.textContent = issue.message;
    if (issue.id) {
      text.type = "button";
      text.onclick = () => focusSetting(issue.id);
      const field = $(issue.id);
      field.setAttribute("aria-invalid", "true");
      const hint = document.createElement("small");
      hint.id = `${issue.id}-error`;
      hint.className = "field-error";
      hint.textContent = issue.message;
      field.after(hint);
    }
    item.append(text);
    $("settings-issues").append(item);
  }
  $("settings-form").scrollTop = 0;
  if (focusFirst && issues[0]?.id) focusSetting(issues[0].id);
}
async function openSettings(required = false) {
  try {
    const response = await fetch("/api/config", { cache: "no-store" });
    if (!response.ok) throw Error("无法读取设置，请确认本地网页服务正在运行后重试。");
    state.configuration = await response.json();
    const data = state.configuration.data;
    $("task-agent").value = state.agent = data.agent;
    state.agentLogin = { agent: data.agent, state: state.configuration.agent_login.state };
    state.roleModels = {};
    fillRoleSelects(data.agent);
    loadAgentFields(data.agent);
    $("workspace").value = data.workspace || "";
    $("proactive-progress").checked = data.feedback.proactive_progress === true;
    $("length-penalty").value = data.duplex.length_penalty;
    $("gemini-model").value = data.gemini.model;
    $("gemini-key").value = "";
    $("gemini-key-status").textContent = state.configuration.gemini_key_configured
      ? "已保存密钥，留空表示继续使用。"
      : "还没有密钥。也可以在 deploy/web/.env 里设置 GEMINI_API_KEY。";
    for (const [role, section] of [["routing", "routing"], ["response", "responses"], ["multimodal", "multimodal"]]) {
      $(`${role}-provider`).value = data[section].provider;
      $(`${role}-model`).value = data[section].model || "";
      $(`${role}-effort`).value = data[section].effort;
    }
    $("routing-mode").value = data.routing.mode;
    $("routing-timeout").value = data.routing.timeout_s;
    const busy = state.connected || state.connecting || state.stopping;
    for (const node of $("settings-form").querySelectorAll("input,select,button")) node.disabled = busy;
    updateRoleFields();
    renderLogin();
    state.configured = state.configuration.configured;
    render();
    $("settings-message").textContent = busy ? "请先结束对话，再修改设置。" : "";
    clearIssues();
    if (!$("settings").open) $("settings").showModal();
    if (!busy && (required || !state.configured))
      showIssues(settingsIssues(state.configuration.check?.problems), true);
  } catch (error) {
    notify(errorText(error), true);
  }
}
async function saveSettings(event) {
  event.preventDefault();
  if (!state.configuration || state.connected) return;
  const issues = settingsIssues();
  if (issues.length) {
    showIssues(issues, true);
    return;
  }
  clearIssues();
  const button = $("save-settings");
  button.disabled = true;
  try {
    storeAgentFields(state.agent);
    const data = structuredClone(state.configuration.data);
    data.agent = state.agent;
    data.language = "zh";
    data.workspace = $("workspace").value.trim();
    data.feedback.proactive_progress = $("proactive-progress").checked;
    data.duplex.length_penalty = Number($("length-penalty").value);
    // Routing fields are saved in direct mode too: an agent switch rewrites them while hidden.
    data.routing.mode = $("routing-mode").value;
    data.routing.timeout_s = Number($("routing-timeout").value);
    for (const [role, section] of [["routing", "routing"], ["response", "responses"], ["multimodal", "multimodal"]]) {
      data[section].provider = $(`${role}-provider`).value;
      data[section].model = $(`${role}-model`).value.trim() || null;
      data[section].effort = $(`${role}-effort`).value;
    }
    data.gemini.model = $("gemini-model").value.trim();
    data.gemini.api_key = $("gemini-key").value.trim();
    const response = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Venus-Config-Token": state.configuration.token },
      body: JSON.stringify({ data, revision: state.configuration.revision }),
    });
    const result = await response.json();
    if (!response.ok) {
      showIssues(settingsIssues(result.problems || [result.detail || "设置没有保存成功，请检查后重试。"]), true);
      return;
    }
    state.configuration = result;
    state.configured = result.configured;
    $("gemini-key").value = "";
    state.agentLogin = { agent: result.data.agent, state: result.agent_login.state };
    renderLogin();
    if (result.configured) {
      $("settings").close();
      notify("设置已保存。");
    } else {
      $("settings-message").textContent = `已保存。还差一步：按顶部的提示登录 ${AGENTS[result.data.agent].name}。`;
      showIssues(settingsIssues(result.check?.problems));
      $("settings-form").scrollTop = 0;
    }
    void refreshStatus();
  } catch (error) {
    showIssues([{ message: `设置没有保存成功：${errorText(error)}` }]);
  } finally {
    button.disabled = false;
  }
}

// ---------- Events ----------

for (const button of document.querySelectorAll(".mode")) button.onclick = () => setMode(button.dataset.mode);
for (const key of ["microphone", "camera"])
  $(`${key}-device`).onchange = (event) => {
    devices[key] = event.target.value;
    try {
      localStorage.setItem("voice-devices", JSON.stringify(devices));
    } catch {}
  };
$("start").onclick = () => void startSession();
$("stop").onclick = () => void stopSession().catch(() => {});
function toggleMic() {
  state.muted = !state.muted;
  capture.setMuted(state.muted);
  render();
}
$("mute-mic").onclick = toggleMic;
$("mute-speaker").onclick = () => {
  state.speakerMuted = !state.speakerMuted;
  player.setMuted(state.speakerMuted);
  render();
};
$("settings-button").onclick = () => void openSettings();
$("notice-action").onclick = () => void openSettings(true);
$("notice-close").onclick = () => {
  state.headsetTipClosed = true;
  try {
    localStorage.setItem(HEADSET_TIP, "1");
  } catch {}
  render();
};
$("settings-close").onclick = () => $("settings").close();
$("settings").addEventListener("click", (event) => {
  if (event.target === $("settings")) $("settings").close();
});
$("settings-form").onsubmit = (event) => void saveSettings(event);
$("settings-form").addEventListener("input", () => {
  if (!$("settings-issues-box").hidden) showIssues(settingsIssues());
});
$("task-agent").onchange = () => {
  storeAgentFields(state.agent);
  const agent = $("task-agent").value;
  // Model names belong to one vendor: roles that follow the agent keep one name per agent.
  for (const role of ["routing", "response", "multimodal"])
    if ($(`${role}-provider`).value === state.agent) {
      state.roleModels[`${state.agent}:${role}`] = $(`${role}-model`).value;
      $(`${role}-model`).value = state.roleModels[`${agent}:${role}`] || "";
    }
  fillRoleSelects(agent);
  state.agent = agent;
  loadAgentFields(agent);
  updateRoleFields();
  $("settings-message").textContent = "";
  void checkLogin();
};
$("routing-mode").onchange = updateRoleFields;
for (const role of ["routing", "response", "multimodal"])
  $(`${role}-provider`).onchange = () => {
    $(`${role}-model`).value = "";
    updateRoleFields();
  };
$("gemini-model").oninput = updateRoleFields;
$("copy-command").onclick = async () => {
  try {
    await navigator.clipboard.writeText($("login-command").textContent);
    notify("已复制。");
  } catch {
    notify("无法复制，请手动选择命令。", true);
  }
};
$("recheck-login").onclick = async () => {
  const button = $("recheck-login");
  button.disabled = true;
  try {
    await checkLogin();
    if ($("task-agent").value !== state.configuration.data.agent) return;
    const status = await refreshStatus(true);
    if (status?.configured) {
      clearIssues();
      $("settings-message").textContent = "都设置好了，可以开始对话。";
    }
  } finally {
    button.disabled = false;
  }
};
$("clear-transcript").onclick = async () => {
  const restart = state.connected;
  if (restart && !confirm("这会结束当前对话并重新开始，之前的上下文会丢失。")) return;
  try {
    await player.prime();
    await stopSession();
    tasks.clear();
    clearTranscript();
    if (restart) await startSession();
  } catch {}
};
$("save-transcript").onclick = () => {
  const lines = [...state.transcript.values()].map(
    (entry) => `${PRODUCT} · ${new Date(entry.time).toLocaleString("zh-CN")}\n${entry.text}`,
  );
  saveBlob(new Blob([lines.join("\n\n")], { type: "text/plain;charset=utf-8" }), `${PRODUCT}-对话记录.txt`);
};
window.addEventListener("keydown", (event) => {
  if (event.key.toLowerCase() !== "m" || !state.connected || event.repeat) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target.closest?.("input, select, textarea, dialog")) return;
  toggleMic();
});
window.addEventListener("pagehide", () => {
  state.epoch++;
  player.clear();
  capture.stop();
  if (state.socket?.readyState === WebSocket.OPEN) send({ type: "stop_session" });
  state.socket?.close();
});
navigator.mediaDevices?.addEventListener?.("devicechange", () => void enumerateDevices());
setInterval(() => {
  if (state.connected) render();
}, 1000);
setInterval(() => {
  if (!state.connected && !state.connecting && !state.stopping) void refreshStatus();
}, 15000);
function animate() {
  if (waveform.active) waveform.draw(capture.readWaveform());
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);
render();
void enumerateDevices();
void refreshStatus();
