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

import { api, subscribe } from './api.js';
import { AuthPanel } from './auth.js';
import { BootstrapPanel } from './bootstrap.js';
import { Chat } from './chat.js';
import { FilesPanel } from './files.js';
import { IterationsPanel, OrchestratorPanel, TimelinePanel } from './history.js';
import { modelOverrides, resetModelPicker, wireModelPicker } from './models.js';
import { GraphPanel } from './graph.js';
import { Inspector, renderActivity } from './inspector.js';
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

const chat = new Chat({
  onSend: answer,
  // Fetched on demand, not streamed: one reported turn held 14,885 events in
  // seventeen minutes, and pushing those down the event stream would evict the
  // transcript from the ring buffer to show you something nobody asked for.
  onDetail: () => (state.session ? api.currentActivity(state.session.id) : null),
});

const graph = new GraphPanel(el('tab-graph'), {
  onSelect: (name) => selectNode(name),
});

const bootstrap = new BootstrapPanel(el('tab-bootstrap'), {
  load: () => api.getBootstrap(state.session.id),
  save: (body) => api.putBootstrap(state.session.id, body),
  reset: () => api.resetBootstrap(state.session.id),
  start: () => startConversation(),
  models: (refresh) => api.models(refresh),
  onError: (error) => showError(error),
});

const plan = new PlanPanel(el('tab-plan'), {
  onSave: savePlan,
  // Clicking a plan step lights up the nodes that own it. `steps` is what
  // decides how much of the plan each node is shown, so this makes the
  // context-control design visible instead of buried in two JSON files.
  onSelectStep: (stepId, owners) => graph.highlight(stepId ? owners : []),
});

// The supervisor's reviews, and every node visit in order -- both read-only,
// loaded when their tab is opened and refreshed while it is the one showing.
const iterations = new IterationsPanel(el('tab-iterations'));
const orchestrator = new OrchestratorPanel(el('tab-orchestrator'));
const timeline = new TimelinePanel(el('tab-timeline'));

const files = new FilesPanel(el('files-list'), el('files-view'), {
  onRead: (path) => api.readFile(state.session.id, path),
});

const inspector = new Inspector({
  onSave: saveNode,
  onSelectSteps: () => {},
  getBackends: () => state.agent?.backends || {},
  onSetRole: async (role, body) => {
    const result = await api.setRole(state.session.id, role, body);
    // Redraw with the new model: the graph labels and Context use it too.
    if (state.agent) state.agent.backends = result.backends;
    await loadAgent();
    return result;
  },
  models: (refresh) => api.models(refresh),
});

const auth = new AuthPanel({
  // The repo decides which providers are IN USE, so the panel follows whatever
  // the New-session field is pointed at -- there is usually no open session
  // when somebody goes looking for this.
  getRepo: () => el('new-repo').value.trim() || '',
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
document.addEventListener('panels-resized', () => { graph.resize(); bootstrap.resize(); });

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

  // The conversation so far, from the checkpoint. The event stream replays
  // only what THIS server process has buffered, so a restart used to leave
  // the chat blank while the session still held every word of it. Written
  // before subscribing, so the replay that follows appends rather than
  // interleaves.
  chat.history(detail.transcript || []);

  renderSession(detail);
  loadAgent();
  await bootstrap.refresh();

  // A session nobody has started yet opens on the design agent, so the
  // first thing you see is what is about to interview you -- and where to
  // change it -- rather than an empty graph tab.
  if (!detail.busy && !detail.has_agent && !(detail.transcript || []).length) {
    showTab('canvas-tabs', 'bootstrap');
    chat.status('Not started. Review the design agent, then press Start conversation.');
  }

  // Anything the session already emitted -- a run that started before this tab
  // opened, or a reconnect -- arrives as replay before the live feed.
  state.unsubscribe = subscribe(detail.id, {
    status: (e) => {
      if (e.phase) setPhase(e.phase);
      refreshHistory();
      // A supervisor's redesign replaces the agent on disk, so a new run
      // starting is the moment to redraw the graph.
      if (e.phase === 'running') loadAgent();
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
      refreshHistory();
      chat.question(null);
      chat.status(e.error || 'finished');
      refresh();          // refresh() calls syncRunControls()
    },
    onerror: () => chat.status('reconnecting...'),
  });
}

/** Begin a session that was created without starting -- see the New dialog. */
async function startConversation() {
  if (!state.session) return;
  const detail = await api.startSession(state.session.id, {});
  state.session = detail;
  renderSession(detail);
  chat.status('started');
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
  // The other half of the plan/graph link: selecting a node shows which plan
  // steps it will actually be told about.
  plan.highlightForNode(name);
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

  // Fetched separately from the context: one executor turn can be hundreds of
  // events with command output attached, and the form should not wait for it.
  try {
    await showNodeActivity(name);
  } catch (error) {
    if (error.status !== 404) showError(error);
  }
}

/**
 * One node's conversation, for any node that has a record.
 *
 * Deliberately NOT tied to the graph selection: the design-phase nodes --
 * discussor, planner, designer -- are not in the generated agent's graph, so
 * selecting them there is impossible, and their turns are the conversation
 * that produced the agent in the first place.
 */
async function showNodeActivity(name) {
  const payload = await api.nodeActivity(state.session.id, name);
  renderActivity(el('tab-activity'), payload, (pick) => {
    showNodeActivity(pick).catch((error) => showError(error));
  });
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

/** Re-read the history tab that is showing, if either is. */
async function refreshHistory(force = '') {
  if (!state.session) return;
  const showing = el('tab-iterations').classList.contains('active') ? 'iterations'
    : el('tab-timeline').classList.contains('active') ? 'timeline'
    : el('tab-orchestrator').classList.contains('active') ? 'orchestrator' : '';
  const which = force || showing;
  try {
    if (which === 'iterations') iterations.render(await api.iterations(state.session.id));
    if (which === 'orchestrator') orchestrator.render(await api.orchestrator(state.session.id));
    if (which === 'timeline') timeline.render(await api.timeline(state.session.id));
  } catch (error) {
    if (error.status !== 404) showError(error);
  }
}

/** Re-read the file listing. Called when the Files tab is opened. */
async function refreshFiles() {
  if (!state.session) return;
  try {
    files.render(await api.listFiles(state.session.id));
  } catch (error) {
    showError(error);
  }
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
    chat.question(detail.pending, detail.commands);
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
  syncRunControls();
  // Editing the design agent is refused while it runs, so the panel has to
  // follow the busy flag rather than whatever it was when it last loaded.
  bootstrap.setBusy(Boolean(detail.busy));
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

/**
 * Enable Pause and Stop only while something is actually running.
 *
 * A stop button that is clickable when nothing is running teaches you to
 * distrust it. `busy` comes from the server -- whether a worker thread is
 * alive -- rather than from anything this tab guessed.
 */
function syncRunControls() {
  const running = Boolean(state.session?.busy);
  el('btn-pause').disabled = !running;
  el('btn-stop').disabled = !running;
  // The live view of the turn in flight follows the same flag.
  chat.setLive(running);
}

function wireRunControls() {
  el('btn-pause').addEventListener('click', async () => {
    if (!state.session) return;
    el('btn-pause').disabled = true;
    try {
      const result = await api.pauseSession(state.session.id);
      chat.status(result.detail || 'stopping after this step');
    } catch (error) {
      showError(error);
      syncRunControls();
    }
  });

  el('btn-stop').addEventListener('click', async () => {
    if (!state.session) return;
    // No confirmation dialog: the button says what it does, and the cost is
    // one node's turn rather than anything irreversible. Everything the graph
    // had already finished is checkpointed.
    el('btn-stop').disabled = true;
    try {
      const result = await api.stopSession(state.session.id);
      chat.status(result.detail || 'stopping');
    } catch (error) {
      showError(error);
      syncRunControls();
    }
  });
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
  const message = String(error);
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
    // Loaded when opened rather than on every refresh: the listing walks two
    // directory trees, and a session with 2,000 artifacts should not pay for
    // that while somebody is reading the graph.
    if (tab.dataset.tab === 'files') refreshFiles();
    if (['iterations', 'timeline', 'orchestrator'].includes(tab.dataset.tab)) {
      refreshHistory(tab.dataset.tab);
    }
    for (const other of strip.querySelectorAll('.tab')) {
      other.classList.toggle('active', other === tab);
    }
    const panel = strip.parentElement;
    for (const body of panel.querySelectorAll('.tab-body')) {
      body.classList.toggle('active', body.id === `tab-${tab.dataset.tab}`);
    }
    // Drawn while hidden, a graph has no size to lay out into.
    if (tab.dataset.tab === 'bootstrap') requestAnimationFrame(() => bootstrap.resize());
    if (tab.dataset.tab === 'graph') requestAnimationFrame(() => graph.resize());
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

  // Start stays disabled until there is a task, so the dialog cannot post
  // something the server is bound to refuse. Better than explaining the
  // refusal afterwards.
  const taskBox = el('new-task');
  const startButton = el('new-ok');
  const syncStart = () => { startButton.disabled = !taskBox.value.trim(); };
  taskBox.addEventListener('input', syncStart);

  wireModelPicker(el('new-repo'));

  el('btn-new').addEventListener('click', () => {
    el('new-error').hidden = true;
    resetModelPicker();
    syncStart();
    newDialog.showModal();
    taskBox.focus();
  });
  el('new-cancel').addEventListener('click', () => newDialog.close());
  el('open-cancel').addEventListener('click', () => openDialog.close());

  el('form-new').addEventListener('submit', async (event) => {
    event.preventDefault();
    const prebuilt = el('new-prebuilt').value.trim() || null;
    const body = {
      repo: el('new-repo').value.trim() || '.',
      task: el('new-task').value.trim(),
      session_dir: el('new-session-dir').value.trim() || null,
      sessions_root: sessionsRoot(),
      pre_build_agent: prebuilt,
      research: el('new-research').checked,
      supervisor: el('new-supervisor').value || null,
      backend_role: modelOverrides(),
      max_iterations: Number(el('new-max-iterations').value) || null,
      // Reviewing means: create the session, show the design agent, and let
      // the Start button on that tab begin it. A pre-built agent skips the
      // design phase, so there is nothing to review.
      start: !(el('new-review').checked && !prebuilt),
    };
    try {
      const detail = await api.createSession(body);
      newDialog.close();
      openSession(detail);
    } catch (error) {
      const box = el('new-error');
      box.textContent = String(error);
      box.hidden = false;
    }
  });

  el('btn-open').addEventListener('click', async () => {
    const list = el('session-list');
    list.replaceChildren();
    let sessions = [];
    try {
      sessions = await api.listSessions(el('new-repo').value.trim() || '.',
                                        sessionsRoot());
    } catch (error) {
      showError(error);
      return;
    }

    if (!sessions.length) {
      list.innerHTML = '<div class="empty-state"><p class="muted">No sessions yet.</p></div>';
    }

    for (const session of sessions) {
      const item = document.createElement('div');
      item.className = 'session-item';
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
            sessions_root: sessionsRoot(),
            start: true,
          });
          openDialog.close();
          openSession(detail);
        } catch (error) {
          showError(error);
        }
      });
      item.append(row, deleteControls(session, item));
      list.append(item);
    }
    openDialog.showModal();
  });
}

/**
 * A Delete button for one row of the Open list, which asks before it does
 * anything: the folder -- conversation, agent, results -- goes for good, and
 * the session's branch (its code) only if that box is ticked.
 */
function deleteControls(session, item) {
  const box = document.createElement('div');
  box.className = 'session-delete';
  const ask = document.createElement('button');
  ask.type = 'button';
  ask.className = 'btn ghost small';
  ask.textContent = 'Delete…';

  const confirmBox = document.createElement('div');
  confirmBox.className = 'session-delete-confirm';
  confirmBox.hidden = true;
  const warning = document.createElement('p');
  warning.textContent = `Delete ${session.name} for good? Its folder goes: `
    + 'the conversation, the agent, and every result under artifacts/. '
    + (session.open ? 'It is running now and will be stopped first. ' : '');
  const branchLabel = document.createElement('label');
  const branch = document.createElement('input');
  branch.type = 'checkbox';
  branchLabel.append(branch, ` Also delete its git branch agent/${session.name} `
    + '(the code it wrote), if it has one');
  const yes = document.createElement('button');
  yes.type = 'button';
  yes.className = 'btn danger small';
  yes.textContent = 'Delete for good';
  const no = document.createElement('button');
  no.type = 'button';
  no.className = 'btn ghost small';
  no.textContent = 'Cancel';
  confirmBox.append(warning, branchLabel, yes, no);

  ask.addEventListener('click', () => { confirmBox.hidden = false; ask.hidden = true; });
  no.addEventListener('click', () => { confirmBox.hidden = true; ask.hidden = false; });
  yes.addEventListener('click', async () => {
    yes.disabled = true;
    try {
      const result = await api.deleteSession(session.id, {
        repo: el('new-repo').value.trim() || '.',
        sessions_root: sessionsRoot(),
        drop_branch: branch.checked,
      });
      // The page was showing it: start clean rather than leave a view of a
      // session that no longer exists.
      if (state.session?.id === session.id) {
        window.location.reload();
        return;
      }
      item.remove();
      chat.status(result.detail);
    } catch (error) {
      yes.disabled = false;
      warning.textContent = String(error);
    }
  });
  box.append(ask, confirmBox);
  return box;
}

// ---------------------------------------------------------------------------

// The sessions folder: what the server resolves (flag, config, or
// <repo>/.agent/sessions) unless you changed it here -- then yours, remembered
// in this browser, so the next New and Open look in the same place.
const ROOT_KEY = 'sessionsRoot';
let defaultRoot = '';

function sessionsRoot() {
  const value = el('new-sessions-root').value.trim();
  try {
    if (value && value !== defaultRoot) localStorage.setItem(ROOT_KEY, value);
    else localStorage.removeItem(ROOT_KEY);
  } catch { /* storage blocked: still used for this request */ }
  return value && value !== defaultRoot ? value : null;
}

// Show the real path this server was started for, rather than a bare "."
// that could mean two different directories.
api.defaults().then((defaults) => {
  defaultRoot = defaults.sessions_dir || '';
  let remembered = '';
  try { remembered = localStorage.getItem(ROOT_KEY) || ''; } catch { /* none */ }
  el('new-sessions-root').value = remembered || defaultRoot;
  el('new-repo').value = defaults.repo;
  el('new-repo').title = 'Absolute path, so there is no doubt which project '
    + 'this is. A relative path here is taken from ' + defaults.repo;
}).catch(() => { /* leave the field as typed */ });

api.schema().then((schema) => inspector.setSchema(schema)).catch(() => {
  // Not fatal: the inspector falls back to its built-in option lists. Worth
  // knowing about though, because it means /api/schema is broken.
  chat.logLine('could not load /api/schema; using fallback field options', 'error');
});

// Asked once at startup, so the dot is already right when somebody notices
// it. Quiet on failure by design -- see AuthPanel.poll.
auth.poll();

wireTabs('canvas-tabs');
wireTabs('inspector-tabs');
wireJson();
wireRunControls();
wireResize();
wireDialogs();

// Exported so the panels added in later steps can reach the shared bits
// without importing app.js and creating a cycle.
export { state, chat, graph, plan, inspector, topology, auth, bootstrap, refresh, showError };
