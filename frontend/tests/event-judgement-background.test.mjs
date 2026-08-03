import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("event judgement registers the whole refresh pipeline as a background task", async () => {
  const source = await readFile(
    new URL("../src/components/intel/WatchlistEventJudgement.tsx", import.meta.url),
    "utf8",
  );

  const start = source.indexOf("startBackgroundTask<EventJudgementTaskData>(");
  const gather = source.indexOf("const nextSnapshot = await gatherSnapshot(seeds);");

  assert.notEqual(start, -1);
  assert.notEqual(gather, -1);
  assert.ok(start < gather, "evidence refresh must start after the background task is registered");
  assert.match(source, /phase:\s*"refreshing"/);
  assert.match(source, /phase:\s*configured\s*\?\s*"analyzing"\s*:\s*"done"/);
});

test("unsubscribing a background-task view does not abort its controller", async () => {
  const source = await readFile(
    new URL("../src/lib/backgroundTasks.ts", import.meta.url),
    "utf8",
  );
  const hookStart = source.indexOf("export function useBackgroundTask");
  const runnerStart = source.indexOf("export function startBackgroundTask");
  const hookSource = source.slice(hookStart, runnerStart);

  assert.match(hookSource, /listeners\.delete\(key\)/);
  assert.doesNotMatch(hookSource, /\.abort\(\)|cancelBackgroundTask/);
});

test("per-asset judgement is normalized to one opinionated paragraph", async () => {
  const source = await readFile(
    new URL("../src/components/intel/WatchlistEventJudgement.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /你的任务是形成观点，不是整理材料/);
  assert.match(source, /输出必须严格为一段连续中文/);
  assert.match(source, /const oneParagraph =/);
  assert.doesNotMatch(source, /每个事件必须包含/);
});

test("event judgement turns inline evidence ids into source links", async () => {
  const source = await readFile(
    new URL("../src/components/intel/WatchlistEventJudgement.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /const renderCitedParagraph =/);
  assert.match(source, /split\(\/\(\\\[\[ANIW\]\\d\+\\\]\)\/gi\)/);
  assert.match(source, /href=\{item\.url\}/);
  assert.match(source, /aria-label=\{`打开来源：\$\{item\.title\}`\}/);
  assert.match(
    source,
    /evidence=\{analyses\[assetKey\(asset\)\]\?\.evidence \|\| evidenceForAsset\(snapshot, asset\)\}/,
  );
});

test("a single asset can refresh without rerunning the whole watchlist", async () => {
  const source = await readFile(
    new URL("../src/components/intel/WatchlistEventJudgement.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /const refreshAsset = \(asset: WatchedAsset\) =>/);
  assert.match(source, /gatherSnapshot\(\[\{/);
  assert.match(source, /activeAssetKey:\s*key/);
  assert.match(source, /onRefresh=\{refreshAsset\}/);
  assert.match(source, /刷新全部研判/);
  assert.match(source, /已保留上次成功结果/);
});
