/*
  WHAT:  The Files tab -- the files in this session's own folder: its agent,
         plan and reports, and the results a run wrote. Nothing outside it.
  WHY:   A run produces a dataloader, a training script, a baseline and a
         report, and until this tab the only way to read any of them was to
         leave the browser and go find them on disk. The inspector could show
         you the prompt that ASKED for a training script, and the command that
         ran it, and not the script.
  CONCEPT: A list on the left, one file on the right.

  --------------------------------------------------------------------------
  WHY SESSION FILES AND RESULTS ARE GROUPED, NOT MIXED
  --------------------------------------------------------------------------
  Measured on a real session: 1,985 artifacts, one JSON per GPU allocation
  and per smoke cell. In one time-ordered list the dozen files a person wants
  to read sit a thousand rows down. So the server returns the session's own
  files first and this renders the two groups under their own headings, with
  the results group collapsed.

  --------------------------------------------------------------------------
  NO SYNTAX HIGHLIGHTING
  --------------------------------------------------------------------------
  Deliberate. It would mean a highlighter library from a CDN, and this page is
  served from a loopback server that is meant to work with no network at all.
  A monospace <pre> with line numbers is most of the value for none of that.
*/

const KB = 1024;

function size(bytes) {
  if (bytes < KB) return `${bytes} B`;
  if (bytes < KB * KB) return `${(bytes / KB).toFixed(1)} KB`;
  return `${(bytes / KB / KB).toFixed(1)} MB`;
}

function when(seconds) {
  const age = Date.now() / 1000 - seconds;
  if (age < 90) return 'just now';
  if (age < 3600) return `${Math.round(age / 60)}m ago`;
  if (age < 86400) return `${Math.round(age / 3600)}h ago`;
  return `${Math.round(age / 86400)}d ago`;
}

export class FilesPanel {
  constructor(listEl, viewEl, { onRead }) {
    this.listEl = listEl;
    this.viewEl = viewEl;
    this.onRead = onRead;
    this.selected = '';
    this.files = [];
  }

  render(payload) {
    this.files = payload?.files || [];
    this.listEl.replaceChildren();

    if (!this.files.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      const p = document.createElement('p');
      p.className = 'muted';
      p.textContent = 'Nothing written yet.';
      empty.append(p);
      this.listEl.append(empty);
      return;
    }

    const code = this.files.filter((f) => f.origin === 'code');
    const own = this.files.filter((f) => f.origin === 'session');
    const results = this.files.filter((f) => f.origin === 'artifact');
    // Code and session files open, results shut. The asymmetry is the point:
    // those are a handful of files somebody means to read, results are
    // usually two thousand files somebody means to spot-check.
    if (code.length) this.listEl.append(this.group('Code', code, true));
    if (own.length) this.listEl.append(this.group('This session', own, true));
    if (results.length) this.listEl.append(this.group('Results', results, false));
  }

  group(title, entries, open) {
    const box = document.createElement('details');
    box.className = 'file-group';
    box.open = open;
    const head = document.createElement('summary');
    head.textContent = `${title} (${entries.length})`;
    box.append(head);

    for (const entry of entries) {
      const row = document.createElement('button');
      row.className = 'file-row';
      row.classList.toggle('selected', entry.path === this.selected);
      // Not disabled: a row you cannot read still tells you the file exists
      // and how big it is, and a disabled button says neither.
      row.classList.toggle('unreadable', !entry.readable);

      const name = document.createElement('span');
      name.className = 'file-name';
      // The root prefix is what the server needs; the tail is what a person
      // reads.
      name.textContent = entry.path.replace(/^session\/(workspace\/)?/, '');
      const meta = document.createElement('span');
      meta.className = 'file-meta';
      meta.textContent = `${size(entry.size)} · ${when(entry.modified)}`;
      row.append(name, meta);
      row.addEventListener('click', () => this.select(entry));
      box.append(row);
    }
    return box;
  }

  async select(entry) {
    this.selected = entry.path;
    this.render({ files: this.files });
    this.show({ path: entry.path, text: '', loading: true });
    try {
      const body = await this.onRead(entry.path);
      this.show(body);
    } catch (problem) {
      this.show({ path: entry.path, error: problem.toString() });
    }
  }

  show(body) {
    this.viewEl.replaceChildren();

    const head = document.createElement('div');
    head.className = 'file-view-head';
    const title = document.createElement('code');
    title.textContent = body.path || '';
    head.append(title);
    if (body.size !== undefined) {
      const meta = document.createElement('span');
      meta.className = 'muted';
      meta.textContent = size(body.size);
      head.append(meta);
    }
    this.viewEl.append(head);

    if (body.loading) {
      this.viewEl.append(this.note('Reading…'));
      return;
    }
    if (body.error) {
      this.viewEl.append(this.note(body.error, 'error'));
      return;
    }
    // A file we will not send whole says WHY -- "too big" and "not text" are
    // different problems with different answers, and "nothing appeared" is
    // neither.
    if (body.skipped) {
      this.viewEl.append(this.note(`Not shown: ${body.skipped}.`));
      return;
    }

    const pre = document.createElement('pre');
    pre.className = 'file-text';
    const lines = (body.text || '').split('\n');
    const gutter = document.createElement('code');
    gutter.className = 'file-gutter';
    gutter.textContent = lines.map((_, i) => i + 1).join('\n');
    const text = document.createElement('code');
    text.className = 'file-body';
    text.textContent = body.text || '';
    pre.append(gutter, text);
    this.viewEl.append(pre);
  }

  note(message, kind) {
    const box = document.createElement('div');
    box.className = kind === 'error' ? 'file-note error' : 'file-note muted';
    box.textContent = message;
    return box;
  }
}
