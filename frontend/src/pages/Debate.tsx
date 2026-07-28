import { useState } from "react";
import { Swords, Play, Square, Save, CheckCircle2, Circle, AlertTriangle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { StockSearchInput } from "@/components/ui/StockSearchInput";
import { debateStream, type DebateStage } from "@/lib/agents";
import { addNote } from "@/lib/notes";
import { cancelBackgroundTask, startBackgroundTask, updateBackgroundTask, useBackgroundTask } from "@/lib/backgroundTasks";

interface StageBox {
  stage: DebateStage;
  label: string;
  content: string;
  done: boolean;
}
interface DebateTaskData {
  code: string;
  rounds: number;
  status: string;
  progress: { title: string; ok: boolean }[];
  missing: string[];
  stages: StageBox[];
  error: string;
  saved: boolean;
}
const DEBATE_TASK_KEY = "debate:current";
const EMPTY_DEBATE: DebateTaskData = {
  code: "", rounds: 1, status: "", progress: [], missing: [], stages: [], error: "", saved: false,
};

// 多方用品牌橙、空方用蓝灰、主持用中性——刻意不用红绿，
// 免得和 A 股「红涨绿跌」撞车被读成涨跌信号。
const STAGE_TONE: Record<DebateStage, string> = {
  bull: "border-primary/50 bg-primary/[0.06]",
  bull_rebut: "border-primary/30 bg-primary/[0.03]",
  bear: "border-sky-500/40 bg-sky-500/[0.06]",
  bear_rebut: "border-sky-500/25 bg-sky-500/[0.03]",
  referee: "border-border bg-background/40",
};

const DOSSIER_HINT = "多空双方拿到的是同一份接口实时拉取的数据，谁也不能靠编数字赢。";

export function Debate() {
  const [code, setCode] = useState("");
  const [rounds, setRounds] = useState(1);
  const debateTask = useBackgroundTask<DebateTaskData>(DEBATE_TASK_KEY, EMPTY_DEBATE);
  const running = debateTask.status === "running";
  const { status, progress, missing, stages, saved } = debateTask.data;
  const error = debateTask.data.error || (debateTask.status === "error" ? debateTask.error || "" : "");

  async function start() {
    const c = code.trim();
    if (!/^\d{6}$/.test(c)) {
      startBackgroundTask(DEBATE_TASK_KEY, { ...EMPTY_DEBATE, error: "请输入 6 位 A 股代码" }, async () => {});
      return;
    }
    const initial: DebateTaskData = { ...EMPTY_DEBATE, code: c, rounds };
    startBackgroundTask(DEBATE_TASK_KEY, initial, async (update, signal) => {
      await debateStream(c, rounds, {
        onStatus: (next) => update((data) => ({ ...data, status: next })),
        onDossierProgress: (title, ok, loaded, total) => {
          update((data) => ({
            ...data,
            status: `正在拉取客观事实底稿… ${loaded}/${total}`,
            progress: [...data.progress, { title, ok }],
          }));
        },
        onDossierReady: (_sections, miss) => update((data) => ({ ...data, missing: miss, status: "底稿就绪，辩论开始" })),
        onStageStart: (stage, label) =>
          update((data) => ({ ...data, stages: [...data.stages, { stage, label, content: "", done: false }] })),
        onDelta: (stage, text) =>
          update((data) => ({ ...data, stages: data.stages.map((b) => (b.stage === stage && !b.done ? { ...b, content: b.content + text } : b)) })),
        onStageDone: (stage, _label, content) =>
          update((data) => ({ ...data, stages: data.stages.map((b) => (b.stage === stage && !b.done ? { ...b, content, done: true } : b)) })),
        onError: (message, stage) => update((data) => ({ ...data, error: stage ? `${stage}：${message}` : message })),
      }, signal);
      update((data) => ({ ...data, status: "辩论完成" }));
    });
  }

  function stop() {
    cancelBackgroundTask(DEBATE_TASK_KEY);
  }

  function save() {
    const body = stages.map((s) => `## ${s.label}\n\n${s.content}`).join("\n\n---\n\n");
    addNote("多空辩论", `多空辩论 · ${debateTask.data.code}`, body);
    updateBackgroundTask(DEBATE_TASK_KEY, EMPTY_DEBATE, (data) => ({ ...data, saved: true }));
  }

  const finished = stages.length > 0 && stages.every((s) => s.done);

  return (
    <div>
      <PageHeader
        title="多空辩论"
        subtitle="同一份客观数据，多方与空方各自立论、互相质疑，最后由中立主持归纳分歧点与验证清单——不给买卖结论，判断留给你自己。"
      />

      <GlassCard>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">股票代码</label>
            <StockSearchInput
              value={code}
              onChange={setCode}
              onPick={(c) => {
                setCode(c);
                updateBackgroundTask(DEBATE_TASK_KEY, EMPTY_DEBATE, (data) => ({ ...data, error: "" }));
              }}
              placeholder="代码 / 中文 / 首字母，如 600519、茅台、MT"
              disabled={running}
              className="w-44"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">辩论深度</label>
            <select
              value={rounds}
              onChange={(e) => setRounds(Number(e.target.value))}
              disabled={running}
              className="rounded-lg border border-border/60 bg-background/60 px-3 py-2 text-sm outline-none focus:border-primary/60"
            >
              <option value={1}>一轮 · 各自陈述</option>
              <option value={2}>两轮 · 加交叉反驳</option>
            </select>
          </div>
          {running ? (
            <button onClick={stop}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border/60 px-4 py-2 text-sm hover:text-destructive">
              <Square className="h-4 w-4" /> 中止
            </button>
          ) : (
            <button onClick={start}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary/90 px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary">
              <Play className="h-4 w-4" /> 开始辩论
            </button>
          )}
          {finished && !running && (
            <button onClick={save} disabled={saved}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border/60 px-4 py-2 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50">
              <Save className="h-4 w-4" /> {saved ? "已存入沉淀" : "存入沉淀"}
            </button>
          )}
        </div>

        {/* 开销提示：辩论比问答重得多，让用户在点下去之前就知道要花多久、调几次模型 */}
        {!running && !status && (
          <p className="mt-3 text-[11px] leading-relaxed text-muted-foreground/70">
            ⏱ {rounds === 2
              ? "两轮约 3 分钟 · 5 次模型调用 · 约 6 万字进上下文"
              : "一轮约 100 秒 · 3 次模型调用 · 约 3.5 万字进上下文"}
            （每个角色都会带上完整底稿）。其中拉底稿约 35 秒、走公开数据接口，不消耗 token。
            省额度可用「订阅接入」的本机 CLI，或选中档模型——数据已备齐，模型只做组织和表达。
          </p>
        )}

        {status && <p className="mt-3 text-xs text-muted-foreground">{status}</p>}
        {error && (
          <p className="mt-3 flex items-start gap-1.5 text-xs text-destructive">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {error}
          </p>
        )}

        {progress.length > 0 && (
          <div className="mt-4 border-t border-border/40 pt-3">
            <p className="mb-2 text-[11px] text-muted-foreground">{DOSSIER_HINT}</p>
            <div className="flex flex-wrap gap-x-4 gap-y-1.5">
              {progress.map((p) => (
                <span key={p.title} className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                  {p.ok
                    ? <CheckCircle2 className="h-3 w-3 text-primary/70" />
                    : <Circle className="h-3 w-3 text-muted-foreground/40" />}
                  {p.title}
                </span>
              ))}
            </div>
            {missing.length > 0 && (
              <p className="mt-2 text-[11px] text-warning">
                未取到：{missing.join("、")}（双方立论时不得臆测这部分）
              </p>
            )}
          </div>
        )}
      </GlassCard>

      <div className="mt-4 space-y-4">
        {stages.map((s) => (
          <div key={s.stage} className={`rounded-xl border p-4 ${STAGE_TONE[s.stage]}`}>
            <div className="mb-2 flex items-center gap-2">
              <Swords className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm font-semibold">{s.label}</span>
              {!s.done && <span className="animate-pulse text-[11px] text-muted-foreground">生成中…</span>}
            </div>
            <div className="prose prose-sm prose-invert max-w-none text-foreground prose-table:text-sm">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{s.content || "…"}</ReactMarkdown>
            </div>
          </div>
        ))}
      </div>

      {stages.length === 0 && !running && (
        <GlassCard className="mt-4">
          <div className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
            <Swords className="h-8 w-8 text-muted-foreground/40" />
            输入一个代码开始。后端会先拉一份客观事实底稿，再让多方 / 空方基于同一份数据互相质疑。
            <span className="text-xs">产出的是「分歧点 + 验证清单」，不是买卖建议。</span>
          </div>
        </GlassCard>
      )}

    </div>
  );
}
