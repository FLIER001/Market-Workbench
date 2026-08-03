// 自选页「AI分析」列：每行一个按钮，点击后调用后端 /api/deep-analysis 代理
// 把「深入分析 <名称>」发给独立部署的 hermes-agent（约 20 分钟级慢任务），
// 回传打分（满分 5 分）+ 简要分析。任务注册进 backgroundTasks，
// 页面跳转后回来仍可查看进度/结果；点分数可打开阅读弹窗。

import { useState } from "react";
import { createPortal } from "react-dom";
import { Loader2, Sparkles, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ApiError, authHeaders } from "@/lib/api";
import { backgroundTaskKey, startBackgroundTask, useBackgroundTask } from "@/lib/backgroundTasks";
import { cn } from "@/lib/utils";

interface DeepAnalysisData {
  name: string;
  content?: string;
  score?: number | null;
  startedAt?: number;
}

const EMPTY: DeepAnalysisData = { name: "" };

// 从结论文本中提取 5 分制评分：优先「六关评分 2.7」「评分 4.2/5」，其次「3.8 分（满分5分）」。
function extractScore(content: string): number | null {
  const patterns = [
    /评分[^0-9]{0,8}(\d(?:\.\d)?)\s*(?:\/\s*5|分)?/,
    /(\d(?:\.\d)?)\s*\/\s*5\s*分?/,
    /(\d(?:\.\d)?)\s*分[（(]?\s*满分\s*5\s*分/,
  ];
  for (const pattern of patterns) {
    const match = content.match(pattern);
    if (!match) continue;
    const value = Number(match[1]);
    if (value >= 0 && value <= 5) return Math.round(value * 10) / 10;
  }
  return null;
}

async function requestDeepAnalysis(prompt: string, signal: AbortSignal): Promise<string> {
  let resp: Response;
  try {
    resp = await fetch("/api/deep-analysis", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ prompt }),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("连接不到后端，请先启动 backend（uvicorn app:app --port 8900）", 0);
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body?.detail) detail = body.detail;
    } catch { /* 保留状态码 */ }
    throw new ApiError(detail, resp.status);
  }
  const body = await resp.json();
  return String(body?.data?.content || "").trim();
}

function scoreColor(score: number | null | undefined): string {
  if (score == null) return "text-muted-foreground";
  if (score >= 4) return "text-emerald-400";
  if (score >= 3) return "text-amber-400";
  return "text-rose-400";
}

function formatElapsed(startedAt?: number): string {
  if (!startedAt) return "";
  const minutes = Math.max(1, Math.round((Date.now() - startedAt) / 60000));
  return `${minutes} 分钟`;
}

export function DeepAnalysisButton({ code, name }: { code: string; name?: string }) {
  const label = name || code;
  const taskKey = backgroundTaskKey("watchlist-deep-analysis", code);
  const task = useBackgroundTask<DeepAnalysisData>(taskKey, EMPTY);
  const [open, setOpen] = useState(false);

  const running = task.status === "running";
  const score = task.data.score;
  const hasResult = task.status === "done" && !!task.data.content;

  const start = () => {
    const began = startBackgroundTask<DeepAnalysisData>(
      taskKey,
      { name: label, startedAt: Date.now() },
      async (update, signal) => {
        const content = await requestDeepAnalysis(`深入分析 ${label}`, signal);
        update((current) => ({ ...current, content, score: extractScore(content) }));
      },
    );
    if (!began) setOpen(true);
  };

  return (
    <>
      {running ? (
        <span
          className="inline-flex items-center gap-1 text-xs text-primary/80"
          title={`深度分析中（约 20 分钟）${task.data.startedAt ? `，已进行 ${formatElapsed(task.data.startedAt)}` : ""}`}
        >
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        </span>
      ) : hasResult ? (
        <button
          onClick={() => setOpen(true)}
          className={cn("font-mono text-xs font-semibold transition-opacity hover:opacity-70", scoreColor(score))}
          title="查看深度分析"
        >
          {score != null ? score.toFixed(1) : "查看"}
        </button>
      ) : (
        <button
          onClick={start}
          className={cn(
            "transition-colors hover:text-primary",
            task.status === "error" ? "text-warning" : "text-muted-foreground/50",
          )}
          title={task.status === "error" ? `分析失败：${task.error || "未知错误"}（点击重试）` : "AI 深度分析（约 20 分钟）"}
        >
          <Sparkles className="h-3.5 w-3.5" />
        </button>
      )}

      {open && hasResult && createPortal(
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4 backdrop-blur-[2px]"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setOpen(false);
          }}
          role="presentation"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="deep-analysis-title"
            className="flex max-h-[82vh] w-full max-w-2xl flex-col rounded-2xl border border-border/70 bg-background/95 p-5 shadow-2xl"
          >
            <div className="mb-3 flex shrink-0 items-start justify-between gap-4">
              <div>
                <h3 id="deep-analysis-title" className="flex items-center gap-2 text-base font-semibold">
                  AI 深度分析
                  {score != null && (
                    <span className={cn("font-mono text-sm font-bold", scoreColor(score))}>
                      {score.toFixed(1)}<span className="text-xs font-normal text-muted-foreground"> / 5</span>
                    </span>
                  )}
                </h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  {task.data.name} · <span className="font-mono">{code}</span>
                  {task.updatedAt > 0 && (
                    <span> · {new Date(task.updatedAt).toLocaleString("zh-CN", { hour12: false })}</span>
                  )}
                </p>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={start}
                  className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                  title="重新分析（约 20 分钟）"
                >
                  重新分析
                </button>
                <button
                  onClick={() => setOpen(false)}
                  className="rounded-md p-1 text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                  title="关闭"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>
            <div className="prose prose-sm prose-invert max-w-none overflow-y-auto pr-1 text-foreground">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{task.data.content || ""}</ReactMarkdown>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
