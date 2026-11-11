import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source = readFileSync(new URL('../src/lib/dataFreshness.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const mod = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
const { formatRefreshTime, dataTimeFor, markDataPayload, subscribeRefresh } = mod;

test('header timestamp reads date, weekday in full-width parens and seconds', () => {
  const dated = { ms: new Date(2026, 10, 8, 16, 53, 32).getTime(), hasTime: true };
  assert.equal(formatRefreshTime(dated), '2026-11-08（星期日） 16:53:32');
  assert.equal(formatRefreshTime({ ms: new Date(2026, 0, 1, 9, 5, 7).getTime(), hasTime: true }), '2026-01-01（星期四） 09:05:07');
  // 只有日期的数据不硬凑 00:00:00
  assert.equal(formatRefreshTime({ ms: new Date(2026, 10, 6).getTime(), hasTime: false }), '2026-11-06（星期五）');
});

test('payload time comes from backend stamps, tier 1 beats cache stamps', () => {
  markDataPayload('/macro', { updated: '2026-11-08 17:52', cached_at: '2026-11-08T18:05:06+08:00' });
  assert.equal(formatRefreshTime(dataTimeFor('/macro')), '2026-11-08（星期日） 17:52:00');
  // 后到的二档缓存时间不能盖掉一档数据时点
  markDataPayload('/macro', { cached_at: '2026-11-08T19:00:00+08:00', nested: { fetched_at: '2026-11-08 18:59' } });
  assert.equal(formatRefreshTime(dataTimeFor('/macro')), '2026-11-08（星期日） 17:52:00');
  // 同档取更晚的那个
  markDataPayload('/pulse', { as_of: '2026-11-08T16:53:32+08:00', curve: { generated_at: '2026-11-08T17:10:00+08:00' } });
  assert.equal(formatRefreshTime(dataTimeFor('/pulse')), '2026-11-08（星期日） 17:10:00');
});

test('payload without usable stamps leaves the page blank instead of using entry time', () => {
  markDataPayload('/factor', { factors: [{ id: 'x', name: 'y' }], has_data: true });
  assert.equal(dataTimeFor('/factor'), null);
  // 只有钟点的行情时间无法定位日期，同样跳过
  markDataPayload('/quote', { '600519': { quote_time: '16:14:58' } });
  assert.equal(dataTimeFor('/quote'), null);
});

test('route scope is kept apart and subscribers see later values', () => {
  let hits = 0;
  const off = subscribeRefresh(() => { hits += 1; });
  markDataPayload('/liquidity', { updated: '2026-11-08 18:05' });
  assert.equal(formatRefreshTime(dataTimeFor('/liquidity')), '2026-11-08（星期日） 18:05:00');
  assert.equal(dataTimeFor('/gold'), null);
  off();
  hits = 0;
  markDataPayload('/liquidity', { updated: '2026-11-08 18:20' });
  assert.equal(formatRefreshTime(dataTimeFor('/liquidity')), '2026-11-08（星期日） 18:20:00');
  assert.equal(hits, 0);
});
