/**
 * Shared API client — CSRF injection, AbortController, error handling
 */
var Api = (function() {
  var csrfToken = '';
  var csrfPromise = null;
  var _origFetch = window.fetch.bind(window);

  function isMutating(method) {
    return method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS';
  }

  function init() {
    if (csrfPromise) return csrfPromise;
    csrfPromise = _origFetch('/api/csrf-token')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        csrfToken = d.token || '';
        return csrfToken;
      })
      .catch(function(e) {
        csrfPromise = null;
        throw e;
      });
    return csrfPromise;
  }

  function headersHas(headers, name) {
    if (!headers) return false;
    if (headers instanceof Headers) return headers.has(name);
    return Object.prototype.hasOwnProperty.call(headers, name);
  }

  function setHeader(headers, name, value) {
    if (headers instanceof Headers) {
      headers.set(name, value);
      return headers;
    }
    headers = headers || {};
    headers[name] = value;
    return headers;
  }

  function parseResponse(r) {
    var contentType = r.headers.get('content-type') || '';
    var bodyPromise = contentType.indexOf('application/json') >= 0 ? r.json() : r.text();
    return bodyPromise.then(function(body) {
      if (!r.ok) {
        if (r.status === 401) {
          // Session expired — redirect to login after a short delay
          if (window.UI) UI.toast('会话已过期，请重新登录', 'error');
          setTimeout(function() { window.location.href = '/login'; }, 1500);
        }
        var err = new Error((body && body.error) || r.statusText || '请求失败');
        err.status = r.status;
        err.body = body;
        throw err;
      }
      return body;
    });
  }

  // Configurable global error handler
  var _onError = null;

  function onError(handler) {
    _onError = handler;
  }

  function _handleError(err) {
    if (_onError) {
      _onError(err);
    } else if (window.UI) {
      UI.toast((err.body && err.body.error) || err.message || '请求失败', 'error');
    }
  }

  // Override global fetch to auto-inject CSRF token on mutating requests
  window.fetch = function(url, opts) {
    opts = opts || {};
    var method = (opts.method || 'GET').toUpperCase();
    if (!isMutating(method)) {
      return _origFetch(url, opts);
    }
    return init().then(function() {
      if (csrfToken && !headersHas(opts.headers, 'X-CSRF-Token')) {
        opts.headers = setHeader(opts.headers, 'X-CSRF-Token', csrfToken);
      }
      return _origFetch(url, opts);
    });
  };

  /**
   * Fetch with auto-cancel — returns { promise, controller }.
   * Calling again with the same key aborts the previous request.
   */
  var _controllers = {};

  function fetchWithCancel(key, url, opts) {
    if (_controllers[key]) _controllers[key].abort();
    var ctrl = new AbortController();
    _controllers[key] = ctrl;
    opts = opts || {};
    opts.signal = ctrl.signal;
    var p = fetch(url, opts).catch(function(e) {
      if (e.name === 'AbortError') return { _aborted: true };
      throw e;
    });
    return { promise: p, controller: ctrl };
  }

  /** GET helper */
  function get(url) {
    return fetch(url).then(parseResponse);
  }

  /** POST helper */
  function post(url, data) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    }).then(parseResponse);
  }

  /** PUT helper */
  function put(url, data) {
    return fetch(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    }).then(parseResponse);
  }

  /** DELETE helper */
  function del(url, data) {
    var opts = { method: 'DELETE', headers: { 'Content-Type': 'application/json' } };
    if (data) opts.body = JSON.stringify(data);
    return fetch(url, opts).then(parseResponse);
  }

  return {
    init: init,
    get: get,
    post: post,
    put: put,
    del: del,
    fetchWithCancel: fetchWithCancel,
    parseResponse: parseResponse,
    onError: onError,
    csrfToken: function() { return csrfToken; }
  };
})();

window.Api = Api;
