import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const src = readFileSync(new URL("../src/hooks/useSWR.ts", import.meta.url), "utf8");

// 切窗口「不及时」的根因：首个响应被 refreshing 轮询循环扣住，页面要等 17s 才换表。
test("first response is committed before the refresh poll loop", () => {
  const body = src.slice(src.indexOf("p = (async () => {"), src.indexOf("inflight.set(scopedKey, p)"));
  const firstCommit = body.indexOf("commit(next)");
  const pollLoop = body.indexOf("for (const delay of REFRESH_POLL_DELAYS)");
  assert.ok(firstCommit > -1, "首个响应应写入缓存并上屏");
  assert.ok(pollLoop > -1, "轮询循环应存在");
  assert.ok(firstCommit < pollLoop, "必须先 commit 首个响应，再进入轮询追新");
});

// 切 key 时必须同步换成本 key 的缓存值，否则页面上还挂着上一个窗口的行。
test("key change swaps in that key's cache before revalidating", () => {
  const effect = src.slice(src.indexOf("useEffect(() => {"), src.indexOf("}, [revalidate, ...deps])"));
  assert.match(effect, /cache\.has\(scopedKey\)/);
  assert.match(effect, /loadPersisted<T>\(scopedKey\)/);
  assert.match(effect, /setData\(cached \?\? null\)/);
  assert.match(effect, /setLoading\(cached == null\)/);
  assert.ok(
    effect.indexOf("setData(cached") < effect.indexOf("revalidate()"),
    "先换缓存再重取",
  );
});

// 上一个 key 的轮询结果不得盖掉新 key 的数据。
test("stale key results cannot overwrite the current key", () => {
  assert.match(src, /keyRef\.current === requestKey/);
  assert.match(src, /if \(!current\(\)\) break;/);
});

// 后端冷重建约 80s，轮询总时长必须盖过它，否则「完成后自动更新」不成立。
test("refresh poll window outlasts the backend rebuild", () => {
  const delays = src.match(/REFRESH_POLL_DELAYS = \[([^\]]+)\]/)?.[1];
  assert.ok(delays, "REFRESH_POLL_DELAYS 未定义");
  const total = delays
    .split(",")
    .reduce((sum, n) => sum + Number(n.trim().replaceAll("_", "")), 0);
  assert.ok(total > 80_000, `轮询总时长 ${total}ms 应超过 80s 冷构建`);
  assert.ok(total < 150_000, `轮询总时长 ${total}ms 应小于前端 150s 超时`);
  assert.match(src, /for \(const delay of REFRESH_POLL_DELAYS\) \{/);
});

// pollRefreshing 必须每轮都把值交给 onValue 上屏：它现在被 PlateScores/SectorScores/SectorDetail
// 共用，若改成只返回终值，这些页首次加载就会白转两分钟（回到改造前的毛病）。
test("pollRefreshing commits every polled value to the caller", () => {
  const body = src.slice(src.indexOf("export async function pollRefreshing"));
  assert.match(body, /onValue\(value\)/, "首个响应应交给 onValue 上屏");
  assert.match(body, /for \(const delay of REFRESH_POLL_DELAYS\)/, "应复用统一的长轮询窗口");
  const loop = body.slice(body.indexOf("for (const delay of REFRESH_POLL_DELAYS)"));
  assert.match(loop, /onValue\(value\)/, "每轮轮询结果也要上屏");
});

// 真正执行 pollRefreshing：光靠正则断言看不出「上屏顺序」这种时序问题。
test("pollRefreshing paints the first response before awaiting anything", async () => {
  const raw = src.slice(src.indexOf("export async function pollRefreshing"));
  const body = raw
    .slice(0, raw.indexOf("\n}\n") + 3)
    .replace("export async function", "async function")
    .replace(/<T extends CachePayload>/, "")
    .replace(/first: T,/, "first,")
    .replace(/fetcher: \(\) => Promise<T>,/, "fetcher,")
    .replace(/onValue: \(v: T\) => void,/, "onValue,")
    .replace(/: Promise<T>/, "");

  const waits = [];
  const fn = new Function("REFRESH_POLL_DELAYS", "waitUntilVisible", `${body}; return pollRefreshing;`)(
    [1, 1, 1],
    async (ms) => { waits.push(ms); },
  );

  const painted = [];
  const responses = [
    { cache_state: "refreshing", n: 1 },
    { cache_state: "refreshing", n: 2 },
    { cache_state: "fresh", n: 3 },
  ];
  let i = 0;
  const promise = fn(responses[0], async () => responses[++i], (v) => painted.push(v.n));
  // 还没 await 任何东西，第一帧就该已经上屏。
  assert.deepEqual(painted, [1], "首个响应应在同步阶段上屏");
  await promise;
  assert.deepEqual(painted, [1, 2, 3], "每轮轮询结果都应上屏，直到不再是 refreshing");
});

// 四个旧调用点必须全部改用 pollRefreshing，否则又回到「等轮询结束才上屏」。
test("slow pages use pollRefreshing instead of blocking on the final value", () => {
  for (const file of ["PlateScores.tsx", "SectorScores.tsx", "SectorDetail.tsx"]) {
    const page = readFileSync(new URL(`../src/pages/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(page, /resolveRefreshing/, `${file} 不应再有阻塞式调用`);
    assert.match(page, /pollRefreshing/, `${file} 应改用 pollRefreshing`);
  }
});
