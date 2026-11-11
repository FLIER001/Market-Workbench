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
      {loading ? "刷新中…" : label}
    </button>
  );
}
