import { PageHeader } from "@/components/ui/PageHeader";
import { FundScreenPanel } from "@/components/funds/FundScreenPanel";

// 基金板块：PFS V3.0 Manager-First 优质潜力基金筛选。
// 基金持仓已合并到「持仓」栏目→场外基金子栏目；自选基金在「自选」栏目下管理。
export function Funds() {
  return (
    <div>
      <PageHeader
        title="标的筛选"
        subtitle="优质潜力基金 · Manager-First / Gate + Q + P + Confidence + Penalty"
      />
      <FundScreenPanel />
    </div>
  );
}
