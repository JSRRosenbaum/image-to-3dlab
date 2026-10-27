// Managed transport only. A lost submission is reconciled by identity, never replayed.
export function mountPrefix(pathname = globalThis.location?.pathname || '/') {
  const index = pathname.indexOf('/viewer/');
  return index < 0 ? '' : pathname.slice(0, index);
}

export function apiUrl(value) {
  if (typeof value !== 'string') return value;
  const prefix = mountPrefix();
  const target = new URL(value, globalThis.location?.href || 'http://localhost/');
  if (target.origin !== (globalThis.location?.origin || target.origin) || !target.pathname.startsWith('/')) return value;
  if (prefix && !target.pathname.startsWith(`${prefix}/`) && target.pathname !== prefix) target.pathname = prefix + target.pathname;
  return /^[a-z][a-z\d+.-]*:/i.test(value) ? target.href : `${target.pathname}${target.search}${target.hash}`;
}

const key = () => `assetfurnace:graft:pending:${location.origin}${mountPrefix()}`;
const mutationLockName = () => `assetfurnace:graft:mutation:${location.origin}${mountPrefix()}`;
function withMutationLock(callback) {
  const locks = globalThis.navigator?.locks;
  if (!locks?.request) throw new Error('Safe cross-tab mutation locking is unavailable; no request was sent.');
  return locks.request(mutationLockName(), callback);
}

async function reconcilePendingLocked({ wait = false } = {}) {
  const pending = readPending();
  if (!pending) return null;
  const deadline = Date.now() + (wait ? 120000 : 0);
  do {
    try {
      const response = await globalThis.fetch(apiUrl(`/api/graft/requests/${pending.id}`));
      if (response.ok) {
        const value = await response.json();
        const native = nativeResponse(value);
        if (native) {
          writePending(null);
          window.dispatchEvent(new CustomEvent('graft-reconciled', { detail: { ...pending, ...value } }));
          return native;
        }
      }
    } catch { /* Transient reconciliation failures never authorize a replay. */ }
    if (!wait || Date.now() >= deadline) break;
    await sleep(500);
  } while (true);
  throw new Error(`Request ${pending.id} is still uncertain. Its original owner is being reconciled; no work was resubmitted.`);
}

export function reconcilePending(options = {}) {
  return withMutationLock(() => reconcilePendingLocked(options));
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const readPending = () => JSON.parse(localStorage.getItem(key()) || 'null');
const writePending = (value) => value ? localStorage.setItem(key(), JSON.stringify(value)) : localStorage.removeItem(key());

function nativeResponse(value) {
  if (value.response) return new Response(JSON.stringify({ ...value.response.body, graft: value.graft }), {
    status: value.response.status, headers: { 'Content-Type': 'application/json' },
  });
  if (value.graft?.state === 'rejected') return new Response(JSON.stringify({ error: value.error || 'No compatible worker accepted this request.', graft: value.graft }), {
    status: 409, headers: { 'Content-Type': 'application/json' },
  });
  return null;
}


export function apiFetch(input, init = {}) {
  const method = (init.method || 'GET').toUpperCase();
  const url = apiUrl(input);
  if (method !== 'POST') return globalThis.fetch(url, init);
  return withMutationLock(() => postLocked(input, init, url));
}

async function postLocked(input, init, url) {
  if (readPending()) {
    await reconcilePendingLocked();
    throw new Error('The earlier request has been reconciled. Review its result before starting another action.');
  }
  const id = crypto.randomUUID();
  writePending({ id, path: input, created: Date.now() });
  const headers = new Headers(init.headers || {});
  headers.set('X-Graft-Request-ID', id);
  try {
    const response = await globalThis.fetch(url, { ...init, headers });
    const value = await response.clone().json().catch(() => null);
    if (value?.graft?.job_id === id) {
      if (value.response || value.graft.state === 'rejected') {
        const result = nativeResponse(value);
        writePending(null);
        return result;
      }
      if (!('response' in value)) {
        writePending(null);
        return response;
      }
    }
  } catch { /* Lost response does not authorize another POST. */ }
  return reconcilePendingLocked({ wait: true });
}

if (typeof window !== 'undefined') {
  window.addEventListener('DOMContentLoaded', async () => {
    const policy = document.createElement('details');
    const summary = document.createElement('summary');
    summary.className = 'ghost';
    summary.textContent = 'Workers…';
    const panel = document.createElement('div');
    panel.style.cssText = 'position:fixed;right:14px;top:58px;z-index:1000;max-height:75vh;overflow:auto;width:min(600px,calc(100vw - 28px));padding:16px;border:1px solid var(--line);background:var(--panel);box-shadow:0 8px 24px #0008';
    policy.append(summary, panel);
    (document.querySelector('header') || document.body).append(policy);
    try {
      const response = await globalThis.fetch(apiUrl('/api/graft/capabilities'));
      const value = await response.json();
      if (!response.ok || value.managed !== true) throw new Error('Managed control is unavailable.');
      summary.textContent = 'Managed workers';
      const explanation = document.createElement('p');
      explanation.textContent = 'Placement follows the configured worker policy; accepted work stays with its owner. Installed engines in Setup are not an execution grant. The operation policy below applies before acceptance. Files stay on their recorded worker; use Download for a local copy.';
      panel.append(explanation);
      for (const worker of value.workers) {
        const heading = document.createElement('strong');
        heading.textContent = worker.worker;
        panel.append(heading);
        const list = document.createElement('ul');
        const entries = Object.entries(worker.capabilities || {});
        if (!entries.length) entries.push(['worker', { enabled: false, reason: worker.error || 'No operations advertised' }]);
        for (const [operation, capability] of entries) {
          const item = document.createElement('li');
          item.textContent = `${operation}: ${capability.enabled ? 'enabled; content identity and admission checked before acceptance' : capability.reason || 'not installed or not authorized'}`;
          list.append(item);
        }
        panel.append(list);
      }
    } catch (error) {
      summary.textContent = 'Workers unavailable';
      panel.textContent = `${error.message} No local execution fallback.`;
      policy.open = true;
    }
  });
  window.addEventListener('DOMContentLoaded', () => {
    if (!readPending()) return;
    const notice = document.createElement('p');
    notice.role = 'status';
    notice.textContent = `Reconciling request ${readPending().id}; no work will be replayed.`;
    document.body.prepend(notice);
    reconcilePending({ wait: true }).then(() => { notice.textContent = 'Earlier request reconciled. Its progress is available in Activity.'; })
      .catch((error) => { notice.textContent = error.message; });
  });
}
