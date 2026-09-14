/*
  WHAT:  The provider-login panel -- who is signed in, and signing them in.
  WHY:   A turn on a signed-out provider fails minutes in, with a message about
         the model rather than about the login. This is the one screen that
         answers it before anything is spent.
  CONCEPT: Poll while a flow is running, stop when it settles.

  --------------------------------------------------------------------------
  WHY POLLING AND NOT THE EVENT STREAM
  --------------------------------------------------------------------------
  The SSE stream in api.js belongs to a SESSION. A login has nothing to do with
  a session -- you do it before there is one, which is exactly the case that
  matters -- so it cannot ride on that stream without inventing a second
  meaning for it. A login lasts a minute or two and produces a screen's worth
  of text; a one-second poll is the whole cost.
*/

import { api } from './api.js';

const el = (id) => document.getElementById(id);

//: While a flow runs. Fast enough that the code appears as soon as the CLI
//: prints it, slow enough to be invisible next to a human reading a link.
const POLL_MS = 1000;

export class AuthPanel {
  constructor({ getRepo }) {
    this.getRepo = getRepo;
    this.timer = null;
    this.provider = null;     // the flow on screen, if any
    this.providers = [];
    this.canLogin = true;

    this.dialog = el('dialog-auth');

    el('btn-auth').addEventListener('click', () => this.open());
    el('auth-close').addEventListener('click', () => this.close());
    el('btn-auth-cancel').addEventListener('click', () => this.cancel());
    el('btn-auth-submit').addEventListener('click', () => this.submit());
    el('btn-auth-copy').addEventListener('click', () => this.copyCode());

    el('auth-flow-input').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        this.submit();
      }
    });
  }

  // ---- the dot in the topbar -------------------------------------------

  /**
   * Refresh the summary without opening anything.
   *
   * Called at startup and after a run ends. Deliberately quiet on failure: a
   * server too old for these routes should leave the rest of the page working,
   * and the dot simply stays neutral.
   */
  async poll() {
    try {
      await this.load();
    } catch {
      el('auth-dot').className = 'auth-dot';
    }
  }

  async load() {
    const body = await api.authStatus(this.getRepo());
    this.providers = body.providers || [];
    this.canLogin = body.can_login_here !== false;
    this.whyNot = body.why_not || '';

    // Only what is IN USE colours the dot. A signed-out provider nothing is
    // pointed at is not a problem, and an amber dot that is always amber is a
    // dot nobody looks at.
    const broken = this.providers.filter((p) => p.in_use && !p.ok);
    const dot = el('auth-dot');
    dot.className = `auth-dot ${broken.length ? 'bad' : 'good'}`;
    el('btn-auth').title = broken.length
      ? `${broken.map((p) => p.provider).join(', ')} — not signed in`
      : 'Provider logins';
    return body;
  }

  // ---- the dialog --------------------------------------------------------

  async open() {
    this.showFlow(null);
    el('auth-list').replaceChildren();
    this.dialog.showModal();

    try {
      await this.load();
    } catch (error) {
      el('auth-why-not').hidden = false;
      el('auth-why-not').textContent = String(error);
      return;
    }
    this.render();
  }

  close() {
    this.stopPolling();
    this.dialog.close();
  }

  render() {
    const why = el('auth-why-not');
    why.hidden = this.canLogin;
    why.textContent = this.canLogin ? '' : this.whyNot;

    const list = el('auth-list');
    list.replaceChildren();

    for (const provider of this.providers) {
      list.append(this.row(provider));
    }
  }

  row(provider) {
    const row = document.createElement('div');
    row.className = `auth-row ${provider.ok ? 'ok' : (provider.logged_in === null ? 'unknown' : 'bad')}`;

    const name = document.createElement('span');
    name.className = 'auth-name';
    name.textContent = provider.provider;

    const badge = document.createElement('span');
    badge.className = 'auth-badge';
    // Three states, not two. "Could not tell" must not read as "signed out",
    // or people re-run a login that is working -- the same three-valued rule
    // the probe itself keeps. See agent/backends/auth.py.
    badge.textContent = !provider.installed ? 'not installed'
      : provider.logged_in === null ? 'unknown'
      : provider.logged_in ? 'signed in' : 'signed out';

    const detail = document.createElement('span');
    detail.className = 'auth-detail';
    detail.textContent = [
      provider.account,
      provider.method,
      provider.detail,
    ].filter(Boolean).join(' · ');

    const head = document.createElement('div');
    head.className = 'auth-row-head';
    head.append(name, badge);
    if (provider.in_use) {
      const used = document.createElement('span');
      used.className = 'auth-inuse';
      used.textContent = 'in use';
      head.append(used);
    }

    row.append(head, detail);
    row.append(this.action(provider));
    return row;
  }

  /**
   * What you can do about this provider.
   *
   * Three outcomes, and the third is the honest one: `agy` signs in through a
   * full-screen terminal interface, so the panel shows the command rather than
   * a button that cannot work. The server decides which -- browser_login comes
   * from the probe, so this stays true if a CLI grows a headless flow.
   */
  action(provider) {
    const box = document.createElement('div');
    box.className = 'auth-action';

    if (!provider.installed) {
      box.append(note(`Install ${provider.login_command.split(' ')[0] || provider.provider} on the server first.`));
      return box;
    }

    if (!provider.browser_login || !this.canLogin) {
      if (provider.note) box.append(note(provider.note));
      if (provider.login_command) box.append(command(provider.login_command));
      return box;
    }

    const button = document.createElement('button');
    button.type = 'button';
    button.className = `btn tiny ${provider.ok ? 'ghost' : 'primary'}`;
    button.textContent = provider.ok ? 'Sign in again' : 'Sign in';
    button.addEventListener('click', () => this.start(provider));
    box.append(button);

    if (provider.ok) {
      // Worth a line, because it is worse than it sounds: MEASURED, `codex
      // login` deletes the stored credentials the moment it starts, not when
      // it succeeds. The server puts them back if the attempt does not end in
      // a login -- which is the half somebody needs to know before clicking.
      box.append(note('Replaces the login this machine is using. If the '
        + 'attempt does not finish, the current one is put back.'));
    }
    return box;
  }

  // ---- running a flow ----------------------------------------------------

  async start(provider) {
    this.provider = provider.provider;
    this.showFlow(provider.provider);
    el('auth-flow-output').textContent = 'Starting…';

    try {
      this.paint(await api.loginStart(provider.provider, provider.ok));
    } catch (error) {
      this.fail(error);
      return;
    }
    this.startPolling();
  }

  startPolling() {
    this.stopPolling();
    this.timer = setInterval(async () => {
      if (!this.provider) return;
      try {
        const body = await api.loginState(this.provider);
        this.paint(body);
        if (!body.running) {
          this.stopPolling();
          // The row above the flow still says "signed out"; the whole point of
          // finishing is that it no longer does.
          await this.load();
          this.render();
        }
      } catch (error) {
        this.stopPolling();
        this.fail(error);
      }
    }, POLL_MS);
  }

  stopPolling() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  paint(body) {
    el('auth-flow-title').textContent = `Signing in to ${body.provider} — ${body.state}`;

    const url = el('auth-flow-url');
    url.textContent = body.url || '(waiting for the link…)';
    url.href = body.url || '#';

    el('auth-flow-code-step').hidden = !body.code;
    el('auth-flow-code').textContent = body.code || '';

    el('auth-flow-input-row').hidden = !body.accepts_input;
    el('auth-flow-input-label').textContent = body.input_label || 'Paste the code';

    const error = el('auth-flow-error');
    const restored = (body.restored || []).length
      // Said out loud rather than done silently. Somebody who just watched a
      // sign-in fail needs to know whether it cost them the login they had.
      ? ' The previous login has been put back, so nothing was lost.'
      : '';
    error.hidden = !body.error && !restored;
    error.textContent = (body.error || '') + restored;

    el('auth-flow-output').textContent = body.output || '';
    el('btn-auth-cancel').hidden = !body.running;
  }

  fail(error) {
    const box = el('auth-flow-error');
    box.hidden = false;
    box.textContent = String(error);
    el('btn-auth-cancel').hidden = true;
  }

  showFlow(provider) {
    el('auth-flow').hidden = !provider;
    if (!provider) {
      this.stopPolling();
      this.provider = null;
      return;
    }
    el('auth-flow-error').hidden = true;
    el('auth-flow-code-step').hidden = true;
    el('auth-flow-input-row').hidden = true;
    el('btn-auth-cancel').hidden = false;
  }

  async submit() {
    const input = el('auth-flow-input');
    const text = input.value.trim();
    if (!text || !this.provider) return;
    // Cleared immediately: it is a single-use credential and there is no
    // reason for it to sit in a form field afterwards.
    input.value = '';
    try {
      this.paint(await api.loginInput(this.provider, text));
    } catch (error) {
      this.fail(error);
    }
  }

  async cancel() {
    if (!this.provider) return;
    this.stopPolling();
    try {
      await api.loginCancel(this.provider);
      this.paint(await api.loginState(this.provider));
    } catch { /* it had already finished */ }
    await this.load();
    this.render();
  }

  copyCode() {
    const code = el('auth-flow-code').textContent;
    if (code && navigator.clipboard) navigator.clipboard.writeText(code);
  }
}

function note(text) {
  const node = document.createElement('p');
  node.className = 'auth-note';
  node.textContent = text;
  return node;
}

function command(text) {
  const node = document.createElement('code');
  node.className = 'auth-command';
  node.textContent = text;
  return node;
}
