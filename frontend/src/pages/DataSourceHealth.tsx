import { useState } from "react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { RefreshCw, Activity, Database, CheckCircle2, XCircle, AlertTriangle, Clock } from "lucide-react";
import { api, type SourceHealthData, type SourceHealthUpstream, type SourceHealthDataset } from "@/lib/api";
import { useSWR } from "@/hooks/useSWR";
import { cn } from "@/lib/utils";

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const m = iso.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  if (m) return `${m[1]} ${m[2]}`;
  return iso.replace("T", " ").slice(0, 16);
}

function fmtAge(iso: string | null): string {
  if (!iso) return "";
  const ts = Date.parse(iso.includes("T") ? iso : iso.replace(" ", "T"));
  if (!Number.isFinite(ts)) return "";
  const hours = (Date.now() - ts) / 3_600_000;
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} 分钟前`;
  if (hours < 48) return `${Math.round(hours)} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}

function UpstreamRow({ u, onProbe, probing }: { u: SourceHealthUpstream; onProbe: (key: string) => void; probing: boolean }) {
  const ok = u.status === "ok";
  return (
    <tr className="border-t border-border/40">
      <td className="py-2.5 pl-3 pr-2">
        <div className="flex items-center gap-2">
          {ok
            ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" />
            : <XCircle className="h-3.5 w-3.5 shrink-0 text-danger" />}
          <span className="text-sm">{u.name}</span>
        </div>
      </td>
      <td className="px-2 py-2.5 text-xs text-muted-foreground">{u.pages}</td>
      <td className={cn("px-2 py-2.5 text-right text-xs tabular-nums", ok ? "text-muted-foreground" : "text-danger")}>
        {ok ? `${u.latency_ms} ms` : "—"}
      </td>
      <td className="max-w-[220px] py-2.5 pl-2 pr-2 text-xs text-danger/90">
        {u.error ?? ""}
      </td>
      <td className="py-2.5 pl-1 pr-3 text-right">
        <button
          onClick={() => onProbe(u.key)}
          disabled={probing}
          title={`单独探活 ${u.name}`}
          className="rounded p-1 text-muted-foreground/70 transition-colors hover:bg-muted/60 hover:text-foreground disabled:opacity-40"
        >
          <RefreshCw className={cn("h-3 w-3", probing && "animate-spin")} />
        </button>
      </td>
    </tr>
  );
}

function DatasetRow({ d }: { d: SourceHealthDataset }) {
  const error = d.cache_state === "error";
  const age = fmtAge(d.cached_at);
  return (
    <tr className="border-t border-border/40">
      <td className="py-2.5 pl-3 pr-2">
        <div className="flex items-center gap-2">
          {error
            ? <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-danger" />
            : d.cache_state === "refreshing"
              ? <Clock className="h-3.5 w-3.5 shrink-0 text-primary animate-pulse" />
              : <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-success" />}
          <span className="text-sm">{d.name}</span>
        </div>
      </td>
      <td className="px-2 py-2.5 text-xs text-muted-foreground">{d.page}</td>
      <td className="px-2 py-2.5 text-xs tabular-nums text-muted-foreground">{fmtTime(d.cached_at)}</td>
      <td className="px-2 py-2.5 text-right text-xs tabular-nums text-muted-foreground">
        {age || d.detail || "—"}
      </td>
      <td className="max-w-[220px] py-2.5 pl-2 pr-3 text-xs text-danger/90">{d.refresh_error ?? ""}</td>
    </tr>
  );
}

export function DataSourceHealth() {
  const { data, loading, revalidating, revalidate, setData } = useSWR<SourceHealthData>(
    "source-health",
    (fresh) => api.sourceHealth(fresh),
  );
  const [probingKey, setProbingKey] = useState<string | null>(null);

  const refresh = () => revalidate(true);
  // 单源探活：后端只重探这一个上游，其余沿用上次结果；返回完整报告直接写回缓存
  const probeOne = async (key: string) => {
    if (probingKey) return;
    setProbingKey(key);
    try {
      const next = await api.sourceHealth(false, key);
      setData(next);
    } catch {
      /* 单源失败静默：行上状态本身就是结果 */
    } finally {
      setProbingKey(null);
    }
  };
  const summary = data?.summary;
  const okCount = summary ? summary.upstream_total - summary.upstream_failed.length : null;

  return (
    <div>
      <PageHeader
        title="数据源健康"
        subtitle="上游探活一次连通性 + 各页面最近一次成功取数的时间"
        actions={
          <button
            onClick={refresh}
            className="flex items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-sm transition-colors hover:bg-muted/60"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", revalidating && "animate-spin")} />
            重新探活
          </button>
        }
      />

      {loading && !data ? (
        <div className="space-y-3">
          <div className="glass h-24 animate-pulse rounded-2xl" />
          <div className="glass h-72 animate-pulse rounded-2xl" />
        </div>
      ) : !data ? (
        <GlassCard>
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <AlertTriangle className="h-4 w-4 text-warning" />
            健康检查不可用：请确认后端已启动并升级到最新版本。
          </div>
        </GlassCard>
      ) : (
        <div className="space-y-4">
          {/* 总览 */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <GlassCard className="p-4">
              <div className="text-xs text-muted-foreground">上游连通</div>
              <div className={cn("mt-1 text-2xl font-bold tabular-nums",
                summary && summary.upstream_failed.length === 0 ? "text-success" : "text-warning")}>
                {okCount != null ? `${okCount}/${summary?.upstream_total}` : "—"}
              </div>
              <div className="mt-0.5 text-[11px] text-muted-foreground">
                {summary && summary.upstream_failed.length > 0
                  ? summary.upstream_failed.join("、")
                  : "全部正常"}
              </div>
            </GlassCard>
            <GlassCard className="p-4">
              <div className="text-xs text-muted-foreground">缓存异常</div>
              <div className={cn("mt-1 text-2xl font-bold tabular-nums",
                summary && summary.dataset_error.length === 0 ? "text-success" : "text-danger")}>
                {summary?.dataset_error.length ?? "—"}
              </div>
              <div className="mt-0.5 text-[11px] text-muted-foreground">
                {summary && summary.dataset_error.length > 0
                  ? summary.dataset_error.join("、")
                  : "各数据集刷新正常"}
              </div>
            </GlassCard>
            <GlassCard className="p-4">
              <div className="text-xs text-muted-foreground">最近探活</div>
              <div className="mt-1 text-sm font-semibold tabular-nums">{fmtTime(data.checked_at)}</div>
              <div className="mt-0.5 text-[11px] text-muted-foreground">60 秒内重复访问直接复用</div>
            </GlassCard>
            <GlassCard className="p-4">
              <div className="text-xs text-muted-foreground">后端版本</div>
              <div className="mt-1 text-sm font-semibold tabular-nums">{data.version}</div>
              <div className="mt-0.5 text-[11px] text-muted-foreground">与前端版本号应一致</div>
            </GlassCard>
          </div>

          {/* 上游探活 */}
          <GlassCard>
            <div className="mb-2 flex items-center gap-2">
              <Activity className="h-4 w-4 text-primary" />
              <h3 className="text-sm font-semibold">上游探活</h3>
              <span className="text-[11px] text-muted-foreground">
                每个源发一次最小请求，超时 8 秒
              </span>
            </div>
            <div className="-mx-2 overflow-x-auto">
              <table className="w-full min-w-[640px] border-collapse">
                <thead>
                  <tr className="text-left text-[11px] text-muted-foreground">
                    <th className="py-1.5 pl-3 pr-2 font-normal">数据源</th>
                    <th className="px-2 py-1.5 font-normal">支撑页面</th>
                    <th className="px-2 py-1.5 text-right font-normal">延迟</th>
                    <th className="py-1.5 pl-2 pr-2 font-normal">错误</th>
                    <th className="py-1.5 pl-1 pr-3 font-normal">探活</th>
                  </tr>
                </thead>
                <tbody>
                  {data.upstreams.map((u) => (
                    <UpstreamRow key={u.key} u={u} onProbe={probeOne} probing={probingKey === u.key} />
                  ))}
                </tbody>
              </table>
            </div>
          </GlassCard>

          {/* 数据集缓存状态 */}
          <GlassCard>
            <div className="mb-2 flex items-center gap-2">
              <Database className="h-4 w-4 text-primary" />
              <h3 className="text-sm font-semibold">页面数据集状态</h3>
              <span className="text-[11px] text-muted-foreground">
                最近一次成功取数时间；空白表示该页还没被访问过
              </span>
            </div>
            {data.datasets.length === 0 ? (
              <div className="py-6 text-center text-sm text-muted-foreground">
                暂无缓存记录 —— 打开各页面后这里会逐个出现
              </div>
            ) : (
              <div className="-mx-2 overflow-x-auto">
                <table className="w-full min-w-[640px] border-collapse">
                  <thead>
                    <tr className="text-left text-[11px] text-muted-foreground">
                      <th className="py-1.5 pl-3 pr-2 font-normal">数据集</th>
                      <th className="px-2 py-1.5 font-normal">页面</th>
                      <th className="px-2 py-1.5 font-normal">最近成功</th>
                      <th className="px-2 py-1.5 text-right font-normal">距今</th>
                      <th className="py-1.5 pl-2 pr-3 font-normal">刷新错误</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.datasets.map((d) => <DatasetRow key={d.key} d={d} />)}
                  </tbody>
                </table>
              </div>
            )}
          </GlassCard>

          <p className="text-[11px] leading-relaxed text-muted-foreground/60">
            探活复用各模块的真实请求入口（含直连/代理自适应与限流），结果即实际链路可用性；
            数据集状态只读缓存时间戳，不触发任何重建。个别源失败通常是上游风控/限流的间歇现象，
            页面自身有 last-good 缓存兜底，不代表功能立即不可用。
          </p>
        </div>
      )}
    </div>
  );
}
