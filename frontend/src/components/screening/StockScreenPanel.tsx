import { useRef, useEffect } from "react";
import { Link } from "react-router-dom";
import { Sparkles, Send, Loader2, Wrench, AlertCircle, Settings, RotateCcw } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { hasLlm, chatStream } from "@/lib/llm";
import { SaveNoteButton } from "@/components/ui/SaveNoteButton";
import { backgroundTaskKey, resetBackgroundTask, startBackgroundTask, useBackgroundTask } from "@/lib/backgroundTasks";

// 自然语言选股：用户用一句话描述条件，AI 调用数据工具逐项核验、给出候选清单与证据。
// 复用「问 AI」同一条链路（用户自己的模型 + 后端工具层），本产品不内置候选池、不打分。

const SYSTEM_CONTEXT = `场景：个股筛选。用户给出一句话筛选条件，你用数据工具逐项核验，产出候选清单。

可用数据边界（决定筛选条件能核验到什么程度）：
- 行情与估值：query_quote（批量现价/PE/PB/市值/换手）/ query_valuation_percentile（PE/PB 历史分位）
- 基本面：query_financials（营收/净利同比、ROE、毛利率）/ query_company_info
- 资金筹码：query_fund_flow（主力净流入）/ query_holders（股东户数）/ query_dividend（股息率）
- 事件风险：query_announcements / query_lockup（解禁）
- 行业层：query_industry_comparison（行业涨跌与成交额）/ query_industry_reports（行业研报）
- 注意：没有全市场逐票基本面扫描接口，候选代码通常需由用户提供、或从行业对比与资讯线索中找。

工作方式：
1. 条件可核验的（估值分位、资金流向、财务、解禁…）：列「核验通过」并附数据。
2. 条件超出数据边界的：明说核验不了，不要编。
3. 候选不足时给替代思路（如先按行业层缩小范围），不要硬凑。

输出格式：先一句话说明核验口径，然后一张候选表（代码/名称/关键数据/符合哪条），末尾列「未核验项与局限」。只陈述事实与证据，不评级、不推荐买入。`;

const SUGGESTIONS = [
  "高股息：股息率 4% 以上、PE 历史分位低于 30% 的红利股",
  "主力资金近 20 日持续净流入、且近期无解禁",
  "ROE 连续保持高位、估值分位还低的消费白马",
  "帮我核验这几个代码：601899、600900、000858（估值分位 + 资金 + 解禁）",
];

const TOOL_LABEL: Record<string, string> = {
  query_quote: "查行情",
  query_valuation: "查估值",
  query_valuation_percentile: "查估值分位",
  query_financials: "查财务",
  query_fund_flow: "查资金流",
  query_holders: "查股东户数",
  query_dividend: "查分红",
  query_announcements: "查公告",
  query_lockup: "查解禁",
  query_industry_comparison: "行业对比",
  query_industry_reports: "行业研报",
  query_reports: "查研报",
  query_news: "查新闻",
  search_public_news: "联网核验",
};

const argStr = (a: Record<string, unknown>): string => {
  if (Array.isArray(a.codes)) return (a.codes as unknown[]).join(",");
  if (typeof a.code === "string") return a.code;
  return "";
};

interface ToolUse { name: string; arg: string }
interface TaskData {
  question: string;
  answer: string;
  tools: ToolUse[];
  started: boolean;
}
const EMPTY: TaskData = { question: "", answer: "", tools: [], started: false };
const TASK_KEY = backgroundTaskKey("stock-screen", "nl");

export function StockScreenPanel({ prefill }: { prefill?: string | null }) {
  const task = useBackgroundTask<TaskData>(TASK_KEY, EMPTY);
  const running = task.status === "running";
  const err = task.status === "error" ? task.error : null;
  const { question, answer, tools, started } = task.data;
  const inputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prefillUsed = useRef(false);

  useEffect(() => {
    if (started && !running) inputRef.current?.focus();
  }, [started, running]);

  // 外部带参跳转进来时填入输入框，不自动发送（避免误触发一次 LLM 调用）
  useEffect(() => {
    if (prefill && !prefillUsed.current && inputRef.current) {
      inputRef.current.value = prefill;
      prefillUsed.current = true;
      inputRef.current?.focus();
    }
  }, [prefill]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [answer, running]);

  const run = async (q: string) => {
    const query = q.trim();
    if (!query || running) return;
    startBackgroundTask(TASK_KEY, { ...EMPTY, question: query, started: true }, async (update, signal) => {
      const patch = (fn: (d: TaskData) => TaskData) => update((d) => fn(d));
      await chatStream([{ role: "user", content: query }], SYSTEM_CONTEXT, {
        onTool: (tool, args) => patch((d) => ({ ...d, tools: [...d.tools, { name: tool, arg: argStr(args) }] })),
        onDelta: (t) => patch((d) => ({ ...d, answer: d.answer + t })),
      }, signal);
    });
  };

  if (!started) {
    return (
      <div className="space-y-3">
        <div className="rounded-xl border border-border/60 bg-muted/20 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Sparkles className="h-4 w-4 text-primary" /> 用一句话描述你要找的股票
          </div>
          <form
            onSubmit={(e) => { e.preventDefault(); run(inputRef.current?.value || ""); }}
            className="flex items-end gap-2"
          >
            <input
              ref={inputRef}
              placeholder="例如：股息率 4% 以上、估值处于历史低位的红利股"
              className="flex-1 rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50"
            />
            <button
              type="submit"
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary/15 px-3 py-2 text-sm font-medium text-primary hover:bg-primary/25"
            >
              <Send className="h-4 w-4" /> 开始筛选
            </button>
          </form>
          {!hasLlm() && (
            <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
              <Settings className="h-3.5 w-3.5" />
              需要先<Link to="/settings" className="mx-0.5 underline underline-offset-2 hover:text-primary">接入你的 AI</Link>
              ，筛选由你的模型调数据工具完成，本产品不内置候选池、不排名。
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-1.5">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              onClick={() => run(s)}
              className="rounded-full border border-border bg-muted/40 px-2.5 py-1 text-xs hover:border-primary/40 hover:text-primary"
            >
              {s}
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div ref={scrollRef} className="max-h-[60vh] space-y-3 overflow-auto rounded-xl border border-border/60 bg-muted/20 p-4 text-sm">
        <p className="whitespace-pre-wrap break-words rounded-lg bg-primary/15 px-3 py-2 text-foreground">{question}</p>

        {tools.length > 0 && (
          <div className="flex flex-wrap items-center gap-1">
            <span className="text-[10px] text-muted-foreground/70">数据来源</span>
            {tools.map((t, i) => (
              <span key={i} className="inline-flex items-center gap-0.5 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
                <Wrench className="h-2.5 w-2.5" /> {TOOL_LABEL[t.name] || t.name}{t.arg ? ` ${t.arg}` : ""}
              </span>
            ))}
          </div>
        )}

        {answer ? (
          <div className="prose prose-sm dark:prose-invert max-w-none break-words text-foreground">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown>
          </div>
        ) : (
          running && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> AI 正在调取数据逐项核验…
            </div>
          )
        )}

        {running && answer && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> 继续核验中…
          </div>
        )}
        {err && (
          <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive">
            <AlertCircle className="h-3.5 w-3.5 shrink-0" /> {err}
          </div>
        )}
        {!running && answer && <SaveNoteButton kind="问AI" title={`个股筛选 · ${question.slice(0, 24)}`} content={answer} />}
      </div>

      {!running && (
        <form
          onSubmit={(e) => { e.preventDefault(); run(inputRef.current?.value || ""); }}
          className="flex items-end gap-2"
        >
          <input
            ref={inputRef}
            defaultValue=""
            placeholder="换个条件再筛一次，或追加核验要求…"
            className="flex-1 rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50"
          />
          <button type="submit" className="rounded-lg bg-primary/15 px-3 py-2 text-sm font-medium text-primary hover:bg-primary/25">
            <Send className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => resetBackgroundTask(TASK_KEY, EMPTY)}
            className="inline-flex items-center gap-1 rounded-lg px-3 py-2 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
            title="清空本轮结果，重新开始"
          >
            <RotateCcw className="h-3.5 w-3.5" /> 重新开始
          </button>
        </form>
      )}
    </div>
  );
}
