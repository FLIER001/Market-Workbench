# A股优质个股中短期择时与交易信号系统 V2.0

**文档类型：** Quantitative Trading Specification  
**适用市场：** 沪深A股  
**适用股票池：** 已通过基本面、财务质量、估值/成长合理性、重大风险筛选的优质个股池  
**交易方向：** Long Only  
**目标持有周期：** 约5—40个交易日，不设置机械最大持有期  
**信号频率：** 日频，收盘后计算  
**输出信号：** `BUY / ADD / HOLD / REDUCE / SELL / WAIT`  
**核心目标：** 判断“现在是否值得买、已有仓位是否值得继续持有、什么时候降低或退出风险”，而不是预测下一交易日涨跌。

---

# 1. 结论：择时系统需要从“单点触发”改成“状态机”

V1的核心结构是：

```text
20日均线启动
→ 50日新高确认
→ 5/20日成交量确认
→ 1%价格过滤
→ 20日均线跌破退出
```

这一框架存在结构性问题。

第一，20日均线、50日突破和1%过滤带都属于**预先人为指定的技术参数**。现有研究并不能证明“20日+50日+1%”是A股个股中短期交易的稳定最优组合。2015年的中国指数技术规则研究甚至发现，简单MA/TRB规则在加入交易成本后，利润可以被完全消除；2024年对上证综指、创业板共38,456条规则的大规模检验进一步发现，在校正数据挖掘、进行样本外检验并考虑交易成本后，真正能够持续胜出的技术规则非常少。

第二，文献本身并非一致否定技术分析。Jiang、Tong、Song对超过28,000个中国市场技术信号使用Step-SPA检验，发现部分技术规则确有择时价值；JFQA的研究也发现移动平均信号在部分高不确定性资产组合上有显著经济价值。正确结论因此不是“技术分析无效”，而是：

> **技术信号可以提供信息，但不能把某一个参数化技术规则直接当作最终交易策略。**



第三，这一点也与国内机构近年的实践一致。华泰2025年的A股择时体系明确指出单指标存在噪声和样本内外不稳定问题，转向估值、资金、技术、情绪等多维度分层合成；招商2026年的四维择时框架进一步采用动态宏观、非线性情绪、估值回归和价量趋势；国盛的择时体系长期同时观察流动性、经济、估值、资金、技术和情绪/拥挤度，而非依赖一根均线。

因此，本系统改为：

```text
市场状态
    ↓
行业状态
    ↓
个股趋势资格
    ↓
相对强弱 + 价量结构 + 趋势质量
    ↓
突破买点 / 回踩再启动买点
    ↓
波动率风险定仓
    ↓
趋势确认后加仓
    ↓
风险优先的分级退出
```

也即：

> **Regime Filter → Setup → Trigger → Position → Risk Exit**

而不是：

> **某条均线被突破 → 买入。**

---

# 2. 研究证据如何转化为策略设计

## 2.1 不直接采用短周期“过去涨得多就继续买”

A股短期动量证据明显弱于美国市场。

2023年Pacific-Basin Finance Journal直接研究中国短期动量，结论是：中国市场不存在其检验定义下的短期动量，反而存在显著短期反转；另一项中国研究也发现传统动量构造并不显著，而动量表现明显依赖市场状态。

中国市场更值得利用的并不是裸价格动量，而包括：

- **残差动量/相对强弱**：剥离公共因子后的股票自身强弱；
- **价格与成交量联合趋势**；
- **市场状态条件下的趋势持续性**；
- **行业、市场相对趋势，而非单只股票绝对涨幅。**

中国A股研究发现，残差动量相对于传统原始收益动量具有更强的横截面解释能力；2024年Review of Asset Pricing Studies的《Trend Factor in China》进一步发现，中国市场中的趋势因子同时需要价格与成交量信息，而且成交量在中国比美国具有更重要的作用。

因此V2不使用：

```text
过去N天涨幅越大 → 越值得买
```

而使用：

```text
绝对趋势成立
AND
相对行业仍强
AND
相对市场仍强
AND
资金/成交结构支持
AND
趋势不是单日跳涨造成
```

---

## 2.2 成交量不是“5日均量 > 20日均量”这么简单

原策略把：

```text
MA5(volume) > MA20(volume)
```

视为多头确认。

问题在于，放量既可能意味着新增买盘，也可能意味着高位筹码剧烈交换甚至趋势终结。

中金的价量因子研究也强调，价量信息及时，但换手较高、容易受投资者结构与交易制度变化影响；资金流因子与动量、波动率、流动性因子的相关性较低，适合作为增量信号而非单独使用。

因此改为**方向化成交额**：

\[
SV20_t=
\frac{
\sum_{i=t-19}^{t} sign(r_i)\cdot Amount_i
}{
\sum_{i=t-19}^{t} Amount_i
}
\]

其中：

- 上涨日成交额记正；
- 下跌日成交额记负；
- `SV20 > 0` 表示近20日成交资金总体更多发生在上涨阶段；
- `SV20 < 0` 表示成交更多集中在下跌阶段。

它不是文献Trend Factor的完整复制，而是为日频交易系统设计的**可实现价量代理变量**。

---

## 2.3 区分“平滑上涨”与“一天跳涨”

RFS的Frog-in-the-Pan研究发现，相同累计收益下，由连续、小幅信息推动的价格趋势，其后续持续性显著强于由少数剧烈价格跳跃形成的趋势。

因此增加20日趋势效率：

\[
ER20_t=
\frac{C_t-C_{t-20}}
{\sum_{i=t-19}^{t}|C_i-C_{i-1}|}
\]

范围约为：

\[
-1\le ER20\le 1
\]

解释：

```text
ER20 → 1    平滑持续上涨
ER20 → 0    大量震荡、缺少方向
ER20 < 0    趋势偏弱
```

这比“价格站上均线”多回答了一个重要问题：

> **它是稳步涨上来的，还是剧烈震荡后偶然站上来的？**

---

## 2.4 波动率用于控制风险，不直接预测涨跌

动量策略具有明显状态依赖和尾部风险。JFE关于Momentum Crashes的研究发现，动量在极端市场状态可能出现严重连续亏损；Barroso与Santa-Clara进一步表明，根据波动率动态管理风险能够明显改善传统动量的风险效率。

因此本系统不采用：

```text
高波动 → 一定不能买
```

而采用：

```text
高波动
→ 止损距离扩大
→ 单位股数降低
→ 总风险保持稳定
```

即**波动率主要负责仓位和止损尺度，而非承担方向预测。**

---

# 3. 系统变量定义

设交易日为 \(t\)。

| 变量 | 定义 |
|---|---|
| \(C_t,O_t,H_t,L_t\) | 收盘、开盘、最高、最低价 |
| EMA20 | 20日指数移动平均 |
| EMA60 | 60日指数移动平均 |
| ATR20 | 20日平均真实波幅 |
| HC20 | 前20个交易日最高收盘价 |
| R20 | 20日累计收益 |
| R60 | 60日累计收益 |
| RS20_IND | 个股20日收益 − 行业20日收益 |
| RS60_MKT | 个股60日收益 − 市场60日收益 |
| ER20 | 20日趋势效率 |
| SV20 | 20日方向化成交额 |
| AmountRatio | 当日成交额 / 20日成交额中位数 |
| EXT | \((C-EMA20)/ATR20\) |
| CLV | \((C-L)/(H-L)\) |
| Breadth20 | 股票池中收盘价高于EMA20的股票比例 |

其中：

\[
CLV=\frac{C-L}{H-L}
\]

当：

```text
CLV ≈ 1    收盘接近全天最高
CLV ≈ 0    收盘接近全天最低
```

---

# 4. 第一层：市场与行业状态过滤

不允许个股技术图形完全脱离市场环境独立决策。

定义环境得分：

\[
E_t=E_{MKT}+E_{Breadth}+E_{IND}+E_{IRS}
\]

每项取0或1，总分0—4。

### 市场趋势

```text
市场指数 Close > EMA20
AND
EMA20 > EMA60
```

成立：

```text
E_MKT = 1
```

### 市场宽度

```text
Breadth20 > 50%
AND
Breadth20_t > Breadth20_{t-5}
```

成立：

```text
E_Breadth = 1
```

这样可以区分：

```text
指数上涨 + 大多数股票上涨
```

与：

```text
仅少量权重股拉升指数
```

### 行业趋势

所属行业指数：

```text
Close > EMA20 > EMA60
```

成立：

```text
E_IND = 1
```

### 行业相对强弱

```text
R20_industry > R20_market
```

成立：

```text
E_IRS = 1
```

最终环境分级：

| E | 市场状态 | 新开仓 |
|---:|---|---|
| 4 | Strong Risk-On | 正常 |
| 3 | Risk-On | 正常 |
| 2 | Neutral | 只交易高质量信号，降低仓位 |
| 0—1 | Risk-Off | 原则上禁止新开仓 |

这并不是判断“大盘明天涨还是跌”。

其作用是：

> **同一个个股突破信号，在健康市场和系统性弱市中应当具有不同权重。**

这与近年来华泰、招商、国盛机构择时体系普遍采用多维环境判断的方向一致。

---

# 5. 第二层：个股必须先成为“可交易状态”

任何BUY信号产生之前，个股必须满足以下核心资格。

## 5.1 趋势结构

```text
C > EMA20 > EMA60
```

且：

```text
EMA20_t > EMA20_{t-5}
```

表示短中期趋势已经转入上行，而不是仅在一天内穿越均线。

---

## 5.2 相对强弱

必须满足：

```text
RS20_IND > 0
```

即过去20日跑赢所属行业。

同时优先：

```text
RS60_MKT > 0
```

即过去60日跑赢市场。

这样做是为了减少把：

```text
市场整体暴涨带来的被动上涨
```

错误识别成：

```text
股票自身正在形成独立趋势
```

尤其考虑到中国市场传统短期价格动量证据较弱，而残差/条件动量证据更有价值，因此相对强弱应比裸收益率具有更高优先级。

---

## 5.3 趋势路径质量

默认要求：

```text
ER20 >= 0.20
```

并进行股票池横截面排序。

优先交易：

```text
ER20 Percentile >= 50%
```

避免：

- 上下剧烈震荡；
- 一天大涨改变整个均线结构；
- 新闻刺激形成的脉冲式行情。

---

## 5.4 价格位置不过度延伸

定义：

\[
EXT=\frac{C-EMA20}{ATR20}
\]

默认：

```text
EXT <= 2.0
```

即使趋势很好：

```text
EXT > 2ATR
```

也不立即BUY，而进入：

```text
WAIT_EXTENDED
```

等待价格重新靠近趋势。

这是V2非常重要的变化：

> **好股票 + 好趋势 ≠ 任何价格都可以买。**

---

# 6. 第三层：买入不再只有一种触发方式

V1只允许：

```text
刚刚突破20日均线 → 买
```

这会出现一个明显缺陷：

如果股票进入基本面优质池时，已经连续20天运行在MA20之上，则永远等不到“第一次穿越”。

因此V2采用两个互补入口。

---

## 6.1 BUY-B：突破启动型

首先满足：

```text
E >= 2
C > EMA20 > EMA60
RS20_IND > 0
ER20 >= 0.20
SV20 > 0
EXT <= 2.0
```

然后产生突破：

\[
C_t >
HC20_{t-1}+0.3ATR20_t
\]

其中使用：

> **过去20日最高收盘价**

而不是最高盘中价，以减少盘中尖峰噪声。

同时要求：

```text
CLV >= 0.60
```

即突破日收盘不能明显远离最高价。

成交额要求：

```text
0.9 <= AmountRatio <= 2.5
```

解释：

- `<0.9`：突破缺乏交易参与；
- `1.0—2.0`：正常放量；
- 极端放量并不自动增加信号强度；
- `>2.5`后需要警惕一次性事件和短期拥挤。

最终：

```text
BUY_BREAKOUT = TRUE
```

---

## 6.2 BUY-P：趋势回踩再启动型

这类信号用于已经建立趋势的股票。

过去3个交易日内至少一次进入EMA10—EMA20附近：

```text
min(L_{t-2:t})
<= EMA20 + 0.3ATR20
```

但没有破坏中期趋势：

```text
C > EMA60
RS20_IND > 0
```

随后当日重新转强：

```text
C_t > EMA10_t
AND
C_t > H_{t-1}
AND
SV20 > 0
```

且：

```text
EXT <= 1.5
```

则：

```text
BUY_PULLBACK = TRUE
```

它表达的是：

> 趋势已存在 → 短期抛压释放 → 支撑未破 → 再次向上。

对于5—40个交易日的中短期交易，这比“只有第一次突破均线才可以买”更完整。

---

# 7. 信号评分：决定“有没有资格交易”和“多个机会先买谁”

硬条件负责排除错误状态，评分负责排序。

定义：

\[
Q=
20Q_{ENV}
+20Q_{TREND}
+25Q_{RS}
+20Q_{PV}
+15Q_{PATH}
\]

总分100。

### 环境分

\[
Q_{ENV}=\frac{E}{4}
\]

### 趋势分

由：

```text
C > EMA20
EMA20 > EMA60
EMA20斜率 > 0
```

综合形成0—1得分。

### 相对强弱分

股票池内：

```text
RS20_IND
RS60_MKT
```

横截面百分位的平均值。

### 价量分

以：

```text
SV20
```

在股票池中的横截面百分位为主。

### 路径质量分

主要使用：

```text
ER20 percentile
```

同时对过度延伸的EXT进行扣分。

评分本身**不是上涨概率**，只用于：

- 同一天多个BUY信号排序；
- 判断信号质量是否足够；
- 监控持仓质量是否持续恶化。

默认阈值：

```text
BUY       Q >= 70
ADD       Q >= 75
健康持有   Q >= 60
警戒      55 <= Q < 60
显著恶化   Q < 55
```

不得根据一次全样本回测把阈值从70优化到例如“73.4”。

参数必须看**稳定区间**。

---

# 8. 仓位：从“50%/50%固定加仓”升级为风险定仓

保留V1分两次进入的思想，但“50%”指的是**目标风险仓位的一半**，而不是账户固定百分比。

## 8.1 初始保护位

入场价记为 \(P_{entry}\)。

初始止损参考：

\[
Stop_0=
\max(
EMA20-0.7ATR,\;
L10-0.3ATR,\;
P_{entry}-2ATR
)
\]

其中：

- `EMA20 - 0.7ATR`：短期趋势结构；
- `L10 - 0.3ATR`：近期结构低点；
- `Entry - 2ATR`：限制最大技术止损距离。

---

## 8.2 风险定仓

设：

```text
AccountEquity = 账户权益
RiskPerTrade  = 单笔允许亏损比例
```

默认研究值：

```text
RiskPerTrade = 0.6%
```

稳健检验区间：

```text
0.4%—0.8%
```

股票数量：

\[
Shares=
\frac{
AccountEquity\times RiskPerTrade
}{
P_{entry}-Stop_0
}
\]

再向下取整到市场允许的交易单位，并受到：

```text
单股最大仓位 Wmax
```

约束。

市场状态进一步调整：

```text
E = 3—4    RiskMultiplier = 1.0
E = 2      RiskMultiplier = 0.6
E = 0—1    RiskMultiplier = 0
```

因此：

```text
TargetPosition
=
RiskPosition × RiskMultiplier
```

首次BUY：

```text
50% × TargetPosition
```

---

# 9. 加仓：只有市场证明第一次买入是对的才增加风险

初始BUY后的10个交易日作为**确认窗口**。

不再要求机械突破“50日新高”。

满足：

```text
E >= 2
Q >= 75
SV20 > 0
EXT <= 2.5
```

且二选一：

```text
C >= Entry + 1 × ATR_entry
```

或：

```text
形成新的20日最高收盘价
```

则：

```text
ADD
```

增加到：

```text
100% TargetPosition
```

也就是说：

> **先让市场给出浮盈，再增加风险。**

如果10日内没有ADD：

```text
禁止机械补仓
```

但也不机械卖出。

若：

```text
Q >= 60
趋势结构仍成立
```

则保留初始仓位。

只有同时出现：

```text
持有 >= 10日
AND
MFE < 0.5 × ATR_entry
AND
Q < 60
```

才触发时间退出。

其中：

```text
MFE = Maximum Favorable Excursion
```

即持仓以来最大有利波动。

这比V1的：

```text
10天不突破 → 不加仓
```

进一步解决了“资金长期占用但走势没有兑现”的问题。

---

# 10. 卖出必须比买入更重视风险，而不是更慢

V1存在明显不对称：

```text
买入：
一个价格突破即可触发

卖出：
价格跌破 + 成交量走弱 + 相对强弱走弱
才退出
```

结果可能是：

> 进场很敏感，退出反而迟钝。

V2按照风险严重程度分级。

---

## 10.1 一级：硬保护 SELL

任何时候：

```text
C < Stop0
```

直接：

```text
SELL
```

形成趋势后使用移动保护位：

\[
Trail_t=
\max(
EMA60_t-0.5ATR_t,\;
HighestCloseSinceEntry-3ATR_t
)
\]

若：

```text
C < Trail
```

则：

```text
SELL
```

不等待成交量或RS再次确认。

---

## 10.2 二级：短趋势失效

若：

\[
C < EMA20-0.5ATR
\]

同时出现：

```text
RS20_IND < 0
```

或：

```text
SV20 < 0
```

则：

```text
SELL
```

若只是：

```text
C < EMA20-0.5ATR
```

但：

```text
RS20_IND > 0
SV20 > 0
行业趋势未破坏
```

则先：

```text
REDUCE 50%
```

给予最多2个收盘日恢复：

```text
重新站上EMA20 → HOLD
仍未站回EMA20 → SELL
```

---

## 10.3 三级：环境恶化

若：

```text
E <= 1
```

同时：

```text
C < EMA20
```

则至少：

```text
REDUCE
```

若再满足：

```text
RS20_IND < 0
```

则：

```text
SELL
```

这样可以避免：

> 市场和行业已经系统性转弱，但因为个股尚未跌穿某一个固定止损价而继续死扛。

---

## 10.4 四级：评分持续恶化

```text
Q < 55
连续2个交易日
```

则：

```text
SELL
```

单日突然跌至：

```text
55 <= Q < 60
```

仅进入：

```text
WARNING
```

防止评分噪声导致频繁交易。

---

## 10.5 五级：时间止损

持有10个交易日后：

```text
MFE < 0.5ATR_entry
AND
未创10/20日新高
AND
Q < 60
```

则：

```text
SELL_TIME
```

目的不是说“10天以后一定下跌”。

而是：

> 原交易假设是中短期趋势启动；10天后仍没有价格兑现，机会成本已经显著上升。

---

## 10.6 六级：过热减仓，而非固定止盈

仍然不设置：

```text
盈利10%止盈
盈利20%止盈
```

因为这会系统性截断趋势交易的右尾收益。

但出现：

```text
EXT > 3ATR
AND
AmountRatio > 2.5
AND
CLV < 0.40
```

说明：

```text
价格严重远离趋势
+ 极端放量
+ 当日高位回落
```

则：

```text
REDUCE 1/3—1/2
```

剩余仓位继续由趋势止损管理。

---

# 11. 最终状态机

每只股票只有以下状态：

```text
WATCH
  │
  ├─ BUY-B / BUY-P
  ↓
PILOT_POSITION
  │
  ├─ ADD
  ↓
FULL_POSITION
  │
  ├─ HOLD
  │
  ├─ REDUCE
  ↓
REDUCED_POSITION
  │
  ├─ RECOVER → HOLD
  │
  └─ FAIL → SELL
  ↓
CASH
  │
  └─ COOLDOWN
```

信号优先级：

```text
SELL
>
REDUCE
>
ADD
>
BUY
>
HOLD
>
WAIT
```

风险信号永远覆盖买入信号。

---

# 12. 每日标准化信号输出

系统每天收盘后必须输出完整记录，而不能只输出“买/卖”。

```text
date
ticker
signal
signal_strength
market_regime
environment_score
trigger_type
trend_state
RS20_IND
RS60_MKT
ER20
SV20
EXT
ATR20
entry_price
target_weight
stop_price
trail_price
valid_until
reason_codes
```

示例：

```text
Ticker:        600XXX
Signal:        BUY
Strength:      78
Regime:        RISK_ON
Trigger:       PULLBACK_RESUME
TargetWeight:  6.4%
InitialBuy:    3.2%
Stop:          31.42
ValidUntil:    T+2

Reason:
MKT_UP
IND_UP
IND_RS_POS
TREND_20_60
FLOW_POS
PATH_SMOOTH
PULLBACK_RESUME
NOT_EXTENDED
```

建议标准Reason Code：

```text
MKT_UP          市场趋势正向
BREADTH_UP      市场宽度改善
IND_UP          行业趋势正向
IND_RS_POS      行业跑赢市场
TRD_UP          个股趋势成立
RS_POS          个股相对行业强
FLOW_POS        方向化成交额正
PATH_SMOOTH     趋势路径较平滑
BRK20           20日突破
PULLBACK        趋势回踩
RESUME          再启动
EXTENDED        价格过度延伸
GAP_RISK        跳空风险
STOP_HARD       硬止损
TREND_BREAK     趋势破坏
RS_BREAK        相对强弱失效
FLOW_BREAK      价量转弱
REGIME_OFF      市场环境恶化
TIMEOUT         时间止损
EXHAUSTION      过热/衰竭
LIMIT_BLOCK     涨跌停无法执行
```

---

# 13. 下一交易日执行规则

所有日线信号：

```text
T日收盘计算
T+1执行
```

严禁回测：

```text
使用T日收盘数据生成信号
同时假设T日收盘成交
```

这属于前视偏差。

普通A股采用T+1交易制度，因此新买入股票不能假设当日重新卖出。深交所投教资料明确说明A股当日买入证券次日才能卖出；交易制度、价格限制及相关细则在历史上又有过调整，因此回测必须按当时有效规则模拟。

---

## 13.1 防止次日高开追涨

若BUY信号产生后：

\[
O_{t+1}>C_t+0.8ATR_t
\]

则：

```text
取消市价追入
状态 → WAIT_GAP
```

当日收盘重新评价。

BUY信号最多保留：

```text
2个交易日
```

若始终涨停封死无法成交：

```text
UNFILLED
```

而不是在回测中虚构成交。

---

## 13.2 卖出无法成交

如果SELL产生但股票跌停无法实际卖出：

```text
状态 = PENDING_SELL
```

账户风险仍然存在。

必须在：

```text
下一可成交时点
```

继续退出。

这部分不能按理论止损价计算已实现亏损。

---

# 14. 参数不采用“最优值”，而采用稳定区间

推荐初始参数如下。

| 模块 | 默认值 | 稳健性检验 |
|---|---:|---:|
| 短趋势EMA | 20 | 15—25 |
| 中趋势EMA | 60 | 40—80 |
| 突破窗口 | 20 | 15—30 |
| 突破过滤 | 0.3 ATR | 0.2—0.5 |
| ER窗口 | 20 | 15—30 |
| ER最低值 | 0.20 | 0.10—0.30 |
| 最大入场延伸 | 2 ATR | 1.5—2.5 |
| ADD最大延伸 | 2.5 ATR | 2—3 |
| 初始最大风险距离 | 2 ATR | 1.5—2.5 |
| 移动止损 | 3 ATR | 2.5—3.5 |
| ADD观察期 | 10日 | 5—12日 |
| 时间止损观察期 | 10日 | 8—15日 |
| Cooldown | 3日 | 2—5日 |

特别需要说明：

> **20、60、0.3ATR、2ATR、70分等均为工程初值，而不是论文证明的理论最优值。**

选择最终参数时不寻找：

```text
历史收益最高点
```

而寻找：

```text
参数发生适度变化
→ 收益/回撤/胜率仍大致稳定
```

即寻找：

> **parameter plateau，而不是 parameter peak。**

这是V2与原策略参数逻辑最重要的区别之一。

---

# 15. 回测规范

策略未经以下验证，不应进入实盘信号系统。

## 15.1 Point-in-Time股票池

每个历史日期只能使用当时已经知道的信息。

严禁：

```text
用今天筛出的“优质公司”
倒推2015年回测
```

否则会产生严重幸存者偏差和未来信息泄漏。

---

## 15.2 价格数据

因子计算可使用一致复权序列。

但成交模拟必须使用：

```text
当时真实可成交价格
```

并独立处理：

- 除权；
- 除息；
- 配股；
- 分红；
- 停牌；
- 涨跌停；
- ST/风险警示；
- 退市整理；
- 上市初期特殊价格制度。

---

## 15.3 交易成本

不得只使用统一的：

```text
0.1%
```

必须拆分：

```text
佣金
+ 交易经手费
+ 印花税
+ 滑点
+ 冲击成本
```

并按照历史时期对应制度处理。上交所2026年现行交易规则仍明确要求投资者支付佣金及相关交易费用；证券交易印花税自2023年8月28日起实施减半征收。

至少做：

```text
1×成本
2×成本
3×成本
```

压力测试。

技术策略尤其需要如此，因为大量研究发现“纸面有效”的技术规则在现实交易摩擦后显著弱化。

---

# 16. 样本外验证必须优先于全样本漂亮曲线

建议：

```text
Train 3年
Test 1年
滚动向前
```

或者使用扩展窗口：

```text
2010—2014训练 → 2015测试
2010—2015训练 → 2016测试
……
```

每次测试期禁止重新查看未来数据。

同时至少比较四个Benchmark：

```text
B0  优质股票池等权长期持有
B1  原V1策略
B2  单一MA20择时
B3  V2完整系统
```

这样才能回答：

> V2究竟增加了真正的择时价值，还是只是因为股票池本身就很好？

---

# 17. 必须做消融实验

依次删除：

```text
市场状态过滤
行业状态过滤
相对强弱
SV20价量
ER20路径质量
ATR仓位
双入口机制
时间止损
分级退出
```

分别重新回测。

真正有价值的模块，应表现为至少一种稳定改善：

```text
提高样本外收益
降低最大回撤
提高Calmar
降低尾部损失
降低换手
缩短回撤修复时间
提高5/10/20日信号胜率
```

如果删除某模块后完全没有恶化：

> **删除该模块。**

不要为了让模型看起来复杂而保留指标。

---

# 18. 回测评价指标

不能只看年化收益。

必须同时报告：

```text
CAGR
Annual Volatility
Sharpe
Sortino
Maximum Drawdown
Calmar
Ulcer Index

Excess Return
Information Ratio

Win Rate
Payoff Ratio
Profit Factor

Average Holding Days
Median Holding Days
Turnover
Trades / Year

MAE
MFE

5D Forward Return
10D Forward Return
20D Forward Return

BUY Hit Rate
ADD Hit Rate
REDUCE effectiveness
SELL effectiveness

Unfilled Ratio
Limit-block Ratio
Slippage sensitivity
Capacity
```

特别关注：

\[
Expectancy =
WinRate\times AvgWin
-
LossRate\times AvgLoss
\]

趋势系统完全可以：

```text
胜率只有45%
```

但：

```text
平均盈利 / 平均亏损 = 2.5
```

最终仍然非常有效。

因此不要为了提高“买入信号正确率”把系统优化成高胜率、低盈亏比策略。

---

# 19. 统计检验

因为策略包含多个指标和多个候选阈值，应至少实施：

```text
Block Bootstrap
White Reality Check
SPA / Step-SPA
Multiple-testing correction
```

并保存：

```text
所有试验过的模型
```

不能只保留最终最好的参数。

中国技术交易规则研究从2019年的超过28,000个信号到2024年的38,456条规则，都说明数据挖掘校正对判断技术策略是否真正有效至关重要，而且近年来样本中的技术规则优势进一步弱化。

---

# 20. 策略伪代码

```python
for stock in quality_pool:

    if not tradable(stock):
        signal = WAIT
        continue

    E = calc_environment_score(
        market_trend,
        market_breadth,
        industry_trend,
        industry_relative_strength
    )

    trend_ok = (
        close > ema20
        and ema20 > ema60
        and ema20 > ema20_5d_ago
    )

    relative_ok = rs20_industry > 0

    structure_ok = (
        er20 >= 0.20
        and sv20 > 0
        and extension <= 2.0
    )

    breakout = (
        close > high_close_20_prev + 0.3 * atr20
        and clv >= 0.60
        and 0.9 <= amount_ratio <= 2.5
    )

    pullback_resume = (
        recent_low_near_ema20
        and close > ema10
        and close > high_prev_day
        and rs20_industry > 0
        and sv20 > 0
    )

    Q = calc_signal_score()

    # ---------- no position ----------
    if position == 0:

        if E <= 1:
            signal = WAIT

        elif (
            trend_ok
            and relative_ok
            and structure_ok
            and Q >= 70
            and (breakout or pullback_resume)
        ):
            signal = BUY

        else:
            signal = WAIT

    # ---------- has position ----------
    else:

        if close < hard_stop:
            signal = SELL

        elif close < ema60 - 0.5 * atr20:
            signal = SELL

        elif (
            close < ema20 - 0.5 * atr20
            and (rs20_industry < 0 or sv20 < 0)
        ):
            signal = SELL

        elif (
            close < ema20 - 0.5 * atr20
            or (E <= 1 and close < ema20)
        ):
            signal = REDUCE

        elif Q < 55 for 2 consecutive days:
            signal = SELL

        elif (
            holding_days >= 10
            and mfe < 0.5 * atr_entry
            and Q < 60
        ):
            signal = SELL

        elif (
            not full_position
            and holding_days <= 10
            and Q >= 75
            and E >= 2
            and confirmation
        ):
            signal = ADD

        elif (
            extension > 3
            and amount_ratio > 2.5
            and clv < 0.40
        ):
            signal = REDUCE

        else:
            signal = HOLD
```

---

# 21. V1 → V2核心变化

| V1 | V2 |
|---|---|
| 单一均线择时 | 市场—行业—个股三级状态 |
| 刚突破MA20才能买 | 突破 + 回踩再启动双入口 |
| 固定1%过滤 | ATR波动率自适应过滤 |
| 50日新高加仓 | 浮盈/新高 + 评分确认后加仓 |
| 5/20日均量 | 方向化成交额SV20 |
| 裸价格趋势 | 相对行业、市场强弱 |
| 只看价格位置 | 增加ER20趋势路径质量 |
| 不区分追高程度 | EXT/ATR控制延伸 |
| 固定50%仓位 | 账户风险预算定仓 |
| 跌破MA20再等量价共同恶化 | 风险优先的分级退出 |
| 10日只决定是否加仓 | 增加机会成本型时间止损 |
| 无过热处理 | 过热只减仓，不固定止盈 |
| 无市场状态 | Risk-On / Neutral / Risk-Off |
| 信号=理论成交 | 信号、订单、实际成交分离 |
| 参数当成研究最优 | 参数平台 + Walk-Forward |
| 单一收益率评价 | 收益、风险、交易成本、容量、信号质量联合评价 |

---

# 22. 最终策略原则

本系统的核心不再是：

> **寻找一个能准确预测股票涨跌的技术指标。**

而是：

> **在已经筛选出的优质股票中，只在市场环境、行业趋势、个股绝对趋势、相对强弱、价格路径和资金结构共同支持时承担风险；当交易假设开始被证伪时，以预先定义的优先级退出风险。**

因此交易逻辑应始终遵循：

```text
基本面决定“买什么”
市场与行业决定“现在是否适合承担风险”
趋势和相对强弱决定“哪只股票正在兑现”
价量和路径决定“这个趋势质量如何”
Trigger决定“在哪里进入”
ATR决定“承担多少风险”
市场实际走势决定“是否加仓”
风险规则决定“什么时候离开”
```

这比：

```text
站上20日线 → 买
跌破20日线 → 卖
```

更符合一个真正用于生成可执行买卖信号的中短期系统。

---

# 23. 核心研究依据

研究证据按“设计影响”而不是简单罗列排列：

**技术规则稳健性与数据挖掘**

- Chuang et al., *Profitability of technical trading rules in the Chinese stock market*, Pacific-Basin Finance Journal, 2024：38,456条中国市场技术规则、数据挖掘校正、样本外与成本检验。
- Jiang, Tong & Song, *Technical Analysis Profitability Without Data Snooping Bias: Evidence from Chinese Stock Market*, International Review of Finance, 2019：超过28,000个技术信号，Step-SPA检验。
- *Profitability of simple technical trading rules of Chinese stock exchange indexes*：MA/TRB在加入交易成本后优势消失。
- Han, Yang & Zhou, *A New Anomaly: The Cross-Sectional Profitability of Technical Analysis*, JFQA, 2013：技术择时效果具有横截面异质性，并非简单的单规则普适性。

**中国A股动量、趋势与价量**

- Liu, Zhou & Zhu, *Trend Factor in China: The Role of Large Individual Trading*, Review of Asset Pricing Studies, 2024：价格与成交量联合趋势，成交量在中国市场尤其重要。
- Yue, Li & Ruan, *Does short-term momentum exist in China?*, Pacific-Basin Finance Journal, 2023：发现中国短周期主要表现为反转而非其定义下的短期动量。
- *Residual momentum and the cross-section of stock returns: Chinese evidence*, Finance Research Letters, 2019：残差动量显著优于简单原始收益动量。
- *Signed momentum in the Chinese stock market*, Pacific-Basin Finance Journal：传统动量不显著，动量收益与市场状态高度相关。
- *Cross-sectional and time-series momentum returns: Is China different?*, Pacific-Basin Finance Journal：中美动量特征存在明显差异。
- *Anomalies in the China A-share market*, Pacific-Basin Finance Journal：A股过去收益类异常整体证据较弱，但残差动量与反转相对突出。

**趋势质量与风险控制**

- Da, Gurun & Warachka, *Frog in the Pan: Continuous Information and Momentum*, Review of Financial Studies, 2014：平滑连续信息形成的趋势与后续持续性关系更强。
- Daniel & Moskowitz, *Momentum Crashes*, Journal of Financial Economics, 2016：动量风险高度状态依赖，并存在严重尾部风险。
- Barroso & Santa-Clara, *Momentum Has Its Moments*, Journal of Financial Economics, 2015：动态风险管理显著改善动量策略风险特征。
- *A trend factor: Any economic gains from using information over investment horizons?*, Journal of Financial Economics, 2016：同时利用短、中、长期趋势优于只依赖单一时间尺度。

**国内机构框架**

- 华泰金融工程，《再论A股择时：多维度融合》，2025：估值、情绪、资金、技术多维分层合成，重点解决单指标不稳定。
- 招商证券，《四维择时框架2.0》，2026：动态宏观、非线性情绪、估值回归、价量趋势。
- 国盛金融工程，“择时雷达”：从流动性、基本面、估值、资金、技术、拥挤/反转等维度形成综合判断。
- 中金公司，《价量因子手册》：系统考察动量/反转、流动性、波动、量价关系、资金流等价量信息，并强调投资者结构、交易制度与换手成本。

---

# 24. 适用边界

本策略仅负责：

```text
中短期交易时机
+
仓位风险控制
```

不负责判断：

```text
企业是否优质
财报是否造假
盈利预测是否下修
重大监管风险
产业逻辑是否发生永久变化
```

因此一旦上层基本面系统判定：

```text
投资逻辑失效
```

基本面退出信号优先级高于任何技术HOLD信号。

完整体系应为：

```text
优质股票筛选系统
        ↓
候选股票池
        ↓
本中短期择时系统
        ↓
组合风险预算
        ↓
订单与成交执行系统
        ↓
组合监控与归因
```

本技术文档描述的是其中第二层到第四层之间的**择时与交易信号引擎**，而不是独立的股票选择模型。