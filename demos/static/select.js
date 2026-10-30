/** Dropdowns that open below their field. A native menu on macOS covers the field
 * and cannot be styled, so each <select> stays as the source of truth, hidden, and
 * this draws it. Code that reads or sets the select needs no changes. */
const OPTION_HEIGHT = 32;
const LIST_PADDING = 8;
let nextId = 0;

/** Where a list goes: below the field if it fits, else on the roomier side. */
export function place(field, listHeight, viewportHeight, gap = 4, margin = 8) {
  const below = viewportHeight - field.bottom - gap - margin;
  const above = field.top - gap - margin;
  const showBelow = listHeight <= below || below >= above;
  const maxHeight = Math.max(0, Math.min(listHeight, showBelow ? below : above));
  return { top: showBelow ? field.bottom + gap : field.top - gap - maxHeight, maxHeight };
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function enhance(select) {
  const id = `select-${nextId++}`;
  const wrapper = document.createElement("div");
  wrapper.className = "select";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "select-button";
  button.setAttribute("role", "combobox");
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", id);
  const label = document.createElement("span");
  label.className = "select-value";
  button.append(label, icon("chevron-down"));
  const list = document.createElement("ul");
  list.id = id;
  list.className = "select-list";
  list.setAttribute("role", "listbox");
  list.popover = "auto";
  button.popoverTargetElement = list;
  select.before(wrapper);
  wrapper.append(button, select);
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");
  // Inside a dialog: the rest of the page is inert. Outside any <label>: a click
  // on an option would count as a click on the label and reopen the list.
  (select.closest("dialog, [popover]") || document.body).append(list);

  let active = -1;
  const choices = () => [...select.options].filter((option) => !option.hidden && !option.disabled);
  const isOpen = () => list.matches(":popover-open");
  const sync = () => {
    label.textContent = select.selectedOptions[0]?.textContent ?? "";
    button.disabled = select.disabled;
    for (const name of ["aria-invalid", "aria-describedby"]) {
      if (select.hasAttribute(name)) button.setAttribute(name, select.getAttribute(name));
      else button.removeAttribute(name);
    }
  };
  const mark = (index) => {
    active = index;
    [...list.children].forEach((row, i) => row.classList.toggle("active", i === index));
    const row = list.children[index];
    if (row) {
      button.setAttribute("aria-activedescendant", row.id);
      row.scrollIntoView({ block: "nearest" });
    } else button.removeAttribute("aria-activedescendant");
  };
  const choose = (option) => {
    list.hidePopover();
    button.focus();
    if (select.value === option.value) return;
    select.value = option.value;
    select.dispatchEvent(new Event("input", { bubbles: true }));
    select.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const closeOnMove = (event) => {
    if (event.target !== list) list.hidePopover();
  };

  list.addEventListener("beforetoggle", (event) => {
    button.setAttribute("aria-expanded", String(event.newState === "open"));
    if (event.newState !== "open") return;
    const options = choices();
    list.replaceChildren(
      ...options.map((option, i) => {
        const row = document.createElement("li");
        row.id = `${id}-${i}`;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(option.selected));
        const text = document.createElement("span");
        text.textContent = option.textContent;
        row.append(text);
        if (option.selected) row.append(icon("check"));
        row.onclick = () => choose(option);
        row.onpointermove = () => {
          if (active !== i) mark(i);
        };
        return row;
      }),
    );
    const field = button.getBoundingClientRect();
    const height = options.length * OPTION_HEIGHT + LIST_PADDING;
    const { top, maxHeight } = place(field, height, window.innerHeight);
    Object.assign(list.style, {
      top: `${top}px`,
      left: `${field.left}px`,
      width: `${field.width}px`,
      maxHeight: `${maxHeight}px`,
    });
  });
  list.addEventListener("toggle", (event) => {
    if (event.newState === "open") {
      mark(choices().findIndex((option) => option.selected));
      window.addEventListener("scroll", closeOnMove, true);
      window.addEventListener("resize", closeOnMove);
    } else {
      mark(-1);
      window.removeEventListener("scroll", closeOnMove, true);
      window.removeEventListener("resize", closeOnMove);
    }
  });
  button.addEventListener("keydown", (event) => {
    const count = list.children.length;
    if (!isOpen()) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        list.showPopover();
      }
      return;
    }
    const moves = {
      ArrowDown: Math.min(active + 1, count - 1),
      ArrowUp: Math.max(active - 1, 0),
      Home: 0,
      End: count - 1,
    };
    if (event.key in moves) {
      event.preventDefault();
      mark(moves[event.key]);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (active >= 0) choose(choices()[active]);
    } else if (event.key === "Tab") list.hidePopover();
  });

  // Keep the drawn field right when code changes the select.
  const nativeValue = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  Object.defineProperty(select, "value", {
    get() {
      return nativeValue.get.call(this);
    },
    set(value) {
      nativeValue.set.call(this, value);
      sync();
    },
  });
  select.focus = (options) => button.focus(options);
  new MutationObserver(sync).observe(select, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["disabled", "aria-invalid", "aria-describedby", "selected"],
  });
  sync();
}

export function enhanceSelects() {
  document.querySelectorAll("select").forEach(enhance);
}
