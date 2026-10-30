import { t, getLanguage, setLanguage, setAgent, AGENT_NAMES } from "./i18n.js";
import { AudioPlayer, encode } from "./audio.js";
import { MediaCapture } from "./capture.js";
import { MicrophoneWaveform } from "./waveform.js";
import { TaskTray } from "./tasks.js";

const $ = (id) => document.getElementById(id);
const ASSISTANT = "Venus";
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
  configuration: null,
  configured: null,
  checkingSettings: false,
  muted: false,
  outputMuted: false,
  agentLogin: { agent: "", state: "" },
  transcript: new Map(),
  lastBackpressure: 0,
  online: true,
};
const devices = { microphone: "", camera: "" };
try {
  Object.assign(
    devices,
    JSON.parse(localStorage.getItem("venus-devices") || "{}"),
  );
} catch {}
const microphoneWaveform = new MicrophoneWaveform($("mic-visualizer"));
const tasks = new TaskTray({ send, download: downloadArtifact });
const player = new AudioPlayer({
  ack: send,
  onText: appendText,
  onActivity: (active) => {
    if (state.connected) setPhase(active ? "speaking" : "listening");
  },
  onError: () => {
    notify(t("browserAudio"), true);
    void stopSession().catch(() => {});
  },
});
const capture = new MediaCapture($("preview-video"), {
  onChunk: sendInput,
  onEnded: () => {
    notify(t("deviceEnded"), true);
    void stopSession().catch(() => {});
  },
  onBackpressure: backpressure,
});
let toastTimer;
let toastSticky = false;
function notify(message, error = false, sticky = false) {
  clearTimeout(toastTimer);
  toastSticky = sticky;
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  if (sticky) return;
  toastTimer = setTimeout(
    () => ($("toast").hidden = true),
    error ? 7000 : 4200,
  );
}
function humanError(error) {
  const key = {
    NotAllowedError: "permissionDenied",
    PermissionDeniedError: "permissionDenied",
    NotFoundError: "deviceMissing",
    NotReadableError: "deviceBusy",
    InsecureContextError: "insecure",
  }[error?.name];
  return key ? t(key) : error?.message || t("connectionFailed");
}
function setPhase(phase) {
  state.phase = phase;
  document.body.dataset.phase = phase;
  render();
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
    notify(t("slowConnection"));
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
function render() {
  const busy = state.connected || state.connecting || state.stopping;
  document.body.dataset.mode = state.mode;
  if (state.model) $("model-name").textContent = state.model;
  for (const button of document.querySelectorAll(".mode-tab")) {
    const available = state.inputModes.includes(button.dataset.mode);
    button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
    button.disabled = busy || !available;
    button.title = available ? "" : t("notDeployed");
  }
  const previewing = state.mode === "camera" && Boolean(capture.stream);
  const micActive = Boolean(capture.stream) && !state.stopping;
  const modeIcon = state.mode === "camera" ? "#i-camera" : "#i-mic";
  $("preview-video").hidden = !previewing;
  microphoneWaveform.setState(micActive, state.muted);
  $("media-placeholder").hidden = previewing || micActive;
  $("placeholder-icon").setAttribute("href", modeIcon);
  $("placeholder-label").textContent = t(state.mode === "camera" ? "cameraOff" : "microphoneOff");
  $("floor-chip").hidden = !state.connected && !state.connecting;
  $("floor-label").textContent = t(
    state.connecting ? "connecting" : state.phase === "speaking" ? "speaking" : "listening",
  );
  $("lamp-timer").textContent = state.connected ? elapsed() : "";
  $("camera-field").hidden = state.mode !== "camera";
  $("microphone-device").disabled = $("camera-device").disabled = busy;
  $("start-button").hidden = state.connected || state.connecting;
  $("start-button").disabled = state.stopping || state.checkingSettings || !state.inputModes.length;
  $("start-icon").setAttribute("href", state.configured === false ? "#i-settings" : modeIcon);
  $("start-label").textContent = t(
    state.checkingSettings
      ? "checkingSettings"
      : state.stopping
        ? "ending"
        : state.configured === false
          ? "completeSettings"
          : "start",
  );
  $("stop-button").hidden = !(state.connected || state.connecting);
  $("stop-label").textContent = t(state.connecting ? "cancel" : "end");
  $("mute-mic").hidden = !state.connected;
  $("mute-mic").setAttribute("aria-pressed", String(state.muted));
  $("mute-mic").querySelector("use").setAttribute("href", state.muted ? "#i-mic-off" : "#i-mic");
  $("mute-output").hidden = !state.connected;
  $("mute-output").setAttribute("aria-pressed", String(state.outputMuted));
  $("mute-output").querySelector("use").setAttribute("href", state.outputMuted ? "#i-sound-off" : "#i-sound");
  let hint = state.mode === "camera" ? "cameraHint" : "startHint";
  if (state.connected) hint = state.muted ? "mutedHint" : "liveHint";
  if (state.connecting) hint = "connecting";
  if (state.stopping) hint = "ending";
  if (!busy && state.configured === false) hint = "notConfigured";
  $("control-hint").textContent = t(hint);
  const status = $("connection-state");
  const needsSetup = !busy && state.online && state.configured === false;
  status.classList.toggle("pending", state.connecting || state.stopping);
  status.classList.toggle("setup", needsSetup);
  status.classList.toggle("offline", !state.online);
  status.lastElementChild.textContent = state.stopping
    ? t("ending")
    : state.connecting
      ? t("connecting")
      : state.connected
        ? t("live")
        : t(!state.online ? "offline" : needsSetup ? "setupNeeded" : "available");
  $("save-transcript").disabled = !state.transcript.size;
  $("reset-button").disabled =
    state.connecting || state.stopping || (!state.transcript.size && !state.connected);
}
function elapsed() {
  const seconds = Math.max(
    0,
    Math.floor((Date.now() - state.startedAt) / 1000),
  );
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
function setMode(mode) {
  if (state.connected || state.connecting || state.stopping) return;
  if (!state.inputModes.includes(mode)) return;
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
async function requireSettings() {
  if (state.checkingSettings) return false;
  state.checkingSettings = true;
  render();
  try {
    const status = await refreshStatus(true);
    if (!status) {
      notify(t("network"), true);
      return false;
    }
    if (!status.configured) {
      await openSettings(true);
      return false;
    }
    return true;
  } finally {
    state.checkingSettings = false;
    render();
  }
}
async function startSession() {
  if (state.connected || state.connecting || state.stopping || state.checkingSettings) return;
  if (state.configured === false) {
    await requireSettings();
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
    if (!status) throw Error(t("network"));
    if (!status.configured) {
      state.checkingSettings = false;
      render();
      await openSettings(true);
      return;
    }
    if (status.busy) throw Error(t("busy"));
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
    if (epoch !== state.epoch) return;
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
    } else notify(humanError(error), true);
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
        reject(Error(t("connectionFailed")));
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
        return;
      }
      if (message.type === "fatal_error") {
        serverRejected = true;
        const known = {
          busy: "busy",
          configuration: "notConfigured",
          model_busy: "modelBusy",
          model_link_lost: "modelLinkLost",
          model_mismatch: "modelMismatch",
          model_unreachable: "modelUnreachable",
        }[message.code];
        const error = Error(
          known ? t(known) : message.error || t("connectionFailed"),
        );
        error.code = message.code;
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(error);
        } else if (error.code === "configuration") {
          state.configured = false;
          void stopSession().then(() => openSettings(true)).catch(() => {});
        } else {
          notify(humanError(error), true);
          void stopSession().catch(() => {});
        }
        return;
      }
      if (message.type === "playback") player.enqueue(message);
      else if (message.type === "text")
        appendText(message.text, message.utterance_id);
      else if (message.type === "works") tasks.update(message.works);
      else if (message.type === "model_link")
        message.state === "down"
          ? notify(t("modelReconnecting"), true, true)
          : notify(t("modelReconnected"));
      else if (message.type === "command_error") notify(message.error, true);
    };
    socket.onerror = () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(Error(t("network")));
      }
    };
    socket.onclose = () => {
      clearTimeout(timer);
      if (!settled) {
        settled = true;
        reject(Error(t("network")));
      }
      if (epoch === state.epoch && !state.stopping && !serverRejected) {
        notify(t("network"), true);
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
            reject(Error(t("sessionClosing")));
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
      // A reconnecting notice belongs to the session that just ended.
      if (toastSticky) $("toast").hidden = true;
      setPhase("idle");
      void refreshStatus();
    }
  })();
  try {
    await state.stopPromise;
  } catch (error) {
    notify(humanError(error), true);
    throw error;
  } finally {
    state.stopPromise = null;
  }
}
function clearTranscript() {
  state.transcript.clear();
  $("transcript").replaceChildren();
  const empty = document.createElement("p");
  empty.className = "empty-copy";
  empty.dataset.i18n = "historyEmpty";
  empty.textContent = t("historyEmpty");
  $("transcript").append(empty);
  render();
}
function appendText(text, id) {
  if (!text) return;
  const key = id || `text-${state.transcript.size}`;
  let entry = state.transcript.get(key);
  // Follow new text only for someone already at the end, not someone reading back.
  const log = $("transcript");
  const following = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  if (!entry) {
    $("transcript").querySelector(".empty-copy")?.remove();
    const node = document.createElement("article");
    node.className = "transcript-entry";
    const heading = document.createElement("header");
    heading.textContent = ASSISTANT;
    const time = document.createElement("time");
    time.textContent = new Date().toLocaleTimeString(getLanguage(), {
      hour: "2-digit",
      minute: "2-digit",
    });
    heading.append(time);
    const paragraph = document.createElement("p");
    node.append(heading, paragraph);
    $("transcript").append(node);
    entry = { text: "", node, paragraph, time: Date.now() };
    state.transcript.set(key, entry);
    render();
  }
  entry.text += text;
  entry.paragraph.textContent = entry.text;
  if (following) log.scrollTop = log.scrollHeight;
}
async function downloadArtifact(workId, artifact) {
  if (!state.sessionId || !state.token) {
    notify(t("noResult"));
    return;
  }
  try {
    const response = await fetch(
      `/api/sessions/${encodeURIComponent(state.sessionId)}/works/${encodeURIComponent(workId)}/artifacts/${artifact.index}`,
      { headers: { "X-Venus-Session-Token": state.token } },
    );
    if (!response.ok) throw Error(t("noResult"));
    saveBlob(await response.blob(), artifact.name);
  } catch (error) {
    notify(humanError(error), true);
  }
}
function saveBlob(blob, name) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
async function enumerateDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  try {
    const available = await navigator.mediaDevices.enumerateDevices();
    for (const [key, kind] of [
      ["microphone", "audioinput"],
      ["camera", "videoinput"],
    ]) {
      const select = $(key + "-device");
      select.replaceChildren(new Option(t("defaultDevice"), ""));
      for (const [i, device] of available
        .filter((value) => value.kind === kind)
        .entries())
        select.add(
          new Option(device.label || `${t(key)} ${i + 1}`, device.deviceId),
        );
      select.value = devices[key];
    }
  } catch {}
}
function updateRoutingOptions() {
  const options = $("auto-routing-options");
  const automatic = $("routing-mode").value === "auto";
  options.hidden = !automatic;
  options.disabled =
    !automatic || state.connected || state.connecting || state.stopping;
}
function updateProviders() {
  const busy = state.connected || state.connecting || state.stopping;
  for (const role of ["routing", "response", "multimodal"]) {
    const gemini = $(`${role}-provider`).value === "gemini";
    $(`${role}-effort`).closest("label").hidden = gemini;
    $(`${role}-effort`).disabled = busy || gemini;
    $(`${role}-model`).placeholder = gemini ? $("gemini-model").value : "";
  }
  $("multimodal-agent-audio-hint").hidden = $("multimodal-provider").value === "gemini";
}
const EFFORTS = {
  codex: ["minimal", "low", "medium", "high", "xhigh"],
  claude: ["low", "medium", "high", "xhigh", "max"],
};
const LOGIN_COMMANDS = {
  codex: "docker compose exec web codex login --device-auth",
  claude: "docker compose exec -it web claude auth login",
};
const AGENT_COMMANDS = { codex: (binary) => [binary, "app-server"], claude: (binary) => [binary] };
// The model, effort and executable fields edit the selected agent's own settings section.
function storeAgentFields(agent) {
  const section = state.configuration.data[agent];
  section.model = $("backend-model").value.trim() || null;
  section.effort = $("backend-effort").value || null;
  section.command = AGENT_COMMANDS[agent]($("agent-binary").value.trim());
}
function loadAgentFields(agent) {
  const section = state.configuration.data[agent];
  $("backend-model").value = section.model || "";
  $("backend-effort").value = section.effort || "";
  $("agent-binary").value = section.command[0];
}
// Every role that used an agent follows the selected one; Gemini roles stay on Gemini.
function updateAgent() {
  const agent = $("task-agent").value;
  setAgent(agent);
  for (const id of ["backend-effort", "routing-effort", "response-effort", "multimodal-effort"]) {
    const select = $(id);
    for (const option of select.options)
      option.hidden = option.disabled = option.value !== "" && !EFFORTS[agent].includes(option.value);
    if (select.selectedOptions[0]?.disabled) select.value = "low";
  }
  for (const option of document.querySelectorAll(".agent-option")) {
    option.value = agent;
    option.textContent = AGENT_NAMES[agent];
  }
  renderAgentLogin();
}
$("task-agent").onchange = () => {
  storeAgentFields(state.agent);
  const agent = $("task-agent").value;
  // Model names belong to one vendor: roles that follow the agent keep one name per agent.
  for (const role of ["routing", "response", "multimodal"])
    if ($(`${role}-provider`).value === state.agent) {
      state.roleModels[`${state.agent}:${role}`] = $(`${role}-model`).value;
      $(`${role}-model`).value = state.roleModels[`${agent}:${role}`] || "";
    }
  state.agent = agent;
  loadAgentFields(state.agent);
  updateAgent();
  updateProviders();
  $("settings-message").textContent = "";
  void checkAgentLogin();
};
$("routing-mode").onchange = () => { updateRoutingOptions(); updateProviders(); };
for (const role of ["routing", "response", "multimodal"]) {
  $(`${role}-provider`).onchange = () => {
    $(`${role}-model`).value = "";
    updateProviders();
  };
}
$("gemini-model").oninput = updateProviders;
function renderAgentLogin() {
  const agent = $("task-agent").value;
  const known = state.agentLogin.agent === agent;
  const login = known ? state.agentLogin.state : "";
  const unsaved = agent !== state.configuration?.data?.agent;
  $("agent-login-status").textContent = !known
    ? t("agentChecking")
    : t(login === "logged_in" && unsaved ? "agentLogin_logged_in_unsaved" : `agentLogin_${login}`);
  $("agent-login-status").dataset.state = login;
  $("agent-login-steps").hidden = !["not_logged_in", "not_subscription"].includes(login);
  $("agent-login-help").textContent = t(`agentLoginHelp_${agent}`);
  $("agent-login-command").textContent = LOGIN_COMMANDS[agent];
}
async function checkAgentLogin() {
  const agent = $("task-agent").value;
  state.agentLogin = { agent: "", state: "" };
  renderAgentLogin();
  let result = { state: "error" };
  try {
    const response = await fetch(`/api/agent-login?agent=${agent}`, { cache: "no-store" });
    if (response.ok) result = await response.json();
  } catch {}
  // The user may have switched again while this was running.
  if ($("task-agent").value !== agent) return;
  state.agentLogin = { agent, state: result.state };
  renderAgentLogin();
}
$("copy-login-command").onclick = async () => {
  try {
    await navigator.clipboard.writeText($("agent-login-command").textContent);
    notify(t("copied"));
  } catch {
    notify(t("connectionFailed"), true);
  }
};
$("recheck-login").onclick = async () => {
  const button = $("recheck-login");
  button.disabled = true;
  try {
    await checkAgentLogin();
    if ($("task-agent").value !== state.configuration.data.agent) return;
    const status = await refreshStatus(true);
    if (status?.configured) {
      clearSettingsIssues();
      $("settings-message").textContent = t("settingsReady");
    }
  } finally { button.disabled = false; }
};
function settingsIssues(serverProblems = []) {
  const issues = [];
  const add = (id, key) => { if (!issues.some(x => x.id === id)) issues.push({id, message: t(key)}); };
  if (!$("backend-workspace").value.trim()) add("backend-workspace", "workspaceRequired");
  if (!$("agent-binary").value.trim()) add("agent-binary", "agentRequired");
  const usesGemini = $("response-provider").value === "gemini" || $("multimodal-provider").value === "gemini" || ($("routing-mode").value === "auto" && $("routing-provider").value === "gemini");
  if (usesGemini && !state.configuration?.gemini_key_configured && !$("gemini-key").value.trim()) add("gemini-key", "geminiRequired");
  for (const node of $("settings-form").querySelectorAll("input,select")) {
    if (node.disabled || (!usesGemini && node.id === "gemini-model")) continue;
    if (!node.checkValidity() && !issues.some(x => x.id === node.id))
      issues.push({id: node.id, message: `${node.closest("label")?.querySelector("span")?.textContent || node.id}: ${t("fieldRequired")}`});
  }
  // The panel at the top already says what to do about the login.
  const login = state.configuration?.agent_login;
  const shown = (serverProblems || []).filter((message) => message !== login?.message || login.state === "missing");
  for (const message of shown) {
    if (/Configure Task workspace/i.test(message)) add("backend-workspace", "workspaceRequired");
    else if (/executable not found/i.test(message)) add("agent-binary", "agentRequired");
    else if (/Gemini API key/i.test(message)) add("gemini-key", "geminiRequired");
    else issues.push({message: String(message).split(" / ")[getLanguage() === "zh" && String(message).includes(" / ") ? 1 : 0]});
  }
  return issues;
}
function focusSetting(id) {
  const field = $(id);
  if (!field) return;
  const details = field.closest("details");
  if (details) details.open = true;
  field.focus({preventScroll: true});
  field.scrollIntoView({block: "center", behavior: "smooth"});
}
function clearSettingsIssues() {
  $("settings-guidance").hidden = true;
  $("settings-issues").replaceChildren();
  for (const node of $("settings-form").querySelectorAll("[aria-invalid]")) {
    node.removeAttribute("aria-invalid");
    const errorId = `${node.id}-error`;
    $(errorId)?.remove();
    node.setAttribute("aria-describedby", (node.getAttribute("aria-describedby") || "").split(" ").filter(id => id && id !== errorId).join(" "));
  }
}
function showSettingsIssues(issues, focusFirst = false) {
  clearSettingsIssues();
  if (!issues.length) return;
  $("settings-guidance").hidden = false;
  $("settings-guidance-copy").textContent = t("setupInstructions");
  for (const issue of issues) {
    const li = document.createElement("li");
    const item = document.createElement(issue.id ? "button" : "span");
    item.textContent = issue.message;
    if (issue.id) {
      item.type = "button";
      item.onclick = () => focusSetting(issue.id);
      const field = $(issue.id);
      field.setAttribute("aria-invalid", "true");
      const hint = document.createElement("small");
      hint.id = `${issue.id}-error`;
      hint.className = "setting-error";
      hint.textContent = issue.message;
      field.after(hint);
      field.setAttribute("aria-describedby", `${field.getAttribute("aria-describedby") || ""} ${hint.id}`.trim());
    }
    li.append(item);
    $("settings-issues").append(li);
  }
  if (focusFirst && issues[0]?.id) focusSetting(issues[0].id);
}
$("settings-form").addEventListener("input", () => {
  if (!$("settings-guidance").hidden) showSettingsIssues(settingsIssues());
});

async function openSettings(required = false) {
  try {
    const response = await fetch("/api/config", { cache: "no-store" });
    if (!response.ok) throw Error(t("settingsLoadFailed"));
    state.configuration = await response.json();
    const data = state.configuration.data;
    $("task-agent").value = state.agent = data.agent;
    state.agentLogin = { agent: data.agent, state: state.configuration.agent_login.state };
    state.roleModels = {};
    updateAgent();
    loadAgentFields(data.agent);
    $("backend-workspace").value = data.workspace || "";
    $("gemini-model").value = data.gemini.model;
    $("gemini-key").value = "";
    $("gemini-key-status").textContent = t(state.configuration.gemini_key_configured ? "geminiKeyConfigured" : "geminiKeyMissing");
    for (const [role, section] of [["routing", "routing"], ["response", "responses"], ["multimodal", "multimodal"]]) {
      $(`${role}-provider`).value = data[section].provider;
    }
    $("multimodal-model").value = data.multimodal.model || "";
    $("multimodal-effort").value = data.multimodal.effort;
    $("reply-language").value = data.language;
    $("proactive-progress").value = String(data.feedback.proactive_progress === true);
    $("length-penalty").value = data.duplex.length_penalty;
    $("routing-mode").value = data.routing.mode;
    $("routing-model").value = data.routing.model || "";
    $("routing-effort").value = data.routing.effort;
    $("routing-timeout").value = data.routing.timeout_s;
    $("response-model").value = data.responses.model || "";
    $("response-effort").value = data.responses.effort;
    const busy = state.connected || state.connecting || state.stopping;
    for (const node of $("settings-form").querySelectorAll(
      "input,select,button",
    ))
      node.disabled = busy;
    updateRoutingOptions();
    updateProviders();
    state.configured = state.configuration.configured;
    $("settings-message").textContent = busy ? t("settingsActive") : "";
    clearSettingsIssues();
    if (!$("settings-dialog").open) $("settings-dialog").showModal();
    $("recheck-login").disabled = busy;
    if (!busy && (required || !state.configured))
      showSettingsIssues(settingsIssues(state.configuration.check?.problems), true);
  } catch (error) {
    notify(humanError(error), true);
  }
}
$("settings-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!state.configuration || state.connected) return;
  const issues = settingsIssues();
  if (issues.length) { showSettingsIssues(issues, true); return; }
  clearSettingsIssues();
  const button = $("save-settings");
  button.disabled = true;
  try {
    storeAgentFields(state.agent);
    const data = structuredClone(state.configuration.data);
    data.agent = state.agent;
    data.workspace = $("backend-workspace").value.trim();
    data.language = $("reply-language").value;
    data.feedback.proactive_progress = $("proactive-progress").value === "true";
    data.duplex.length_penalty = Number($("length-penalty").value);
    // Routing fields are saved in General mode too: an agent switch rewrites them while hidden.
    data.routing.mode = $("routing-mode").value;
    data.routing.provider = $("routing-provider").value;
    data.routing.model = $("routing-model").value.trim() || null;
    data.routing.effort = $("routing-effort").value;
    data.routing.timeout_s = Number($("routing-timeout").value);
    data.responses.provider = $("response-provider").value;
    data.multimodal.provider = $("multimodal-provider").value;
    data.multimodal.model = $("multimodal-model").value.trim() || null;
    data.multimodal.effort = $("multimodal-effort").value;
    data.gemini.model = $("gemini-model").value.trim();
    data.gemini.api_key = $("gemini-key").value.trim();
    data.responses.model = $("response-model").value.trim() || null;
    data.responses.effort = $("response-effort").value;
    const response = await fetch("/api/config", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Venus-Config-Token": state.configuration.token,
      },
      body: JSON.stringify({ data, revision: state.configuration.revision }),
    });
    const result = await response.json();
    if (!response.ok) {
      showSettingsIssues(settingsIssues(result.problems || [result.detail || t("settingsSaveFailed")] ), true);
      return;
    }
    state.configuration = result;
    state.configured = result.configured;
    $("gemini-key").value = "";
    state.agentLogin = { agent: result.data.agent, state: result.agent_login.state };
    renderAgentLogin();
    if (!result.configured) {
      $("settings-message").textContent = t("settingsSavedLoginNeeded");
      showSettingsIssues(settingsIssues(result.check?.problems), false);
      $("settings-dialog").scrollTop = 0;
    } else {
      $("settings-dialog").close();
      notify(t("settingsSaved"));
    }
    void refreshStatus();
  } catch (error) {
    showSettingsIssues([{ message: t("settingsSaveFailed") + " " + humanError(error) }], false);
  } finally {
    button.disabled = false;
  }
};
for (const button of document.querySelectorAll(".mode-tab"))
  button.onclick = () => setMode(button.dataset.mode);
for (const key of ["microphone", "camera"])
  $(key + "-device").onchange = (event) => {
    devices[key] = event.target.value;
    try {
      localStorage.setItem("venus-devices", JSON.stringify(devices));
    } catch {}
  };
$("start-button").onclick = () => void startSession();
$("stop-button").onclick = () => void stopSession().catch(() => {});
$("mute-mic").onclick = () => {
  state.muted = !state.muted;
  capture.setMuted(state.muted);
  render();
};
$("mute-output").onclick = () => {
  state.outputMuted = !state.outputMuted;
  player.setMuted(state.outputMuted);
  render();
};
$("settings-button").onclick = () => void openSettings();
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.querySelector(".dialog-close").onclick = () => dialog.close();
  dialog.addEventListener("click", (event) => {
    const rect = dialog.getBoundingClientRect();
    if (
      event.target === dialog &&
      (event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom)
    )
      dialog.close();
  });
}
$("reset-button").onclick = async () => {
  const restart = state.connected;
  if (restart && !confirm(t("restartHint"))) return;
  try {
    await player.prime();
    await stopSession();
    tasks.clear();
    clearTranscript();
    if (restart) await startSession();
    else notify(t("resetDone"));
  } catch {}
};
$("save-transcript").onclick = () => {
  saveBlob(
    new Blob(
      [
        [...state.transcript.values()]
          .map(
            (entry) =>
              `${ASSISTANT} · ${new Date(entry.time).toLocaleString()}\n${entry.text}`,
          )
          .join("\n\n"),
      ],
      { type: "text/plain;charset=utf-8" },
    ),
    `${ASSISTANT}-conversation.txt`,
  );
};
$("language-button").onclick = () =>
  setLanguage(getLanguage() === "en" ? "zh" : "en");
window.addEventListener("venus-language", () => {
  render();
  if (state.configuration) renderAgentLogin();
  void enumerateDevices();
});
window.addEventListener("keydown", (event) => {
  if (event.key.toLowerCase() !== "m" || !state.connected || event.repeat) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target.closest?.("input, select, textarea, dialog")) return;
  $("mute-mic").click();
});
window.addEventListener("pagehide", () => {
  state.epoch++;
  player.clear();
  capture.stop();
  if (state.socket?.readyState === WebSocket.OPEN)
    send({ type: "stop_session" });
  state.socket?.close();
});
navigator.mediaDevices?.addEventListener?.(
  "devicechange",
  () => void enumerateDevices(),
);
setInterval(() => {
  if (state.connected) render();
}, 1000);
setInterval(() => {
  if (!state.connected && !state.connecting && !state.stopping)
    void refreshStatus();
}, 15000);
function animate(time) {
  if (microphoneWaveform.active && !document.hidden)
    microphoneWaveform.draw(capture.readWaveform(), time);
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);
render();
void enumerateDevices();
void refreshStatus();
