import { RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";

interface Props {
  onClick: () => void;
  loading?: boolean;
  disabled?: boolean;
  label?: string;
  className?: string;
}

export function RefreshButton({ onClick, loading = false, disabled = false, label = "刷新", className }: Props) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || loading}
      aria-label={label}
      className={cn("inline-flex h-[30px] items-center gap-1.5 border border-border bg-card px-3 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50", className)}
    >
      <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
      {/* 两个状态的文案各占一格（不可见），按钮宽度恒为较宽者，切换时不跳动 */}
      <span className="inline-grid">
        <span className="invisible col-start-1 row-start-1">{label}</span>
        <span className="invisible col-start-1 row-start-1">刷新中…</span>
        <span className="col-start-1 row-start-1">{loading ? "刷新中…" : label}</span>
      </span>
    </button>
  );
}
