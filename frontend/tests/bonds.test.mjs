import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source = readFileSync(new URL('../src/lib/bondView.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, { compilerOptions: { module:ts.ModuleKind.ESNext } }).outputText;
const { changeBp, numberText, breakEvenText } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
test('percentage yield changes convert to bp, existing bp stays bp', () => {
  assert.ok(Math.abs(changeBp([{v:1.6601},{v:1.648}])+1.21)<1e-8);
  assert.equal(changeBp([{v:30},{v:32}],1,false),2);
  assert.equal(changeBp([{v:1}]),null);
  assert.equal(changeBp([{v:1},{v:Infinity}]),null);
});
test('missing financing stays missing and negative breakeven describes required rally', () => {
  assert.equal(numberText(null),'—');
  assert.equal(numberText(NaN),'—');
  assert.equal(numberText(-.0001,2), '0.00');
  assert.equal(breakEvenText(-2.825),'需下行 2.83 bp 保本');
  assert.equal(breakEvenText(2.825),'可上行 2.83 bp');
  assert.equal(breakEvenText(null),'—');
});
const { priorIsCurrent } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
test('persisted ready priors expire and future observations never activate', () => {
  assert.equal(priorIsCurrent({status:'ready',observed_at:'2026-10-16'},'2026-10-19'),true);
  assert.equal(priorIsCurrent({status:'ready',observed_at:'2026-10-16'},'2026-10-21'),false);
  assert.equal(priorIsCurrent({status:'missing',observed_at:'2026-10-20'},'2026-10-21'),false);
  assert.equal(priorIsCurrent({status:'ready',observed_at:'2026-10-22'},'2026-10-21'),false);
});
