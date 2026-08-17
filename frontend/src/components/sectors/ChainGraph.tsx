import { useEffect, useMemo, useRef, useState } from "react";
import { GraphChart } from "echarts/charts";
import { LegendComponent, TooltipComponent } from "echarts/components";
import { init, use, type ECharts } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import type { IndustryChainData } from "@/lib/api";

use([GraphChart, TooltipComponent, LegendComponent, CanvasRenderer]);

const cssColor = (name: string, alpha?: number) => {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return alpha == null ? `hsl(${value})` : `hsl(${value} / ${alpha})`;
};

const STAGE_COLOR: Record<string, string> = { 上游: "#38bdf8", 中游: "#a78bfa", 下游: "#fbbf24" };

// 产业链关系图：分层布局（左→右 = 上游→下游），实线=供给、虚线=协同/交叉。
// 点击节点回调联动环节选中；悬停高亮相连边。
export function ChainGraph({
  chain,
  focusNode,
  onSelect,
}: {
  chain: IndustryChainData;
  focusNode: string | null;
  onSelect: (nodeId: string) => void;
}) {
  const { structure } = chain;
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ECharts | null>(null);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;

  const [themeTick, setThemeTick] = useState(0);
  useEffect(() => {
    const obs = new MutationObserver(() => setThemeTick((t) => t + 1));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  const stages = useMemo(
    () => (["上游", "中游", "下游"] as const).filter((s) => structure.nodes.some((n) => n.stage === s)),
    [structure.nodes],
  );
  const stageX = useMemo(() => {
    const xs: Record<string, number> = {};
    stages.forEach((s, i) => { xs[s] = 140 + i * 260; });
    return xs;
  }, [stages]);

  const option = useMemo(() => {
    void themeTick;
    const stageCount: Record<string, number> = {};
    const nodePos: Record<string, { x: number; y: number }> = {};
    structure.nodes.forEach((node) => {
      const idx = stageCount[node.stage] ?? 0;
      stageCount[node.stage] = idx + 1;
      const total = structure.nodes.filter((n) => n.stage === node.stage).length;
      const span = 130 + total * 55;
      nodePos[node.id] = {
        x: stageX[node.stage],
        y: span / 2 - (total - 1 - idx) * 55 + 20,
      };
    });

    const foreground = cssColor("--foreground");
    const muted = cssColor("--muted-foreground");
    const bg = cssColor("--background", 0.96);
    const border = cssColor("--border", 0.45);
    const primary = cssColor("--primary");

    return {
      animation: false,
      tooltip: {
        backgroundColor: bg, borderColor: border, textStyle: { color: foreground, fontSize: 12 },
        extraCssText: "box-shadow: 0 10px 28px rgba(0,0,0,.28); border-radius: 10px;",
        formatter: (p: any) => {
          if (p.dataType !== "node") return p.data.label ?? "";
          const companies = (p.data.companies ?? []).join("、");
          return `<b>${p.name}</b><br/>${p.data.desc ?? ""}${companies ? `<br/><span style="color:${muted}">${companies}</span>` : ""}`;
        },
      },
      series: [
        {
          type: "graph",
          layout: "none",
          roam: true,
          zoom: 1.05,
          edgeSymbol: ["none", "arrow"],
          edgeSymbolSize: 7,
          label: { show: true, color: foreground, fontSize: 11, position: "bottom", distance: 3 },
          itemStyle: { borderWidth: 0 },
          lineStyle: { color: cssColor("--muted-foreground", 0.5), width: 1.4, curveness: 0.12 },
          emphasis: { focus: "adjacency", lineStyle: { width: 2.4 } },
          data: structure.nodes.map((node) => ({
            id: node.id,
            name: node.name,
            x: nodePos[node.id].x,
            y: nodePos[node.id].y,
            symbolSize: 16 + Math.min(node.companies.length, 5) * 4,
            itemStyle: { color: STAGE_COLOR[node.stage] },
            desc: node.description,
            companies: node.companies.map((c) => c.name),
            label: { color: focusNode === node.id ? primary : foreground },
          })),
          links: structure.links.map((link) => ({
            source: link.from,
            target: link.to,
            lineStyle: link.kind === "cross" ? { type: "dashed" } : {},
            label: {
              show: link.kind === "cross",
              formatter: "协同",
              color: muted,
              fontSize: 9,
            },
          })),
        },
      ],
    };
  }, [structure, stageX, focusNode, themeTick]);

  useEffect(() => {
    if (!boxRef.current) return;
    const chart = chartRef.current ?? init(boxRef.current);
    chartRef.current = chart;
    chart.setOption(option as any, true);
    chart.off("click");
    chart.on("click", (p: any) => {
      if (p.dataType === "node") selectRef.current(p.data.id as string);
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [option]);

  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);

  return (
    <div>
      <div ref={boxRef} className="h-[340px] w-full sm:h-[380px]" />
      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
        {stages.map((stage) => (
          <span key={stage} className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-full" style={{ background: STAGE_COLOR[stage] }} />
            {stage}
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-0 w-5 border-t border-muted-foreground" /> 供给/流向
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-0 w-5 border-t border-dashed border-muted-foreground" /> 协同/交叉
        </span>
        <span>节点大小 = 代表企业数；悬停查看环节说明；单击选中环节</span>
      </div>
    </div>
  );
}
