import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/pages/Gold.tsx", import.meta.url), "utf8");

test("gold renders the persistent SWR snapshot without waiting for PAXG", () => {
  assert.match(source, /\{ data: spot, loading, revalidating, revalidate \} = useSWR<GoldScoreData>/);
  assert.doesNotMatch(source, /Promise\.all\(\[api\.goldScore\(\), api\.paxgSpot\(\)\]\)/);
});
