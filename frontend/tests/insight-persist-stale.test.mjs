import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// 「油价/黄金页每次打开都是 8-18 / 8-14 旧解读」的回归防线。
// 根因：persist 旧格式（无 t 戳）值永远秒显且写回链路一旦失败就冻结在旧时点。
// 修复 1：AI 解读 persist key 全部升 v2，一次性丢弃冻结的 v1 旧值；
// 修复 2：loadPersisted 对无 t 戳的旧格式直接淘汰（不再无限期兼容读取）；
// 修复 3：insight 类接口超时从 150s 降到 45s，避免冷生成期间关页放弃写回。

const swr = readFileSync(new URL("../src/hooks/useSWR.ts", import.meta.url), "utf8");
const api = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");

test("loadPersisted discards legacy entries without a timestamp", () => {
  // 无 t 戳 = 时点不可知的遗留值：必须删除而非兼容读取
  assert.match(swr, /parsed\.t == null \|\| Date\.now\(\) - parsed\.t > PERSIST_MAX_AGE_MS/);
  assert.doesNotMatch(swr, /parsed\.t != null && Date\.now\(\) - parsed\.t > PERSIST_MAX_AGE_MS/);
});

test("insight endpoints use bounded 45s timeout instead of 150s", () => {
  assert.match(api, /INSIGHT_TIMEOUT_MS = 45_000/);
  for (const name of ["allocationInsight", "bondsInsight", "goldInsight", "oilInsight"]) {
    const re = new RegExp(`${name}: \\(refresh = false\\) =>\\s*\\n\\s*get<[^>]+>\\([^)]*INSIGHT_TIMEOUT_MS`);
    assert.match(api, re, `${name} 应使用 INSIGHT_TIMEOUT_MS`);
  }
});

for (const [file, key] of [
  ["../src/pages/Gold.tsx", "gold-insight:v2"],
  ["../src/pages/Oil.tsx", "oil-insight:v2"],
  ["../src/pages/Bonds.tsx", "bonds-insight:v2"],
  ["../src/pages/Allocation.tsx", "allocation-insight:v2"],
]) {
  test(`${file} uses versioned insight persist key`, () => {
    const source = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.match(source, new RegExp(`"${key}"`));
    assert.doesNotMatch(source, new RegExp(`"${key.replace(":v2", ":v1")}"`));
  });
}
