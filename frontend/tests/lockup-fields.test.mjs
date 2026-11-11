import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const api = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const page = readFileSync(new URL("../src/pages/StockData.tsx", import.meta.url), "utf8");
const body = page.match(/const unlockWan = \(r: LockupRow\) => \{([\s\S]*?)\n\};/)?.[1];
assert.ok(body, "quantity formatter is present");
const unlockWan = new Function("r", body);

test("new and legacy unlock quantities are both already 万股", () => {
  assert.equal(unlockWan({ unlock_shares_wan: 314.4384 }), "314.44 万股");
  assert.equal(unlockWan({ able_shares: 314.4384 }), "314.44 万股");
  assert.equal(unlockWan({ unlock_shares_wan: 0, able_shares: 20 }), "0.00 万股");
});

test("null new value does not fall back to legacy zero placeholder", () => {
  assert.equal(unlockWan({ unlock_shares_wan: null, able_shares: 0 }), "—");
  assert.equal(unlockWan({}), "—");
  assert.equal(unlockWan({ unlock_shares_wan: Number.NaN }), "—");
});

test("API coverage type matches the server object and is not rendered as a React child", () => {
  assert.match(api, /coverage\?: \{/);
  assert.match(api, /complete\?: boolean/);
  assert.match(api, /warnings\?: string\[\]/);
  assert.doesNotMatch(page, /\{lockup\.coverage\}/);
  assert.match(page, /lockup\.coverage\.source/);
  assert.match(page, /lockup\.warnings\.join/);
});

test("upcoming and history panels show single-batch quantities, never raw percentages", () => {
  assert.doesNotMatch(page, /pct\(h\.ratio\)/);
  assert.equal(page.match(/单批解禁 \{unlockWan\(h\)\}/g)?.length, 2);
  assert.match(page, /lockup\.scheduled/);
  assert.match(page, /未取得完整覆盖时不代表无解禁风险/);
  assert.doesNotMatch(page, /未来 90 天无待解禁。/);
});
