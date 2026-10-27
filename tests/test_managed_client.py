from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node is required to execute browser ES modules")


@needs_node
def test_managed_urls_keep_the_canopy_mount_and_external_urls_unchanged():
    module = (REPO / "viewer" / "studio" / "managed.js").as_uri()
    script = f'''
      globalThis.location = {{ pathname: "/upstream/assetfurnace/viewer/studio.html", origin: "http://127.0.0.1:9292", href: "http://127.0.0.1:9292/upstream/assetfurnace/viewer/studio.html" }};
      const m = await import({json.dumps(module)});
      console.log(JSON.stringify([
        m.apiUrl("/api/assets"), m.apiUrl("/output/a.glb"),
        m.apiUrl("/upstream/assetfurnace/api/graft/requests/x"),
        m.apiUrl("/events?x=1"), m.apiUrl("https://example.test/a")
      ]));
    '''
    result = subprocess.run([NODE, "--input-type=module", "--eval", script], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == [
        "/upstream/assetfurnace/api/assets", "/upstream/assetfurnace/output/a.glb",
        "/upstream/assetfurnace/api/graft/requests/x", "/upstream/assetfurnace/events?x=1",
        "https://example.test/a",
    ]


@needs_node
def test_uncertain_mutation_keeps_its_identity_and_reconciles_without_repost():
    module = (REPO / "viewer" / "studio" / "managed.js").as_uri()
    script = f'''
      const store = new Map();
      globalThis.location = {{ pathname: "/upstream/assetfurnace/viewer/studio.html", origin: "http://127.0.0.1:9292", href: "http://127.0.0.1:9292/upstream/assetfurnace/viewer/studio.html" }};
      globalThis.localStorage = {{ getItem: (k) => store.get(k) ?? null, setItem: (k,v) => store.set(k,v), removeItem: (k) => store.delete(k) }};
      Object.defineProperty(globalThis, "crypto", {{ value: {{ randomUUID: () => "550e8400-e29b-41d4-a716-446655440000" }}, configurable: true }});
      Object.defineProperty(globalThis, "navigator", {{ value: {{ locks: {{ request: async (_, callback) => callback() }} }}, configurable: true }});
      globalThis.window = {{ addEventListener() {{}}, dispatchEvent() {{}} }};
      globalThis.CustomEvent = class {{ constructor(type, options) {{ this.type = type; this.detail = options.detail; }} }};
      globalThis.setTimeout = (callback) => {{ queueMicrotask(callback); return 0; }};
      let posts = 0, reconciles = 0, id = null;
      globalThis.fetch = async (url, init = {{}}) => {{
        if (init.method === "POST") {{ posts++; id = new Headers(init.headers).get("X-Graft-Request-ID"); throw new Error("connection lost"); }}
        reconciles++;
        if (reconciles === 1) throw new Error("temporary read failure");
        return new Response(JSON.stringify({{ graft: {{ job_id: id, state: "completed" }}, response: {{ status: 202, body: {{ id: "run-1" }} }} }}), {{ status: 200 }});
      }};
      const m = await import({json.dumps(module)});
      const response = await m.apiFetch("/api/chains", {{ method: "POST" }});
      console.log(JSON.stringify({{ posts, reconciles, id, status: response.status, body: await response.json(), saved: [...store.values()] }}));
    '''
    result = subprocess.run([NODE, "--input-type=module", "--eval", script], check=True, capture_output=True, text=True)
    out = json.loads(result.stdout)
    assert out["posts"] == 1 and out["reconciles"] == 2
    assert out["id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert out["status"] == 202 and out["body"]["id"] == "run-1" and out["saved"] == []

@needs_node
def test_concurrent_mutations_keep_each_response_with_its_own_request():
    module = (REPO / "viewer" / "studio" / "managed.js").as_uri()
    script = f'''
      const store = new Map();
      globalThis.location = {{ pathname: "/upstream/assetfurnace/viewer/studio.html", origin: "http://127.0.0.1:9292", href: "http://127.0.0.1:9292/upstream/assetfurnace/viewer/studio.html" }};
      globalThis.localStorage = {{ getItem: (k) => store.get(k) ?? null, setItem: (k,v) => store.set(k,v), removeItem: (k) => store.delete(k) }};
      let tail = Promise.resolve();
      Object.defineProperty(globalThis, "navigator", {{ value: {{ locks: {{ request: (_, callback) => {{
        const before = tail;
        let release;
        tail = new Promise((resolve) => {{ release = resolve; }});
        return before.then(callback).finally(release);
      }} }} }}, configurable: true }});
      let sequence = 0, posts = 0;
      Object.defineProperty(globalThis, "crypto", {{ value: {{ randomUUID: () => `uuid-${{++sequence}}` }}, configurable: true }});
      globalThis.window = {{ addEventListener() {{}}, dispatchEvent() {{}} }};
      globalThis.CustomEvent = class {{ constructor(type, options) {{ this.type = type; this.detail = options.detail; }} }};
      globalThis.fetch = async (url, init = {{}}) => {{
        if (init.method !== "POST") throw new Error("unexpected reconciliation");
        posts++;
        const id = new Headers(init.headers).get("X-Graft-Request-ID");
        const job = `job-${{id}}`;
        return new Response(JSON.stringify({{ graft: {{ job_id: id, state: "completed" }}, response: {{ status: 202, body: {{ id: job }} }} }}), {{ status: 200 }});
      }};
      const m = await import({json.dumps(module)});
      const responses = await Promise.all([
        m.apiFetch("/api/chains", {{ method: "POST" }}),
        m.apiFetch("/api/chains", {{ method: "POST" }})
      ]);
      console.log(JSON.stringify({{ posts, bodies: await Promise.all(responses.map((r) => r.json())), saved: [...store.values()] }}));
    '''
    result = subprocess.run([NODE, "--input-type=module", "--eval", script], check=True, capture_output=True, text=True)
    out = json.loads(result.stdout)
    assert out["posts"] == 2
    assert out["bodies"] == [{"id": "job-uuid-1", "graft": {"job_id": "uuid-1", "state": "completed"}},
                             {"id": "job-uuid-2", "graft": {"job_id": "uuid-2", "state": "completed"}}]
    assert out["saved"] == []
