import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const bonds = readFileSync(new URL("../src/pages/Bonds.tsx", import.meta.url), "utf8");
const sector = readFileSync(new URL("../src/pages/SectorDetail.tsx", import.meta.url), "utf8");

test("bonds uses one coherent dashboard and snapshot-bound AI cache", () => {
  assert.match(bonds, /"bonds-overview:v2"/);
  assert.match(bonds, /bonds-insight:v3:\$\{fingerprint\}/);
  assert.match(bonds, /ai\?\.input_fingerprint === data\?\.input_fingerprint/);
  assert.doesNotMatch(bonds, /api\.bondsFramework|api\.bondsSegments/);
});

test("industry chain renders its local last-good before revalidation", () => {
  assert.match(sector, /vr-industry-chain:\$\{key\}:v1/);
  assert.match(sector, /setChain\(cached\)/);
  assert.match(sector, /产业链更新失败，继续展示上次缓存/);
});
