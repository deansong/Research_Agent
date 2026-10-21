/*
  WHAT:  Two views of a session's history: the supervisor's ITERATIONS --
         each design-and-run, what the review found and what was decided --
         and the TIMELINE, every node visit in order.
  WHY:   The supervisor can re-plan and redesign on its own (auto mode), and
         a run loops through checkers and substeps. Without these, the only
         record of "what happened, and why did the agent change" was a folder
         of JSON files on disk.
  CONCEPT: Read-only renderings of GET /iterations and GET /timeline.
*/

function node(tag, className, text) {
  const out = document.createElement(tag);
  if (className) out.className = className;
  if (text !== undefined && text !== null) out.textContent = String(text);
  return out;
}

function empty(container, text) {
  const box = node('div', 'empty-state');
  box.append(node('p', 'muted', text));
  container.replaceChildren(box);
}

const VERDICT_CLASS = {
  done: 'ok', stop: 'muted', improve_experiment: 'warn', improve_agent: 'warn',
};

export class IterationsPanel {
  constructor(container) {
    this.container = container;
  }

  render(payload) {
    const iterations = payload?.iterations || [];
    if (!iterations.length) {
      empty(this.container, payload?.mode === 'off'
        ? 'The supervisor is off for this session: one run, no review.'
        : 'No iterations yet. After a run finishes, the supervisor reviews it here.');
      return;
    }

    const head = node('div', 'it-head muted',
      `Supervisor: ${payload.mode || '?'}  ·  ${iterations.length} of at most `
      + `${payload.max_iterations || '?'} iteration(s)`);
    const list = node('div', 'it-list');
    for (const it of iterations) list.append(this.card(it));
    this.container.replaceChildren(head, list);
  }

  card(it) {
    const review = it.review || {};
    const card = node('div', 'it-card');

    const title = node('div', 'it-title');
    title.append(node('strong', '', `Iteration ${it.n}`));
    title.append(node('span', `it-verdict ${VERDICT_CLASS[review.verdict] || ''}`,
      review.verdict || '(no review)'));
    if (review.decision) title.append(node('span', 'it-decision', `→ ${review.decision}`));
    card.append(title);

    card.append(node('div', 'muted',
      `agent ${it.agent || '?'}  ·  ${it.nodes} nodes  ·  ${it.steps} plan steps  ·  `
      + `${it.visits} node visits`));

    if (review.summary) card.append(node('p', 'it-summary', review.summary));
    if ((review.findings || []).length) {
      const list = node('ul', 'it-findings');
      for (const finding of review.findings) list.append(node('li', '', finding));
      card.append(list);
    }
    for (const [key, label] of [['plan_feedback', 'Plan feedback'],
                                ['design_feedback', 'Design feedback'],
                                ['note', 'Your note']]) {
      if (review[key]) {
        const row = node('div', 'it-feedback');
        row.append(node('span', 'it-label', `${label}: `), node('span', '', review[key]));
        card.append(row);
      }
    }
    if (it.report) {
      const details = node('details', 'it-report');
      details.append(node('summary', '', 'What the supervisor read'));
      details.append(node('pre', '', it.report));
      card.append(details);
    }
    return card;
  }
}

export class TimelinePanel {
  constructor(container) {
    this.container = container;
  }

  render(payload) {
    const visits = payload?.visits || [];
    if (!visits.length) {
      empty(this.container, 'Nothing has run yet. Every node visit is listed here as it happens.');
      return;
    }
    const table = node('table', 'tl-table');
    const head = node('tr');
    for (const label of ['time', 'node', 'model', 'route', 'seconds', 'tokens']) {
      head.append(node('th', '', label));
    }
    table.append(head);

    for (const visit of visits) {
      const row = node('tr', `tl-${visit.kind || 'agent'}`);
      const time = String(visit.at || '').replace('T', ' ').slice(0, 19);
      let what = visit.node;
      if (visit.kind === 'human') what = `${visit.node}  (${visit.purpose || 'you'})`;
      if (visit.kind === 'supervisor') what = `supervisor — iteration ${visit.iteration}: ${visit.verdict}`;
      if (visit.kind === 'orchestrator') {
        what = `${visit.node} — ${visit.action}${visit.target && visit.target !== visit.node
          ? ` → ${visit.target}` : ''}${visit.reason ? `: ${visit.reason}` : ''}`;
      }
      if (visit.kind === 'iteration_end') {
        what = `iteration ${visit.iteration} ends — ${visit.verdict}, ${visit.decision}`;
      }
      let route = visit.route ?? '';
      if (visit.kind === 'human' && visit.answer) route = `${visit.answer} → ${visit.route}`;
      const model = visit.model || visit.provider
        ? `${visit.provider || ''}/${visit.model || ''}` : '';
      for (const value of [time, what, model, route, visit.seconds ?? '',
                           visit.output_tokens ?? '']) {
        row.append(node('td', '', value));
      }
      table.append(row);
    }
    const wrap = node('div', 'tl-wrap');
    wrap.append(table);
    this.container.replaceChildren(wrap);
  }
}


const STATUS_CLASS = {
  done: 'ok', finished: 'ok', failed: 'warn', stale: 'warn', blocked: 'warn',
  doing: '', validating: '', todo: 'muted',
};

/**
 * What the orchestrator knows and has decided -- its memory
 * (<session>/orchestrator/memory.json): the step book, its notes, its last
 * decisions, and how full its conversation is. The one place to see the
 * run's state as the orchestrator sees it.
 */
export class OrchestratorPanel {
  constructor(container) {
    this.container = container;
  }

  render(payload) {
    const memory = payload?.memory || {};
    if (!Object.keys(memory).length) {
      empty(this.container, payload?.hub === false
        ? 'This agent has no orchestrator: it is a pipeline of workers and checkers.'
        : 'Nothing yet. Once the design is approved, the discussor runs the '
          + 'experiment as its orchestrator, and what it knows shows here.');
      return;
    }
    const parts = [];
    const conv = [`conversation ${memory.thread_id ? memory.thread_id.slice(0, 18) : '(none)'}`,
                  `${memory.provider || '?'}/${memory.model || 'default'}`];
    if (memory.thread_from === 'discussor') conv.push("the discussor's, continued");
    if (memory.fill != null) conv.push(`${memory.fill}% of its context window`);
    if (memory.turns != null) conv.push(`${memory.turns} decision(s) this run`);
    parts.push(node('div', 'it-head muted', conv.join('  ·  ')));

    const book = memory.book || {};
    if (Object.keys(book).length) {
      const table = node('table', 'tl-table');
      const head = node('tr');
      for (const label of ['step', 'status', 'visits', 'failed', 'summary', 'report']) {
        head.append(node('th', '', label));
      }
      table.append(head);
      for (const [name, entry] of Object.entries(book)) {
        const row = node('tr');
        row.append(node('td', '', name));
        row.append(node('td', `it-verdict ${STATUS_CLASS[entry.status] || ''}`, entry.status || 'todo'));
        row.append(node('td', '', entry.visits ?? 0));
        row.append(node('td', '', entry.failures ?? 0));
        row.append(node('td', '', entry.status === 'failed' && entry.problem
          ? `problem: ${entry.problem}` : (entry.summary || '')));
        row.append(node('td', 'muted', (entry.report_path || '').split('/').slice(-2).join('/')));
        table.append(row);
      }
      const wrap = node('div', 'tl-wrap');
      wrap.append(table);
      parts.push(wrap);
    }

    const sections = [
      ['What was agreed', memory.requirements ? [memory.requirements] : []],
      ['Its notes', memory.notes || []],
      ['Its last decisions', (memory.decisions || []).slice(-15).reverse()],
      ['Earlier iterations', (memory.iterations || []).map(
        (i) => `${i.n}: ${i.verdict} — ${i.summary || ''} → ${i.decision}`
          + (i.note ? ` (you: ${i.note})` : ''))],
      ['Fresh conversations', memory.rolled_at || []],
    ];
    for (const [title, items] of sections) {
      if (!items.length) continue;
      const card = node('div', 'it-card');
      card.append(node('strong', '', title));
      const list = node('ul', 'it-findings');
      for (const item of items) list.append(node('li', '', item));
      card.append(list);
      parts.push(card);
    }
    this.container.replaceChildren(...parts);
  }
}
