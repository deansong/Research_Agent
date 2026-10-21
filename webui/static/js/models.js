// The New session dialog's model picker: one row per role, a provider
// dropdown and a model dropdown, filled from what each provider says it
// offers (GET /api/models) and preset to what config already resolves each
// role to (GET /api/config).
//
// Only rows somebody CHANGED are sent, as --backend-role style overrides, so
// leaving the section closed means exactly what it meant before it existed:
// the config files decide.

import { api } from './api.js';

const el = (id) => document.getElementById(id);

//: What each role is for, in the words a person choosing a model needs.
const ROLE_NOTES = {
  discussor: 'asks what you want',
  planner: 'writes the plan',
  designer: 'designs the agent graph',
  coder: 'agent nodes that write code',
  runner: 'agent nodes that run experiments',
  coder_light: 'work nodes marked tier "light" -- a cheaper model',
  checker: 'agent nodes that verify (read-only)',
  supervisor: 'reviews each run, decides what next',
};

let modelsByProvider = null;   // provider -> {models: [...], error, source}
let rows = [];                 // [{role, initial: {provider, model}, provider, model}]
let loading = null;

export function wireModelPicker(repoInput) {
  const details = el('new-models');
  details.addEventListener('toggle', () => {
    if (details.open) loadModelPicker(repoInput.value.trim() || '.');
  });
  el('new-models-refresh').addEventListener('click', () => {
    modelsByProvider = null;
    loadModelPicker(repoInput.value.trim() || '.', true);
  });
}

export async function loadModelPicker(repo, refresh = false) {
  if (loading) return loading;
  const status = el('new-models-status');
  status.textContent = 'Asking each provider which models it offers…';
  loading = (async () => {
    try {
      const [config, listed] = await Promise.all([
        api.config(repo),
        modelsByProvider && !refresh ? null : api.models(refresh),
      ]);
      if (listed) {
        modelsByProvider = {};
        for (const entry of listed.providers) modelsByProvider[entry.provider] = entry;
      }
      rows = Object.entries(config.roles).map(([role, spec]) => ({
        role,
        initial: { provider: spec.provider || '', model: spec.model || '' },
        provider: spec.provider || '',
        model: spec.model || '',
      }));
      render();
      const broken = Object.values(modelsByProvider).filter((p) => p.error);
      status.textContent = broken.length
        ? broken.map((p) => `${p.provider}: ${p.error}`).join(' · ')
        : 'Preset to what your config resolves each role to. Change only what you want.';
    } catch (error) {
      status.textContent = `Could not load models: ${error}`;
    } finally {
      loading = null;
    }
  })();
  return loading;
}

/** `role=provider:model` for every row that differs from its preset. */
export function modelOverrides() {
  return rows
    .filter((r) => r.provider && (r.provider !== r.initial.provider || r.model !== r.initial.model))
    .map((r) => `${r.role}=${r.provider}${r.model ? `:${r.model}` : ''}`);
}

/** Back to the presets -- called when the dialog opens again. */
export function resetModelPicker() {
  for (const row of rows) {
    row.provider = row.initial.provider;
    row.model = row.initial.model;
  }
  if (rows.length) render();
}

function render() {
  const body = el('new-models-rows');
  body.replaceChildren();
  const providers = Object.keys(modelsByProvider || {}).sort();

  for (const row of rows) {
    const tr = document.createElement('tr');

    const name = document.createElement('td');
    name.className = 'model-role';
    name.textContent = row.role;
    if (ROLE_NOTES[row.role]) {
      const note = document.createElement('span');
      note.className = 'muted';
      note.textContent = ` ${ROLE_NOTES[row.role]}`;
      name.append(note);
    }

    const providerSelect = document.createElement('select');
    for (const p of new Set([...providers, row.provider].filter(Boolean))) {
      providerSelect.append(option(p, p, p === row.provider));
    }

    const modelSelect = document.createElement('select');
    const fillModels = () => {
      modelSelect.replaceChildren();
      const offered = (modelsByProvider?.[row.provider]?.models) || [];
      const ids = offered.map((m) => m.id);
      if (!row.model || !ids.includes(row.model)) {
        // Keep whatever is configured selectable even when the provider did
        // not list it -- an alias, or a provider that could not answer.
        modelSelect.append(option(row.model, row.model
          ? `${row.model} (configured)` : '(provider default)', true));
      }
      for (const m of offered) {
        const label = m.label && m.label !== m.id ? `${m.id} — ${m.label}` : m.id;
        modelSelect.append(option(m.id, label + (m.default ? ' (default)' : ''),
                                  m.id === row.model));
      }
    };
    fillModels();

    providerSelect.addEventListener('change', () => {
      row.provider = providerSelect.value;
      // A model id belongs to the provider it was named for.
      const offered = (modelsByProvider?.[row.provider]?.models) || [];
      const preferred = offered.find((m) => m.default) || offered[0];
      row.model = row.provider === row.initial.provider
        ? row.initial.model : (preferred ? preferred.id : '');
      fillModels();
    });
    modelSelect.addEventListener('change', () => { row.model = modelSelect.value; });

    const providerCell = document.createElement('td');
    providerCell.append(providerSelect);
    const modelCell = document.createElement('td');
    modelCell.append(modelSelect);
    tr.append(name, providerCell, modelCell);
    body.append(tr);
  }
}

function option(value, label, selected) {
  const o = document.createElement('option');
  o.value = value;
  o.textContent = label;
  o.selected = Boolean(selected);
  return o;
}
