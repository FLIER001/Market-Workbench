import { useEffect, useRef, useState } from "react";
import { Loader2, Search } from "lucide-react";
import { api, type FundSearchResult } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  onPick: (fund: FundSearchResult) => void;
  placeholder?: string;
  className?: string;
}

// 基金模糊搜索（代码/简称/拼音），防抖 300ms，键盘可选。
export function FundSearchInput({ onPick, placeholder = "搜基金：代码 / 名称 / 拼音", className }: Props) {
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<FundSearchResult[]>([]);
  const [show, setShow] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const [searching, setSearching] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestRef = useRef(0);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const q = query.trim();
    if (!q) {
      requestRef.current += 1;
      setSuggestions([]);
      setShow(false);
      setSearching(false);
      return;
    }
    const id = ++requestRef.current;
    setSearching(true);
    // 有新输入时先隐藏旧下拉，避免搜索中闪烁
    setSuggestions([]);
    setShow(false);
    debounceRef.current = setTimeout(() => {
      api.fundSearch(q)
        .then((res) => {
          if (id !== requestRef.current) return;
          setSuggestions(res);
          setShow(true);
          setHighlight(-1);
        })
        .catch(() => {
          if (id !== requestRef.current) return;
          setSuggestions([]);
          setShow(false);
        })
        .finally(() => {
          // 只有最新请求的 finally 才清除转圈（否则旧请求会误关新请求的转圈）
          if (id === requestRef.current) setSearching(false);
        });
    }, 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [query]);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (!formRef.current?.contains(e.target as Node)) {
        setShow(false);
        setHighlight(-1);
      }
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const pick = (f: FundSearchResult) => {
    onPick(f);
    setQuery("");
    setSuggestions([]);
    setShow(false);
    setHighlight(-1);
  };

  return (
    <form ref={formRef} className={cn("relative", className)} onSubmit={(e) => e.preventDefault()}>
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => suggestions.length && setShow(true)}
        onKeyDown={(e) => {
          if (!show || suggestions.length === 0) return;
          if (e.key === "ArrowDown") { e.preventDefault(); setHighlight((h) => Math.min(h + 1, suggestions.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setHighlight((h) => Math.max(h - 1, 0)); }
          else if (e.key === "Enter" && highlight >= 0) { e.preventDefault(); pick(suggestions[highlight]); }
          else if (e.key === "Escape") { setShow(false); setHighlight(-1); }
        }}
        placeholder={placeholder}
        className="w-full rounded-xl border border-border bg-black/20 py-2 pl-9 pr-9 text-sm outline-none transition focus:border-primary/60"
      />
      {searching && (
        <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2">
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        </span>
      )}
      {show && suggestions.length > 0 && (
        <ul className="absolute z-50 mt-1 max-h-72 w-full overflow-auto rounded-xl border border-border bg-background shadow-xl">
          {suggestions.map((f, i) => (
            <li key={f.code}>
              <button
                type="button"
                onMouseDown={(e) => { e.preventDefault(); pick(f); }}
                onMouseEnter={() => setHighlight(i)}
                className={cn(
                  "flex w-full items-center gap-2 px-3 py-2 text-left text-sm",
                  i === highlight ? "bg-primary/15" : "",
                )}
              >
                <span className="font-mono text-xs text-muted-foreground">{f.code}</span>
                <span className="flex-1 truncate">{f.name}</span>
                <span className="shrink-0 text-xs text-muted-foreground">{f.type}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </form>
  );
}
