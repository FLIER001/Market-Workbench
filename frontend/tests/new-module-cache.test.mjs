import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const bonds = readFileSync(new URL("../src/pages/Bonds.tsx", import.meta.url), "utf8");
const sector = readFileSync(new URL("../src/pages/SectorDetail.tsx", import.meta.url), "utf8");

test("bonds persists all page snapshots and refreshes every block", () => {
  assert.equal([...bonds.matchAll(/\{ persist: true \}/g)].length, 3);
  assert.match(bonds, /revalidateFw\(true\)/);
  assert.match(bonds, /revalidateSeg\(true\)/);
});

test("industry chain renders its local last-good before revalidation", () => {
  assert.match(sector, /vr-industry-chain:\$\{key\}:v1/);
  assert.match(sector, /setChain\(cached\)/);
  assert.match(sector, /产业链更新失败，继续展示上次缓存/);
});
