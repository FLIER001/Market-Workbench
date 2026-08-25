import { useRef, useState } from "react";
import { KeyRound, Sparkles, ShieldCheck, Check, Trash2, Terminal, DatabaseBackup, Download, Upload } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { toast } from "sonner";
import { loadLlm, saveLlm, clearLlm } from "@/lib/llm";
import { loadAccessKey, saveAccessKey } from "@/lib/api";
import { subscriptionModels, apiModels, PROVIDER_BASE, isCliProvider, aiModels, type ProviderId } from "@/lib/ai-models";
import { auth, type UserDataExport } from "@/lib/auth";
import { isLoggedIn, pullBackendToLocal } from "@/lib/userData";

export function Settings() {
  const existing = loadLlm();
  const existingIsCli = existing ? isCliProvider(existing.provider) : false;

  const [mode, setMode] = useState<"api" | "subscription">(existing && existingIsCli ? "subscription" : "api");
  // 订阅：选中的 CLI model id
  const [cliId, setCliId] = useState(existing && existingIsCli ? existing.model : "");
  // API：选中的模型 id + 可编辑的 baseURL / model / key
  const firstApi = apiModels[0];
  // 如果保存的 model 不在 apiModels 列表里（自定义/豆包 ep-xxx），select 回显 "custom"
  const savedApiId = existing && !existingIsCli
    ? (apiModels.some((m) => m.id === existing.model) ? existing.model : "custom")
    : firstApi.id;
  const [apiId, setApiId] = useState(savedApiId);
  const [baseURL, setBaseURL] = useState(existing && !existingIsCli ? existing.baseURL : (PROVIDER_BASE[firstApi.provider] || ""));
  const [modelName, setModelName] = useState(existing && !existingIsCli ? existing.model : firstApi.id);
  const [apiKey, setApiKey] = useState(existing && !existingIsCli ? existing.apiKey : "");
  // 后端访问密钥（对应部署时的 VR_API_KEY）；本机自用不设鉴权时留空
  const [accessKey, setAccessKey] = useState(loadAccessKey());

  const providerOf = (id: string): ProviderId => aiModels.find((m) => m.id === id)?.provider ?? "openai-compatible";

  const pickApiModel = (id: string) => {
    const m = apiModels.find((x) => x.id === id);
    if (!m) return;
    setApiId(id);
    // "custom" 不覆盖 modelName，保留用户手动填的值
    if (id !== "custom") {
      setModelName(id);
    }
    setBaseURL(PROVIDER_BASE[m.provider] || "");
    // 换服务商/模型时清空 key，避免把上一家（或旧快照）的 key 带过去覆盖新配置
    setApiKey("");
  };

  const saveApi = () => {
    // 保存就是纯粹的"页面填什么存什么"——不做任何与已存值的"智能"合并，
    // 否则旧 key 会被当成"最新值"写回，把用户刚输入的新 key 覆盖掉。
    if (!baseURL.trim() || !apiKey.trim() || !modelName.trim()) {
      toast.error("请填完 Base URL、API Key、Model");
      return;
    }
    saveLlm({ provider: providerOf(apiId), baseURL: baseURL.trim(), apiKey: apiKey.trim(), model: modelName.trim() });
    toast.success("已保存到本地，全站「问 AI / 复盘」现在可用");
  };

  const saveSubscription = () => {
    const m = subscriptionModels.find((x) => x.id === cliId);
    if (!m || m.comingSoon) {
      toast.error("请选择一个可用的订阅（暂不支持标「即将支持」的）");
      return;
    }
    saveLlm({ provider: m.provider, baseURL: "", apiKey: "", model: m.id });
    toast.success(`已选「${m.name}」订阅，全站「问 AI / 复盘」将调用本机 ${m.name}`);
  };

  const forget = () => {
    clearLlm();
    setApiKey("");
    setCliId("");
    toast.success("已清除本地配置");
  };

  const saveAccess = () => {
    const k = accessKey.trim();
    saveAccessKey(k);
    setAccessKey(k);
    toast.success(k ? "已保存后端访问密钥（存本地）" : "已清除后端访问密钥");
  };

  return (
    <div>
      <PageHeader title="接入 AI" subtitle="配置一次，全站的「问 AI」「复盘」都能用你自己的模型" />

      <div className="mb-4 flex items-start gap-2 rounded-lg border border-success/25 bg-success/5 p-3 text-xs text-muted-foreground">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-success" />
        <span>API key <b className="text-foreground">只存在你本地浏览器</b>，仅在你提问时发给你自己的后端去调模型，不上传、不进仓库。所有分析由你的模型给出，本产品不校准。</span>
      </div>

      {/* 两种接入方式 */}
      <div className="mb-4 grid gap-3 sm:grid-cols-2">
        <GlassCard glow={mode === "subscription"} onClick={() => setMode("subscription")}
          className={mode === "subscription" ? "ring-1 ring-primary/40" : "opacity-80"}>
          <div className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-primary" />
            <h3 className="font-semibold">订阅接入</h3>
            {mode === "subscription" && <Check className="ml-auto h-4 w-4 text-primary" />}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">调本机已登录的 AI CLI（Claude Code / Qwen / DeepSeek / Codex…），用订阅额度，<b className="text-foreground">免 API key</b>。需后端在本机跑。</p>
        </GlassCard>

        <GlassCard glow={mode === "api"} onClick={() => setMode("api")}
          className={mode === "api" ? "ring-1 ring-primary/40" : "opacity-80"}>
          <div className="flex items-center gap-2">
            <KeyRound className="h-5 w-5 text-primary" />
            <h3 className="font-semibold">API 接入</h3>
            {mode === "api" && <Check className="ml-auto h-4 w-4 text-primary" />}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">粘贴 API key，支持 DeepSeek / 豆包 / MiniMax / OpenAI / OpenRouter / 任意兼容端点。<b className="text-foreground">现已可用。</b></p>
        </GlassCard>
      </div>

      <GlassCard>
        {mode === "subscription" ? (
          <div className="space-y-3 text-sm">
            <p className="text-xs text-muted-foreground">
              选一个你本机已安装并登录的 CLI。Market Workbench 后端会用它以你的订阅额度作答，<b className="text-foreground">不用填 key</b>。
              <span className="text-muted-foreground/60">（仅当后端跑在你本机时可用；复盘 / 今日要点 / 个股问 AI 等场景。）</span>
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              {subscriptionModels.map((m) => {
                const on = cliId === m.id;
                return (
                  <button key={m.id} disabled={m.comingSoon} onClick={() => setCliId(m.id)}
                    className={`flex items-center gap-2.5 rounded-lg border px-3 py-2.5 text-left transition-colors ${
                      m.comingSoon
                        ? "cursor-not-allowed border-border/50 opacity-40"
                        : on
                        ? "border-primary/50 bg-primary/10"
                        : "border-border hover:bg-muted/40"
                    }`}>
                    <Terminal className={`h-4 w-4 shrink-0 ${on ? "text-primary" : "text-muted-foreground"}`} />
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5 font-medium">
                        {m.name}
                        {m.comingSoon && <span className="rounded bg-muted/60 px-1 py-0.5 text-[9px] text-muted-foreground">即将支持</span>}
                        {on && <Check className="h-3.5 w-3.5 text-primary" />}
                      </div>
                      <div className="truncate text-[11px] text-muted-foreground">{m.description}</div>
                    </div>
                  </button>
                );
              })}
            </div>
            <div className="flex items-center gap-2 pt-1">
              <button onClick={saveSubscription} className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary shadow-glow hover:bg-primary/25">
                保存
              </button>
              {existing && (
                <button onClick={forget} className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:text-destructive">
                  <Trash2 className="h-4 w-4" /> 清除
                </button>
              )}
            </div>
          </div>
        ) : (
          <div className="space-y-4 text-sm">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">选择模型</label>
              <select value={apiId} onChange={(e) => pickApiModel(e.target.value)}
                className="w-full rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50">
                {apiModels.map((m) => (
                  <option key={m.id} value={m.id}>{m.name} —— {m.description}</option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Base URL</label>
              <input value={baseURL} onChange={(e) => setBaseURL(e.target.value)} placeholder="https://api.deepseek.com"
                className="w-full rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50" />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Model</label>
              <input value={modelName} onChange={(e) => setModelName(e.target.value)} placeholder="模型名称（豆包填 ep-… 接入点 ID）"
                className="w-full rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50" />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">API Key</label>
              <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-…"
                className="w-full rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50" />
            </div>

            <div className="flex items-center gap-2">
              <button onClick={saveApi} className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary shadow-glow hover:bg-primary/25">
                保存（存本地）
              </button>
              {existing && (
                <button onClick={forget} className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:text-destructive">
                  <Trash2 className="h-4 w-4" /> 清除
                </button>
              )}
            </div>
          </div>
        )}
      </GlassCard>

      {/* 后端访问密钥：仅当后端部署时设置了 VR_API_KEY（公网防蹭用）才需要填 */}
      <GlassCard className="mt-4">
        <h3 className="mb-1 flex items-center gap-1.5 text-sm font-semibold">
          <KeyRound className="h-4 w-4 text-primary" /> 后端访问密钥（可选）
        </h3>
        <p className="mb-3 text-xs text-muted-foreground">
          仅当后端部署时设置了 <code className="rounded bg-muted/50 px-1">VR_API_KEY</code>（公网部署防蹭用）才需要填，填后端同一个值；
          本机自用没设鉴权就留空。同样只存本地浏览器。
        </p>
        <div className="flex items-center gap-2">
          <input type="password" value={accessKey} onChange={(e) => setAccessKey(e.target.value)} placeholder="与后端 VR_API_KEY 保持一致"
            className="flex-1 rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50" />
          <button onClick={saveAccess} className="rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary hover:bg-primary/25">
            保存
          </button>
        </div>
      </GlassCard>

      <DataBackupCard />
    </div>
  );
}

// 用户数据导入/导出：把账号里的自选/备注/研究记录/AI 配置/持仓账本打包成一个 JSON 备份文件。
// 换电脑、换部署、换账号迁移都靠它。导出会抹掉 AI 的 API key（备份文件不该带着密钥离开本机）。
function DataBackupCard() {
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<{ name: string; payload: UserDataExport } | null>(null);
  const [dataMode, setDataMode] = useState<"merge" | "replace">("merge");
  const [ledgersMode, setLedgersMode] = useState<"skip" | "merge" | "replace">("merge");

  const logged = isLoggedIn();

  const doExport = async () => {
    setBusy(true);
    try {
      const data = await auth.exportData();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `vibe-research-backup-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast.success("备份文件已下载（不含 API key 与密码）");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导出失败");
    } finally {
      setBusy(false);
    }
  };

  const pickFile = async (f: File | undefined) => {
    if (!f) return;
    if (f.size > 50 * 1024 * 1024) {
      toast.error("备份文件超过 50MB，请确认选的是导出的 JSON 文件");
      return;
    }
    try {
      const parsed = JSON.parse(await f.text()) as UserDataExport;
      if (parsed?.format !== "vibe-research-user-data") {
        toast.error("不是本产品的备份文件");
        return;
      }
      setFile({ name: f.name, payload: parsed });
    } catch {
      toast.error("文件不是合法的 JSON");
    }
  };

  const doImport = async () => {
    if (!file) return;
    setBusy(true);
    try {
      const result = await auth.importData(file.payload, dataMode, ledgersMode);
      const parts = [
        result.applied.length > 0 ? `同步数据 ${result.applied.length} 项` : "",
        ledgersMode !== "skip" ? `持仓账本（${ledgersMode === "merge" ? "并入" : "覆盖"}）` : "",
      ].filter(Boolean);
      toast.success(parts.length > 0 ? `导入完成：${parts.join("，")}` : "导入完成（备份里没有可变更的内容）");
      setFile(null);
      // 导入可能改了云端数据，重新拉一遍让本地缓存对齐
      await pullBackendToLocal();
      window.location.reload();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导入失败");
    } finally {
      setBusy(false);
    }
  };

  const label = "mb-1.5 block text-xs font-medium text-muted-foreground";
  const select = "w-full rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50";

  return (
    <GlassCard className="mt-4">
      <h3 className="mb-1 flex items-center gap-1.5 text-sm font-semibold">
        <DatabaseBackup className="h-4 w-4 text-primary" /> 用户数据导入 / 导出
      </h3>
      <p className="mb-3 text-xs text-muted-foreground">
        把账号里的自选股/ETF、分组、个股备注、研究记录、AI 接入配置和两个持仓账本（证券 + 场外基金）打包成一个 JSON 文件——
        换电脑、重新部署、账号间搬家时用它恢复。导出<b className="text-foreground">不含</b> API key、密码和登录会话。
      </p>

      {!logged ? (
        <p className="rounded-lg border border-border/50 bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          未登录：数据导出/导入按账号隔离，请先登录再使用。
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col justify-between gap-2 rounded-lg border border-border/50 p-3">
            <div>
              <div className="flex items-center gap-1.5 text-sm font-medium"><Download className="h-4 w-4 text-primary" /> 导出备份</div>
              <p className="mt-1 text-xs text-muted-foreground">下载当前账号的完整数据快照（JSON 文件）。</p>
            </div>
            <button onClick={doExport} disabled={busy}
              className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary shadow-glow hover:bg-primary/25 disabled:opacity-50">
              <Download className="h-4 w-4" /> 导出
            </button>
          </div>

          <div className="flex flex-col justify-between gap-2 rounded-lg border border-border/50 p-3">
            <div>
              <div className="flex items-center gap-1.5 text-sm font-medium"><Upload className="h-4 w-4 text-primary" /> 导入备份</div>
              <p className="mt-1 text-xs text-muted-foreground">选择之前导出的备份文件，恢复到当前账号。</p>
            </div>
            <input ref={fileRef} type="file" accept="application/json,.json" className="hidden"
              onChange={(e) => { pickFile(e.target.files?.[0]); e.target.value = ""; }} />
            <button onClick={() => fileRef.current?.click()} disabled={busy}
              className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted/40 disabled:opacity-50">
              <Upload className="h-4 w-4" /> 选择文件
            </button>
          </div>

          {file && (
            <div className="space-y-3 rounded-lg border border-primary/30 bg-primary/5 p-3 sm:col-span-2">
              <div className="text-xs">
                已选择 <b className="text-foreground">{file.name}</b>
                {file.payload.user?.username && <>（导出自账号 <b className="text-foreground">{file.payload.user.username}</b>）</>}
                {file.payload.exported_at_text && <>，备份时间 {file.payload.exported_at_text}</>}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label className={label}>自选 / 备注 / 研究记录 / AI 配置</label>
                  <select value={dataMode} onChange={(e) => setDataMode(e.target.value as "merge" | "replace")} className={select}>
                    <option value="merge">合并 —— 只补当前账号没有的（推荐）</option>
                    <option value="replace">替换 —— 以备份为准整体覆盖</option>
                  </select>
                </div>
                <div>
                  <label className={label}>持仓账本（证券 + 场外基金）</label>
                  <select value={ledgersMode} onChange={(e) => setLedgersMode(e.target.value as "skip" | "merge" | "replace")} className={select}>
                    <option value="merge">合并 —— 当前持仓优先，只补没有的代码</option>
                    <option value="replace">替换 —— 用备份的账本整体覆盖</option>
                    <option value="skip">跳过 —— 本次不动持仓</option>
                  </select>
                </div>
              </div>
              {(dataMode === "replace" || ledgersMode === "replace") && (
                <p className="rounded border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
                  「替换」会用备份内容覆盖当前账号数据（备份里没有的会被清掉），建议先导出一份当前数据再导入。
                </p>
              )}
              <div className="flex items-center gap-2">
                <button onClick={doImport} disabled={busy}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-4 py-2 text-sm font-medium text-primary shadow-glow hover:bg-primary/25 disabled:opacity-50">
                  <Upload className="h-4 w-4" /> {busy ? "导入中…" : "开始导入"}
                </button>
                <button onClick={() => setFile(null)} disabled={busy}
                  className="rounded-lg px-3 py-2 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50">
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </GlassCard>
  );
}
