import { useRef, useState, useEffect } from "react";
import { createPortal } from "react-dom";
import { api, type SearchResult } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  value: string;
  onChange: (v: string) => void;
  onPick: (code: string) => void;
  placeholder?: string;
  className?: string;
  disabled?: boolean;
}

/** 券商式自动提示输入框：代码 / 中文 / 拼音首字母 → 下拉选择。
 *  下拉用 createPortal 挂到 document.body + fixed 定位，跳出父级 stacking context，
 *  避免被 GlassCard 等容器遮挡。 */
export function StockSearchInput({ value, onChange, onPick, placeholder, className, disabled }: Props) {
  const [suggestions, setSuggestions] = useState<SearchResult[]>([]);
  const [showDrop, setShowDrop] = useState(false);
  const [hlIdx, setHlIdx] = useState(-1);
  const [pos, setPos] = useState({ top: 0, left: 0, width: 0 });
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const dropRef = useRef<HTMLDivElement>(null);

  // debounce 300ms 自动搜索
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const q = value.trim();
    if (!q) { setSuggestions([]); setShowDrop(false); return; }
    debounceRef.current = setTimeout(() => {
      api.search(q).then(setSuggestions).catch(() => setSuggestions([]));
    }, 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [value]);

  // 点外部关闭下拉（portal 里的下拉也要算"内部"，否则点选项会先触发关闭）
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const t = e.target as Node;
      if (wrapRef.current?.contains(t)) return;
      if (dropRef.current?.contains(t)) return;
      setShowDrop(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // 下拉打开时按输入框实际位置定位（fixed），滚动 / 缩放时跟随
  useEffect(() => {
    if (!showDrop || !inputRef.current) return;
    const update = () => {
      const r = inputRef.current!.getBoundingClientRect();
      setPos({ top: r.bottom + 4, left: r.left, width: r.width });
    };
    update();
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [showDrop]);

  const pick = (s: SearchResult) => {
    onChange(s.code);
    setShowDrop(false);
    setHlIdx(-1);
    setTimeout(() => onPick(s.code), 0);
  };

  const dropdown = showDrop && suggestions.length > 0
    ? createPortal(
        <div
          ref={dropRef}
          style={{ position: "fixed", top: pos.top, left: pos.left, width: Math.max(pos.width, 256), zIndex: 9999 }}
          className="rounded-lg border border-border bg-background shadow-lg"
        >
          {suggestions.map((s, i) => (
            <button
              key={s.code}
              type="button"
              onClick={() => pick(s)}
              onMouseEnter={() => setHlIdx(i)}
              className={cn(
                "flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted/50",
                i === hlIdx && "bg-muted/50",
              )}
            >
              <span className="font-mono text-xs text-muted-foreground">{s.code}</span>
              <span className="flex-1 truncate">{s.name}</span>
              <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">{s.market}</span>
            </button>
          ))}
        </div>,
        document.body,
      )
    : null;

  return (
    <div className="relative" ref={wrapRef}>
      <input
        ref={inputRef}
        value={value}
        onChange={(e) => { onChange(e.target.value); setShowDrop(true); setHlIdx(-1); }}
        onFocus={() => suggestions.length > 0 && setShowDrop(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            if (hlIdx >= 0 && hlIdx < suggestions.length) pick(suggestions[hlIdx]);
            else if (suggestions.length > 0) pick(suggestions[0]);
            else onPick(value.trim().toUpperCase());
          } else if (e.key === "ArrowDown") {
            e.preventDefault(); setHlIdx((i) => Math.min(i + 1, suggestions.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault(); setHlIdx((i) => Math.max(i - 1, -1));
          } else if (e.key === "Escape") {
            setShowDrop(false); setHlIdx(-1);
          }
        }}
        placeholder={placeholder}
        disabled={disabled}
        className={cn("rounded-lg border border-border bg-black/20 px-3 py-2 text-sm outline-none focus:border-primary/50", className)}
      />
      {dropdown}
    </div>
  );
}
