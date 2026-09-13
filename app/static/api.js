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
  // Query values that are null, undefined or '' are dropped rather than sent
  // as empty strings: several endpoints treat a present-but-empty parameter
  // differently from an absent one.
  url(path, params) {
    if (!params) return path;
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') q.append(k, v);
    }
    const s = q.toString();
    return s ? `${path}?${s}` : path;
  },

  async request(method, path, { body, params, raw = false } = {}) {
    const opts = { method };
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

  get(path, params)   { return api.request('GET', path, { params }); },
  post(path, body)    { return api.request('POST', path, { body }); },
  put(path, body)     { return api.request('PUT', path, { body }); },
  patch(path, body)   { return api.request('PATCH', path, { body }); },
  del(path, body)     { return api.request('DELETE', path, { body }); },
};
