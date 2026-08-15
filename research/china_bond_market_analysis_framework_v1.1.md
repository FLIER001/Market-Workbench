# 中国债券市场分析框架：技术文档

> 版本：v1.1  
> 适用范围：中国利率债、地方政府债、政策性金融债、信用债及相关利率衍生品  
> 目标：建立一套适用于中国市场、可用于研究、估值、策略和交易决策的统一债券分析框架。  
> 方法：以顶级学术文献提供定价原理，以中国制度、市场结构和机构行为做本土化修正。

---

## 1. 核心定价框架

中国债市分析不应停留在“基本面、政策面、资金面、供需面”并列打分，而应围绕两个核心定价对象展开：

\[
y_t^{(n)}
=
E_t\left(\bar r_{t,t+n}\right)
+
TP_t^{(n)}
\]

其中：

- \(y_t^{(n)}\)：\(n\) 年期无风险债券收益率；
- \(E_t(\bar r_{t,t+n})\)：未来短端利率路径的市场预期；
- \(TP_t^{(n)}\)：期限溢价。

对应两条主链：

```text
宏观 / 地产 / 信用
        ↓
央行反应函数
        ↓
政策利率与银行间资金价格
        ↓
未来短端利率预期
        ↓
        ├──────────────┐
        │              │
        │          国债收益率曲线
        │              │
        └──────────────┤
                       ↑
政府债久期供给
+ 银行/保险/基金资产负债表
+ 融资条件
+ 市场微观结构
        ↓
     期限溢价
```

信用债在无风险曲线之上进一步定价：

\[
CreditSpread
=
ExpectedLoss
+
RiskPremium
+
LiquidityPremium
-
ImplicitSupport
\]

因此，完整框架可以压缩为三层：

```text
第一层：宏观 + 央行
        → 未来短端利率

第二层：供给 + 机构 + 资金 + 市场结构
        → 期限溢价

第三层：基本面 + 风险偏好 + 流动性 + 隐性支持
        → 信用利差
```

这三层分别回答：

1. 无风险利率中枢在哪里；
2. 当前曲线为什么偏离宏观公平值；
3. 信用资产相对无风险曲线应提供多少补偿。

---

## 2. 中国市场的制度修正

国际债券定价框架可以解释“为什么利率会动”，但直接套用到中国市场会遗漏四个关键制度变量。

### 2.1 货币政策正在转向价格型框架

中国短端研究应围绕：

\[
7D\ Repo
\rightarrow
DR007
\rightarrow
NCD/IRS
\rightarrow
1Y-5Y\ Bond
\]

而不是继续把 MLF、信贷额度和传统数量工具视为同等权重的政策锚。

研究重点是：

- 主要政策利率；
- 利率走廊；
- 银行间市场利率相对政策利率的偏离；
- 银行负债成本；
- 市场对未来短率路径的定价。

### 2.2 银行间市场具有核心定价地位

中国债券资产主要在银行间市场交易，资金获取能力、交易对手结构和抵押品属性都会进入资产价格。

因此：

> repo 不是辅助技术指标，而是债券定价变量。

### 2.3 市场仍存在分割

银行间与交易所市场、境内与境外人民币市场之间，并非始终存在充分套利。

结果是：

- 相似资产可能长期存在价差；
- 政策冲击可能只传导到局部市场；
- 抵押品资格、交易准入和投资者约束可以单独影响估值。

### 2.4 政府信用存在显性与隐性两层

中国信用债尤其是城投债不能只以企业违约概率定价。

需要显式考虑：

\[
ObservedSpread
=
FundamentalRisk
+
Liquidity
+
RiskPremium
-
SupportValue
\]

其中 \(SupportValue\) 可能来自：

- 地方政府支持预期；
- 再融资便利；
- 特殊置换或化债政策；
- 平台在区域金融体系中的重要性。

---

## 3. 无风险利率：从宏观到未来短率

### 3.1 宏观核心是名义增长与融资需求

债券定价对应的是名义增长环境：

\[
NominalGrowth
\approx
RealGrowth + Inflation
\]

但中国还需要把地产和信用独立拆出，因为二者同时影响：

- 银行资产扩张；
- 居民与企业融资需求；
- 地方财政；
- 土地财政；
- 通胀和风险偏好。

建议将宏观状态压缩为四个变量：

| 状态变量 | 重点指标 | 债市含义 |
|---|---|---|
| 增长 | PMI、新订单、工业增加值、出口、生产高频 | 决定真实利率与政策约束 |
| 通胀 | CPI、核心 CPI、PPI、GDP 平减指数、大宗商品 | 决定名义利率中枢 |
| 地产 | 销售、房价、开工、土地成交、房企融资 | 影响信用需求、地方财政、银行资产扩张 |
| 信用 | 社融结构、信贷结构、企业中长贷、居民贷款 | 衡量融资需求与信用周期 |

可构建：

\[
MacroScore_t
=
w_g z(Growth_t)
+
w_\pi z(Inflation_t)
+
w_h z(Housing_t)
+
w_c z(Credit_t)
\]

权重不建议固定，应通过：

- 滚动回归；
- 状态空间模型；
- Markov Regime Switching；
- 历史样本的预测能力；

动态估计。

### 3.2 宏观不能直接用于日内择时

宏观层主要回答：

- 当前 Regime 是复苏、滞胀、衰退还是低通胀；
- 中期政策利率约束往哪个方向移动；
- 1—3个月和6—12个月的利率中枢是否改变。

不建议把单月宏观数据直接映射为交易信号。

---

## 4. 央行反应函数与资金面

### 4.1 研究目标

宏观变化通过央行反应函数进入市场：

```text
增长 / 通胀 / 地产 / 信用
            ↓
      央行政策目标
            ↓
       政策利率
            ↓
      DR007 / 银行负债成本
            ↓
       IRS / NCD
            ↓
       1Y—5Y 国债
```

核心并不是判断“经济好不好”，而是估计：

\[
E_t(r_{t+1}), E_t(r_{t+2}), \dots
\]

即未来短端利率路径。

### 4.2 核心指标

| 模块 | 指标 |
|---|---|
| 政策利率 | 7D 逆回购、MLF、LPR、降准/降息 |
| 银行间资金 | DR001、DR007、R001、R007 |
| 银行负债 | 1Y AAA NCD、同业存款、存款利率 |
| 利率预期 | FR007 IRS、Repo IRS、远期利率 |
| 流动性数量 | OMO 净投放、财政存款、缴税、现金回笼、银行净融出 |

重点构造：

\[
FundingStress_t = R007_t - DR007_t
\]

衡量非银相对银行融资压力；

\[
BankFundingPremium_t
=
NCD_{1Y,t} - PolicyRate_t
\]

衡量银行负债压力。

### 4.3 资金面不能只看价格

中国资金环境应写成：

\[
FundingCondition
=
Price
+
Quantity
+
Collateral
+
Counterparty
\]

即同时考虑：

- repo 利率；
- 融资量；
- 可质押资产；
- 折扣率；
- 交易对手风险；
- 非银是否能够顺利滚动杠杆。

---

## 5. 收益率曲线：先描述，再解释

### 5.1 PCA

收益率曲线主要可压缩为：

- Level；
- Slope；
- Curvature。

用途：

- 监控主要曲线风险；
- 判断哪一类因子主导市场；
- 构建曲线交易；
- 分解 P&L。

### 5.2 Dynamic Nelson-Siegel

\[
y_t(\tau)
=
L_t
+
S_t
\frac{1-e^{-\lambda\tau}}{\lambda\tau}
+
C_t
\left(
\frac{1-e^{-\lambda\tau}}{\lambda\tau}
-
e^{-\lambda\tau}
\right)
\]

其中：

- \(L_t\)：长期水平；
- \(S_t\)：斜率；
- \(C_t\)：中段曲率。

DNS/PCA 只能描述**怎么变**，不能解释**为什么变**。

因此二者只能作为第一层曲线状态模型。

---

## 6. 期限溢价：长端利率的关键分解

### 6.1 基本分解

\[
10Y_t
=
ExpectedShortRate_t
+
TermPremium_t
\]

长债收益率下降可能有两种完全不同的原因：

1. 市场降低未来政策利率预期；
2. 期限溢价因供需、配置、避险或风险承载能力下降而压缩。

这两个过程对交易含义不同。

### 6.2 顶刊文献提供的核心结论

**Cochrane–Piazzesi：**

债券预期超额收益具有显著时变性。

含义：

> 不能把长期收益率理解为未来短率平均值加一个固定常数。

**Adrian–Crump–Moench：**

可以用线性回归构建实用的无套利期限结构模型，将收益率分解为：

- expected short rate；
- term premium。

含义：

> 长债交易应显式区分“政策预期”和“期限溢价”。

**Ang–Piazzesi / Rudebusch–Wu：**

通胀、真实活动和货币政策与收益率曲线因子存在稳定的宏观金融联系。

含义：

> 曲线因子不是纯统计变量，应映射到宏观和政策状态。

### 6.3 模型实施

建议使用 ACM/Affine Term Structure Model 输出：

- 2Y/5Y/10Y/30Y expected short rate component；
- term premium；
- term premium 历史分位；
- 收益率变动的预期成分贡献；
- 收益率变动的风险溢价成分贡献。

核心研究问题：

> 当前 10Y/30Y 的低收益率，到底是政策预期低，还是期限溢价异常低？

---

## 7. 中国式期限溢价：供给、机构与久期

### 7.1 Preferred Habitat 是核心理论

Vayanos–Vila 的核心思想是：

不同投资者有不同期限偏好，套利者风险承载能力有限，因此局部供需能够长期影响收益率。

Greenwood–Vayanos 进一步说明：

> 政府债务的期限结构和久期供给会影响债券收益率及未来超额收益。

这对中国尤其重要，因为：

- 银行偏好中短久期配置；
- 保险天然需要长久期与超长久期资产；
- 公募和券商交易盘更受净值、排名和融资约束；
- 外资受汇率与套保成本影响；
- 各机构的资产负债表约束明显不同。

### 7.2 不要只看发行额，要看净久期供给

定义：

\[
NetDurationSupply_t
=
\sum_i NetIssuance_{i,t}
\times DV01_{i,t}
\]

进一步：

\[
EffectiveDurationSupply_t
=
NetDurationSupply_t
-
StructuralDemand_t
\]

这比单纯统计“本月发行3万亿”更有意义。

例如：

- 3万亿2年债；
- 3万亿30年债；

对市场需要承接的利率风险完全不同。

### 7.3 投资者行为框架

| 投资者 | 核心约束 | 对曲线影响 |
|---|---|---|
| 银行 | 存款、信贷需求、资本占用、负债成本 | 1Y—10Y |
| 保险 | 保费、负债久期、ALM 缺口 | 10Y—30Y |
| 公募 | 申赎、排名、久期、净值波动 | 全曲线，偏交易 |
| 理财 | 规模、净值稳定、流动性需求 | 中短端与信用 |
| 券商/交易盘 | repo、杠杆、carry、basis | 曲线与期现 |
| 外资 | 中美利差、汇率、套保成本、指数资金 | 国债/政金债 |

长期利率公平价值应写成：

\[
FairYield
=
ExpectedShortRate
+
EquilibriumTermPremium
\]

而非“历史分位越低越贵”。

---

## 8. 中国市场微观结构：宏观之外的独立定价因子

中国市场的实证研究提供了几类重要证据。

### 8.1 抵押品渠道

针对中国双重上市债券的准自然实验显示：

当一类债券获得央行工具抵押品资格后，其二级市场信用利差显著下降；相关研究估计幅度约为 **42—62bp**，并进一步传导到一级市场，新发债利差下降约 **54bp**。

结论：

> 一只债“能不能拿去融资”，本身就是估值变量。

因此模型需要加入：

- 抵押资格；
- repo haircuts；
- 质押便利度；
- 可融资规模；
- 交易对手接受度。

### 8.2 市场分割

中国银行间市场和交易所市场长期具有：

- 投资者结构不同；
- 监管体系不同；
- 抵押融资机制不同；
- 套利通道有限。

结论：

> 同一信用主体、相似现金流资产出现价差，不一定意味着无风险套利。

### 8.3 境内外人民币曲线

早期中国境内与离岸人民币国债研究发现：

- 境内曲线更受政策利率和货币条件驱动；
- 离岸曲线更受市场预期、汇率和流动性约束影响；
- 两者之间的价格传导并不充分。

这一结论提示：

> 中国债券研究必须区分政策定价和市场定价的权重。

### 8.4 需要监控的市场结构指标

\[
MarketStructure_t
=
Funding
+
Collateral
+
Liquidity
+
Leverage
+
Segmentation
\]

核心数据包括：

- R007-DR007；
- repo 成交；
- 回购杠杆；
- 质押券结构；
- CTD；
- 国债期货基差；
- 隐含回购利率 IRR；
- 银行间/交易所价差；
- 成交量；
- 换手率；
- 做市深度；
- 外资持仓；
- 人民币汇率；
- FX hedge cost。

---

## 9. 信用债：无风险曲线之上的第二层定价

### 9.1 统一信用利差框架

\[
Spread_t
=
EL_t
+
EBP_t
+
LP_t
-
IS_t
\]

其中：

- \(EL\)：Expected Loss；
- \(EBP\)：Excess Bond Premium；
- \(LP\)：Liquidity Premium；
- \(IS\)：Implicit Support。

### 9.2 Excess Bond Premium

Gilchrist–Zakrajšek 的核心贡献是：

信用利差中除了预期违约损失，还包含一个与金融体系风险承载能力相关的超额信用风险溢价。

因此信用债利差收窄不一定代表：

> 企业基本面改善。

也可能是：

> 市场风险偏好提升、配置需求增强、风险资本更加宽松。

### 9.3 产业债

研究链：

```text
盈利能力
→ 自由现金流
→ 杠杆与偿债能力
→ 融资可得性
→ 违约概率 / 回收率
→ 信用利差
```

重点指标：

- EBITDA / Interest；
- FCF；
- Net Debt / EBITDA；
- 短债占比；
- 债券到期墙；
- 融资成本；
- 一级发行结果；
- 行业景气；
- 二级流动性。

### 9.4 城投债

城投核心不是“企业价值”，而是：

\[
LGFVCredit
=
FiscalCapacity
+
PlatformImportance
+
DebtStructure
+
Refinancing
+
PolicySupport
\]

重点：

| 维度 | 指标 |
|---|---|
| 财政能力 | 一般公共预算收入、税收、转移支付 |
| 土地财政 | 政府性基金收入、土地成交 |
| 债务压力 | 地方政府债、区域城投有息负债、债务率 |
| 平台质量 | 股东层级、平台定位、资产质量、现金流 |
| 再融资 | 净融资、银行授信、非标、到期墙 |
| 政策支持 | 化债、置换、特殊再融资、地方支持行为 |

中国城投研究表明，LGFV 相对市场化企业存在与隐性政府支持相符的利差折价，且政策变化会改变这种折价。

因此评级只能作为输入，最终判断应是：

\[
NetCreditRisk
=
FundamentalDefaultRisk
-
SupportValue
\]

---

## 10. 三种投资周期对应三套权重

不建议固定使用：

```text
基本面 30%
政策面 20%
资金面 20%
供需 20%
情绪 10%
```

不同投资周期的主导因子不同。

### 10.1 1—4周：交易视角

主要权重：

- DR007 / R007；
- NCD；
- repo 杠杆；
- 机构申赎；
- 国债发行节奏；
- carry & roll；
- 期货 basis；
- 拥挤度。

宏观数据主要作为 surprise factor。

### 10.2 1—3个月：波段视角

主要权重：

- 央行反应函数；
- 信用周期；
- 地产；
- 财政发行；
- term premium；
- 配置力量。

### 10.3 6—12个月：配置视角

主要权重：

- 名义增长趋势；
- 通胀 regime；
- 潜在政策利率；
- 财政与政府债供给；
- 银行/保险长期配置需求；
- 期限溢价中枢。

因此：

> 权重必须 Regime-dependent，而不是固定分配。

---

## 11. 研究系统的模型栈

### Layer 1：收益率曲线状态模型

输入：

- 中债国债零息曲线；
- 政金债；
- 地方债；
- IRS；
- NCD。

模型：

- PCA；
- DNS；
- Forward Curve；
- Carry & Roll。

输出：

- Level / Slope / Curvature；
- 关键期限相对价值；
- 曲线异常点。

### Layer 2：宏观—政策—短率模型

输入：

- 增长；
- 通胀；
- 地产；
- 信用；
- 政策利率；
- DR007；
- NCD；
- IRS。

模型：

- Taylor-style reaction function；
- VAR / BVAR；
- Local Projection；
- State Space Model。

输出：

- 政策利率路径；
- 资金利率路径；
- 2Y / 5Y 公平价值。

### Layer 3：期限溢价与供需模型

输入：

- 全期限收益率；
- 政府债净发行；
- DV01；
- 机构持仓与流量；
- repo；
- 波动率；
- 风险偏好。

模型：

- ACM；
- Affine Term Structure Model；
- Preferred Habitat / Demand Factor Model；
- Fair Value Regression。

输出：

- Term Premium；
- 10Y / 30Y 公平价值；
- 久期供给冲击；
- 长端估值偏离。

### Layer 4：信用利差模型

输入：

- 财务数据；
- 区域财政；
- 流动性；
- 一级发行；
- 二级成交；
- 政策支持。

输出：

- Expected Loss；
- Excess Bond Premium；
- Liquidity Premium；
- Implicit Support；
- Fair Spread。

---

## 12. 数据源设计

### 12.1 官方与基础数据

| 数据源 | 主要用途 |
|---|---|
| 中国人民银行 PBOC | 政策利率、OMO、货币政策框架、金融统计 |
| 中债估值中心 / ChinaBond | 国债、政金债、信用债、零息曲线、估值 |
| CFETS | 银行间利率、外汇、IRS、交易数据 |
| 财政部 | 国债、地方债发行与财政数据 |
| 国家统计局 | 增长、通胀、地产、工业 |
| 外汇局 | 跨境流动与外资相关数据 |
| 中金所 | 国债期货、CTD、合约与交割信息 |

### 12.2 国际与跨市场数据

| 数据源 | 用途 |
|---|---|
| ADB AsianBondsOnline | 中国债券存量、期限结构、投资者结构、外资持仓 |
| IMF | 中国宏观、地产、财政与金融稳定框架 |
| BIS | 全球利率、跨境银行与金融条件 |
| FRED / US Treasury | 美债利率及全球定价锚 |

### 12.3 数据频率

建议统一划分：

```text
Tick / Intraday
→ repo、国债期货、IRS、现券

Daily
→ 收益率曲线、NCD、成交、机构流量代理

Weekly
→ 发行、到期、央行操作、基金规模

Monthly
→ 宏观、社融、银行资产负债、保险配置

Quarterly
→ 财政、企业财务、区域信用、机构资产负债表
```

所有模型必须保留：

- release date；
- observation date；
- revision flag；

避免 look-ahead bias。

---

## 13. 核心指标体系

建议 Dashboard 控制在 50—80 个高信息密度变量。

### 宏观与政策

- PMI / 新订单
- 工业增加值
- 出口
- CPI / Core CPI / PPI
- GDP Deflator
- 地产销售 / 房价 / 土地成交
- 社融 / 信贷结构
- 7D 逆回购
- MLF / LPR
- RRR / policy events

### 资金与银行负债

- DR001 / DR007
- R001 / R007
- R007 - DR007
- OMO 净投放
- 银行净融出
- 1Y AAA NCD
- NCD - Policy Rate
- FR007 IRS

### 曲线与估值

- 1Y / 2Y / 5Y / 10Y / 30Y 国债
- 10Y-1Y
- 10Y-2Y
- 30Y-10Y
- PCA Level / Slope / Curvature
- DNS Factors
- 1Y1Y / 2Y1Y / 5Y5Y Forward
- ACM Term Premium
- Carry & Roll

### 供需与机构

- 国债净发行
- 地方债净发行
- 政金债净发行
- Net Duration Supply
- 银行持债变化
- 保险持债变化
- 基金规模 / 净申购
- 理财规模
- 外资持仓

### 市场微观结构

- repo 杠杆
- 国债期货基差
- CTD
- IRR
- 现券成交量
- 换手率
- 流动性利差
- 银行间 / 交易所价差

### 信用

- AAA / AA+ / AA 利差
- 城投区域利差
- 产业行业利差
- 一级发行利率
- 取消发行
- 净融资
- 到期墙
- 信用事件
- 政策支持事件

---

## 14. 日常研究与交易决策流程

每日研究只回答五个问题：

1. **宏观 Regime 是否变化？**
2. **央行反应函数或资金价格是否偏离预期？**
3. **收益率变化来自短率预期还是期限溢价？**
4. **边际买卖力量和净久期供给是什么？**
5. **当前价格相对公平值已 Price-in 多少？**

最终输出：

```yaml
market_view:
  direction: bullish | neutral | bearish
  horizon: 1w | 1m | 3m | 12m

rates:
  duration: long | neutral | short
  preferred_tenor: 2Y | 5Y | 10Y | 30Y
  curve: steepener | flattener | neutral
  butterfly: optional

credit:
  sector: optional
  rating: optional
  spread_view: tighter | neutral | wider

valuation:
  expected_short_rate_gap_bp:
  term_premium_gap_bp:
  carry_roll_bp:
  fair_value_gap_bp:

positioning:
  conviction: 1-5
  suggested_risk_budget:
  stop_condition:
  invalidation_signal:
```

标准研究观点必须区分：

- 方向；
- 久期；
- 曲线；
- 品种；
- carry；
- 仓位；
- 失效条件。

---

## 15. 交易表达

同一个宏观观点可以有不同交易表达。

### 15.1 Outright Duration

适用于：

- expected short rate 明显偏离；
- term premium 同方向；
- carry 可接受。

### 15.2 Curve

适用于：

- 政策影响短端；
- 供需影响长端；
- 不希望承担过多 level risk。

常见表达：

- 2s10s steepener / flattener；
- 5s10s；
- 10s30s。

### 15.3 Butterfly

适用于：

- 曲率出现异常；
- 某一关键期限受供需冲击；
- DNS/PCA residual 显著。

### 15.4 Futures / Basis

适用于：

- 期现偏离；
- CTD 切换；
- repo 与隐含回购率错配；
- 套利资本约束造成 basis 异常。

### 15.5 Credit Spread

适用于：

- 无风险利率观点与信用风险观点分离；
- EBP、流动性与基本面出现错位。

---

## 16. 关键文献与模型映射

| 文献 | 核心结论 | 在本框架中的用途 |
|---|---|---|
| Ang & Piazzesi | 宏观变量与期限结构因子共同决定收益率 | 宏观—曲线模型 |
| Rudebusch & Wu | 收益率曲线因子具有货币政策与宏观基础 | 央行反应函数 |
| Cochrane & Piazzesi | 债券风险溢价显著时变 | 不把 TP 当常数 |
| Adrian, Crump & Moench | 可实用估计 expected short rates 与 TP | ACM term premium |
| Vayanos & Vila | 偏好栖息地 + 有限套利决定局部收益率 | 机构需求模型 |
| Greenwood & Vayanos | 政府债久期供给影响收益率和超额收益 | Net Duration Supply |
| Gilchrist & Zakrajšek | 信用利差含 Excess Bond Premium | 信用风险偏好分解 |
| 中国抵押品渠道研究 | 抵押资格可显著压低信用利差 | Collateral factor |
| 中国双市场研究 | 银行间/交易所存在分割 | Segmentation factor |
| 中国境内外曲线研究 | 政策与市场因素权重不同 | Onshore/offshore 分解 |
| LGFV 隐性担保研究 | 城投利差含政府支持价值 | Implicit Support |

---

## 17. 最小可行研究系统

如果从零落地，优先级建议：

### Phase 1：曲线与资金

先实现：

- 中债零息曲线；
- PCA / DNS；
- DR007 / R007；
- NCD；
- IRS；
- carry & roll。

目标：

> 解释日常 1Y—10Y 利率变化。

### Phase 2：宏观与期限溢价

加入：

- 宏观状态；
- 央行反应函数；
- ACM；
- forward curve。

目标：

> 区分 expected short rates 与 term premium。

### Phase 3：供需与机构

加入：

- 国债/地方债/政金债 DV01；
- 银行、保险、公募、理财持仓与流量；
- repo leverage；
- 国债期货 basis。

目标：

> 解释 10Y/30Y 与模型公平值偏离。

### Phase 4：信用

加入：

- 财务；
- 区域财政；
- LGFV 支持函数；
- 一级发行；
- 流动性；
- EBP。

目标：

> 形成利率 + 信用统一配置框架。

---

## 18. 结论

中国债市研究的核心不是建立更多分类，而是建立明确的因果顺序：

\[
\boxed{
Macro
\rightarrow
CentralBank
\rightarrow
ShortRateExpectations
}
\]

\[
\boxed{
Supply
+
InstitutionalBalanceSheet
+
Funding
+
MarketStructure
\rightarrow
TermPremium
}
\]

两者决定无风险曲线：

\[
\boxed{
Yield
=
ExpectedShortRates
+
TermPremium
}
\]

然后：

\[
\boxed{
CreditSpread
=
ExpectedLoss
+
RiskPremium
+
Liquidity
-
ImplicitSupport
}
\]

成熟的研究系统最终不输出“看多/看空债市”这一类单点判断，而应输出：

\[
\boxed{
方向
\times
久期
\times
曲线
\times
品种
\times
Carry
\times
仓位
\times
失效条件
}
\]

研究的目标不是解释历史，而是形成一套可以持续更新、可以被数据证伪、可以直接映射到交易和风险预算的债券定价系统。
