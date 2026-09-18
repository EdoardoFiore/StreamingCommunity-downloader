// One way to call the panel's API.
//
// Every call site used to hand-roll the same four steps: build the URL, set
// the JSON headers, parse with safeJson, then branch on res.ok and dig the
// message out of the payload. Eighty-one of them, each slightly different,
// and the ones that forgot the last step reported "[object Object]".
//
// Loaded after core.js, which installs the fetch wrapper that carries the
// CSRF token and redirects on 401. This builds on that rather than replacing
// it, so calls written before it existed keep working unchanged.

class ApiError extends Error {
  constructor(status, message, data) {
    super(message || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

// FastAPI answers a validation failure with an *array* of error objects, so
// the `data.detail || 'Errore'` idiom renders "[object Object]".
function _detailText(data) {
  const detail = data && data.detail;
  if (!detail) return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(e => (e && (e.msg || e.message)) || '').filter(Boolean).join('; ');
  }
  return String(detail);
}

const api = {
  // Only null and undefined are dropped. An empty string is a value the
  // caller chose, and removing it turns a supplied-but-empty *required*
  // parameter into a missing one - which is how the episode list started
  // answering "field required" whenever the site version was not yet known.
  url(path, params) {
    if (!params) return path;
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) q.append(k, v);
    }
    const s = q.toString();
    return s ? `${path}?${s}` : path;
  },

  // `signal` is here because the search and the start page both abandon a
  // call in flight when the query changes under them. Without it those two
  // would have had to stay on raw fetch, which is how a second way of calling
  // the API survives a migration meant to end it.
  async request(method, path, { body, params, signal, raw = false } = {}) {
    const opts = { method };
    if (signal) opts.signal = signal;
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(api.url(path, params), opts);
    if (raw) return res;

    // 204, and any other empty body: there is nothing to parse, and safeJson
    // would throw on the empty string.
    const text = await res.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); }
      catch {
        if (res.ok) throw new Error(`HTTP ${res.status}: ${text.slice(0, 120)}`);
        throw new ApiError(res.status, text.slice(0, 120), null);
      }
    }
    if (!res.ok) throw new ApiError(res.status, _detailText(data), data);
    return data;
  },

  get(path, params, opts) { return api.request('GET', path, { params, ...opts }); },
  post(path, body)    { return api.request('POST', path, { body }); },
  put(path, body)     { return api.request('PUT', path, { body }); },
  patch(path, body)   { return api.request('PATCH', path, { body }); },
  del(path, body)     { return api.request('DELETE', path, { body }); },
};


// The message to show when a call fails.
//
// The hand-rolled call sites made a distinction worth keeping: a non-ok
// response showed the server's own detail, while the surrounding catch showed
// "Errore di rete". Once api.* throws for both, a single catch would collapse
// them and start blaming the network for a 403. So: an ApiError carries the
// server's words, anything else really is the connection.
function errText(error, fallback = 'Errore') {
  if (error instanceof ApiError) return error.message || fallback;
  return 'Errore di rete';
}
