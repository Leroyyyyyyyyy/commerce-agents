const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function loadApi(fetch) {
  class AgentApi {
    constructor(root, prefix) { this.base = root + prefix; this.session = "test-session"; }
    headers() { return { "Content-Type": "application/json", "X-Session-Id": this.session }; }
    async post(path, body) {
      try {
        const response = await fetch(this.base + path, { method: "POST", headers: this.headers(), body: JSON.stringify(body) });
        return response.ok ? await response.json() : null;
      } catch { return null; }
    }
  }
  const source = ts.transpileModule(readFileSync(__dirname + "/api.ts", "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(source, {
    exports, require: () => ({ AgentApi }), fetch, crypto: globalThis.crypto,
    process: { env: {} },
  });
  return exports;
}

function loadButton(apiState = { session: "test-session" }) {
  const source = ts.transpileModule(readFileSync(__dirname + "/../components/ProductTile.tsx", "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const ref = { current: null };
  const exports = {};
  const modules = {
    react: { useState: () => ["idle", () => {}], useRef: () => ref },
    "react/jsx-runtime": { jsx: (type, props) => ({ type, props }) },
    "web-shared": { hasOptions: () => false, useStoreFrame: () => ({}) },
    "@/lib/api": { api: apiState },
    "@/lib/flight": { flyToCart: () => {} },
  };
  vm.runInNewContext(source, {
    exports, require: (name) => modules[name] ?? {}, crypto: globalThis.crypto,
    window: { setTimeout: () => {} },
  });
  return {
    async click(onAdd, productId = "AR-1202") {
      const button = exports.AddButton({ product: { product_id: productId }, onAdd });
      await button.props.onClick({ stopPropagation() {}, currentTarget: {} });
    },
  };
}

const cart = { item_count: 1 };
const success = () => ({ ok: true, status: 200, json: async () => ({ cart }) });

test("new successful additions generate distinct operation IDs", async () => {
  const requests = [];
  const api = loadApi(async (_, init) => { requests.push(JSON.parse(init.body)); return success(); });
  await api.addToCart("AR-1202");
  await api.addToCart("AR-1202");
  assert.notEqual(requests[0].operation_id, requests[1].operation_id);
});

test("ambiguous failure can retry exactly the same operation ID and payload", async () => {
  const requests = [];
  const api = loadApi(async (_, init) => {
    requests.push(JSON.parse(init.body));
    if (requests.length === 1) throw new Error("response lost");
    return success();
  });
  const id = crypto.randomUUID();
  const failed = await api.addToCart("AR-1202", 1, id);
  assert.equal(failed.retryable, true);
  assert.equal(failed.cart, null);
  const retried = await api.addToCart("AR-1202", 1, id);
  assert.equal(retried.retryable, false);
  assert.deepEqual(retried.cart, cart);
  assert.deepEqual(requests[0], requests[1]);
});

test("a button retains the intent for a retry, then creates a new intent after success", async () => {
  const button = loadButton();
  const ids = [];
  const add = async (_, intent) => { ids.push(intent.operationId); return ids.length !== 1; };
  await button.click(add);
  await button.click(add);
  await button.click(add);
  assert.equal(ids[0], ids[1]);
  assert.notEqual(ids[1], ids[2]);
});

test("different buttons with the same product are different intents, not parameter deduplication", async () => {
  const ids = [];
  const add = async (_, intent) => { ids.push(intent.operationId); return false; };
  await loadButton().click(add);
  await loadButton().click(add);
  assert.notEqual(ids[0], ids[1]);
});

test("a known refusal or changed session ends the button intent", async () => {
  const api = { session: "first" };
  const button = loadButton(api);
  const ids = [];
  const add = async (_, intent) => { ids.push(intent.operationId); intent.retryable = false; return false; };
  await button.click(add);
  await button.click(add);
  assert.notEqual(ids[0], ids[1]);
  const uncertain = async (_, intent) => { ids.push(intent.operationId); return false; };
  await button.click(uncertain);
  api.session = "second";
  await button.click(uncertain);
  assert.notEqual(ids[2], ids[3]);
});

test("5xx and malformed success remain ambiguous; 4xx is a known refusal", async () => {
  for (const [response, retryable] of [
    [{ ok: false, status: 503 }, true],
    [{ ok: true, status: 200, json: async () => { throw new Error("truncated"); } }, true],
    [{ ok: true, status: 200, json: async () => ({}) }, true],
    [{ ok: false, status: 400 }, false],
    [{ ok: false, status: 409 }, false],
    [{ ok: false, status: 422 }, false],
  ]) {
    const api = loadApi(async () => response);
    assert.equal((await api.addToCart("AR-1202")).retryable, retryable);
  }
});
