import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../src/pages/Liquidity.tsx", import.meta.url),
  "utf8",
);

test("liquidity uses the shared persistent stale-while-revalidate cache", () => {
  assert.match(source, /useSWR<LiquidityData>/);
  assert.match(source, /"liquidity:v5"/);
  assert.match(source, /\{ persist: true \}/);
  assert.match(source, /5 \* 60_000/);
  assert.match(source, /loading \|\| revalidating/);
});

test("refresh failures stay visible even when cached data exists", () => {
  assert.match(source, /\{err && \(/);
  assert.doesNotMatch(source, /err && !data/);
});

test("liquidity cards use one five-band vocabulary and only 50 is neutral", () => {
  assert.match(source, /idx\.favorable === "high"/);
  assert.match(source, /delta >= 20 \? "有利"/);
  assert.match(source, /delta > 0 \? "偏多"/);
  assert.match(source, /delta === 0 \? "中性"/);
  assert.match(source, /delta > -20 \? "偏空" : "不利"/);
  assert.doesNotMatch(source, /低压力|中等压力|高压力|低预警|高预警|流入偏强|流出偏强/);
  assert.doesNotMatch(source, /全市场合计/);
});
