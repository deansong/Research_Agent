/*
  WHAT:  The design agent -- the fixed graph that interviews you, plans, and
         designs your agent -- with every node's model and every prompt it
         sends, editable before the conversation starts.
  WHY:   Everything else on this page is about the agent being DESIGNED. The
         agent doing the designing was invisible: its prompts lived in
         prompts.py and its models in config resolution, so "what is about to
         interview me, and what has it been told?" had no answer here.
  CONCEPT: The server is the source of truth. It sends full values with the
         defaults beside them; this sends full values back; the server stores
         only the difference. So Reset is "put the default back and save".

  Edits are refused while the session is running -- the graph is built at
  Start, so a mid-turn edit would look saved and do nothing. Pause, edit,
  Start again.
*/

import { GraphPanel } from './graph.js';

const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
};

const KIND_LABEL = { agent: 'model', human: 'you', tool: 'no model' };

export class BootstrapPanel {
  /**
   * `load()`  -> the view from GET /bootstrap
   * `save(body)` / `reset()` -> the view after the change
   * `start()` -> begins the conversation
   */
  constructor(container, { load, save, reset, start, models, onError }) {
    this.container = container;
    this.api = { load, save, reset, start, models };
    // provider -> {models: [{id, label, default}], source, error}: what each
    // provider says it offers (GET /api/models). Loaded once, after the
    // first render, because asking takes a few seconds.
    this.models = null;
    this.onError = onError || (() => {});
    this.view = null;
    this.draft = null;        // what the forms currently say
    this.status = '';
    this.graph = null;
    this.open = new Set(['discussor']);
  }

  clear(message = 'Open or start a session to see the agent that designs yours.') {
    this.view = null;
    this.draft = null;
    this.container.replaceChildren(
      el('div', { class: 'empty-state' }, el('p', { class: 'muted', text: message })));
  }

  async refresh() {
    try {
      this.render(await this.api.load());
      this.loadModels();
    } catch (error) {
      if (error.status === 404) { this.clear(); return; }
      this.onError(error);
    }
  }

  async loadModels(force = false) {
    if (!this.api.models || this.loadingModels || (this.models && !force)) return;
    this.loadingModels = true;
    try {
      const listed = await this.api.models(force);
      this.models = Object.fromEntries((listed.providers || []).map((p) => [p.provider, p]));
      if (this.view) this.draw();
    } catch (error) {
      this.onError(error);
    } finally {
      this.loadingModels = false;
    }
  }

  /** The model a provider uses unless told otherwise: its own default, else its first. */
  defaultModel(provider) {
    const offered = this.models?.[provider]?.models || [];
    const chosen = offered.find((m) => m.default) || offered[0];
    return chosen ? chosen.id : (this.view.suggested_models[provider] || '');
  }

  /** Re-fit the drawing: cytoscape lays out to zero size in a hidden tab. */
  resize() {
    this.graph?.resize();
  }

  /** Called whenever the session's busy flag may have changed. */
  setBusy(busy) {
    if (!this.view || this.view.busy === busy) return;
    // A fresh view carries `editable` and `started`, which both follow busy.
    this.refresh();
  }

  render(view) {
    this.view = view;
    this.draft = draftFrom(view);
    this.status = '';
    this.draw();
  }

  // ---- drawing -----------------------------------------------------------

  draw() {
    const view = this.view;
    const scroll = this.container.scrollTop;
    this.container.replaceChildren();

    const graphBox = el('div', { class: 'bs-graph' });
    this.container.append(this.header(), this.notices(), graphBox, this.nodeList());

    this.graph = new GraphPanel(graphBox, {
      onSelect: (name) => { if (name) this.reveal(name); },
    });
    this.graph.render(view.view);
    this.container.scrollTop = scroll;
  }

  header() {
    const view = this.view;
    const dirty = this.isDirty();
    const canStart = view.editable && !view.started;

    const title = el('div', { class: 'bs-title' },
      el('strong', { text: 'Design agent' }),
      el('span', { class: 'muted', text:
        view.busy ? ' running — pause the session to change it'
          : view.started ? ' changes apply from the next turn after Start'
            : ' not started — review it, change anything, then start' }));

    const saveButton = el('button', {
      type: 'button', class: 'btn tiny primary', disabled: !dirty || !view.editable,
      text: 'Save changes', onclick: () => this.saveDraft(),
    });
    const discard = el('button', {
      type: 'button', class: 'btn tiny ghost', disabled: !dirty,
      text: 'Discard', onclick: () => this.render(this.view),
    });
    const resetAll = el('button', {
      type: 'button', class: 'btn tiny ghost',
      disabled: !view.exists || !view.editable,
      title: 'Delete this session\'s bootstrap.json and use the defaults',
      text: 'Reset all', onclick: () => this.resetAll(),
    });
    const start = canStart ? el('button', {
      type: 'button', class: 'btn primary', id: 'btn-bootstrap-start',
      title: dirty ? 'Saves your changes first' : 'Begin the conversation',
      text: 'Start conversation', onclick: () => this.startRun(),
    }) : null;

    return el('div', { class: 'bs-head' }, title,
      el('div', { class: 'bs-actions' },
        el('span', { class: 'muted bs-status', text: this.status || (dirty ? 'unsaved changes' : '') }),
        discard, resetAll, saveButton, start));
  }

  notices() {
    const view = this.view;
    const box = el('div', { class: 'bs-notices' });
    box.append(el('div', { class: 'muted' },
      'Saved per session in ', el('code', { text: view.path }),
      view.exists ? ' — only what differs from the defaults is written.'
        : ' (nothing changed yet; everything below is the default).'));
    for (const warning of view.warnings || []) {
      box.append(el('div', { class: 'bs-warning', text: warning }));
    }
    return box;
  }

  nodeList() {
    const list = el('div', { class: 'bs-nodes' });
    for (const name of this.view.order) {
      list.append(this.nodeCard(this.view.nodes[name]));
    }
    return list;
  }

  nodeCard(node) {
    const modified = this.nodeModified(node.name);
    const card = el('details', {
      class: `bs-node bs-${node.kind}`, 'data-node': node.name,
      open: this.open.has(node.name),
    });
    card.addEventListener('toggle', () => {
      if (card.open) this.open.add(node.name); else this.open.delete(node.name);
    });

    const backend = node.backend
      ? `${node.backend.provider}${node.backend.model ? ` · ${node.backend.model}` : ''}` : '';
    card.append(el('summary', {},
      el('span', { class: 'bs-name', text: node.name }),
      el('span', { class: `bs-kind kind-${node.kind}`, text: KIND_LABEL[node.kind] || node.kind }),
      backend ? el('span', { class: 'muted', text: backend }) : null,
      modified ? el('span', { class: 'bs-modified', text: 'changed' }) : null));

    card.append(el('p', { class: 'bs-summary', text: node.summary }));

    if (node.kind === 'agent') {
      card.append(this.backendForm(node), this.promptForms(node),
        el('div', { class: 'field' },
          el('span', { class: 'field-label', text: 'Per-turn prompt (built from state, not editable)' }),
          el('p', { class: 'bs-turn', text: node.turn_prompt })));
    } else if (node.kind === 'human') {
      card.append(this.commandList(node));
    } else if (node.name === 'validator') {
      card.append(this.attemptsForm());
    }
    return card;
  }

  backendForm(node) {
    const draft = this.draft.nodes[node.name];
    const disabled = !this.view.editable;
    const box = el('div', { class: 'bs-backend' });

    const provider = el('select', { disabled, onchange: (event) => {
      draft.provider = event.target.value;
      // A model id belongs to the provider it was named for: never carry one
      // across. Preselect what that provider itself says is its default.
      draft.model = this.defaultModel(draft.provider);
      draft.options = {};
      this.draw();
    } });
    for (const name of this.view.providers) {
      provider.append(el('option', { value: name, text: name, selected: name === draft.provider }));
    }

    // The provider's OWN list (GET /api/models) -- a dropdown of exactly
    // what it accepts. A provider that could not answer, or before the list
    // has loaded, falls back to typing the id.
    const listed = this.models?.[draft.provider];
    const offered = listed?.models || [];
    let model;
    if (offered.length) {
      model = el('select', { disabled, onchange: (event) => {
        draft.model = event.target.value;
        this.touch();
      } });
      model.append(el('option', { value: '', text: '(provider default)', selected: !draft.model }));
      if (draft.model && !offered.some((m) => m.id === draft.model)) {
        // Kept selectable rather than silently swapped for the first option.
        model.append(el('option', { value: draft.model, selected: true,
          text: `${draft.model}  (not in ${draft.provider}'s list)` }));
      }
      for (const m of offered) {
        const label = m.label && m.label !== m.id ? `${m.id} — ${m.label}` : m.id;
        model.append(el('option', { value: m.id, selected: m.id === draft.model,
          text: label + (m.default ? '  (default)' : '') }));
      }
    } else {
      model = el('input', {
        value: draft.model, disabled, spellcheck: 'false',
        placeholder: '(provider default)',
        oninput: (event) => { draft.model = event.target.value.trim(); this.touch(); },
      });
    }
    const suggestions = el('div', { class: 'muted bs-models-note', text:
      !this.models ? 'Asking the providers which models they offer...'
        : listed?.error ? `${draft.provider}: ${listed.error}`
          : listed?.source ? `from ${listed.source}` : '' });

    const defaults = node.default_backend;
    box.append(
      el('div', { class: 'bs-row' },
        el('label', { class: 'field' }, el('span', { class: 'field-label', text: 'Provider' }), provider),
        el('label', { class: 'field' }, el('span', { class: 'field-label', text: 'Model' }), model, suggestions),
        el('div', { class: 'field' }, el('span', { class: 'field-label', text: 'Access' }),
          el('span', { class: 'bs-fixed', title: node.access === 'read_only'
            ? 'Only looks at the repository'
            : 'Runs commands (its paper lookups) in the session\'s own workspace',
            text: node.access }))),
      el('div', { class: 'muted' },
        `Default: ${defaults.provider}${defaults.model ? ` · ${defaults.model}` : ''} `,
        '(from the built-in defaults and .agent/config.json).'));

    if (draft.provider !== node.backend.provider) {
      box.append(el('div', { class: 'muted bs-note',
        text: `Save to see the ${draft.provider} backend's options.` }));
      return box;
    }

    const grid = el('div', { class: 'bs-options' });
    for (const option of node.options) {
      const value = draft.options[option.name];
      const input = el('input', {
        disabled, spellcheck: 'false',
        type: option.type === 'string' ? 'text' : 'number',
        step: option.type === 'integer' ? '1' : 'any',
        value: value === null || value === undefined ? '' : String(value),
        placeholder: option.default === null ? '(none)' : String(option.default),
        oninput: (event) => {
          draft.options[option.name] = event.target.value === '' ? null : event.target.value;
          this.touch();
        },
      });
      grid.append(el('label', { class: 'field', title: option.help },
        el('span', { class: 'field-label', text: option.name + (option.modified ? ' •' : '') }),
        input,
        option.help ? el('span', { class: 'muted', text: option.help }) : null));
    }
    if (node.options.length) {
      box.append(el('details', { class: 'bs-options-box', open: node.options.some((o) => o.modified) },
        el('summary', { class: 'muted', text: 'Timeouts and limits' }), grid));
    }
    return box;
  }

  promptForms(node) {
    const draft = this.draft.nodes[node.name];
    const box = el('div', { class: 'bs-prompts' });
    for (const prompt of node.prompts) {
      const text = draft.prompts[prompt.name];
      const counter = el('span', { class: 'muted bs-count' });
      const area = el('textarea', {
        rows: prompt.name === 'repair_prefix' ? 3 : 14, spellcheck: 'false',
        disabled: !this.view.editable,
      });
      area.value = text;
      const updateCount = () => {
        const changed = area.value !== prompt.default;
        counter.textContent = `${area.value.length.toLocaleString()} chars`
          + (changed ? ' · changed' : '');
      };
      area.addEventListener('input', () => {
        draft.prompts[prompt.name] = area.value;
        updateCount();
        this.touch();
      });
      updateCount();

      const reset = el('button', {
        type: 'button', class: 'btn tiny ghost', disabled: !this.view.editable,
        text: 'Default', title: 'Put the default text back (then Save)',
        onclick: () => {
          area.value = prompt.default;
          draft.prompts[prompt.name] = prompt.default;
          updateCount();
          this.touch();
        },
      });

      box.append(el('div', { class: 'field bs-prompt' },
        el('div', { class: 'bs-prompt-head' },
          el('span', { class: 'field-label', text: prompt.label }), counter, reset),
        el('span', { class: 'muted', text: prompt.sent }),
        area));
    }
    return box;
  }

  commandList(node) {
    const list = el('ul', { class: 'bs-commands' });
    for (const command of node.commands) {
      list.append(el('li', {},
        el('code', { text: `/${command.name}${command.argument ? ` ${command.argument}` : ''}` }),
        ' ', command.summary,
        command.contexts.length
          ? el('span', { class: 'muted', text: `  (at: ${command.contexts.join(', ')})` }) : null));
    }
    return list;
  }

  attemptsForm() {
    const limits = this.view.max_design_attempts;
    const input = el('input', {
      type: 'number', min: '1', max: String(limits.limit), step: '1',
      disabled: !this.view.editable, value: String(this.draft.max_design_attempts),
      oninput: (event) => {
        this.draft.max_design_attempts = event.target.value;
        this.touch();
      },
    });
    return el('label', { class: 'field bs-attempts' },
      el('span', { class: 'field-label', text: 'Max design attempts' }), input,
      el('span', { class: 'muted', text:
        `How many times the designer may try to fix a rejected design before `
        + `asking you. Default ${limits.default}.` }));
  }

  // ---- state -------------------------------------------------------------

  touch() {
    // Only the header depends on dirtiness; redrawing the forms would steal
    // focus from the textarea being typed in.
    const head = this.container.querySelector('.bs-head');
    if (head) head.replaceWith(this.header());
  }

  isDirty() {
    if (!this.view || !this.draft) return false;
    return JSON.stringify(this.draft) !== JSON.stringify(draftFrom(this.view));
  }

  nodeModified(name) {
    const node = this.view.nodes[name];
    if (node.kind === 'agent') {
      return node.backend.modified || node.options.some((o) => o.modified)
        || node.prompts.some((p) => p.modified);
    }
    if (name === 'validator') {
      return this.view.max_design_attempts.value !== this.view.max_design_attempts.default;
    }
    return false;
  }

  reveal(name) {
    const card = this.container.querySelector(`.bs-node[data-node="${name}"]`);
    if (!card) return;
    card.open = true;
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  body() {
    const nodes = {};
    for (const [name, draft] of Object.entries(this.draft.nodes)) {
      nodes[name] = {
        provider: draft.provider,
        model: draft.model,
        options: draft.options,
        prompts: draft.prompts,
      };
    }
    return { nodes, max_design_attempts: this.draft.max_design_attempts };
  }

  async saveDraft() {
    this.status = 'saving...';
    this.touch();
    try {
      this.render(await this.api.save(this.body()));
      this.status = 'saved';
      this.touch();
      return true;
    } catch (error) {
      this.status = error.message || 'save failed';
      this.touch();
      this.onError(error);
      return false;
    }
  }

  async resetAll() {
    try {
      this.render(await this.api.reset());
      this.status = 'reset to defaults';
      this.touch();
    } catch (error) {
      this.onError(error);
    }
  }

  async startRun() {
    if (this.isDirty() && !(await this.saveDraft())) return;
    try {
      await this.api.start();
      await this.refresh();
    } catch (error) {
      this.onError(error);
    }
  }
}

/** The editable part of a view, in the shape `body()` sends. */
function draftFrom(view) {
  const nodes = {};
  for (const name of view.order) {
    const node = view.nodes[name];
    if (node.kind !== 'agent') continue;
    const options = {};
    for (const option of node.options) {
      options[option.name] = option.value === undefined ? null : option.value;
    }
    const prompts = {};
    for (const prompt of node.prompts) prompts[prompt.name] = prompt.value;
    nodes[name] = {
      provider: node.backend.provider,
      model: node.backend.model || '',
      options,
      prompts,
    };
  }
  return { nodes, max_design_attempts: view.max_design_attempts.value };
}
