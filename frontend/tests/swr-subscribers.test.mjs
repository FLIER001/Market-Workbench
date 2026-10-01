import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";
import test from "node:test";
import ts from "typescript";

const src = readFileSync(new URL("../src/hooks/useSWR.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const require = createRequire(import.meta.url);

// 执行真实 hook 的转译结果；模拟 hook 生命周期及定时器，不依赖源码正则。
function harness() {
  let active;
  const timers = [];
  const same = (a, b) => !!a && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const hooks = {
    useState(init) {
      const owner = active, i = owner.cursor++;
      if (!(i in owner.slots)) owner.slots[i] = typeof init === "function" ? init() : init;
      return [owner.slots[i], (v) => { owner.slots[i] = typeof v === "function" ? v(owner.slots[i]) : v; }];
    },
    useRef(v) { const i = active.cursor++; return active.slots[i] ?? (active.slots[i] = { current: v }); },
    useCallback(fn, deps) {
      const i = active.cursor++, old = active.slots[i];
      if (!old || !same(old.deps, deps)) active.slots[i] = { fn, deps };
      return active.slots[i].fn;
    },
    useEffect(fn, deps) {
      const owner = active, i = owner.cursor++, old = owner.slots[i];
      if (!old || !same(old.deps, deps)) owner.effects.push(() => {
        old?.cleanup?.();
        owner.slots[i] = { deps, cleanup: fn() };
      });
    },
  };
  const context = { exports: {}, require: (name) => name === "react" ? hooks : name === "@/lib/auth" ? { loadUser: () => null } : require(name),
    document: { hidden: false }, window: { setTimeout: (fn) => timers.push(fn) }, localStorage: {}, console };
  vm.runInNewContext(compiled, context);
  function mount(key, fetcher) {
    const c = { slots: [], cursor: 0, effects: [], result: null,
      render(nextKey = key, nextFetcher = fetcher) {
        c.cursor = 0; c.effects = []; active = c;
        c.result = context.exports.useSWR(nextKey, nextFetcher);
        c.effects.forEach((fn) => fn());
      },
      unmount() { for (const slot of c.slots) slot?.cleanup?.(); },
      get data() { return c.slots[0]; }, get loading() { return c.slots[1]; }, get busy() { return c.slots[2]; },
    };
    c.render(); return c;
  }
  return { mount, timers };
}
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function deferred() { let resolve, reject; const p = new Promise((a, b) => { resolve = a; reject = b; }); return { p, resolve, reject }; }

test("all subscribers receive the first value and polling survives its owner's unmount", async () => {
  const { mount, timers } = harness(), first = deferred();
  let calls = 0;
  const fetcher = () => ++calls === 1 ? first.p : Promise.resolve({ cache_state: "fresh", n: 2 });
  const a = mount("k", fetcher), b = mount("k", fetcher);
  first.resolve({ cache_state: "refreshing", n: 1 }); await flush();
  assert.equal(a.data.n, 1); assert.equal(b.data.n, 1); assert.equal(b.loading, false);
  assert.equal(calls, 1);
  a.unmount(); timers.shift()(); await flush();
  assert.equal(b.data.n, 2); assert.equal(b.busy, false); assert.equal(calls, 2);
  b.unmount();
});

test("a late old-key response cannot replace the new key", async () => {
  const { mount } = harness(), old = deferred(), next = deferred();
  const a = mount("old", () => old.p);
  a.render("new", () => next.p);
  next.resolve({ cache_state: "fresh", n: "new" }); await flush();
  old.resolve({ cache_state: "fresh", n: "old" }); await flush();
  assert.equal(a.data.n, "new"); assert.equal(a.busy, false); a.unmount();
});

test("polling stops when every subscriber unmounts", async () => {
  const { mount, timers } = harness(); let calls = 0;
  const a = mount("k", async () => { calls++; return { cache_state: "refreshing", n: 1 }; });
  await flush(); a.unmount(); timers.shift()(); await flush(); assert.equal(calls, 1);
});

test("mutation broadcasts to the other component sharing its cache", async () => {
  const { mount } = harness();
  const a = mount("k", async () => ({ cache_state: "fresh", n: 1 }));
  const b = mount("k", async () => ({ cache_state: "fresh", n: 1 }));
  await flush(); a.result.setData({ cache_state: "fresh", n: 3 });
  assert.equal(b.data.n, 3); a.unmount(); b.unmount();
});

test("failed polling preserves the shared last-good and clears busy state", async () => {
  const { mount, timers } = harness(); let calls = 0;
  const fetcher = async () => { if (++calls > 1) throw new Error("offline"); return { cache_state: "refreshing", n: 1 }; };
  const a = mount("k", fetcher), b = mount("k", fetcher);
  await flush(); timers.shift()(); await flush();
  assert.equal(b.data.n, 1); assert.equal(b.busy, false); assert.equal(b.loading, false);
  a.unmount(); b.unmount();
});
