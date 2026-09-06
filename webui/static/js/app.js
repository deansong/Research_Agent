/*
  WHAT:  Wires the panels together and owns the one piece of shared state --
         which session is open.
  WHY:   Every other module stays about one thing. This is the only file that
         knows they all exist.
  CONCEPT: A tiny store, not a framework.

  The reference project put all of this -- chat, canvas, every inspector, the
  block library and the run timeline -- in one 528-line component with twelve
  useState hooks. Panels are separate files here for exactly that reason.
*/

import { api, Problem, subscribe } from './api.js';
import { Chat } from './chat.js';

const el = (id) => document.getElementById(id);

const state = {
  session: null,     // the SessionDetail we last fetched
  unsubscribe: null, // closes the SSE stream
  problems: [],
};

const chat = new Chat({ onSend: answer });

// ---------------------------------------------------------------------------
// driving a session
// ---------------------------------------------------------------------------

async function answer(text) {
  if (!state.session) return;
  try {
    await api.answer(state.session.id, text);
    chat.status('sent');
  } catch (error) {
    // Say it out loud. A silent failure here means the graph is still waiting
    // and the human thinks they have replied.
    showError(error);
    chat.setWaiting(true);
  }
}

async function openSession(detail) {
  state.unsubscribe?.();
  state.session = detail;
  chat.clear();
  renderSession(detail);

  // Anything the session already emitted -- a run that started before this tab
  // opened, or a reconnect -- arrives as replay before the live feed.
  state.unsubscribe = subscribe(detail.id, {
    status: (e) => {
      if (e.phase) setPhase(e.phase);
      if (e.detail) chat.logLine(e.detail, 'node');
      if (e.title) chat.logLine(`===== ${e.title} =====`, 'node');
      refresh();
    },
    log: (e) => {
      chat.logLine(e.text);
      activity(e.text);
    },
    question: (e) => {
      chat.question(e, e.commands);
      refresh();
    },
    state: (e) => {
      if (e.has_agent) refresh();
    },
    usage: (e) => renderUsage(e.by_node),
    error: (e) => {
      chat.logLine(`${e.code}: ${e.message}`, 'error');
      addProblem(e);
    },
    done: (e) => {
      setPhase(e.error ? 'error' : 'finished');
      chat.question(null);
      chat.status(e.error || 'finished');
      refresh();
    },
    onerror: () => chat.status('reconnecting...'),
  });
}

async function refresh() {
  if (!state.session) return;
  try {
    const detail = await api.getSession(state.session.id);
    state.session = detail;
    renderSession(detail);
  } catch (error) {
    if (error.status !== 404) showError(error);
  }
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

function renderSession(detail) {
  el('session-name').textContent = detail.name;
  el('session-name').title = detail.folder;
  el('session-agent').textContent = detail.agent_name ? `agent: ${detail.agent_name}` : '';
  setPhase(detail.phase);

  if (detail.waiting && detail.pending) {
    chat.question(detail.pending);
  }

  el('run-facts').replaceChildren(...facts({
    session: detail.name,
    folder: detail.folder,
    repo: detail.repo,
    agent: detail.agent_name || '(none yet)',
    artifacts: detail.artifacts,
    checkpoint: detail.checkpoint,
  }));

  state.problems = detail.problems || [];
  renderProblems();
}

function facts(pairs) {
  const nodes = [];
  for (const [key, value] of Object.entries(pairs)) {
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    dd.textContent = value ?? '';
    dd.title = value ?? '';
    nodes.push(dt, dd);
  }
  return nodes;
}

function setPhase(phase) {
  const pill = el('session-phase');
  pill.textContent = phase;
  pill.className = `pill phase-${phase}`;
}

function activity(text) {
  const log = el('activity-log');
  const line = document.createElement('div');
  line.textContent = text;
  log.append(line);
  // The activity tab is a tail, so trim it. The full transcript is in the chat
  // panel and, more durably, in the session's checkpoint.
  while (log.childElementCount > 400) log.firstElementChild.remove();
}

function renderUsage(byNode) {
  if (!byNode) return;
  for (const [node, usage] of Object.entries(byNode)) {
    const percent = usage.cache_percent;
    activity(`[usage] ${node} input=${usage.last_input_tokens} `
      + `cached=${usage.last_cached_input_tokens}`
      + (percent == null ? '' : ` cache=${percent.toFixed(1)}%`));
  }
}

function addProblem(problem) {
  if (!problem.code) return;
  state.problems = [...state.problems, problem];
  renderProblems();
}

function renderProblems() {
  const body = el('tab-problems');
  const count = el('problem-count');
  const blocking = state.problems.filter((p) => !p.warning);

  count.hidden = state.problems.length === 0;
  count.textContent = String(state.problems.length);
  count.style.background = blocking.length ? '' : 'var(--warn)';

  if (!state.problems.length) {
    body.innerHTML = '<div class="empty-state"><p class="muted">No problems.</p></div>';
    return;
  }

  body.replaceChildren(...state.problems.map((problem) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = `problem${problem.warning ? ' warning' : ''}`;
    row.innerHTML = '';

    const code = document.createElement('code');
    code.textContent = problem.code;
    const where = document.createElement('div');
    where.className = 'where';
    where.textContent = problem.where || '';
    const message = document.createElement('div');
    message.textContent = problem.message;

    row.append(code, where, message);
    // Clicking a problem will select the node it names, once the graph panel
    // exists. Wired here so the affordance is not forgotten later.
    row.addEventListener('click', () => selectFromProblem(problem));
    return row;
  }));
}

function selectFromProblem(problem) {
  const match = /node '([a-z0-9_]+)'/.exec(problem.where || '');
  if (match) document.dispatchEvent(
    new CustomEvent('select-node', { detail: { name: match[1] } }),
  );
}

function showError(error) {
  const message = error instanceof Problem
    ? `${error.code}: ${error.message}`
    : String(error);
  chat.logLine(message, 'error');
  chat.status(message);
}

// ---------------------------------------------------------------------------
// tabs and the resizable columns
// ---------------------------------------------------------------------------

function wireTabs(stripId) {
  const strip = el(stripId);
  strip.addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (!tab) return;
    for (const other of strip.querySelectorAll('.tab')) {
      other.classList.toggle('active', other === tab);
    }
    const panel = strip.parentElement;
    for (const body of panel.querySelectorAll('.tab-body')) {
      body.classList.toggle('active', body.id === `tab-${tab.dataset.tab}`);
    }
  });
}

/**
 * Drag a divider to resize its column.
 *
 * Twenty lines, and it is the first thing anyone wants. Worth noting because
 * the project we took the layout from installed a whole panel library and then
 * hardcoded the widths anyway.
 */
function wireResize() {
  const root = document.documentElement;
  const MIN = 220;

  for (const divider of document.querySelectorAll('.divider')) {
    divider.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      divider.setPointerCapture(event.pointerId);
      divider.classList.add('dragging');
      const which = divider.dataset.resize;

      const move = (moveEvent) => {
        const width = which === 'chat'
          ? moveEvent.clientX
          : window.innerWidth - moveEvent.clientX;
        const clamped = Math.max(MIN, Math.min(width, window.innerWidth - 2 * MIN));
        root.style.setProperty(
          which === 'chat' ? '--col-chat' : '--col-inspector', `${clamped}px`,
        );
        document.dispatchEvent(new CustomEvent('panels-resized'));
      };

      const up = () => {
        divider.classList.remove('dragging');
        divider.removeEventListener('pointermove', move);
        divider.removeEventListener('pointerup', up);
      };

      divider.addEventListener('pointermove', move);
      divider.addEventListener('pointerup', up);
    });
  }
}

// ---------------------------------------------------------------------------
// dialogs
// ---------------------------------------------------------------------------

function wireDialogs() {
  const newDialog = el('dialog-new');
  const openDialog = el('dialog-open');

  el('btn-new').addEventListener('click', () => {
    el('new-error').hidden = true;
    newDialog.showModal();
    el('new-task').focus();
  });
  el('new-cancel').addEventListener('click', () => newDialog.close());
  el('open-cancel').addEventListener('click', () => openDialog.close());

  el('form-new').addEventListener('submit', async (event) => {
    event.preventDefault();
    const body = {
      repo: el('new-repo').value.trim() || '.',
      task: el('new-task').value.trim(),
      session_dir: el('new-session-dir').value.trim() || null,
      pre_build_agent: el('new-prebuilt').value.trim() || null,
    };
    try {
      const detail = await api.createSession(body);
      newDialog.close();
      openSession(detail);
    } catch (error) {
      const box = el('new-error');
      box.textContent = error instanceof Problem
        ? `${error.code}: ${error.message}` : String(error);
      box.hidden = false;
    }
  });

  el('btn-open').addEventListener('click', async () => {
    const list = el('session-list');
    list.replaceChildren();
    let sessions = [];
    try {
      sessions = await api.listSessions(el('new-repo').value.trim() || '.');
    } catch (error) {
      showError(error);
      return;
    }

    if (!sessions.length) {
      list.innerHTML = '<div class="empty-state"><p class="muted">No sessions yet.</p></div>';
    }

    for (const session of sessions) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'session-row';

      const name = document.createElement('div');
      name.className = 'name';
      name.textContent = session.name + (session.has_agent ? '  · has an agent' : '');
      const task = document.createElement('span');
      task.className = 'task';
      task.textContent = session.task || '(no saved task)';

      row.append(name, task);
      row.addEventListener('click', async () => {
        try {
          const detail = await api.createSession({
            repo: el('new-repo').value.trim() || '.',
            session: session.name,
            start: true,
          });
          openDialog.close();
          openSession(detail);
        } catch (error) {
          showError(error);
        }
      });
      list.append(row);
    }
    openDialog.showModal();
  });
}

// ---------------------------------------------------------------------------

wireTabs('canvas-tabs');
wireTabs('inspector-tabs');
wireResize();
wireDialogs();

// Exported so the panels added in later steps can reach the shared bits
// without importing app.js and creating a cycle.
export { state, chat, refresh, showError };
