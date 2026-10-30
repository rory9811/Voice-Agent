/** The background task list: one row per task, updated in place. */
const ENDED = new Set(["delivered", "failed", "cancelled"]);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function stateText(work) {
  if (work.state === "delivered") return "已完成";
  if (work.state === "failed") return "失败";
  if (work.state === "cancelled") return "已取消";
  if (work.state === "cancelling") return "正在取消";
  if (["polish", "completed", "delivering"].includes(work.phase) || ["completed", "delivering"].includes(work.state))
    return "正在返回结果";
  if (work.phase === "routing" || work.capability === "unrouted") return "准备中";
  if (work.state === "queued") return "排队中";
  return "执行中";
}

function detailText(work) {
  const latest = (work.feedback || []).at(-1)?.text;
  return work.error_message || work.result_text || work.progress_text || latest || stateText(work);
}

function clock(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
}

export class TaskList {
  constructor({ send, download, onChange }) {
    Object.assign(this, { send, download, onChange });
    this.box = document.getElementById("tasks");
    this.list = document.getElementById("tasks-list");
    this.empty = document.getElementById("tasks-empty");
    this.count = document.getElementById("tasks-count");
    this.works = new Map();
    this.rows = new Map();
    this.live = false;
    this.endedAt = 0;
    setInterval(() => this.tick(), 1000);
  }
  get running() {
    return [...this.works.values()].filter((work) => !ENDED.has(work.state)).length;
  }
  get total() {
    return this.works.size;
  }
  update(works) {
    const added = works.some((work) => !this.works.has(work.work_id));
    for (const work of works) this.works.set(work.work_id, work);
    this.live = true;
    if (added) this.box.open = true;
    this.render();
  }
  clear() {
    this.works.clear();
    this.rows.clear();
    this.list.replaceChildren();
    this.live = false;
    this.render();
  }
  end() {
    this.live = false;
    this.endedAt = Date.now() / 1000;
    this.render();
  }
  render() {
    this.empty.hidden = this.total > 0;
    this.count.textContent = this.total ? `进行中 ${this.running} / 共 ${this.total}` : "";
    for (const work of this.works.values()) this.renderRow(work);
    this.tick();
    this.onChange();
  }
  renderRow(work) {
    let row = this.rows.get(work.work_id);
    if (!row) {
      row = element("article", "task");
      row.append(
        element("span", "task-mark"),
        element("h3", "task-title"),
        element("time", "task-time"),
        element("p", "task-state"),
        element("button", "task-cancel link-button", "取消"),
        element("div", "task-files"),
      );
      this.rows.set(work.work_id, row);
      this.list.prepend(row);
    }
    const ended = ENDED.has(work.state);
    row.dataset.end = work.state === "delivered" ? "done" : ended ? work.state : this.live ? "" : "left";
    row.querySelector(".task-title").textContent = work.objective;
    row.querySelector(".task-state").textContent = !ended && !this.live ? "对话已结束" : detailText(work);
    const cancel = row.querySelector(".task-cancel");
    cancel.hidden = ended || !this.live;
    cancel.disabled = work.state === "cancelling";
    cancel.textContent = work.state === "cancelling" ? "正在取消" : "取消";
    cancel.onclick = () => {
      cancel.disabled = true;
      this.send({ type: "cancel_work", work_id: work.work_id });
    };
    const files = row.querySelector(".task-files");
    const signature = JSON.stringify(work.artifacts || []) + this.live;
    if (files.dataset.signature !== signature) {
      files.dataset.signature = signature;
      files.replaceChildren(
        ...(work.artifacts || []).map((artifact) => {
          const button = element("button", "task-file", artifact.name);
          button.title = "下载结果";
          button.disabled = !this.live;
          button.onclick = () => this.download(work.work_id, artifact);
          return button;
        }),
      );
    }
  }
  tick() {
    for (const [id, row] of this.rows) {
      const work = this.works.get(id);
      const end = work.completed_at || (!this.live && this.endedAt) || Date.now() / 1000;
      row.querySelector(".task-time").textContent = clock(end - work.created_at);
    }
  }
}
