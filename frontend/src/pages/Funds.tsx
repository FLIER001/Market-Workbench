import { PageHeader } from "@/components/ui/PageHeader";
import { FundScreenPanel } from "@/components/funds/FundScreenPanel";

// 基金板块：全市场筛选（4433 法则 + 业绩排行）。
// 基金持仓已合并到「持仓」栏目→场外基金子栏目；自选基金在「自选」栏目下管理。
export function Funds() {
  return (
    <div>
      <PageHeader
        title="基金"
        subtitle="全市场筛选 · 4433 法则 / 业绩排行"
      />
      <FundScreenPanel />
    </div>
  );
}
