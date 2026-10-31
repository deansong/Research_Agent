// The conversation with the discussor. The composer is never disabled: when
// the team waits for you, a message is your answer; while it works, the
// discussor answers on the side (an aside) and the work goes on.

import { el, clear } from "./dom.js";
import { markdown } from "./md.js";

const PIN_SLACK = 40;   // px from the bottom that still counts as "at the bottom"

export class Chat {
  constructor({ onSend }) {
    this.log = document.getElementById("chat-log");
    this.empty = document.getElementById("chat-empty");
    this.input = document.getElementById("composer-input");
    this.hint = document.getElementById("composer-hint");
    this.mode = document.getElementById("chat-mode");
    this.actions = document.getElementById("chat-actions");
    this.onSend = onSend;
    this.pinned = true;
    this.sending = false;
    document.getElementById("composer").addEventListener("submit", (e) => { e.preventDefault(); this.submit(); });
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.submit(); }
    });
    this.log.addEventListener("scroll", () => {
      this.pinned = this.log.scrollHeight - this.log.scrollTop - this.log.clientHeight < PIN_SLACK;
    });
    document.getElementById("btn-scroll").addEventListener("click", () => { this.pinned = true; this.scroll(); });
  }

  reset() {
    clear(this.log);
    this.log.append(this.empty);
    this.empty.hidden = false;
  }

  history(entries) {
    for (const m of entries) this.message(m.role, m.text, m.kind);
    if (entries.length) {
      this.log.append(el("div", { class: "history-end", text: `${entries.length} earlier messages` }));
    }
    this.pinned = true;
    this.scroll();
  }

  message(role, text, kind, noted) {
    this.empty.hidden = true;
    const human = role === "user";
    const auto = kind === "autopilot" || (human && /^\[autopilot\]/.test(text || ""));
    if (auto) text = String(text).replace(/^\[autopilot\]\s*/, "");
    const node = el("div", { class: `msg ${human ? "human" : "agent"} ${kind === "aside" ? "aside" : ""} ${auto ? "autopilot" : ""}` },
      el("div", { class: "msg-role" }, auto ? "autopilot, for you" : human ? "you" : "discussor",
        kind === "aside" ? el("span", { class: "aside-tag", text: "aside" })
          : kind === "results" ? el("span", { class: "aside-tag", text: "results" }) : null),
      el("div", { class: "msg-text", html: human ? markdown(text).replace(/^<p>|<\/p>$/g, "") : markdown(text) }),
      (noted || []).length ? el("div", { class: "noted", text: `for the team's next step: ${noted.join("; ")}` }) : null);
    this.log.append(node);
    this.scroll();
  }

  line(text, kind) {
    if (!text) return;
    this.empty.hidden = true;
    this.log.append(el("div", { class: `logline ${kind || ""}`, text }));
    this.scroll();
  }

  // What the composer does now, from the run's status.
  setMode(d) {
    let mode = "idle", hint = "";
    if (!d) {
      hint = "Start a new run, or open one.";
    } else if (!d.started && d.stage === "bootstrap") {
      mode = "start"; hint = "Describe your research idea: the team starts from it.";
    } else if (d.busy) {
      mode = "working"; hint = "The team is working — the discussor answers on the side; the work goes on.";
    } else if (d.waiting) {
      mode = "waiting"; hint = "The team waits for you — your answer moves the work on.";
    } else if (d.stopped_midway) {
      mode = "stopped"; hint = "Stopped midway — ask anything, or Carry on.";
    } else {
      hint = "Ask the discussor anything.";
    }
    if (d && d.asides) hint = `The discussor is answering (${d.asides})... ` + hint;
    this.mode.textContent = { start: "new run", working: "team working", waiting: "waiting for you",
      stopped: "stopped", idle: "" }[mode];
    this.mode.className = `chat-mode ${mode}`;
    this.hint.textContent = hint;
    this.input.placeholder = mode === "start" ? "Your research idea..." : "Write to the discussor...";
  }

  // Buttons for what the user can do right now: approvals, go, carry on.
  setActions(buttons) {
    clear(this.actions);
    for (const b of buttons) {
      this.actions.append(el("button", { class: `btn small ${b.kind || ""}`, text: b.label, title: b.title || "", onclick: b.onclick }));
    }
    this.actions.hidden = !buttons.length;
  }

  async submit() {
    const text = this.input.value.trim();
    if (!text || this.sending) return;
    this.sending = true;
    try {
      await this.onSend(text);
      this.input.value = "";
    } finally {
      this.sending = false;
      this.input.focus();
    }
  }

  scroll() {
    if (this.pinned) this.log.scrollTop = this.log.scrollHeight;
  }
}
