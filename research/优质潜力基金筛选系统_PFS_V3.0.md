# 优质潜力基金筛选系统 PFS V3.0
## Potential Fund Selection System — Manager-First 技术文档

> 版本：V3.0
> 日期：2026-08-11
> 适用范围：中国公募基金研究、基金投顾、FOF/MOM、财富管理产品池
> 核心适用对象：主动权益基金；主动债券、FOF 可复用部分框架；指数基金/ETF 使用独立分支
> 上游方法论：`中国公募基金筛选方法论_ManagerFirst_V2.0`
> 系统目标：从全市场基金中筛选出“**真实能力较强、能力可持续、当前仍有容量、尚未过度拥挤、产品实现效率高**”的优质潜力基金。

---

# 0. 文档定位

本文件不是基金评价指标清单，而是一套可工程化实现的基金筛选系统设计。

系统需要解决五个问题：

1. **谁在真正做投资决策？**
2. **这个经理/团队是否具有真实可验证的投资能力？**
3. **这种能力是否具有持续性？**
4. **当前规模、团队、资金流和产品约束是否仍允许能力继续兑现？**
5. **投资者最终通过具体基金产品能获得多少净收益？**

系统研究主键由传统：

```text
fund_id
```

升级为：

```text
manager_id × strategy_id × platform_id × fund_id × date
```

---

# 1. 系统目标函数

系统不直接预测未来绝对收益，而预测未来 24—36 个月的可实现超额收益：

\[
ExpectedFutureAlpha
\]

定义：

\[
E(\alpha_{future})
=
Skill_{posterior}
\times Persistence
\times Portability
\times CapacityRetention
-
TotalCost
\]

其中：

| 变量 | 含义 |
|---|---|
| `Skill_posterior` | 在现有证据下，经理拥有真实投资能力的后验概率 |
| `Persistence` | 已识别能力在未来继续有效的概率 |
| `Portability` | 能力对平台、团队、研究资源变化的可迁移程度 |
| `CapacityRetention` | 当前 AUM 与策略容量下 Alpha 的保留程度 |
| `TotalCost` | 管理费、托管费、销售费用、交易成本、冲击成本等 |

系统目标不是寻找：

```text
过去收益最高
```

而是寻找：

```text
Skill 已出现
+
证据正在累积
+
规模尚未侵蚀 Alpha
+
经理仍有管理带宽
+
资金尚未过度拥挤
+
产品实现效率高
```

---

# 2. 总体系统架构

```text
                    ┌─────────────────┐
                    │  Fund Universe  │
                    └────────┬────────┘
                             ↓
              ┌──────────────────────────┐
              │ Strategy Classification  │
              └────────────┬─────────────┘
                           ↓
           ┌────────────────────────────────┐
           │ Manager × Strategy × Platform │
           └──────────────┬─────────────────┘
                          ↓
                ┌──────────────────┐
                │ Hard Gate Engine │
                └────────┬─────────┘
                         ↓
       ┌────────────────────────────────────┐
       │ Quality Engine                     │
       │ Manager / Process / Verified Skill│
       │ Platform / Implementation          │
       └────────────────┬───────────────────┘
                        ↓
       ┌────────────────────────────────────┐
       │ Potential Engine                   │
       │ Evidence / Capacity / Bandwidth    │
       │ Flow / Platform / Implementation   │
       └────────────────┬───────────────────┘
                        ↓
              ┌────────────────────┐
              │ Confidence Engine  │
              └─────────┬──────────┘
                        ↓
              ┌────────────────────┐
              │ Risk Penalty Engine│
              └─────────┬──────────┘
                        ↓
          ┌────────────────────────────┐
          │ Potential Fund Score (PFS) │
          └──────────────┬─────────────┘
                         ↓
          ┌────────────────────────────┐
          │ Product Selection Engine   │
          └──────────────┬─────────────┘
                         ↓
          ┌────────────────────────────┐
          │ Portfolio De-duplication   │
          └──────────────┬─────────────┘
                         ↓
      Core Buy / Potential Buy / Watch / Review / Exclude
```

---

# 3. 系统核心：Gate + Q + P + C + Penalty

PFS 使用四层结构：

```text
Gate        是否有资格进入候选池
Q           是否真正优质
P           是否仍有未来兑现空间
C           证据可信度有多高
Penalty     是否存在不能被平均掉的结构性风险
```

## 3.1 原始得分

\[
RawScore
=
0.75Q+0.25P
\]

其中：

- \(Q\)：Quality Score，0—100；
- \(P\)：Potential Score，0—100。

`Q` 高于 `P`，因为“优质”必须先成立，潜力只能作为未来增益判断。

---

## 3.2 置信度收缩

定义：

\[
FinalScore
=
50+C(RawScore-50)-RiskPenalty
\]

其中：

\[
0\le C\le1
\]

该公式将证据不足的极端高分向 50 分收缩。

示例：

```text
RawScore = 90
Confidence = 0.60
RiskPenalty = 0
```

则：

\[
FinalScore
=
50+0.6\times(90-50)=74
\]

含义：

> 新锐经理可以进入潜力池，但不能因为短历史中的极端高收益直接超过长周期已验证经理。

---

# 4. Universe：基金池构建

首先按基金投资任务拆分独立 Universe。

```text
主动权益
├── 大盘价值
├── 大盘成长
├── 中盘
├── 小盘
├── 均衡
├── 行业/主题
├── 量化
└── 指数增强

主动债券
├── 纯债
├── 一级债
├── 二级债
├── 可转债
└── 固收+

FOF
├── 权益FOF
├── 平衡FOF
└── 养老FOF

指数产品
├── 宽基ETF
├── 行业ETF
├── 主题ETF
└── Smart Beta
```

严禁跨真实策略直接排名。

---

# 5. Strategy Classification

基金合同分类只能作为初始标签，系统必须建立真实策略分类。

## 5.1 四层分类

### Layer 1：合同定位

```text
基金类型
投资范围
业绩比较基准
权益仓位范围
行业限制
```

### Layer 2：收益风格

通过收益回归识别：

- Market；
- Size；
- Value；
- Growth；
- Momentum；
- Quality；
- Low Volatility；
- 行业因子。

### Layer 3：持仓风格

通过持仓识别：

- 市值；
- 估值；
- 盈利；
- 行业；
- 集中度；
- 换手；
- Holding Horizon。

### Layer 4：策略聚类

使用：

```text
收益相关性
+
持仓相似度
+
因子暴露
+
Benchmark
+
投资目标
```

将基金聚类到真正可比的 `strategy_id`。

---

# 6. Manager × Strategy × Platform

基金经理评价不能直接继承产品全部历史。

## 6.1 Manager Track Record

构建：

```text
manager_id
strategy_id
company_id
fund_id
start_date
end_date
decision_role
co_manager_count
```

经理历史拆分为：

### Career Track Record

经理职业生涯所有可归因策略历史。

### Current Platform Track Record

经理在当前基金公司、当前研究平台下的记录。

### Current Strategy Track Record

经理当前策略的直接记录。

---

## 6.2 经理更换

规则：

```text
前任经理业绩
→ 保留在 Product History
→ 不计入现任经理 Skill Score
```

共管交接期：

```text
Transition Period
```

单独标记。

---

# 7. Gate Engine

Gate 负责排除不值得进入评分阶段的对象。

## 7.1 Hard Exclude

以下原则上直接排除：

- 严重合规或治理事件；
- 基金合同与真实投资行为长期严重不一致；
- 经理身份或投资责任无法确认；
- 产品已明显失去投资可实现性；
- 关键数据严重缺失；
- 基金处于异常运营状态；
- 策略容量显著突破且无法修复。

## 7.2 Soft Gate / Review

进入人工复核：

- 新经理接管不足 18 个月；
- 多经理共管且职责模糊；
- Benchmark 明显不合理；
- 最近发生核心团队变化；
- 风格发生明显漂移；
- AUM 短期快速增长；
- 管理产品数量突然增加。

---

# 8. 成熟经理池与新锐潜力池

为了避免错杀潜力经理，系统建立双池。

## 8.1 Mature Pool

建议：

```text
Current Strategy Track Record ≥ 3 years
```

最好：

```text
覆盖 ≥ 1 个完整市场周期
```

## 8.2 Emerging Manager Pool

建议最低条件：

```text
当前策略历史 ≥ 18个月
+
职业相关记录足够
+
持仓决策样本足够
+
投资流程可识别
+
Confidence ≥ 0.55
```

新锐经理不通过硬性长历史排除，而通过置信度收缩处理。

---

# 9. Quality Score：Q

定义：

\[
Q
=
0.35M
+
0.25Process
+
0.20VerifiedSkill
+
0.10Platform
+
0.10Implementation
\]

---

# 10. Manager / Team Score：35%

| 指标 | 分值 |
|---|---:|
| 决策权清晰度 | 3 |
| Manager Track Record | 4 |
| 选股能力 | **8** |
| 行业配置能力 | 4 |
| 择时/资产配置能力 | 2 |
| 风险控制能力 | 4 |
| 交易能力 | 3 |
| 风格/能力圈稳定性 | 3 |
| 容量管理能力 | 2 |
| Manager Bandwidth | 2 |
| **合计** | **35** |

---

# 11. 选股能力

主动权益中最高权重的单一能力。

## 11.1 Factor-Adjusted Alpha

\[
R_{p,t}-R_{f,t}
=
\alpha
+
\sum_k\beta_k F_{k,t}
+
\epsilon_t
\]

建议控制：

```text
Market
Size
Value
Growth
Momentum
Quality
LowVol
Industry
```

## 11.2 Holdings-Based Selection

使用特征基准：

\[
CS_t
=
\sum_i
w_{i,t}
(R_{i,t+1}-R_{matched(i),t+1})
\]

判断基金经理持有的股票相对于相似股票是否持续表现更优。

## 11.3 Buy / Sell Skill

分别测量：

```text
新增
加仓
减仓
清仓
长期核心仓
```

理想：

\[
E(R_{Buy}-R_{Matched})>0
\]

且：

\[
E(R_{Sell}-R_{Matched})<0
\]

## 11.4 行业内选股

分离：

\[
IndustryAllocation
\]

和：

\[
StockSelectionWithinIndustry
\]

避免把长期押中某一行业误判为个股能力。

---

# 12. 行业配置能力

可使用 Brinson 或类似框架：

\[
ExcessReturn
=
Allocation
+
Selection
+
Interaction
\]

形成：

```text
Manager Industry Skill Matrix
```

例如：

| 能力 | 强 | 中 | 弱 |
|---|---|---|---|
| 科技选股 | ✓ | | |
| 消费选股 | | ✓ | |
| 周期配置 | ✓ | | |
| 金融地产 | | | ✓ |

---

# 13. 择时能力

择时分值较低，采用：

```text
低先验
+
高证明门槛
```

方法可包括：

- Treynor–Mazuy；
- Henriksson–Merton；
- Conditional Beta；
- Holdings-Based Timing；
- 仓位变化与后续市场收益。

择时不能依赖访谈叙事证明。

---

# 14. 风险控制能力

至少评价：

```text
Downside Capture
Maximum Drawdown
Recovery Time
CVaR
Bear-Market Alpha
Tail Loss
```

同时必须控制：

```text
Beta
Low Vol
Quality
Large Cap
```

以区分：

```text
主动风险控制
```

与：

```text
天然低风险风格
```

---

# 15. Trading Skill

高换手经理必须单独评价交易。

可计算：

```text
买入后 1 / 5 / 20 / 60 日超额收益
卖出后 1 / 5 / 20 / 60 日超额收益
成交价格 vs VWAP
Market Impact
交易成本
```

定义：

\[
RealizableSkill
=
GrossSkill
-
TradingCost
\]

---

# 16. Process Score：25%

| 指标 | 分值 |
|---|---:|
| 投资哲学经济逻辑 | 4 |
| Idea → Sell 流程完整性 | 4 |
| 持仓与投资哲学一致性 | 4 |
| 组合构建能力 | 4 |
| 持仓/行业/因子稳定性 | 3 |
| 风险预算与退出机制 | 3 |
| 长期持有与交易纪律 | 3 |
| **合计** | **25** |

核心判断：

\[
StatedProcess
\approx
ObservedBehavior
\]

---

# 17. Style Stability

风格稳定不是要求持仓不变化，而是要求投资逻辑稳定。

可以定义：

\[
StyleStability
=
1-
NormalizedVar(\beta_{style,t})
\]

辅助变量：

- 行业权重迁移；
- 市值暴露变化；
- 估值分位变化；
- 持仓相似度；
- 换手率；
- Active Share。

---

# 18. Verified Skill Score：20%

| 指标 | 分值 |
|---|---:|
| Benchmark-adjusted Excess Return | 3 |
| Multi-Factor Alpha | 4 |
| Alpha Statistical Significance | 3 |
| Holdings-Based Selection Skill | 4 |
| Rolling Persistence | 3 |
| Regime Robustness | 3 |
| **合计** | **20** |

---

# 19. Skill vs Luck

系统必须将“高收益”与“真实能力”区分。

## 19.1 Bootstrap

检验：

```text
历史 Alpha 是否可能由随机噪声产生？
```

## 19.2 False Discovery Rate

在几千只基金中，即使没有任何真实 Alpha，也会出现一批“明星基金”。

因此建议估计：

\[
P(TrueSkill\mid Data)
\]

而不是简单按 Alpha 排名。

## 19.3 Bayesian Shrinkage

\[
\alpha^*
=
w\hat{\alpha}
+
(1-w)\alpha_{prior}
\]

数据越短：

\[
w\downarrow
\]

---

# 20. Rolling Persistence

重点不是“排名重复”，而是“能力重复”。

建议：

```text
Rolling 12M
Rolling 24M
Rolling 36M
Rolling 60M
```

指标：

\[
PositiveAlphaRatio
=
\frac{\#(\alpha_{rolling}>0)}
{\#RollingWindows}
\]

以及：

- Rolling IR > 0；
- Peer top-half ratio；
- Selection Alpha 正值比例；
- 最长连续跑输周期；
- 风格逆风期表现。

---

# 21. Regime Robustness

至少划分：

```text
Bull
Bear
Sideways
Large-Cap
Small-Cap
Value
Growth
High Vol
Low Vol
Rate Up
Rate Down
```

形成：

\[
Skill_{manager,regime}
\]

输出经理环境指纹。

---

# 22. Platform Score：10%

| 指标 | 分值 |
|---|---:|
| 投研团队质量 | 3 |
| 团队稳定性 | 2 |
| 研究覆盖与资源 | 2 |
| 激励与利益一致 | 2 |
| 治理/合规 | 1 |
| **合计** | **10** |

---

# 23. Platform Portability

经理跳槽后，不能假设 Alpha 完整迁移。

定义：

\[
PortabilityScore
=
f(
TeamFollow,
ResearchSupport,
InvestmentAuthority,
RiskSystem,
TradingSystem,
MandateChange
)
\]

跳槽规则：

```text
Career Track Record 保留
Current Platform Confidence 重置
Platform Transfer Penalty 启用
```

---

# 24. Implementation Score：10%

| 指标 | 分值 |
|---|---:|
| Total Cost | 3 |
| AUM / Capacity | 3 |
| Liquidity | 2 |
| Mandate Cleanliness | 1 |
| Share Class / Redemption | 1 |
| **合计** | **10** |

---

# 25. Potential Score：P

潜力分专门回答：

> 这份能力未来还有多少兑现空间？

定义：

\[
P
=
0.25E
+
0.25CH
+
0.15MB
+
0.15FC
+
0.10PT
+
0.10IE
\]

其中：

| 变量 | 权重 |
|---|---:|
| Evidence Accumulation | 25% |
| Capacity Headroom | 25% |
| Manager Bandwidth | 15% |
| Flow & Crowding | 15% |
| Platform Trend | 10% |
| Implementation Edge | 10% |

---

# 26. Evidence Accumulation：25%

不是奖励近期暴涨，而是衡量能力证据是否逐步增强。

可构建：

\[
SkillEvidenceTrend
=
Slope(P(TrueSkill)_t)
\]

例如：

```text
52%
58%
61%
66%
70%
74%
76%
79%
```

为正向信号。

必须同时满足：

```text
Style Stability 未恶化
Holdings Alpha 未恶化
Risk 指标未恶化
非单一行业偶然暴露
```

---

# 27. Capacity Headroom：25%

定义：

\[
CapacityHeadroom
=
1-
\frac{CurrentAUM}
{EstimatedCapacity}
\]

并计算：

\[
DaysToLiquidate_i
=
\frac{Position_i}
{ADV_i\times ParticipationRate}
\]

策略容量估计输入：

- 个股 ADV；
- 持股比例；
- 调仓频率；
- 换手率；
- 组合集中度；
- 市值风格；
- 可接受参与率；
- 市场冲击函数。

最理想潜力特征：

```text
Skill 已验证
+
AUM 尚不大
+
容量较高
+
资金流尚未爆发
```

---

# 28. Manager Bandwidth：15%

定义：

\[
ManagerLoad
=
f(
FundCount,
TotalAUM,
StrategyCount,
StrategyHeterogeneity,
TeamSupport
)
\]

重点监控：

```text
管理基金数量变化
总管理AUM变化
策略跨度变化
共管团队变化
```

如果出现：

```text
3只基金 → 9只基金
50亿 → 400亿
策略越来越杂
```

则 Potential Score 明显下降。

---

# 29. Flow & Crowding：15%

定义：

\[
CrowdingScore
=
f(
RecentNetFlow,
AUMGrowth,
HoldingsCrowding,
OwnershipConcentration,
Liquidity
)
\]

系统偏好：

```text
Skill ↑
+
Flow 尚未爆发
```

系统惩罚：

```text
冠军基金
+
媒体曝光
+
AUM 半年翻倍/数倍
+
策略容量有限
```

---

# 30. Platform Trend：10%

识别未来能力实现环境是否改善。

可跟踪：

- 新增核心研究员；
- 研究覆盖扩张；
- 团队稳定性改善；
- 投资权限改善；
- 风控系统升级；
- 公司治理变化；
- 激励制度改善。

注意：

> Platform Trend 属于弱证据，只能辅助，不能替代已验证投资能力。

---

# 31. Implementation Edge：10%

同一经理/策略下，如果某一基金：

- 费用更低；
- 申赎结构更友好；
- AUM 更合适；
- 投资范围更干净；
- 跟经理核心策略更一致；

则拥有更高 Potential Score。

---

# 32. Confidence Engine

定义：

\[
C
=
0.25C_{Tenure}
+
0.20C_{Cycle}
+
0.20C_{Attribution}
+
0.15C_{Data}
+
0.10C_{Platform}
+
0.10C_{Strategy}
\]

## 32.1 Confidence Components

### Tenure
当前策略历史长度。

### Cycle Coverage
覆盖多少不同市场状态。

### Attribution Clarity
经理业绩归因是否清晰。

### Data Quality
净值、持仓、经理任期等数据完整性。

### Platform Continuity
当前平台是否稳定。

### Strategy Consistency
策略是否可识别且稳定。

## 32.2 建议 Confidence 区间

| 类型 | C |
|---|---:|
| 10年以上、多周期独立管理 | 0.90–1.00 |
| 5年以上稳定经理 | 0.80–0.90 |
| 3—5年 | 0.70–0.85 |
| 18—36个月新锐 | 0.55–0.75 |
| 多人共管职责模糊 | 额外下调 |
| 刚跳槽新平台 | 额外下调 |

---

# 33. Risk Penalty Engine

部分风险不能通过加权平均被掩盖。

定义：

\[
RiskPenalty
=
R_{Style}
+
R_{Capacity}
+
R_{Team}
+
R_{Governance}
+
R_{Flow}
+
R_{SkillDecay}
\]

建议：

| 风险事件 | Penalty |
|---|---:|
| 轻微风格漂移 | -2 |
| 明显风格漂移 | -5 ~ -10 |
| AUM 快速增长且容量有限 | -5 ~ -15 |
| 核心研究团队变化 | -5 |
| Manager Load 大幅上升 | -3 ~ -8 |
| Alpha 来源发生明显变化 | -5 ~ -15 |
| 新平台重置期 | -3 ~ -10 |
| 核心经理离职 | Review / Exclude |
| 严重治理/合规问题 | Exclude |

---

# 34. 优质潜力基金四类候选

## 34.1 已验证、未拥挤型

特征：

```text
Q 高
Skill Evidence 高
AUM 合理
Capacity Headroom 高
Flow 尚未爆发
```

优先级最高。

## 34.2 新锐经理型

特征：

```text
历史较短
+
Holdings Alpha 已出现
+
Process 清晰
+
风格稳定
+
风险控制好
+
Confidence 收缩后仍高分
```

## 34.3 暂时逆风型

特征：

```text
长期 Q 高
+
近期跑输
+
跑输可被风格/环境解释
+
Skill 未恶化
+
Process 未改变
```

需要严格区分：

```text
Style Headwind
```

和：

```text
Skill Decay
```

## 34.4 平台改善型

特征：

```text
已有中高质量经理
+
研究平台/团队明显改善
+
当前策略容量尚可
```

原则上先进入 Watch List，等待能力兑现证据。

---

# 35. 最终分层规则

初版建议：

| 分类 | 条件 |
|---|---|
| Core Buy | `Final ≥ 80` 且 `Q ≥ 80` 且 `C ≥ 0.80` |
| Potential Buy | `Final ≥ 74` 且 `Q ≥ 75` 且 `P ≥ 80` 且 `C ≥ 0.65` |
| Watch List | `Final 68–74` 或 Confidence 偏低 |
| Review | Q 高但出现结构性风险 |
| Exclude | Gate 失败或 `Final < 68` |

潜力研究重点：

\[
Q>75,\quad P>80,\quad 0.65\le C<0.85
\]

这类经理往往并非传统榜单最耀眼，却可能拥有尚未被完全定价的未来 Alpha。

---

# 36. Product Selection Engine

完成经理和策略评分以后，才选择具体基金。

如果同一经理管理多个高度相似产品：

```text
Fund A
Fund B
Fund C
```

先聚类判断是否属于同一策略。

若高度重复，仅保留最优产品：

\[
ProductScore
=
LowestCost
+
BestLiquidity
+
CleanestMandate
+
BestCapacityPosition
+
ManagerCoreStrategyFit
\]

避免重复购买同一 Alpha。

---

# 37. Portfolio De-duplication

高分基金不能直接全部进入组合。

计算：

- Return Correlation；
- Active Return Correlation；
- Factor Correlation；
- Holdings Overlap；
- Sector Overlap；
- Manager Style Overlap。

定义基金的组合边际价值：

\[
MarginalUtility_i
=
ExpectedAlpha_i
-
IncrementalRisk_i
-
Cost_i
\]

最终组合选择的是：

```text
最有边际价值的经理
```

而不是：

```text
单只基金评分最高的前 N 名
```

---

# 38. Research Card

系统最终不能只输出排行榜。

每只候选基金生成：

```text
Fund
Manager
Strategy
Platform

Quality Score
Potential Score
Confidence
Risk Penalty
Final Score

P(True Skill)
P(Selection Skill)
P(Timing Skill)

Expected Alpha
Factor Alpha
Holdings Alpha
Trading Alpha

Style Stability
Capacity Utilization
Capacity Headroom
Manager Load
Flow Crowding

Core Skill
Circle of Competence
Best Regime
Weak Regime

Investment Thesis
Why Good
Why Potential
Key Risk
Invalidation Condition
Sell Trigger
```

最重要的三个字段：

```text
Why Good?
为什么这是优质经理

Why Potential?
为什么未来还有兑现空间

What Breaks the Thesis?
什么情况意味着判断失效
```

---

# 39. 数据架构

## 39.1 fund_master

```text
fund_id
fund_name
fund_type
benchmark_contract
inception_date
company_id
aum
fee_management
fee_custody
fee_sales
status
```

## 39.2 manager_master

```text
manager_id
manager_name
career_start
education
prior_roles
research_background
current_company
current_status
```

## 39.3 manager_fund_history

```text
manager_id
fund_id
start_date
end_date
role
decision_role
co_manager_count
company_id
```

## 39.4 strategy_map

```text
fund_id
date
strategy_id
contract_style
return_style
holding_style
style_confidence
```

## 39.5 nav_daily

```text
date
fund_id
nav
adjusted_nav
return
```

## 39.6 benchmark_daily

```text
date
benchmark_id
return
```

## 39.7 holdings

```text
report_date
fund_id
security_id
weight
sector
industry
market_cap
valuation
liquidity
```

## 39.8 factor_return

```text
date
MKT
SIZE
VALUE
GROWTH
MOM
QUALITY
LOWVOL
...
```

## 39.9 manager_skill_panel

```text
date
manager_id
strategy_id
benchmark_alpha
factor_alpha
holdings_alpha
trading_alpha
selection_skill
allocation_skill
timing_skill
risk_skill
style_stability
skill_probability
```

## 39.10 manager_capacity_panel

```text
date
manager_id
strategy_id
total_aum
strategy_aum
fund_count
strategy_count
avg_position_adv
days_to_liquidate
estimated_capacity
capacity_utilization
manager_load
```

## 39.11 flow_crowding_panel

```text
date
fund_id
net_flow_1m
net_flow_3m
net_flow_6m
aum_growth
holdings_crowding
liquidity_pressure
crowding_score
```

---

# 40. 核心 Feature List

```text
manager_tenure
career_alpha
current_platform_alpha
current_strategy_alpha

benchmark_excess
factor_alpha
alpha_tstat
alpha_bootstrap_p
skill_probability

selection_alpha
industry_allocation_alpha
timing_alpha
trading_alpha
downside_alpha

rolling_alpha_hit_rate
regime_alpha_vector

style_stability
holding_horizon
active_share
tracking_error
turnover
portfolio_concentration

total_aum
strategy_aum
capacity_utilization
capacity_headroom
manager_load

flow_1m
flow_3m
flow_6m
aum_growth
crowding_score

team_stability
platform_continuity
platform_transfer_flag

quality_score
potential_score
confidence_score
risk_penalty
final_score
```

---

# 41. 评分标准化

不能直接对原始值加权。

建议：

```text
Peer Group
↓
Winsorization
↓
Percentile Rank
↓
Direction Adjustment
↓
Score Mapping
```

例如：

\[
Score_{i,k}
=
100\times PercentileRank(x_{i,k})
\]

负向指标：

\[
Score_{negative}
=
100-PercentileRank(x)
\]

---

# 42. 防止双重计分

大量基金指标高度相关。

例如：

```text
Sharpe
Sortino
IR
Alpha
Excess Return
```

不能全部高权重加入。

系统指标应根据经济逻辑分组：

```text
Ability
Risk
Persistence
Behavior
Capacity
Cost
```

组内高度相关变量：

- 聚合；
- PCA；
- 只留代表指标；
- 或设置相关性约束。

---

# 43. Backtest Framework

系统必须使用 Point-in-Time Walk-Forward 回测。

## 43.1 历史时点

例如：

```text
2017-12-31
2018-03-31
2018-06-30
...
```

每个时点只允许使用当时已经公开的数据。

## 43.2 Forward Window

建议同时测试：

```text
6M
12M
24M
36M
```

主目标：

```text
24M / 36M
```

避免模型过度追逐短期行情。

---

# 44. 回测基准

PFS 至少和以下简单策略比较：

```text
Past 1Y Return Rank
Past 3Y Return Rank
Past 3Y Sharpe
Past 3Y Alpha
第三方历史评级（如数据可得）
```

复杂系统如果不能持续优于简单排名，则模型无实际价值。

---

# 45. 回测评价指标

| 指标 | 含义 |
|---|---|
| Top Decile Forward Alpha | 前10%未来 Alpha |
| Top-Bottom Spread | 高低分组收益差 |
| Alpha Hit Rate | 未来 Alpha > 0 比例 |
| Peer Win Rate | 战胜同类中位数比例 |
| Information Ratio | 筛选效率 |
| Downside Capture | 下行控制 |
| Max Drawdown | 风险 |
| Turnover | 模型换手 |
| Capacity | 是否可投资 |
| Calibration | 预测概率与实际成功率是否匹配 |

---

# 46. Calibration

若系统输出：

```text
P(True Skill)=80%
```

则长期样本中，类似样本未来真正表现优异的比例应该接近 80%。

将预测概率分桶：

```text
50–60%
60–70%
70–80%
80–90%
90–100%
```

比较：

```text
Predicted Probability
vs
Observed Success Rate
```

如果严重偏离，则 Skill Probability 需要重新校准。

---

# 47. 数据偏差控制

必须控制：

## Survivorship Bias
历史基金池包含已清盘基金。

## Look-Ahead Bias
历史时点不得使用未来基金经理变动、未来持仓和未来评级。

## Backfill Bias
确认数据当时已经公开。

## Strategy Reclassification Bias
不能用今天的基金分类直接回填历史。

## Manager Attribution Bias
不能把前任业绩归给现任经理。

## Multiple Testing
避免不断尝试指标和权重后只选择最好回测结果。

---

# 48. 模型开发纪律

推荐：

```text
Economic Hypothesis
↓
Feature Definition
↓
Train Period
↓
Validation Period
↓
Out-of-Sample
↓
Live Paper Portfolio
↓
Production
```

严禁：

```text
测试几百套权重
↓
挑最好结果
↓
宣布最优模型
```

必要时使用：

- Bootstrap；
- White Reality Check；
- False Discovery Rate；
- Deflated Sharpe Ratio。

---

# 49. 动态更新频率

| 数据 | 更新频率 |
|---|---|
| NAV / Return | Daily |
| Fund Flow | Daily / Weekly |
| AUM | Monthly / Quarterly |
| Holdings | Quarterly / Semiannual |
| Manager Change | Event Driven |
| Team Change | Event Driven |
| Style Factor | Daily / Monthly |
| Skill Score | Monthly |
| Quality Score | Monthly / Quarterly |
| Potential Score | Monthly |
| Confidence | Monthly / Event |
| Risk Penalty | Event Driven |

---

# 50. Watch Engine

## Manager Events

- 离职；
- 共管变化；
- CIO 变化；
- 研究团队变化；
- 产品数量大幅增加；
- AUM 激增。

## Behavior Events

- 行业暴露突变；
- 风格突变；
- 换手突变；
- 集中度突变；
- Active Risk 下降；
- 持仓市值显著上移。

## Performance Events

- Rolling Alpha 恶化；
- Holdings Alpha 恶化；
- Risk Alpha 恶化；
- Skill Probability 下降。

---

# 51. 卖出规则

不建议：

```text
连续3个月跑输
→ 自动卖出
```

应使用：

```text
Performance Deterioration
+
Behavior Change
+
Skill Evidence Decay
+
Thesis Breakdown
```

优先级最高的卖出信号：

1. 核心经理离职；
2. 决策团队实质改变；
3. 投资流程改变；
4. 能力圈不可解释漂移；
5. 策略容量突破；
6. Manager Load 失控；
7. Alpha 来源消失；
8. 平台明显恶化；
9. 治理/合规风险；
10. 产品实现效率显著下降。

---

# 52. 主动债券分支

主动债券仍以 Manager-First 为核心，但提高平台权重：

| 模块 | 权重 |
|---|---:|
| Manager / Team | 30% |
| Duration / Credit / Leverage Process | 25% |
| Credit Platform & Risk | 20% |
| Verified Skill | 15% |
| Implementation | 10% |

收益拆分：

\[
R
=
Carry
+
Duration
+
Credit
+
Curve
+
Leverage
+
Trading
+
Alpha
-
Cost
\]

---

# 53. ETF / 指数基金分支

ETF 不使用 Manager-First 主模型。

建议：

| 模块 | 权重 |
|---|---:|
| Index Quality | 40% |
| Tracking Quality | 20% |
| Total Cost | 15% |
| Liquidity | 15% |
| AUM Stability | 5% |
| Operational Quality | 5% |

核心：

```text
先选指数
再选基金产品
```

---

# 54. FOF 分支

FOF 经理能力拆解：

\[
FOFSkill
=
AssetAllocation
+
ManagerSelection
+
PortfolioConstruction
+
Rebalancing
\]

需穿透底层：

- Manager overlap；
- Factor overlap；
- Holdings overlap；
- Layered Cost；
- 底层规模风险。

---

# 55. 最终输出

系统最终每天/每月生成：

```text
Core Buy List
Potential Buy List
Watch List
Review List
Exclude List
```

每个候选必须附带 Research Card，而不能只提供数字排名。

---

# 56. PFS 的核心哲学

```text
Quality ≠ Past Return

Potential ≠ Recent Momentum

Manager ≠ Resume

Skill ≠ Alpha Point Estimate

High Active Share ≠ High Skill

Small AUM ≠ Potential

Large AUM ≠ Safety

Short-Term Underperformance ≠ Sell

Fund Ranking ≠ Portfolio Value
```

最终系统评价的是：

\[
FutureInvestableAlpha
\]

而不是过去净值。

---

# 57. 最终公式

\[
PotentialFundValue
=
\left[
SkillProbability
\times
SkillMagnitude
\times
Persistence
\times
Portability
\times
CapacityRetention
\right]
-
TotalCost
-
StructuralRisk
\]

\[
Q
=
35\%Manager
+
25\%Process
+
20\%VerifiedSkill
+
10\%Platform
+
10\%Implementation
\]

\[
P
=
25\%EvidenceAccumulation
+
25\%CapacityHeadroom
+
15\%ManagerBandwidth
+
15\%FlowCrowding
+
10\%PlatformTrend
+
10\%ImplementationEdge
\]

\[
RawScore
=
0.75Q+0.25P
\]

\[
FinalScore
=
50
+
Confidence\times(RawScore-50)
-
RiskPenalty
\]

---

# 58. 一句话总结

> **PFS 不是寻找过去最优秀的基金，而是在 Manager-First 框架下，用投资行为、持仓、收益归因、统计显著性、平台、容量和资金拥挤共同判断：哪些管理人的真实能力已经出现，但尚未被规模、资金流和市场关注完全消耗。**

这就是“优质潜力基金”的系统化定义。
