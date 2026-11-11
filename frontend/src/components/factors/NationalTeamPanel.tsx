import { useRef, useState } from "react";
import { authHeaders } from "@/lib/api";

type Row = { code: string; name: string; industry: string; period: string; disclosed: string; change: number | null; gap: number; score: number | null; selected: boolean };
type Result = { status: string; mode: string; as_of: string; input_sha256: string; regime: { state: string; reason: string }; counts: { input: number; observable: number; rankable: number; excluded: number }; rows: Row[]; excluded: { code: string; reason: string }[]; warnings: string[] };
const endpoint = "/api/factor/research/national-team";
function download(value: unknown, name: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url);
}

export function NationalTeamPanel() {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState("improved");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const generation = useRef(0);
  const invalidate = () => { generation.current++; setResult(null); setError(""); };
  async function request(path: string, init?: RequestInit) {
    const response = await fetch(`${endpoint}/${path}`, { ...init, headers: { ...authHeaders(), "Content-Type": "application/json" } });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "数据格式无效，请核对字段、日期及重复记录。");
    return body.data;
  }
  async function run() {
    if (!file) return;
    const id = ++generation.current;
    setBusy(true); setError(""); setResult(null);
    try {
      if (file.size > 20 * 1024 * 1024) throw new Error("文件超过20MB，请缩小股票池。");
      const input = JSON.parse(await file.text());
      const value = await request("evaluate", { method: "POST", body: JSON.stringify({ ...input, mode }) });
      if (id === generation.current) setResult(value);
    } catch (e) { if (id === generation.current) setError(e instanceof Error ? e.message : "计算失败"); }
    finally { setBusy(false); }
  }
  return <details className="rounded-xl border border-border/60 bg-card/30">
    <summary className="cursor-pointer px-4 py-3 text-sm font-medium">国家队因子 · 披露持仓研究</summary>
    <div className="space-y-4 px-4 pb-4 text-sm">
      <p className="text-muted-foreground">先导入真实披露数据，再计算状态与排名。当前未连接历史持仓库，也未完成样本外回测。</p>
      <p className="text-xs text-muted-foreground">入场期：持仓变化＋距一年高点；其他时期：距一年高点＋轻仓。ETF申赎不作为国家队买入证据。默认仅汇金、证金，主体范围由导入文件指定。</p>
      <div className="flex flex-wrap items-center gap-3">
        <button disabled={busy} className="rounded-lg border px-3 py-2 text-xs" onClick={async () => {
          try { download(await request("template"), "national-team-input.json"); }
          catch (e) { setError(e instanceof Error ? e.message : "下载失败"); }
        }}>下载空白数据模板</button>
        <label className="min-w-0 text-xs">披露数据 JSON<input aria-label="国家队披露数据" className="mt-1 block max-w-full" type="file" accept=".json,application/json" disabled={busy}
          onChange={e => { invalidate(); setFile(e.target.files?.[0] ?? null); }} /></label>
        <label className="text-xs">计算口径<select aria-label="国家队计算口径" value={mode} disabled={busy} onChange={e => { invalidate(); setMode(e.target.value); }} className="ml-2 rounded border bg-background px-2 py-2">
          <option value="improved">改进对照：持股占比变化＋复权价格</option>
          <option value="paper">原文公式：市值变化＋未复权价格</option>
        </select></label>
        <button onClick={run} disabled={busy || !file} className="rounded-lg bg-primary px-3 py-2 text-xs text-primary-foreground disabled:opacity-50">{busy ? "计算中…" : "计算国家队因子"}</button>
      </div>
      <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">数据要求与使用方式</summary>
        <p className="mt-2">JSON包含 as_of、previous_signal、source、universe、platforms、top_n、market、stocks。每只股票需 code/name/industry/eligible、snapshots 和 prices；快照字段为 period/disclosed/source/float_shares/complete/holdings，holdings逐条含 holder/platform/shares；价格含 day/close/adjusted_close。market逐日含 day/available/holding_value/csi800_float_value。</p>
        <p className="mt-2">单位：股、元；float_shares为自由流通股本。提供至少252条价格、818条同主体范围的历史可得日频市场指标。披露当天的持仓暂不使用。缺失上期持仓不记零。完整规范见项目 research/national_team_factor.md。</p>
      </details>
      {error && <p role="alert" className="text-danger">{error}</p>}
      {result && <div className="space-y-3" role="status">
        <p>{result.as_of} · {result.status === "insufficient_data" ? "数据不足，不生成组合" : "研究候选组合，尚未验证"} · {result.regime.state === "inflow" ? "相对强度上升" : result.regime.state === "other" ? "其他时期" : "状态未知"}</p>
        <p className="text-xs text-muted-foreground">{result.regime.reason}。导入 {result.counts.input} 只，可观察 {result.counts.observable} 只，可排名 {result.counts.rankable} 只。</p>
        <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs"><thead><tr>{["代码 / 名称", "行业", "报告期 / 披露日", "持仓变化", "距高点", "相对分数", "候选"].map(h => <th className="px-2 py-2" key={h}>{h}</th>)}</tr></thead>
          <tbody>{result.rows.map(r => <tr key={r.code} className="border-t border-border/40"><td className="px-2 py-2">{r.code} {r.name}</td><td className="px-2">{r.industry}</td><td className="px-2">{r.period} / {r.disclosed}</td><td className="px-2">{r.change === null ? "未知" : result.mode === "improved" ? `${(r.change * 100).toFixed(3)}个百分点` : `${(r.change/1e8).toFixed(3)}亿元`}</td><td className="px-2">{(r.gap*100).toFixed(1)}%</td><td className="px-2">{r.score?.toFixed(1) ?? "—"}</td><td className="px-2">{r.selected ? "研究候选" : "—"}</td></tr>)}</tbody></table></div>
        {result.excluded.length > 0 && <details><summary className="cursor-pointer text-xs">排除记录（{result.excluded.length}）</summary>{result.excluded.map(r => <p className="text-xs" key={r.code}>{r.code}：{r.reason}</p>)}</details>}
        {result.warnings.map(w => <p key={w} className="text-xs text-muted-foreground">{w}</p>)}
        <button className="rounded-lg border px-3 py-2 text-xs" onClick={() => download(result, `national-team-${result.as_of}-${result.mode}.json`)}>导出结果及输入指纹</button>
      </div>}
    </div>
  </details>;
}
