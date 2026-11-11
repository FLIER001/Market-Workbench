import { type ReactNode } from "react";
import { cn } from "@/lib/utils";

interface Props {
  children: ReactNode;
  className?: string;
  glow?: boolean;
  onClick?: () => void;
  title?: string;
}

// 全站共享的数据面板容器；旧组件名保留，避免牵动各研究页面。
export function GlassCard({ children, className, glow, onClick, title }: Props) {
  return (
    <div
      onClick={onClick}
      onKeyDown={onClick ? (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick();
        }
      } : undefined}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      title={title}
      className={cn(
        "glass p-5",
        glow && "glass-glow",
        onClick && "cursor-pointer transition-colors hover:border-primary/50",
        className,
      )}
    >
      {children}
    </div>
  );
}
