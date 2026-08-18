# 因子实验室 · 因子设计（对标论文与业界实践）

日期：2026-08-18 · 状态：已实现（v1.8.0）

设计参照：Alphalens（检验口径）、Microsoft Qlib Alpha158/Alpha360（字段族）、
Bali-Engle-Murray《Empirical Asset Pricing: The Cross Section of Stock Returns》
（因子谱系）、Barra/Axioma 风格因子框架（风险分组）、AQR 与国内头部券商金工研报
（量价与财务质量因子选型）。所有因子只用 T 日及以前数据，配 T+1 起前瞻收益。

## 一、已实现因子（41 个内置 + 35 字段公式引擎 + 12 PIT 财务）

### 价量因子（内置 29 个，`factors.py`）

| 组 | 因子 | 来源/依据 |
|---|---|---|
| 动量/反转 | mom20 / mom60 / mom120（跳近 5 日） | Jegadeesh-Titman 1993；A 股跳期动量（游资月度节奏） |
| | rev5（5 日反转） | Lehmann 1990；A 股短反转最强之一 |
| | dist_52w_high | George-Hwang 2004 JF（52 周高点） |
| | frog20 | Wu et al. 2022 JFE（青蛙效应/连续小涨） |
| | mom_quality60 | Israel-Moskowitz 2013 JF（信息离散度：平滑动量更持续） |
| | trend60 | Han-Zhou-Zhu 2016 JFE（趋势强度 |μ|/σ 近似） |
| 波动/风险 | vol20 / vol60 | Ang et al. 2006（低波动异象） |
| | downside_vol20 | Bawa-Lindenberg 1977（下行风险） |
| | hl_vol20 | Parkinson 1980（高低价估计量） |
| | idio_vol60 | Ang et al. 2006 JF（特质波动之谜，对市场残差） |
| | vol_of_vol20 | Wang-Xu 2022 RFS（不确定性与尾部） |
| | ret_skew60 | Boyer-Mitton-Vorkink 2010 RFS（预期偏度） |
| | ret_kurt60 | Dittmar 2002（高阶偏好/尾部） |
| | max_ret20（MAX） | Bali-Cakici-Whitelaw 2011 JFE（彩票偏好） |
| | maxmin20 | MAX-MIN 组合口径（Bali et al.） |
| 市场结构 | beta60 | Frazzini-Pedersen 2014 JFE（低贝塔异象 BAB） |
| | corr_market60 | Herskovic et al. 2016 JF（共同相关性因子） |
| | coskew60 | Harvey-Siddique 2000 JF（协偏度定价） |
| 流动性/量 | amihud20 | Amihud 2002 JFM（非流动性定价） |
| | illiq_shock | Amihud 突变口径（流动性冲击，Amihud et al. 2015） |
| | abnormal_turnover20 | Datar-Naik-Radcliffe 1998 / Liu 2006（换手率）变体：成交额短/长比 |
| | amount_stab20 / turnover_stability | 流动性稳定性（量侧低波动） |
| | volume_trend20 | 量在价先（Blume-Easley-O'Hara 1994 量信息） |
| 日内结构 | overnight20（隔夜收益和） | Lou-Polk-Skouras 2019 RFS（隔夜 vs 日内分解） |
| | intraday20（日内收益和） | 同上（A 股散户隔夜情绪 vs 机构日内） |

### PIT 财务因子（12 个，`fin_*`，公告日对齐）

| 因子 | 来源/依据 |
|---|---|
| fin_ep（TTM 净利/市值） | Fama-French 1992（价值） |
| fin_ep_ocf（OCF/市值） | 现金版便宜（更抗操纵） |
| fin_sue（标准化未预期盈余） | Bernard-Thomas 1989（盈余公告后漂移 PEAD） |
| fin_accruals（单季 EPS-OCFPS） | Sloan 1996（应计异象，每股价差口径） |
| fin_gross_margin | Novy-Marx 2013 JFE（毛利率质量） |
| fin_roe_ttm | 质量（Haugen-Baker 1996；AQR Quality） |
| fin_rev_grow_ttm / fin_profit_grow_ttm | 成长 |
| fin_earnings_stab / fin_rev_stab | 盈利/营收稳定性（质量，8 季 CV） |
| fin_delta_roe | 质量动量 |
| fin_ocf_to_profit | 利润含金量（应计质量变体） |

注：fin_sue / fin_accruals / fin_*_stab 需要至少 5-8 个报告期历史（真实构建后可用）；
mini 测试集只有 3 期故为空，属预期。

### 公式引擎字段（35 个）

OHLCV/vwap/ret + K 线形态（kmid/klen/kup/klow/ksft，Alpha158）+ 相对昨收
（open0/high0/low0/vwap0）+ 多周期动量（ret5-120）+ 量比（vol_ratio5/20、
turnover20）+ PIT 财务（fn_* 10 个）。算子 19 个（ts_mean/std/sum/max/min/
delta/corr/cov/rank/skew/kurt/scale/rsv、x[n]、cs_rank/cs_zscore、标量）。

## 二、暂无法实现的因子清单（数据缺失）

| 因子 | 缺失数据 | 业界参照 | 可行路径 |
|---|---|---|---|
| 换手率类（turnover、异常换手、零换手天数） | **历史流通股本/自由流通市值序列**（当前只有即时快照） | Datar-Naik-Radcliffe 1998；Liu 2006（A股「流动性」） | Tushare Pro daily_basic（流通股本日线）或聚源；现用成交额短/长比近似 |
| BM / EP 分母（总市值） | 历史总股本 + 不复权价 | Fama-French 1992 | 同上；当前用每股口径（eps/price）规避 |
| 完整应计（ACC = ΔCA-ΔCL-DP） | **资产负债表科目**（流动资产/负债、折旧） | Sloan 1996；Richardson et al. 2005 | 东财 F10 资产负债表（已验证可得），需第二个数据构建阶段 |
| ROA / 资产增长率 | 总资产历史 | Balakrishnan et al. 2010；AQR Quality | 同上（资产负债表） |
| 净资产收益率分解（DuPont 三因子） | 权益乘数（总资产/净资产） | Haugen-Baker；Soliman 2008 | 同上 |
| 资本支出/资产（投资因子） | 现金流量表 CapEx | Titman-Wei-Jia 2004（A 股投资异象） | 东财 F10 现金流量表 |
| 分析师预期（EG/EGIB、预期修正） | **一致预期 EPS 时序（PIT）** | La Porta 1996；Gleason-Lee 2003 | 东财盈利预测接口（逐股慢）；或 Tushare 盈利预测 |
| 机构持仓（基金重仓/北向） | PIT 持仓明细 | Yan-Zhang 2009（聪明钱）；北向因子（境内实证） | 已有 fund_portfolio 数据但非 PIT 落地，需单独构建 |
| 股权激励/回购/增减持 | 公告结构化事件 | 境内实证（增持效应） | 巨潮公告 NLP 抽取，工程量大 |
| 行业中性化 / Barra 风格残差因子 | **历史行业分类（PIT）** | Barra CNE5；Qlib 行业中性化 | 申万现势分类可得但非 PIT（静态偏差，已标注） |
| 涨跌停状态（涨停动量/封板率） | **盘中tick/分时 + PIT 涨停标记** | 境内游资研究 | 分钟数据已部分可得（腾讯 minute），覆盖时长不足 |
| 龙虎榜席位因子 | 席位-股票历史关联 | 游资跟随策略实证 | 工具层已有 query_dragon_tiger，未做 PIT 面板 |
| 融资融券动量 | 融资余额 PIT 时序 | 境内实证（杠杆资金动量） | 工具层已有 margin，未落面板 |
| 限售解禁压力 | 未来解禁日历（前瞻已知，非 PIT 问题而是数据落地） | 境内实证（解禁压制） | query_lockup 已有，可直接并入因子 |
| 商品/汇率暴露 | 个股-商品关联映射 | 宏观因子模型 | 需另建映射表 |
| 日内高频因子（已实现波动/VPIN/大单不平衡） | **逐笔/分钟全历史** | 实证微观结构文献 | 成本高，暂缓 |
| 低频流动性冲击成本（Roll/CHL） | 有效买卖价 | Roll 1984; Corwin-Schultz 2012 | CS 可用日高低价实现——已在 hl_vol 附近，可加 |
| 管理层特征/公司治理 | 高管背景、董事会数据 | Bertrand-Schoar 2003 | 数据源缺失 |
| ESG | ESG 评级 PIT | PRI 实证 | 商业数据源 |
| another：分析师跟踪数变化、盈余公告漂移日期距离 | 公告日历 + 跟踪数 | DellaVigna-Pollet 2009 | 部分可由 NOTICE_DATE 派生（可做） |

### 优先级建议（按数据可得性 × 因子强度）

1. **资产负债表 + 现金流量表落地**（东财 F10 已验证可得）→ 解锁完整应计/
   ROA/杜邦/投资因子，6 个以上新因子；
2. **解禁日历/融资余额/龙虎榜并入因子面板**（工具层已有数据，只差 PIT 落地）
   → 3 个 A 股特色因子；
3. 盈利公告日期距离（NOTICE_DATE 派生，零新数据）→ PEAD 窗口因子；
4. Corwin-Schultz 有效价差（用现有日线高低价）→ 流动性改进版。

## 三、检验与回测口径

不变：MIN_CROSS_SECTION=30、T 收盘因子 × T+1 起收益、五分组/Q5-Q1/换手/
秩自相关、探索级偏差标签（幸存者/前复权/无 PIT 交易状态）随结果返回。
