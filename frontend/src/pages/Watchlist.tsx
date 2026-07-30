import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Check,
  FolderPlus,
  GripVertical,
  NotebookPen,
  Pencil,
  RefreshCw,
  Star,
  Trash2,
  X,
} from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { GlassCard } from "@/components/ui/GlassCard";
import { AskAiButton } from "@/components/ui/AskAiButton";
import { StockSearchInput } from "@/components/ui/StockSearchInput";
import {
  assignCodesToGroup,
  DEFAULT_WATCH_GROUP_ID,
  flattenWatchGroups,
  isEtfCode,
  loadWatchGroups,
  loadWatchNotes,
  parseCodes,
  saveWatchGroups,
  saveWatchNotes,
  WATCH_NOTE_MAX_LENGTH,
  type WatchCollection,
  type WatchGroup,
  type WatchNotes,
} from "@/lib/watchlist";
import { useLiveQuotes, isTradingHours } from "@/hooks/useLiveQuotes";
import { cn } from "@/lib/utils";

const color = (value: number | undefined) =>
  value == null
    ? "text-muted-foreground"
    : value > 0
      ? "text-danger"
      : value < 0
        ? "text-success"
        : "text-muted-foreground";
const pct = (value: number | undefined) =>
  value == null ? "—" : `${value > 0 ? "+" : ""}${value}%`;

const LIVE_KEY = "vr-watchlist-live";
const ALL_GROUPS = "all";

const loadLive = (): boolean => {
  try {
    return localStorage.getItem(LIVE_KEY) === "on";
  } catch {
    return false;
  }
};

const saveLive = (on: boolean) => {
  try {
    localStorage.setItem(LIVE_KEY, on ? "on" : "off");
  } catch {
    /* 存储不可用时，本次会话内仍然有效 */
  }
};

export function Watchlist() {
  const [collection, setCollection] = useState<WatchCollection>("stock");
  const [stockGroups, setStockGroups] = useState<WatchGroup[]>(() => loadWatchGroups("stock"));
  const [etfGroups, setEtfGroups] = useState<WatchGroup[]>(() => loadWatchGroups("etf"));
  const [activeGroupId, setActiveGroupId] = useState(ALL_GROUPS);
  const [targetGroupId, setTargetGroupId] = useState(DEFAULT_WATCH_GROUP_ID);
  const [input, setInput] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const [newGroupName, setNewGroupName] = useState("");
  const [editingGroupId, setEditingGroupId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [draggingGroupId, setDraggingGroupId] = useState<string | null>(null);
  const [dragOverGroupId, setDragOverGroupId] = useState<string | null>(null);
  const [live, setLive] = useState(loadLive);
  const [stockNotes, setStockNotes] = useState<WatchNotes>(loadWatchNotes);
  const [openNoteCode, setOpenNoteCode] = useState<string | null>(null);
  const [noteDraft, setNoteDraft] = useState("");

  const groups = collection === "stock" ? stockGroups : etfGroups;
  const stockCount = useMemo(() => flattenWatchGroups(stockGroups).length, [stockGroups]);
  const etfCount = useMemo(() => flattenWatchGroups(etfGroups).length, [etfGroups]);
  const codes = useMemo(() => flattenWatchGroups(groups), [groups]);
  const activeGroup = groups.find((group) => group.id === activeGroupId);
  const visibleCodes = activeGroupId === ALL_GROUPS ? codes : activeGroup?.codes || [];
  const groupByCode = useMemo(() => {
    const map = new Map<string, string>();
    for (const group of groups) {
      for (const code of group.codes) map.set(code, group.id);
    }
    return map;
  }, [groups]);

  const { quotes, loading, updatedAt, polling, error, refresh } = useLiveQuotes(codes, live);

  const persist = (next: WatchGroup[]) => {
    if (collection === "stock") setStockGroups(next);
    else setEtfGroups(next);
    saveWatchGroups(next, collection);
  };

  const switchCollection = (next: WatchCollection) => {
    setCollection(next);
    setActiveGroupId(ALL_GROUPS);
    setTargetGroupId(DEFAULT_WATCH_GROUP_ID);
    setEditingGroupId(null);
    setDraggingGroupId(null);
    setDragOverGroupId(null);
    setInput("");
    setHint(null);
    setOpenNoteCode(null);
    setNoteDraft("");
  };

  const selectGroup = (id: string) => {
    setActiveGroupId(id);
    if (id !== ALL_GROUPS) setTargetGroupId(id);
    setEditingGroupId(null);
  };

  const createGroup = () => {
    const name = newGroupName.trim().slice(0, 24);
    if (!name) return;
    if (groups.some((group) => group.name.toLowerCase() === name.toLowerCase())) {
      setHint("已有同名分组");
      return;
    }
    const id = `g-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    const next = [...groups, { id, name, codes: [] }];
    persist(next);
    setNewGroupName("");
    setActiveGroupId(id);
    setTargetGroupId(id);
    setHint(`已创建分组“${name}”`);
  };

  const saveRename = () => {
    if (!editingGroupId) return;
    const name = editingName.trim().slice(0, 24);
    if (!name) return;
    if (groups.some((group) => group.id !== editingGroupId && group.name.toLowerCase() === name.toLowerCase())) {
      setHint("已有同名分组");
      return;
    }
    persist(groups.map((group) => group.id === editingGroupId ? { ...group, name } : group));
    setEditingGroupId(null);
    setHint(`分组已改名为“${name}”`);
  };

  const deleteGroup = (id: string) => {
    if (id === DEFAULT_WATCH_GROUP_ID) return;
    const removed = groups.find((group) => group.id === id);
    if (!removed) return;
    const next = groups
      .filter((group) => group.id !== id)
      .map((group) => group.id === DEFAULT_WATCH_GROUP_ID
        ? { ...group, codes: Array.from(new Set([...group.codes, ...removed.codes])) }
        : group);
    persist(next);
    setActiveGroupId(DEFAULT_WATCH_GROUP_ID);
    if (targetGroupId === id) setTargetGroupId(DEFAULT_WATCH_GROUP_ID);
    setEditingGroupId(null);
    setHint(`已删除分组“${removed.name}”，其中 ${removed.codes.length} 只${collection === "stock" ? "股票" : " ETF"}移到未分组`);
  };

  const reorderGroup = (draggedId: string, targetId: string) => {
    if (draggedId === targetId) return;
    const from = groups.findIndex((group) => group.id === draggedId);
    const to = groups.findIndex((group) => group.id === targetId);
    if (from < 0 || to < 0) return;
    const next = [...groups];
    const [dragged] = next.splice(from, 1);
    next.splice(to, 0, dragged);
    persist(next);
    setHint("分组顺序已保存");
  };

  const addOne = (raw: string) => {
    const parsed = parseCodes(raw);
    if (parsed.length === 0) {
      setHint(raw.trim() ? "没识别到 6 位证券代码" : null);
      setInput("");
      return;
    }
    if (collection === "etf" && parsed.some((code) => !isEtfCode(code))) {
      setHint("自选 ETF 仅接受 5、15、16 开头的场内基金代码");
      return;
    }
    if (collection === "stock" && parsed.some(isEtfCode)) {
      setHint("检测到 ETF / 场内基金代码，请切换到“自选 ETF”添加");
      return;
    }
    const result = assignCodesToGroup(groups, targetGroupId, parsed.join(" "));
    if (result.added === 0 && result.moved === 0) {
      setHint(`这些${collection === "stock" ? "股票" : " ETF"}已在目标分组中`);
      setInput("");
      return;
    }
    persist(result.next);
    setInput("");
    const target = groups.find((group) => group.id === targetGroupId)?.name || "未分组";
    const parts = [];
    if (result.added) parts.push(`新增 ${result.added} 只`);
    if (result.moved) parts.push(`移动 ${result.moved} 只`);
    setHint(`${parts.join("，")}到“${target}”`);
  };

  const moveCode = (code: string, groupId: string) => {
    const result = assignCodesToGroup(groups, groupId, code);
    persist(result.next);
    const target = groups.find((group) => group.id === groupId)?.name || "未分组";
    setHint(`${quotes[code]?.name || code} 已移到“${target}”`);
  };

  const removeCode = (code: string) => {
    persist(groups.map((group) => ({
      ...group,
      codes: group.codes.filter((item) => item !== code),
    })));
    if (collection === "stock" && stockNotes[code]) {
      const nextNotes = { ...stockNotes };
      delete nextNotes[code];
      setStockNotes(nextNotes);
      saveWatchNotes(nextNotes);
    }
    if (openNoteCode === code) {
      setOpenNoteCode(null);
      setNoteDraft("");
    }
  };

  const toggleNote = (code: string) => {
    if (openNoteCode === code) {
      setOpenNoteCode(null);
      setNoteDraft("");
      return;
    }
    setOpenNoteCode(code);
    setNoteDraft(stockNotes[code] || "");
  };

  const persistNote = (code: string) => {
    const note = noteDraft.trim().slice(0, WATCH_NOTE_MAX_LENGTH);
    const nextNotes = { ...stockNotes };
    if (note) nextNotes[code] = note;
    else delete nextNotes[code];
    setStockNotes(nextNotes);
    saveWatchNotes(nextNotes);
    setOpenNoteCode(null);
    setNoteDraft("");
    setHint(note ? "备注已保存" : "备注已清除");
  };

  const toggleLive = () => {
    setLive((on) => {
      const next = !on;
      saveLive(next);
      return next;
    });
  };

  const aiContext = useMemo(() => {
    const populated = groups.filter((group) => group.codes.length > 0);
    const itemLabel = collection === "stock" ? "自选股" : "自选 ETF";
    if (populated.length === 0) return `还没有${itemLabel}。`;
    return `我的分组${itemLabel}（本地）：\n` + populated.map((group) => {
      const rows = group.codes.map((code) => {
        const quote = quotes[code];
        const row = quote
          ? `${quote.name}(${code}) 现价${quote.price} ${pct(quote.change_pct)} PE(TTM)${quote.pe_ttm ?? "—"} 换手${quote.turnover_pct ?? "—"}%`
          : `${code}（行情未取到）`;
        const note = collection === "stock" ? stockNotes[code] : "";
        return note ? `${row} 备注：${note}` : row;
      });
      return `【${group.name}】\n${rows.join("\n")}`;
    }).join("\n");
  }, [collection, groups, quotes, stockNotes]);

  const activeLabel = activeGroupId === ALL_GROUPS ? "全部自选" : activeGroup?.name || "未分组";

  return (
    <div>
      <PageHeader
        title="自选"
        subtitle="自选股与自选 ETF 分开管理；分组、排序与 AI 上下文互不混用"
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={toggleLive}
              title={live ? "关闭实时行情" : "开启实时行情（交易时段每 3 秒自动刷新）"}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs transition-colors",
                live
                  ? "border-primary/50 bg-primary/10 text-primary"
                  : "border-border/60 text-muted-foreground hover:text-foreground",
              )}
            >
              <span className="relative flex h-2 w-2">
                {polling && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/70" />}
                <span className={cn("relative inline-flex h-2 w-2 rounded-full", live ? "bg-primary" : "bg-muted-foreground/40")} />
              </span>
              实时行情
            </button>
            {codes.length > 0 && (
              <AskAiButton
                context={aiContext}
                taskId={`watchlist-${collection}`}
                label={collection === "stock" ? "让 AI 读自选股" : "让 AI 读自选 ETF"}
                suggestions={collection === "stock"
                  ? ["按分组比较估值与风险", "哪些分组近期关注度更高", "各组最大的验证点是什么"]
                  : ["按跟踪指数与主题整理", "比较估值与近期活跃度", "各组有哪些重复暴露"]}
              />
            )}
          </div>
        }
      />

      <div className="mb-4 grid max-w-md grid-cols-2 gap-2 rounded-xl border border-border/50 bg-muted/20 p-1.5">
        {([
          ["stock", "自选股", stockCount],
          ["etf", "自选 ETF", etfCount],
        ] as const).map(([id, label, count]) => (
          <button
            key={id}
            onClick={() => switchCollection(id)}
            className={cn(
              "flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-colors",
              collection === id
                ? "bg-primary/15 text-primary shadow-sm"
                : "text-muted-foreground hover:bg-muted/40 hover:text-foreground",
            )}
          >
            {label}
            <span className="rounded-full bg-background/50 px-1.5 py-0.5 text-[10px] opacity-70">{count}</span>
          </button>
        ))}
      </div>

      <GlassCard className="mb-4 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <button
              onClick={() => selectGroup(ALL_GROUPS)}
              className={cn(
                "rounded-lg px-3 py-1.5 text-xs transition-colors",
                activeGroupId === ALL_GROUPS ? "bg-primary/15 text-primary" : "bg-muted/40 text-muted-foreground hover:text-foreground",
              )}
            >
              全部 <span className="ml-1 opacity-60">{codes.length}</span>
            </button>
            {groups.map((group) => (
              <div
                key={group.id}
                onDragOver={(event) => {
                  event.preventDefault();
                  event.dataTransfer.dropEffect = "move";
                  setDragOverGroupId(group.id);
                }}
                onDrop={(event) => {
                  event.preventDefault();
                  const draggedId = event.dataTransfer.getData("text/plain") || draggingGroupId;
                  if (draggedId) reorderGroup(draggedId, group.id);
                  setDraggingGroupId(null);
                  setDragOverGroupId(null);
                }}
                className={cn(
                  "flex items-center rounded-lg transition-all",
                  activeGroupId === group.id ? "bg-primary/15 text-primary" : "bg-muted/40 text-muted-foreground hover:text-foreground",
                  draggingGroupId === group.id && "opacity-40",
                  dragOverGroupId === group.id && draggingGroupId !== group.id && "ring-1 ring-primary/60",
                )}
              >
                <span
                  draggable
                  onDragStart={(event) => {
                    setDraggingGroupId(group.id);
                    event.dataTransfer.effectAllowed = "move";
                    event.dataTransfer.setData("text/plain", group.id);
                  }}
                  onDragEnd={() => {
                    setDraggingGroupId(null);
                    setDragOverGroupId(null);
                  }}
                  className="cursor-grab py-1.5 pl-1.5 active:cursor-grabbing"
                  title="拖动调整分组顺序"
                >
                  <GripVertical className="h-3.5 w-3.5 opacity-45" />
                </span>
                <button
                  onClick={() => selectGroup(group.id)}
                  className="py-1.5 pl-0.5 pr-3 text-xs"
                >
                  {group.name} <span className="ml-1 opacity-60">{group.codes.length}</span>
                </button>
              </div>
            ))}
          </div>

          <div className="flex items-center gap-1.5">
            <input
              value={newGroupName}
              onChange={(event) => setNewGroupName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  createGroup();
                }
              }}
              placeholder="新分组名称"
              maxLength={24}
              className="h-8 w-28 rounded-lg border border-border/60 bg-background/30 px-2 text-xs outline-none focus:border-primary/60"
            />
            <button
              onClick={createGroup}
              disabled={!newGroupName.trim()}
              className="inline-flex h-8 items-center gap-1 rounded-lg border border-border/60 px-2.5 text-xs text-muted-foreground hover:text-primary disabled:opacity-40"
            >
              <FolderPlus className="h-3.5 w-3.5" /> 新建
            </button>
          </div>
        </div>

        {activeGroup && activeGroup.id !== DEFAULT_WATCH_GROUP_ID && (
          <div className="mt-3 flex items-center gap-2 border-t border-border/40 pt-3">
            {editingGroupId === activeGroup.id ? (
              <>
                <input
                  autoFocus
                  value={editingName}
                  onChange={(event) => setEditingName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") saveRename();
                    if (event.key === "Escape") setEditingGroupId(null);
                  }}
                  maxLength={24}
                  className="h-7 w-36 rounded-md border border-border/60 bg-background/30 px-2 text-xs outline-none focus:border-primary/60"
                />
                <button onClick={saveRename} className="text-primary" title="保存名称"><Check className="h-3.5 w-3.5" /></button>
                <button onClick={() => setEditingGroupId(null)} className="text-muted-foreground" title="取消"><X className="h-3.5 w-3.5" /></button>
              </>
            ) : (
              <>
                <button
                  onClick={() => {
                    setEditingGroupId(activeGroup.id);
                    setEditingName(activeGroup.name);
                  }}
                  className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-primary"
                >
                  <Pencil className="h-3 w-3" /> 重命名
                </button>
                <button
                  onClick={() => deleteGroup(activeGroup.id)}
                  className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-destructive"
                  title={`删除分组，组内${collection === "stock" ? "股票" : " ETF"}移到未分组`}
                >
                  <Trash2 className="h-3 w-3" /> 删除分组
                </button>
              </>
            )}
          </div>
        )}
      </GlassCard>

      <GlassCard className="mb-4">
        <label className="mb-1.5 block text-xs text-muted-foreground">
          添加到
          <select
            value={targetGroupId}
            onChange={(event) => setTargetGroupId(event.target.value)}
            className="mx-1 rounded border border-border/60 bg-background/40 px-1.5 py-0.5 text-xs text-foreground"
          >
            {groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
          </select>
          —— 输入代码 / 中文 / 拼音首字母，回车添加
        </label>
        <div className="flex gap-2">
          <StockSearchInput
            value={input}
            onChange={setInput}
            onPick={(code) => addOne(code)}
            placeholder={collection === "stock" ? "如：600519、茅台、MT" : "如：510300、沪深300ETF"}
            className="flex-1"
          />

        </div>
        {hint && <p className="mt-2 text-xs text-muted-foreground/70">{hint}</p>}
      </GlassCard>

      <GlassCard glow>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="flex items-center gap-1.5 font-semibold">
            <Star className="h-4 w-4 text-primary" /> {activeLabel}
            <span className="text-xs font-normal text-muted-foreground">（{visibleCodes.length}）</span>
          </h3>
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground/70">
            {error ? (
              <span className="text-warning">{error}</span>
            ) : (
              <>
                {live && !polling && codes.length > 0 && (
                  <span>{isTradingHours() ? "已暂停（页面未激活）" : "非交易时段 · 已暂停"}</span>
                )}
                {polling && <span className="text-primary/80">实时 · 每 3 秒</span>}
                {updatedAt && <span className="font-mono">{new Date(updatedAt).toLocaleTimeString("zh-CN", { hour12: false })}</span>}
              </>
            )}
            <button onClick={refresh} disabled={loading} className="text-muted-foreground hover:text-primary" title="立即刷新">
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            </button>
          </div>
        </div>

        {visibleCodes.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground/60">
            {codes.length === 0
              ? `还没有${collection === "stock" ? "自选股" : "自选 ETF"}，用上面的框添加。`
              : `这个分组暂时没有${collection === "stock" ? "股票" : " ETF"}。`}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border/50 text-left text-xs text-muted-foreground">
                  {["名称", "代码", "现价", "涨跌%", "PE(TTM)", "PB", "换手%", "分组", ""].map((heading) => (
                    <th key={heading} className="whitespace-nowrap px-2 py-2 font-medium">{heading}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visibleCodes.map((code) => {
                  const quote = quotes[code];
                  return (
                      <tr key={code} className={cn("border-b border-border/30", openNoteCode === code && "bg-primary/[0.035]")}>
                        <td className="px-2 py-2.5 font-medium">
                          <Link
                            to={`/stock-data?code=${code}`}
                            className="underline-offset-4 transition-colors hover:text-primary hover:underline"
                            title={`打开 ${quote?.name || code} 的个股数据`}
                          >
                            {quote?.name || "—"}
                          </Link>
                        </td>
                        <td className="px-2 py-2.5 font-mono text-xs text-muted-foreground">{code}</td>
                        <td className={cn("px-2 py-2.5 font-mono", color(quote?.change_pct))}>{quote ? quote.price : "—"}</td>
                        <td className={cn("px-2 py-2.5 font-mono", color(quote?.change_pct))}>{quote ? pct(quote.change_pct) : "—"}</td>
                        <td className="px-2 py-2.5 font-mono text-muted-foreground">{quote?.pe_ttm ?? "—"}</td>
                        <td className="px-2 py-2.5 font-mono text-muted-foreground">{quote?.pb ?? "—"}</td>
                        <td className="px-2 py-2.5 font-mono text-muted-foreground">{quote?.turnover_pct ?? "—"}</td>
                        <td className="px-2 py-2.5">
                          <select
                            value={groupByCode.get(code) || DEFAULT_WATCH_GROUP_ID}
                            onChange={(event) => moveCode(code, event.target.value)}
                            className="h-7 max-w-28 rounded-md border border-border/50 bg-background/30 px-1.5 text-xs outline-none hover:border-primary/50"
                          >
                            {groups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
                          </select>
                        </td>
                        <td className="px-2 py-2.5">
                          <div className="flex items-center gap-8">
                            {collection === "stock" && (
                              <button
                                onClick={() => toggleNote(code)}
                                className={cn(
                                  "transition-colors hover:text-primary",
                                  stockNotes[code] || openNoteCode === code ? "text-primary" : "text-muted-foreground/50",
                                )}
                                title={stockNotes[code] ? `备注：${stockNotes[code]}` : "添加备注"}
                              >
                                <NotebookPen className="h-3.5 w-3.5" />
                              </button>
                            )}
                            <button
                              onClick={() => removeCode(code)}
                              className="text-muted-foreground/50 hover:text-destructive"
                              title={`移出${collection === "stock" ? "自选股" : "自选 ETF"}`}
                            >
                              <X className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </td>
                      </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>

      {collection === "stock" && openNoteCode && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4 backdrop-blur-[2px]"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) toggleNote(openNoteCode);
          }}
          role="presentation"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="watch-note-title"
            className="w-full max-w-lg rounded-2xl border border-border/70 bg-background/95 p-5 shadow-2xl"
          >
            <div className="mb-4 flex items-start justify-between gap-4">
              <div>
                <h3 id="watch-note-title" className="text-base font-semibold">自选股笔记</h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  {quotes[openNoteCode]?.name || "股票"} · <span className="font-mono">{openNoteCode}</span>
                </p>
              </div>
              <button
                onClick={() => toggleNote(openNoteCode)}
                className="rounded-md p-1 text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                title="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <textarea
              autoFocus
              rows={5}
              maxLength={WATCH_NOTE_MAX_LENGTH}
              value={noteDraft}
              onChange={(event) => setNoteDraft(event.target.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") persistNote(openNoteCode);
                if (event.key === "Escape") toggleNote(openNoteCode);
              }}
              placeholder="输入简短备注，例如：等待业绩拐点验证"
              className="w-full resize-none rounded-xl border border-border/70 bg-muted/20 px-3 py-2.5 text-sm leading-6 outline-none transition-colors placeholder:text-muted-foreground/45 focus:border-primary/60 focus:ring-2 focus:ring-primary/10"
            />

            <div className="mt-3 flex items-center justify-between gap-3">
              <span className="text-[11px] text-muted-foreground/60">
                {noteDraft.length}/{WATCH_NOTE_MAX_LENGTH} · ⌘/Ctrl + Enter 保存
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => toggleNote(openNoteCode)}
                  className="h-8 rounded-lg px-3 text-xs text-muted-foreground hover:bg-muted/50"
                >
                  取消
                </button>
                <button
                  onClick={() => persistNote(openNoteCode)}
                  className="h-8 rounded-lg bg-primary/15 px-4 text-xs font-medium text-primary hover:bg-primary/25"
                >
                  保存
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
