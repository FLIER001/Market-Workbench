import { type ReactNode } from "react";
import { RefreshCw } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { cn } from "@/lib/utils";

/**
 * 冷启动骨架屏共用件。
 *
 * 背景：黄金 / 油价 / 基金 PFS 这类页面首次构建要 50 秒到 2 分钟（拉历史序列 +
 * 算历史分位）。此前它们的加载态是一行文字，整页空白且看不出要等多久；更糟的是
 * 用户不知道「是不是卡死了」。骨架屏先按真实布局铺出灰块，让页面结构立刻就位，
 * 再配一句诚实的耗时说明（仅首次 + 之后秒开）。
 *
 * 这些骨架只在前端三层缓存（内存 / 本地持久化 / 后端磁盘快照）全部落空时出现，
 * 正常情况下永远看不到。
 */

export function SkeletonBlock({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-lg bg-muted/30", className)} />;
}

export interface ColdStartNoticeProps {
  /** 主标题，例如「首次计算中」 */
  title: string;
  /** 具体在做什么 + 实测耗时 */
  detail: string;
  /** 补充说明，通常提「算完会缓存，之后秒开」 */
  hint?: string;
}

export function ColdStartNotice({ title, detail, hint }: ColdStartNoticeProps) {
  return (
    <GlassCard className="mb-4 flex items-start gap-2.5 p-3.5">
      <RefreshCw className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
      <div className="text-xs leading-relaxed">
        <span className="font-medium text-foreground/80">{title}</span>
        <span className="ml-1 text-muted-foreground">{detail}</span>
        {hint && <div className="mt-0.5 text-muted-foreground/70">{hint}</div>}
      </div>
    </GlassCard>
  );
}

interface PageSkeletonProps extends ColdStartNoticeProps {
  /** 指标卡数量 */
  cards?: number;
  /** 指标卡高度类名 */
  cardHeight?: string;
  /** 是否渲染顶部三栏（左卡 / 主分数盘 / 右卡）——黄金、油价等评分页都有 */
  hero?: boolean;
}

/** 评分页通用首屏骨架：通知卡 + 顶部三栏 + 维度区 + 指标卡网格。 */
export function PageSkeleton({
  title, detail, hint, cards = 6, cardHeight = "h-36", hero = true,
}: PageSkeletonProps) {
  return (
    <div aria-busy="true" aria-live="polite">
      <ColdStartNotice title={title} detail={detail} hint={hint} />

      {hero && (
        <div className="mb-5 grid items-stretch gap-4 lg:grid-cols-[minmax(220px,1fr)_auto_minmax(220px,1fr)]">
          <GlassCard className="p-4">
            <SkeletonBlock className="h-3 w-2/5" />
            <SkeletonBlock className="mt-3 h-40" />
          </GlassCard>
          <GlassCard className="flex min-w-[280px] items-center gap-5 p-5" glow>
            <SkeletonBlock className="h-32 w-32 shrink-0 rounded-full" />
            <div className="space-y-2">
              <SkeletonBlock className="h-6 w-24" />
              <SkeletonBlock className="h-3 w-20" />
              <SkeletonBlock className="h-10 w-48" />
            </div>
          </GlassCard>
          <GlassCard className="p-5">
            <SkeletonBlock className="h-3 w-1/3" />
            <div className="mt-3 space-y-2.5">
              <SkeletonBlock className="h-4 w-full" />
              <SkeletonBlock className="h-4 w-4/5" />
              <SkeletonBlock className="h-4 w-3/5" />
            </div>
          </GlassCard>
        </div>
      )}

      <GlassCard className="mb-5 p-5">
        <SkeletonBlock className="h-3 w-20" />
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => <SkeletonBlock key={i} className="h-24" />)}
        </div>
      </GlassCard>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: cards }).map((_, i) => (
          <SkeletonBlock key={i} className={cardHeight} />
        ))}
      </div>
    </div>
  );
}

/** 表格类页面的首屏骨架（基金筛选这类「先汇总经理任职与风险数据」的场景）。 */
export function TableSkeleton({
  rows = 8, columns = 6, className, children,
}: { rows?: number; columns?: number; className?: string; children?: ReactNode }) {
  return (
    <div className={cn("p-4", className)} aria-busy="true" aria-live="polite">
      {children}
      <div className="mt-3 space-y-2">
        {Array.from({ length: rows }).map((_, r) => (
          <div key={r} className="grid gap-3" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
            {Array.from({ length: columns }).map((_, c) => (
              <SkeletonBlock key={c} className="h-5" />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
