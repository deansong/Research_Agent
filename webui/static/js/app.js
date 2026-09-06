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
import { GraphPanel } from './graph.js';
import { Inspector } from './inspector.js';
import { PlanPanel, ownersByStep } from './plan.js';
import { TopologyPanel } from './topology.js';

const el = (id) => document.getElementById(id);

const state = {
  session: null,     // the SessionDetail we last fetched
  unsubscribe: null, // closes the SSE stream
  problems: [],
  agent: null,       // {graph, nodes, view, editable} from /agent
  plan: null,
  selected: null,    // the node name being inspected
};

const chat = new Chat({ onSend: answer });

const graph = new GraphPanel(el('tab-graph'), {
  onSelect: (name) => selectNode(name),
});

const plan = new PlanPanel(el('tab-plan'), {
  onSave: savePlan,
  // Clicking a plan step lights up the nodes that own it. `steps` is what
  // decides how much of the plan each node is shown, so this makes the
  // context-control design visible instead of buried in two JSON files.
  onSelectStep: (stepId, owners) => graph.highlight(stepId ? owners : []),
});

const inspector = new Inspector({
  onSave: saveNode,
  onSelectSteps: () => {},
});

const topology = new TopologyPanel(el('tab-wiring'), {
  getAgent: () => state.agent,
  onSave: (document_) => saveAgent(document_),
  onSelect: (name) => { selectNode(name); showTab('canvas-tabs', 'graph'); },
});

// A problem row in the right-hand panel names a node; clicking it selects one.
document.addEventListener('select-node', (event) => {
  selectNode(event.detail.name);
  showTab('canvas-tabs', 'graph');
});

// Cytoscape cannot notice the column being dragged; it has to be told.
document.addEventListener('panels-resized', () => graph.resize());

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
  state.agent = null;
  state.selected = null;
  renderSession(detail);
  loadAgent();

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
      // The plan appears at the plan_review question and the agent at the
      // first work-phase one, so a question is the moment to look for both.
      loadPlan();
      if (e.purpose !== 'discussion' && e.purpose !== 'plan_review') loadAgent();
    },
    state: (e) => {
      if (e.has_agent) { refresh(); loadAgent(); }
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

async function loadAgent() {
  if (!state.session) return;
  try {
    state.agent = await api.getAgent(state.session.id);
  } catch (error) {
    // 404 just means the design phase has not finished. Anything else is real.
    if (error.status !== 404) showError(error);
    state.agent = null;
    graph.render(null);
    inspector.clear('No agent yet.');
    return;
  }

  graph.render(state.agent.view);
  topology.draw();
  drawJson();
  state.problems = state.agent.problems || [];
  renderProblems();

  if (state.selected) selectNode(state.selected);
  await loadPlan();
}

async function loadPlan() {
  if (!state.session) return;
  try {
    const got = await api.getPlan(state.session.id);
    state.plan = got.plan;
    plan.render(got.plan, ownersByStep(state.agent?.view, got.plan));
  } catch (error) {
    if (error.status !== 404) showError(error);
  }
}

async function savePlan(edited) {
  const result = await api.putPlan(state.session.id, edited);
  state.plan = edited;
  plan.render(edited, ownersByStep(state.agent?.view, edited));
  for (const problem of result.problems || []) addProblem(problem);
  return result;
}

async function selectNode(name) {
  state.selected = name;
  graph.select(name);
  if (!name || !state.agent) {
    inspector.clear();
    return;
  }

  const kind = state.agent.view.nodes.find((n) => n.id === name)?.kind;
  inspector.show(name, kind, state.agent.nodes[name]);
  showTab('inspector-tabs', 'configure');

  try {
    inspector.showContext(await api.nodeContext(state.session.id, name));
  } catch (error) {
    showError(error);
  }
}

async function saveNode(name, entry) {
  // Send the WHOLE document, not a patch. graph.json and nodes.json have to
  // agree about which nodes exist, and the server validates them together --
  // a per-node PATCH endpoint would let them disagree in between.
  const result = await saveAgent({
    graph: state.agent.graph,
    nodes: { ...state.agent.nodes, [name]: entry },
  });
  await selectNode(name);
  return result;
}

async function saveAgent(document_) {
  const result = await api.putAgent(state.session.id, document_);
  await loadAgent();
  // Saving is allowed to leave the agent invalid -- you cannot rewire a graph
  // without passing through states where something is unreachable -- so the
  // problems are shown rather than the save being blocked. Jump to them, since
  // an edit that broke something is worth noticing straight away.
  if ((result.problems || []).some((p) => !p.warning)) {
    showTab('inspector-tabs', 'problems');
  }
  return result;
}

// ---- the raw JSON escape hatch -------------------------------------------

function drawJson() {
  const editor = el('json-editor');
  if (document.activeElement === editor) return;   // do not clobber a live edit
  editor.value = state.agent
    ? JSON.stringify({ graph: state.agent.graph, nodes: state.agent.nodes }, null, 2)
    : '';
  el('btn-json-save').disabled = true;
  el('json-status').textContent = '';
}

function wireJson() {
  const editor = el('json-editor');
  const save = el('btn-json-save');

  editor.addEventListener('input', () => {
    // Parse as you type, so a stray comma is caught before you press save
    // rather than after. Cheap: this document is a few kilobytes.
    try {
      JSON.parse(editor.value);
      save.disabled = false;
      el('json-status').textContent = 'unsaved';
    } catch (error) {
      save.disabled = true;
      el('json-status').textContent = error.message;
    }
  });

  save.addEventListener('click', async () => {
    save.disabled = true;
    el('json-status').textContent = 'saving...';
    try {
      const result = await saveAgent(JSON.parse(editor.value));
      const blocking = (result.problems || []).filter((p) => !p.warning).length;
      el('json-status').textContent = blocking
        ? `saved, ${blocking} problem(s)` : 'saved';
    } catch (error) {
      el('json-status').textContent = error.message || 'save failed';
      save.disabled = false;
    }
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

/** Switch a tab strip from code -- the app drives these, not only clicks. */
function showTab(stripId, name) {
  const strip = el(stripId);
  const tab = strip.querySelector(`.tab[data-tab="${name}"]`);
  if (tab) tab.click();
}


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

api.schema().then((schema) => inspector.setSchema(schema)).catch(() => {
  // Not fatal: the inspector falls back to its built-in option lists. Worth
  // knowing about though, because it means /api/schema is broken.
  chat.logLine('could not load /api/schema; using fallback field options', 'error');
});

wireTabs('canvas-tabs');
wireTabs('inspector-tabs');
wireJson();
wireResize();
wireDialogs();

// Exported so the panels added in later steps can reach the shared bits
// without importing app.js and creating a cycle.
export { state, chat, graph, plan, inspector, topology, refresh, showError };
