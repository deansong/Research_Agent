/*
  WHAT:  Everything that talks to the server.
  WHY:   One place that knows the endpoints, so a route rename is one edit.
  CONCEPT: fetch() for requests, EventSource for the live feed.

  --------------------------------------------------------------------------
  THE ONE RULE ABOUT ERRORS
  --------------------------------------------------------------------------
  When the server says no, we say so. The reference project we drew on catches
  a failed request and quietly applies a locally-invented change instead, so
  the user's graph ends up in a state the server never sanctioned and nobody is
  told. Every call here throws a Problem, and the caller shows it.
*/

/** The server's error envelope -- the same shape as a validation problem. */
export class Problem extends Error {
  constructor({ code, where, message }, status) {
    super(message || 'Request failed');
    this.code = code || 'error';
    this.where = where || '';
    this.status = status;
  }

  /**
   * The whole problem in one line, `where` included.
   *
   * `where` carries the thing the problem is ABOUT -- the path that was tried,
   * the node that is unreachable -- and leaving it out is how "bad_repo: Not a
   * directory" happens: technically complete, and it does not tell you which
   * directory the server actually looked at.
   */
  toString() {
    return this.where
      ? `${this.code}: ${this.where} — ${this.message}`
      : `${this.code}: ${this.message}`;
  }
}

async function request(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    let detail = { code: `http_${response.status}`, message: response.statusText };
    try {
      const payload = await response.json();
      // FastAPI nests our envelope under `detail`; its own validation errors
      // put a list there instead, hence the shape check.
      if (payload.detail && typeof payload.detail === 'object' && payload.detail.code) {
        detail = payload.detail;
      } else if (Array.isArray(payload.detail)) {
        detail = { code: 'invalid_request', message: describeValidation(payload.detail) };
      }
    } catch { /* not JSON; keep the status line */ }
    throw new Problem(detail, response.status);
  }

  return response.status === 204 ? null : response.json();
}

function describeValidation(errors) {
  return errors
    .map((e) => `${(e.loc || []).slice(1).join('.') || 'body'}: ${e.msg}`)
    .join('; ');
}

export const api = {
  listSessions:  (repo) => request('GET', `/api/sessions${repo ? `?repo=${encodeURIComponent(repo)}` : ''}`),
  createSession: (body) => request('POST', '/api/sessions', body),
  getSession:    (id)   => request('GET', `/api/sessions/${encodeURIComponent(id)}`),
  startSession:  (id, body) => request('POST', `/api/sessions/${encodeURIComponent(id)}/start`, body || {}),
  answer:        (id, text) => request('POST', `/api/sessions/${encodeURIComponent(id)}/answer`, { text }),
  pauseSession:  (id)   => request('POST', `/api/sessions/${encodeURIComponent(id)}/pause`, {}),
  stopSession:   (id)   => request('POST', `/api/sessions/${encodeURIComponent(id)}/stop`, {}),
  closeSession:  (id)   => request('DELETE', `/api/sessions/${encodeURIComponent(id)}`),
  getPlan:       (id)   => request('GET', `/api/sessions/${encodeURIComponent(id)}/plan`),
  putPlan:       (id, plan) => request('PUT', `/api/sessions/${encodeURIComponent(id)}/plan`, { plan }),
  getAgent:      (id)   => request('GET', `/api/sessions/${encodeURIComponent(id)}/agent`),
  putAgent:      (id, doc) => request('PUT', `/api/sessions/${encodeURIComponent(id)}/agent`, doc),
  validateAgent: (id, doc) => request('POST', `/api/sessions/${encodeURIComponent(id)}/agent/validate`, doc),
  nodeContext:   (id, name) =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/nodes/${encodeURIComponent(name)}/context`),
  nodeActivity:  (id, name) =>
    request('GET', `/api/sessions/${encodeURIComponent(id)}/nodes/${encodeURIComponent(name)}/activity`),
  // The turn running right now, whichever node it belongs to -- what the
  // "still working" line expands into.
  currentActivity: (id) => request('GET', `/api/sessions/${encodeURIComponent(id)}/activity`),
  config:        ()     => request('GET', '/api/config'),
  schema:        ()     => request('GET', '/api/schema'),
  defaults:      ()     => request('GET', '/api/defaults'),
};

/**
 * Subscribe to a session's events.
 *
 * EventSource does the reconnecting for us, and sends back the last `id:` it
 * saw as a Last-Event-ID header -- which is exactly why the server puts the
 * sequence number there. So a dropped connection resumes with no gap and no
 * repeats, and this function does not have to do anything clever about it.
 *
 * `handlers` maps an event type to a callback; see webui/events.py for the
 * closed list of types. Returns a function that closes the stream.
 */
export function subscribe(sessionId, handlers) {
  const source = new EventSource(`/api/sessions/${encodeURIComponent(sessionId)}/events`);

  for (const [type, handler] of Object.entries(handlers)) {
    if (type === 'onerror') continue;
    source.addEventListener(type, (event) => {
      let data = {};
      try { data = JSON.parse(event.data); } catch { /* a comment frame */ }
      handler(data);
    });
  }

  // Fires on a dropped connection too, which EventSource then retries by
  // itself -- so this is informational, not fatal.
  source.onerror = () => handlers.onerror?.(source.readyState);

  return () => source.close();
}
