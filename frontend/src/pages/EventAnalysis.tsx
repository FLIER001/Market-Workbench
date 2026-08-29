import { PageHeader } from "@/components/ui/PageHeader";
import { HolderIncreasePanel } from "@/components/event/HolderIncreasePanel";

// 事件分析：事件驱动信号研究。第一个模块是高管/管理层/股东增持名单（含透明评分）。
export function EventAnalysis() {
  return (
    <div>
      <PageHeader
        title="事件分析"
        subtitle="事件驱动信号与影响研判 · 高管/股东增持"
      />
      <HolderIncreasePanel />
    </div>
  );
}
