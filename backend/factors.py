"""因子库 + 单因子检验（Alphalens 口径，pandas 3.0 薄实现）。

时点纪律：因子值取 T 日收盘可算出的量 × T+1 起的前瞻收益，杜绝同日成交。
# ponytail: 检验用 pandas groupby 横截面实现，全 A 20 年单因子一次约 10-20s；要批量扫描再上矩阵化/DuckDB
"""
from __future__ import annotations

import math
from datetime import date as calendar_date
from typing import cast

import numpy as np
import pandas as pd

import factor_data

FORWARD_HORIZONS = [1, 5, 10, 20]

MIN_CROSS_SECTION = 30  # 横截面样本少于此数的日子跳过（evaluate 与回测候选共用）

_index_ret_cache: tuple[object, pd.Series] | None = None


def _index_ret_aligned(df: pd.DataFrame) -> pd.Series | None:
    """市场（沪深300）日收益对齐到面板行序。构建数据里没存指数时返回 None。"""
    global _index_ret_cache
    idx_close = factor_data.load_index_close()
    if idx_close.empty:
        return None
    # 缓存键用日历文件的 stat，不用 Series.index.equals：重建后新指数仍可能是同一批日期
    # （补一根新 K 线不会改旧日期序列），只比日期会让旧收益缓存永久存活。
    key = factor_data.stat_key(factor_data._CALENDAR_FILE)
    if _index_ret_cache is None or _index_ret_cache[0] != key:
        _index_ret_cache = (key, idx_close.pct_change())
    m = cast(pd.Series, df["date"]).map(_index_ret_cache[1])
    return m if m.notna().any() else None


# ---------------------------------------------------------------------------
# 因子计算（输入 load_panel() 的日线面板）
# ---------------------------------------------------------------------------

def compute_factors(panel: pd.DataFrame, names: list[str] | None = None) -> pd.DataFrame:
    """长表 → 每股票时间序列对齐的因子宽表（MultiIndex date,code → factor 列）。

    全部因子只用截至当日收盘的数据（shift(1) 基准收益率等价于滚动窗口不含未来）。
    """
    df = panel.sort_values(["code", "date"]).copy()
    g = df.groupby("code", sort=False)

    open_, high, low = df["open"], df["high"], df["low"]
    ret_1d = cast(pd.Series, g["close"].transform(lambda s: s.pct_change()))
    amount = df["amount"]

    def mom(skip_end: int, window: int) -> pd.Series:
        """动量：跳过最近 skip_end 日，再往前 window 日的累计收益。"""
        def _m(s: pd.Series) -> pd.Series:
            return s.shift(skip_end) / s.shift(skip_end + window) - 1.0
        return g["close"].transform(_m)

    def vol(window: int) -> pd.Series:
        return g["close"].transform(lambda s: s.pct_change().rolling(window).std())

    def downside_vol(window: int) -> pd.Series:
        return g["close"].transform(
            lambda s: s.pct_change().where(lambda r: r < 0).rolling(window, min_periods=window // 2).std()
        )

    def amihud(window: int) -> pd.Series:
        # 非流动性 = |日收益| / 成交额，滚动均值。前复权价的收益≈真实收益，成交额为披露值。
        illiq = ret_1d.abs() / amount.replace(0, np.nan)
        return illiq.groupby(df["code"], sort=False).rolling(window).mean().reset_index(level=0, drop=True)

    def amount_stability(window: int) -> pd.Series:
        # 成交额稳定性：滚动均/std（低 = 成交额忽大忽小）。ponytail: 无历史流通股本，换手口径降级为成交额
        m = amount.groupby(df["code"], sort=False).rolling(window).mean().reset_index(level=0, drop=True)
        s = amount.groupby(df["code"], sort=False).rolling(window).std().reset_index(level=0, drop=True)
        return m / s

    # —— 论文级价量因子（全部只用截至 T 日收盘数据）——
    def ret(series: pd.Series) -> pd.Series:
        return series.pct_change()

    close_g = g["close"]
    high_g = g["high"]

    def max_ret(window: int) -> pd.Series:
        """MAX 效应（Bali et al. 2011 JFE）：近 n 日最大单日收益（彩票偏好，高值未来收益低）。"""
        return pd.Series(ret_1d.groupby(df["code"], sort=False).rolling(window).max().reset_index(level=0, drop=True))

    def dist_52w_high() -> pd.Series:
        """52 周高点距离（George & Hwang 2004 JF）：close/252 日最高收盘 - 1（接近高点→动量强）。"""
        def _d(s: pd.Series) -> pd.Series:
            return s / s.rolling(252, min_periods=120).max() - 1.0
        return close_g.transform(_d)

    def overnight(window: int) -> pd.Series:
        """隔夜收益（Lou-Polk-Skouras 2019 RFS）：Σ open/prev_close-1，情绪/散户主导段。"""
        def _o(o: pd.Series, c: pd.Series) -> pd.Series:
            return (o / c.shift(1) - 1.0).rolling(window).sum()
        return cast(pd.Series, pd.concat([open_, df["close"]], axis=1, keys=["o", "c"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _o(x["o"], x["c"])).reset_index(level=0, drop=True).sort_index())

    def intraday(window: int) -> pd.Series:
        """日内收益（Lou-Polk-Skouras 2019）：Σ close/open-1，机构主导段（A股 T+1 下的反向锚）。"""
        def _i(c: pd.Series, o: pd.Series) -> pd.Series:
            return (c / o - 1.0).rolling(window).sum()
        return cast(pd.Series, pd.concat([df["close"], open_], axis=1, keys=["c", "o"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _i(x["c"], x["o"])).reset_index(level=0, drop=True).sort_index())

    def frog(window: int, quant: float = 0.2) -> pd.Series:
        """青蛙效应（Wu et al. 2022 JFE）：近 n 日收益在 n*2 日分布中的分位（第一 Pond 跳跃）。"""
        def _f(s: pd.Series) -> pd.Series:
            r = s.pct_change()
            long_win = r.rolling(window * 2, min_periods=window).quantile(quant)
            return cast(pd.Series, (r.rolling(window).sum() < long_win).astype(float))
        return close_g.transform(_f)

    def abnormal_turnover(window: int) -> pd.Series:
        """异常换手（Datar-Naik-Radcliffe 1998 / Liu 2006 变体）：近 n 日均成交额 / 250 日均成交额。"""
        short_m = amount.groupby(df["code"], sort=False).rolling(window).mean().reset_index(level=0, drop=True)
        long_m = amount.groupby(df["code"], sort=False).rolling(250, min_periods=120).mean().reset_index(level=0, drop=True)
        return short_m / long_m

    def hl_spread(window: int) -> pd.Series:
        """Parkinson 高低价波动率（Corwin-Schultz 前置）：σ² = Σ(ln(high/low))²/(4ln2·n)。"""
        def _p(high_price: pd.Series, low_price: pd.Series) -> pd.Series:
            return ((np.log(high_price / low_price) ** 2).rolling(window).sum() / (4 * np.log(2) * window)) ** 0.5
        return cast(pd.Series, pd.concat([high, low], axis=1, keys=["h", "l"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _p(x["h"], x["l"])).reset_index(level=0, drop=True).sort_index())

    def corr_market(window: int) -> pd.Series:
        """与市场相关（Malkiel-Xu 2006 / Herskovic et al. 2016 JF）：个股收益与指数收益的滚动相关。"""
        idx_ret = _index_ret_aligned(df)
        if idx_ret is None:
            return pd.Series(np.nan, index=df.index)
        def _c(r: pd.Series, m: pd.Series) -> pd.Series:
            return cast(pd.Series, r.rolling(window).corr(m))
        return cast(pd.Series, pd.concat([ret_1d, idx_ret], axis=1, keys=["r", "m"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _c(x["r"], x["m"])).reset_index(level=0, drop=True).sort_index())

    def beta_market(window: int) -> pd.Series:
        """市场贝塔（Frazzini-Pedersen 2014 JFE BAB）：低贝塔异象，低 β 风险调整后收益更高。"""
        idx_ret = _index_ret_aligned(df)
        if idx_ret is None:
            return pd.Series(np.nan, index=df.index)
        def _b(r: pd.Series, m: pd.Series) -> pd.Series:
            var = cast(pd.Series, m.rolling(window).var())
            cov = r.rolling(window).cov(m)
            return cov / var.where(var > 0)
        return cast(pd.Series, pd.concat([ret_1d, idx_ret], axis=1, keys=["r", "m"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _b(x["r"], x["m"])).reset_index(level=0, drop=True).sort_index())

    def idio_vol(window: int) -> pd.Series:
        """特质波动（Ang et al. 2006 JF）：对市场回归的残差 std（高特质波动→低收益，谜题）。"""
        idx_ret = _index_ret_aligned(df)
        if idx_ret is None:
            return pd.Series(np.nan, index=df.index)
        def _iv(r: pd.Series, m: pd.Series) -> pd.Series:
            market_var = cast(pd.Series, m.rolling(window).var())
            beta = r.rolling(window).cov(m) / market_var.where(lambda v: v > 0)
            resid = r - beta * m
            return resid.rolling(window).std()
        return cast(pd.Series, pd.concat([ret_1d, idx_ret], axis=1, keys=["r", "m"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _iv(x["r"], x["m"])).reset_index(level=0, drop=True).sort_index())

    def coskew(window: int) -> pd.Series:
        """协偏度（Harvey-Siddique 2000 JF）：个股与市场收益的滚动协偏（系统性偏度定价）。"""
        idx_ret = _index_ret_aligned(df)
        if idx_ret is None:
            return pd.Series(np.nan, index=df.index)
        def _cs(r: pd.Series, m: pd.Series) -> pd.Series:
            m2 = (m ** 2).rolling(window).mean()
            m3 = (m ** 3).rolling(window).mean()
            rm2 = (r * m ** 2).rolling(window).mean()
            denom = (m3 - m2 ** 2).where(lambda v: v != 0)
            return (rm2 - r.rolling(window).mean() * m2) / denom
        return cast(pd.Series, pd.concat([ret_1d, idx_ret], axis=1, keys=["r", "m"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _cs(x["r"], x["m"])).reset_index(level=0, drop=True).sort_index())

    def illiq_shock() -> pd.Series:
        """非流动性冲击（Amihud 2002 JFM 突变口径）：近 20 日非流动 / 前 250 日均值。"""
        illiq = (ret_1d.abs() / amount.replace(0, np.nan)) \
            .groupby(df["code"], sort=False)
        short_m = illiq.rolling(20).mean().reset_index(level=0, drop=True)
        long_m = illiq.rolling(250, min_periods=120).mean().reset_index(level=0, drop=True)
        return short_m / long_m

    def turnover_stab() -> pd.Series:
        """成交额波动稳定性（低波动异象的量侧）：250 日 均值/std。"""
        m = amount.groupby(df["code"], sort=False).rolling(250, min_periods=120).mean().reset_index(level=0, drop=True)
        s = amount.groupby(df["code"], sort=False).rolling(250, min_periods=120).std().reset_index(level=0, drop=True)
        return m / s

    def ret_skew(window: int) -> pd.Series:
        """收益偏度（Boyer-Mitton-Vorkink 2010 RFS：预期特质偏度，高偏度未来收益低）。"""
        return pd.Series(ret_1d.groupby(df["code"], sort=False).rolling(window).skew().reset_index(level=0, drop=True))

    def ret_kurt(window: int) -> pd.Series:
        """收益峰度（尾部风险，Dittmar 2002 二阶偏好）。"""
        return pd.Series(ret_1d.groupby(df["code"], sort=False).rolling(window).kurt().reset_index(level=0, drop=True))

    def max_min_gap(window: int) -> pd.Series:
        """极值差（Bali et al. MAX-MIN 组合口径）：近 n 日最大收益 - 最小收益。"""
        rmax = ret_1d.groupby(df["code"], sort=False).rolling(window).max().reset_index(level=0, drop=True)
        rmin = ret_1d.groupby(df["code"], sort=False).rolling(window).min().reset_index(level=0, drop=True)
        return rmax - rmin

    def mom_quality(window: int) -> pd.Series:
        """动量质量（Israel-Moskowitz 2013 JF「信息离散度」：平滑上涨 > 忽上忽下）。"""
        def _q(s: pd.Series) -> pd.Series:
            r = s.pct_change()
            sign_sum = cast(pd.Series, np.sign(r)).rolling(window).sum()
            abs_sum = cast(pd.Series, r.abs().rolling(window).sum()).where(lambda v: v > 0)
            return sign_sum / abs_sum
        return close_g.transform(_q)

    def trend_strength(window: int) -> pd.Series:
        """价格趋势强度（Han-Zhou-Zhu 2016 JFE 融合趋势指标近似：|n 日收益|/n 日波动）。"""
        def _t(s: pd.Series) -> pd.Series:
            r = s.pct_change()
            return r.rolling(window).mean() / r.rolling(window).std().where(lambda v: v > 0)
        return close_g.transform(_t)

    def volume_trend(window: int) -> pd.Series:
        """量趋势（量在价先的朴素口径）：近 n/3 日均量 / 近 n 日均量 - 1。"""
        vg = df["volume"].groupby(df["code"], sort=False)
        short_v = vg.rolling(window // 3).mean().reset_index(level=0, drop=True)
        long_v = vg.rolling(window).mean().reset_index(level=0, drop=True)
        return short_v / long_v.where(long_v > 0) - 1.0

    def vol_of_vol(window: int) -> pd.Series:
        """波动率的波动（Wang-Xu 2022 RFS「不确定性」：特质高阶风险）。"""
        v = ret_1d.groupby(df["code"], sort=False).rolling(window).std().reset_index(level=0, drop=True)
        return pd.Series(v.groupby(df["code"], sort=False).rolling(window).std().reset_index(level=0, drop=True))

    def max_corr(window: int) -> pd.Series:
        """同上 corr_market 的短窗口版（15 日）：短期联动（涨跌停潮中的传导）。"""
        return corr_market(window)

    all_factors = {
        # 动量/反转
        "mom20": lambda: mom(5, 20),
        "mom60": lambda: mom(5, 60),
        "mom120": lambda: mom(5, 120),
        "rev5": lambda: g["close"].transform(lambda s: -(s / s.shift(5) - 1.0)),  # 5 日反转：跌得多 → 值大
        "dist_52w_high": lambda: dist_52w_high(),
        "frog20": lambda: frog(20),
        "mom_quality60": lambda: mom_quality(60),
        "trend60": lambda: trend_strength(60),
        # 波动/风险
        "vol20": lambda: vol(20),
        "vol60": lambda: vol(60),
        "downside_vol20": lambda: downside_vol(20),
        "hl_vol20": lambda: hl_spread(20),
        "idio_vol60": lambda: idio_vol(60),
        "vol_of_vol20": lambda: vol_of_vol(20),
        "ret_skew60": lambda: ret_skew(60),
        "ret_kurt60": lambda: ret_kurt(60),
        "max_ret20": lambda: max_ret(20),
        "maxmin20": lambda: max_min_gap(20),
        # 市场结构
        "beta60": lambda: beta_market(60),
        "corr_market60": lambda: corr_market(60),
        "coskew60": lambda: coskew(60),
        # 流动性/量
        "amihud20": lambda: amihud(20),
        "illiq_shock": lambda: illiq_shock(),
        "abnormal_turnover20": lambda: abnormal_turnover(20),
        "amount_stab20": lambda: amount_stability(20),
        "turnover_stability": lambda: turnover_stab(),
        "volume_trend20": lambda: volume_trend(20),
        # 日内结构
        "overnight20": lambda: overnight(20),
        "intraday20": lambda: intraday(20),
    }

    out = df[["date", "code"]].copy()
    for name in (list(all_factors) if names is None else names):
        out[name] = all_factors[name]()
    # 统一交易日历：停牌缺行不能把下一根行情误当成下一交易日。
    calendar = sorted(set(factor_data.load_calendar()) | set(df["date"]))
    prices = df.pivot(index="date", columns="code", values="close").reindex(calendar)
    row_index = pd.MultiIndex.from_arrays([out["date"], out["code"]], names=["date", "code"])
    for h in FORWARD_HORIZONS:
        forward = prices.shift(-h) / prices - 1.0
        out[f"ret_fwd_{h}"] = forward.stack().reindex(row_index).to_numpy()
    out["ret_next_1"] = out["ret_fwd_1"]
    # 上市天数代理（用于剔除次新股）
    out["days_since_list"] = g.cumcount() + 1
    return out.set_index(["date", "code"])


FACTOR_META = {
    # 动量 / 反转
    "mom20": "20 日动量（跳过近 5 日）",
    "mom60": "60 日动量（跳过近 5 日）",
    "mom120": "120 日动量（跳过近 5 日）",
    "rev5": "5 日反转（跌得多值大）",
    "dist_52w_high": "52 周高点距离（George-Hwang 2004）",
    "frog20": "青蛙效应第一 Pond（Wu et al. 2022 JFE）",
    "mom_quality60": "动量质量·信息离散度（Israel-Moskowitz 2013）",
    "trend60": "趋势强度 |收益|/波动（Han-Zhou-Zhu 2016 近似）",
    # 波动 / 风险
    "vol20": "20 日波动率",
    "vol60": "60 日波动率",
    "downside_vol20": "20 日下行波动率",
    "hl_vol20": "高低价 Parkinson 波动率（20 日）",
    "idio_vol60": "特质波动率（Ang et al. 2006，对市场残差）",
    "vol_of_vol20": "波动率的波动（Wang-Xu 2022 RFS）",
    "ret_skew60": "收益偏度（Boyer et al. 2010）",
    "ret_kurt60": "收益峰度（尾部风险）",
    "max_ret20": "MAX 效应·20 日最大单日收益（Bali et al. 2011）",
    "maxmin20": "MAX-MIN 极值差（20 日）",
    # 市场结构
    "beta60": "市场贝塔（Frazzini-Pedersen 2014 低贝塔异象）",
    "corr_market60": "与市场相关（Herskovic et al. 2016）",
    "coskew60": "协偏度（Harvey-Siddique 2000）",
    # 流动性 / 量
    "amihud20": "20 日 Amihud 非流动性",
    "illiq_shock": "非流动性冲击（近 20 日/250 日均值）",
    "abnormal_turnover20": "异常成交（近 20 日/250 日均成交额）",
    "amount_stab20": "20 日成交额稳定性（高=稳定）",
    "turnover_stability": "250 日成交额稳定性",
    "volume_trend20": "量趋势（近 7 日/20 日均量）",
    # 日内结构
    "overnight20": "隔夜收益 20 日和（Lou-Polk-Skouras 2019）",
    "intraday20": "日内收益 20 日和（Lou-Polk-Skouras 2019）",
    # PIT 财务（factor_pit 派生，见 fin_factors）
}


def factor_name_of(factor: str) -> str:
    if factor.startswith("custom:"):
        import factor_expr

        fid = factor.split(":", 1)[1]
        saved = factor_expr.lookup_custom(fid)
        return saved.get("name", fid) if saved is not None else fid
    if factor.startswith("fin_"):
        return FIN_FACTOR_META.get(factor, factor)
    return FACTOR_META.get(factor, factor)


# ---------------------------------------------------------------------------
# PIT 财务因子（业绩报表口径派生；按公告日可见，杜绝前视）
# ---------------------------------------------------------------------------

FIN_FACTOR_META = {
    "fin_ep": "EP 盈收益率 = 最新已公告报告期 EPS/收盘价（Fama-French 1992 价值，累计期口径）",
    "fin_ep_ocf": "OCF 盈利率 = 最新已公告报告期每股经营现金流/收盘价（累计期口径）",
    "fin_sue": "SUE 标准化未预期盈余（Bernard-Thomas 1989 盈余动量）",
    "fin_accruals": "应计质量代理 = 单季 EPS-单季 OCFPS（Sloan 1996 每股价差口径）",
    "fin_gross_margin": "毛利率（Novy-Marx 2013 质量 proxy，累计期口径）",
    "fin_roe": "ROE（加权，最新已公告报告期；非 TTM，Q1 与 Q3 不可直接比）",
    "fin_rev_grow": "营收同比增长（最新已公告报告期，报表披露口径）",
    "fin_profit_grow": "净利同比增长（最新已公告报告期，报表披露口径）",
    "fin_earnings_stab": "盈利稳定性 = 近 8 季单季 EPS 的 均值/std（质量）",
    "fin_rev_stab": "营收稳定性 = 近 8 季单季营收 均值/std（质量）",
    "fin_delta_roe": "ROE 环比改善（质量动量）",
    "fin_ocf_to_profit": "经营现金流/净利（应计质量，高=利润含金量高）",
}


def _fin_factor_frame(panel: pd.DataFrame, factor: str) -> pd.DataFrame:
    """PIT 财务因子：以 as_of 宽表为基础派生，对齐回 compute_factors 的输出结构。"""
    import factor_pit

    base = compute_factors(panel, [])
    # 必须排序：unique() 保序是 first-seen，停牌缺口会造成非单调 → merge_asof 崩
    trade_dates = sorted(base.index.get_level_values("date").unique())
    pit = factor_pit.as_of_panel(trade_dates)  # index=date, columns=(code, field)
    if pit.empty:
        raise ValueError("PIT 财务数据为空：先构建财务数据（构建按钮两阶段）")

    close_w = panel.pivot(index="date", columns="code", values="close").sort_index()
    vals = _fin_derive(pit, close_w)  # dict[factor_name] = date×code 宽表
    if factor not in vals:
        raise ValueError(f"未知财务因子 {factor}（可用：{'、'.join(FIN_FACTOR_META)}）")
    wide = vals[factor]
    base[factor] = wide.stack().reindex(base.index)
    return base


def _fin_derive(pit: pd.DataFrame, close_w: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """从 as_of 面板派生财务因子宽表。pit columns=(code, field)。"""
    out: dict[str, pd.DataFrame] = {}

    def field(name: str) -> pd.DataFrame:
        try:
            # reindex 而非 [] 取列：面板里有股票在 PIT 表无任何记录（次新未出首报）时
            # [] 会抛 KeyError 导致整只因子全 NaN；reindex 对缺失列填 NaN
            return pit.xs(name, axis=1, level=1).reindex(index=close_w.index, columns=close_w.columns)
        except KeyError:
            return pd.DataFrame(np.nan, index=close_w.index, columns=close_w.columns)

    eps = field("eps")
    ocf_ps = field("ocf_ps")
    roe = field("roe")
    rev_yoy = field("rev_yoy")
    profit_yoy = field("profit_yoy")
    gm = field("gross_margin")

    close = close_w.replace(0, np.nan)
    # 业绩报表是累计口径（Q1 累计→Q3 累计），单季化：本期 - 上季累计（Q1 本身即单季）
    # as_of 面板已是「最新可见报告期」；单季化需要历史序列——这里用报表自带的同比/单季字段为主，
    # TTM 类因子用 4 个报告期滚动（需 pit 长表），因此下面从 pit_table 直接算。
    out["fin_ep"] = eps / close
    out["fin_ep_ocf"] = ocf_ps / close
    out["fin_gross_margin"] = gm
    out["fin_roe"] = roe
    out["fin_rev_grow"] = rev_yoy
    out["fin_profit_grow"] = profit_yoy
    out["fin_ocf_to_profit"] = (ocf_ps / eps).where(eps.abs() > 1e-6)

    # 需要 PIT 长表历史序列的因子：SUE / 应计 / 稳定性 / ΔROE
    try:
        import factor_pit as _fp

        fund = _fp.pit_table()
        sue_w, accr_w, estab_w, rtab_w, droe_w = _quarter_series_factors(fund, close_w)
        out.setdefault("fin_sue", sue_w)
        out.setdefault("fin_accruals", accr_w)
        out.setdefault("fin_earnings_stab", estab_w)
        out.setdefault("fin_rev_stab", rtab_w)
        out.setdefault("fin_delta_roe", droe_w)
    except (FileNotFoundError, ValueError):
        pass
    return out


def _quarter_series_factors(fund: pd.DataFrame, close_w: pd.DataFrame) -> tuple:
    """每个公告事件仅用已可见报告做单季化和历史窗口计算。

    每股累计 EPS/现金流按季度差分；缺季保留空值，Q1 跨年重置。
    返回 SUE、每股应计差额、盈利/营收稳定性和 ΔROE 的逐股可见序列。
    更正公告到来时重算依赖窗口，同时保持最新报告期不回退。
    """
    import factor_pit

    records = []
    for code, group in fund.groupby("code", sort=False):
        # Quarterly grid preserves actual lags even when a report is missing.
        group = group.sort_values(["report_date", "notice_date"]).drop_duplicates("report_date", keep="last")
        group.index = pd.PeriodIndex(group["report_date"], freq="Q")
        group = group.reindex(pd.period_range(group.index.min(), group.index.max(), freq="Q"))
        notices = group["notice_date"].fillna("").to_numpy(dtype=str)
        events = np.unique(notices[notices != ""])
        visible = (notices[None, :] != "") & (notices[None, :] <= events[:, None])
        latest = np.where(visible, np.arange(len(group)), -1).max(axis=1)

        def values(column):
            raw = pd.to_numeric(group[column], errors="coerce").to_numpy(dtype=float)
            return np.where(visible, raw[None, :], np.nan)

        def quarter(column):
            cumulative = values(column)
            previous = np.roll(cumulative, 1, axis=1)
            previous[:, 0] = np.nan
            return np.where((group.index.quarter == 1)[None, :], cumulative, cumulative - previous)

        row = np.arange(len(events))
        eps, ocf, revenue, roe = quarter("eps"), quarter("ocf_ps"), quarter("revenue"), values("roe")
        offsets = latest[:, None] - np.arange(7, -1, -1)

        def moments(array):
            history = np.take_along_axis(array, offsets.clip(min=0), axis=1)
            history[offsets < 0] = np.nan
            count = np.isfinite(history).sum(axis=1)
            mean = np.nansum(history, axis=1) / np.maximum(count, 1)
            variance = np.nansum((history - mean[:, None]) ** 2, axis=1) / np.maximum(count - 1, 1)
            std = np.sqrt(variance)
            mean[count < 6] = np.nan
            std[(count < 6) | (std <= 1e-9)] = np.nan
            return mean, std

        eps_mean, eps_std = moments(eps)
        rev_mean, rev_std = moments(revenue)
        lag_eps = eps[row, (latest - 4).clip(min=0)].copy()
        lag_eps[latest < 4] = np.nan
        lag_roe = roe[row, (latest - 1).clip(min=0)].copy()
        lag_roe[latest < 1] = np.nan
        derived = {
            "sue": (eps[row, latest] - lag_eps) / eps_std,
            "accr": eps[row, latest] - ocf[row, latest],
            "earn_stab": eps_mean / eps_std,
            "rev_stab": rev_mean / rev_std,
            "droe": roe[row, latest] - lag_roe,
        }
        for i, event in enumerate(events):
            records.append({"code": code, "notice_date": event,
                            "report_date": str(group.index[latest[i]].end_time.date()),
                            **{name: value[i] for name, value in derived.items()}})

    columns = ("sue", "accr", "earn_stab", "rev_stab", "droe")
    if not records:
        return tuple(pd.DataFrame(np.nan, index=close_w.index, columns=close_w.columns) for _ in columns)
    aligned = factor_pit._as_of_records(pd.DataFrame(records), list(close_w.index))
    return tuple(aligned.xs(name, axis=1, level=1).reindex(columns=close_w.columns) for name in columns)


def _factor_frame(panel: pd.DataFrame, factor: str) -> pd.DataFrame:
    """按因子名取计算结果：内置因子走 compute_factors；custom: 走公式引擎；fin_* 走 PIT 财务。

    返回结构同 compute_factors（MultiIndex date,code，含 factor 列 + 前瞻收益 + 上市天数）。
    """
    if factor.startswith("fin_"):
        return _fin_factor_frame(panel, factor)
    if not factor.startswith("custom:"):
        return compute_factors(panel, [factor])
    import factor_expr

    fid = factor.split(":", 1)[1]
    saved = factor_expr.lookup_custom(fid)
    if saved is None:
        raise KeyError(f"自定义因子 {fid} 不存在")
    return _apply_expr(panel, factor, str(saved["expr"]))


def _apply_expr(panel: pd.DataFrame, column: str, expr: str) -> pd.DataFrame:
    """公式因子：静态编译后在字段宽表上执行，对齐回 compute_factors 的结构。

    只认公式文本本身——不做任何落盘或临时因子登记，因此研究执行不依赖共享因子目录。
    """
    import factor_expr

    return _apply_expr_ast(panel, column, factor_expr.compile_expr(expr))


def _apply_expr_ast(panel: pd.DataFrame, column: str, ast: tuple) -> pd.DataFrame:
    import factor_expr

    # 只构建公式真正引用的字段（展开到原始价量列）：不为单条公式分配整个字段库。
    fields = factor_expr.build_fields(panel, factor_expr.field_dependencies(ast))
    needed_fund = factor_expr.referenced_fields(ast) & set(factor_expr.FUND_FIELDS)
    if needed_fund:
        trade_dates = sorted(set(factor_data.load_calendar()) | set(panel["date"]))
        fund_fields = _fund_wide_fields(trade_dates, needed_fund)
    else:
        fund_fields = None
    fw = factor_expr.evaluate(ast, fields, fund_fields)  # date×code 宽表
    base = compute_factors(panel, [])  # 复用前瞻收益/上市天数的计算口径
    base[column] = fw.stack().reindex(base.index)
    return base


def factor_column(definition: dict) -> str:
    """研究定义的因子列名：内置/自定义用原 id，公式用内容哈希（同公式同名，便于比对）。"""
    expr = str(definition.get("expr") or "")
    if not expr:
        return str(definition.get("factor") or "")
    import hashlib

    return "expr_" + hashlib.sha1(expr.encode("utf-8")).hexdigest()[:12]


def factor_frame(panel: pd.DataFrame, definition: dict) -> pd.DataFrame:
    """按定义计算因子帧（含前瞻收益 + 上市天数），列名为 factor_column(definition)。

    定义里 factor 与 expr 恰好一个；expr 是在沙箱解析器里直接执行的公式快照，
    不产生临时因子记录，也不会被并发运行/目录清扫影响。
    """
    factor = str(definition.get("factor") or "")
    expr = str(definition.get("expr") or "")
    if factor and expr:
        raise ValueError("因子定义只能有 factor 或 expr 之一")
    if factor:
        return _factor_frame(panel, factor)
    if not expr:
        raise ValueError("因子定义缺少 factor 或 expr")
    return _apply_expr(panel, factor_column(definition), expr)


def _fund_wide_fields(trade_dates: list[str], names: set[str] | None = None) -> dict[str, pd.DataFrame] | None:
    """PIT 财务字段宽表（fn_*）。财务数据未构建时返回 None（公式引用即报错提示）。"""
    import factor_expr
    import factor_pit

    try:
        pit = factor_pit.as_of_panel(trade_dates)
    except FileNotFoundError:
        return None
    if pit.empty:
        return None
    out: dict[str, pd.DataFrame] = {}
    for fn_name, col in factor_expr.FUND_FIELDS.items():
        if names is not None and fn_name not in names:
            continue
        # columns 为 (code, field) MultiIndex → 取对应指标列组成 date×code 宽表
        try:
            sub = pit.xs(col, axis=1, level=1)
        except KeyError:
            continue
        out[fn_name] = sub
    return out


# ---------------------------------------------------------------------------
# 单因子检验
# ---------------------------------------------------------------------------

def _validate_window(start: str | None, end: str | None) -> None:
    for value in (start, end):
        if value is not None:
            try:
                valid = calendar_date.fromisoformat(value).isoformat() == value
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError("日期需为 YYYY-MM-DD 格式的有效日期")
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期")


def evaluate(factor: str, start: str | None = None, end: str | None = None,
             min_days_listed: int = 60) -> dict:
    """Alphalens 口径单因子检验。返回 IC/分组/换手/分年指标。"""
    _validate_window(start, end)
    # 先算完整历史再截取信号区间，保留预热/上市龄及区间后的收益标签。
    panel = factor_data.load_panel()
    factors = _factor_frame(panel, factor)
    dates = factors.index.get_level_values("date")
    mask = np.ones(len(factors), dtype=bool)
    if start:
        mask &= dates >= start
    if end:
        mask &= dates <= end
    factors = pd.DataFrame(factors.loc[mask])
    input_rows = len(factors)
    input_days = factors.index.get_level_values("date").nunique()
    factors = pd.DataFrame(factors.loc[factors["days_since_list"] >= min_days_listed])
    eligible_rows = len(factors)

    daily = factors.replace([np.inf, -np.inf], np.nan).dropna(subset=[factor])
    if daily.empty:
        raise ValueError(f"因子 {factor} 在给定区间无有效样本")

    ic_series = {}
    rank_ic_series = {}
    grouped = daily.groupby(level="date")
    n_days = 0
    for date, day in grouped:
        day = day.dropna(subset=["ret_fwd_1"])
        if len(day) < MIN_CROSS_SECTION:  # 检查成对有效样本，不是原始因子覆盖
            continue
        f = day[factor].to_numpy()
        ic = _safe_pearson(f, day["ret_fwd_1"].to_numpy())
        rank_ic = _safe_spearman(f, day["ret_fwd_1"].to_numpy())
        if ic is None or rank_ic is None:
            continue  # 两个都有效才记录，防 None 进 dict 后变 NaN 污染序列
        ic_series[date] = ic
        rank_ic_series[date] = rank_ic
        n_days += 1

    if n_days == 0:
        # 空 IC 序列 mean() 出 NaN → JSON 序列化 500；提前给可读错误
        raise ValueError(
            f"因子 {factor} 的有效横截面天数不足（每日需 ≥{MIN_CROSS_SECTION} 只有有效样本；"
            "常见原因：因子覆盖股票太少、区间太短或数据未构建）")

    ic_s = pd.Series(ic_series).sort_index()
    rank_ic_s = pd.Series(rank_ic_series).sort_index()

    # IC 衰减：不同前瞻期的 Rank IC
    decay = {}
    for h in FORWARD_HORIZONS:
        col = f"ret_fwd_{h}"
        vals = {}
        for date, day in daily.groupby(level="date"):
            day = day.dropna(subset=[col])
            if len(day) < MIN_CROSS_SECTION:
                continue
            r = _safe_spearman(day[factor].to_numpy(), day[col].to_numpy())
            if r is not None:
                vals[date] = r
        s = pd.Series(vals)
        decay[h] = {
            "rank_ic_mean": float(s.mean()) if len(s) else None,
            "rank_ic_ir": float(s.mean() / s.std()) if len(s) > 1 and s.std() > 0 else None,
            "days": int(len(s)),
        }

    # 五分组（按因子值分位，Q5=因子值最大）
    quantile_ret, turnover, rank_autocorr = _quantile_stats(daily, factor)
    long_excess, ls_spread = _quantile_vs_benchmark(daily, factor)

    by_year = _ic_by_year(rank_ic_s)
    paired = daily.dropna(subset=["ret_fwd_1"])
    used = paired[paired.index.get_level_values("date").isin(rank_ic_s.index)]
    horizon_returns = {h: _quantile_stats(daily, factor, h)[0] for h in FORWARD_HORIZONS[1:]}

    return {
        "factor": factor,
        "factor_name": factor_name_of(factor),
        "data_version": factor_data.data_version(),
        "start": str(daily.index.get_level_values("date").min()),
        "end": str(daily.index.get_level_values("date").max()),
        "n_days": n_days,
        "avg_coverage": float(np.mean(used.groupby(level="date").size().to_numpy())),
        "sample": {
            "input_rows": input_rows, "eligible_rows": eligible_rows,
            "factor_rows": len(daily), "paired_rows": len(paired), "used_rows": len(used),
            "input_days": int(input_days), "skipped_days": int(input_days - n_days),
            "loss_ratio": 1.0 - len(used) / input_rows if input_rows else None,
            "min_cross_section": MIN_CROSS_SECTION,
        },
        "ic": {
            "ic_mean": float(ic_s.mean()),
            "rank_ic_mean": float(rank_ic_s.mean()),
            "rank_ic_ir": float(rank_ic_s.mean() / rank_ic_s.std()) if rank_ic_s.std() > 0 else None,
            "rank_ic_positive_ratio": float((rank_ic_s > 0).mean()),
            "ic_std": float(np.std(ic_s.to_numpy(), ddof=1)) if len(ic_s) > 1 else None,
        },
        "ic_series": {"dates": list(rank_ic_s.index), "values": [round(float(v), 4) for v in rank_ic_s.values]},
        "ic_decay": decay,
        "quantile_returns": quantile_ret,       # Q1..Q5 → 日均收益（bp）
        "quantile_returns_by_horizon": {1: quantile_ret, **horizon_returns},
        "quantile_turnover": turnover,          # 当期组新增成员数 / 当期组人数
        "long_excess_bp": long_excess,          # Q5 组日均超额（bp）
        "long_short_bp": ls_spread,             # Q5-Q1 日均值（bp）
        "rank_autocorr": rank_autocorr,
        "by_year": by_year,
        "biases": factor_data.BIAS_LABELS,
        "timing_note": "T 日收盘信号，检验标签为 T 收盘→T+h 收盘收益（非可成交收益）；结束日期限定信号日",
        "methodology": {
            "quantiles": 5, "ties": "同值不拆组；不足五个唯一分位边界的日期跳过",
            "turnover": "当期组新增成员数 / 当期组人数，相邻有效日期；非组合交易换手",
            "ic_ir": "日度 IC 均值 / 日度 IC 标准差，未年化；多日前瞻标签重叠",
            "returns": "各期累计前瞻收益的横截面等权、日期等权均值，单位 bp；未扣交易成本",
        },
    }


def _quantile_bins(values: pd.Series) -> pd.Series | None:
    """同值不按股票代码强拆；离散因子无法形成五组时显式跳过。"""
    try:
        return pd.Series(pd.qcut(values, 5, labels=False), index=values.index) + 1
    except ValueError:
        return None


def _quantile_stats(daily: pd.DataFrame, factor: str, horizon: int = 1, *, check=None) -> tuple[dict, dict, float | None]:
    """五分组日均收益（bp）、分组换手、因子秩自相关（相邻两日秩的 Pearson）。"""
    ret_by_q: dict[str, list[float]] = {f"Q{i}": [] for i in range(1, 6)}
    prev_rank: pd.Series | None = None
    prev_bins: pd.Series | None = None
    autocorrs: list[float] = []
    turnover_acc: dict[str, list[float]] = {f"Q{i}": [] for i in range(1, 6)}

    for _date, day in daily.groupby(level="date"):
        if check is not None:
            check()
        if len(day) < MIN_CROSS_SECTION:
            prev_rank, prev_bins = None, None
            continue
        ranks = pd.Series(day[factor]).rank()
        bins = _quantile_bins(pd.Series(day[factor]))
        if bins is None:
            prev_rank, prev_bins = None, None
            continue
        # 分组收益 = 该组股票 T+1 日收益的等权均值（ret_fwd_1 即 T→T+1 收益）。
        # 尾部日无 T+1 收益（全 NaN → mean 为 NaN），跳过防污染整个均值。
        paired_count = day[f"ret_fwd_{horizon}"].notna().sum()
        for q in range(1, 6):
            mask = bins == q
            mean_ret = day.loc[mask.values, f"ret_fwd_{horizon}"].mean()
            if paired_count >= MIN_CROSS_SECTION and mean_ret == mean_ret:  # not NaN
                ret_by_q[f"Q{q}"].append(float(mean_ret) * 1e4)
            if prev_bins is not None:
                cur_members = set(day.index.get_level_values("code")[mask.to_numpy()])
                prev_members = set(prev_bins.index.get_level_values("code")[prev_bins.to_numpy() == q])
                if cur_members:
                    turnover_acc[f"Q{q}"].append(
                        len(cur_members - prev_members) / len(cur_members)
                    )
        if prev_rank is not None:
            # 两组秩的索引是 (date,code) MultiIndex，join 前先去掉 date 层（每天 code 唯一）
            joined = pd.concat([prev_rank.droplevel("date"), ranks.droplevel("date")],
                               axis=1, join="inner").dropna()
            if len(joined) >= MIN_CROSS_SECTION:
                corr = _safe_pearson(joined.iloc[:, 0], joined.iloc[:, 1])
                if corr is not None:
                    autocorrs.append(corr)
        prev_rank, prev_bins = ranks, bins

    quantile_ret = {k: float(np.mean(v)) if v else None for k, v in ret_by_q.items()}
    turnover = {k: float(np.mean(v)) if v else None for k, v in turnover_acc.items()}
    return quantile_ret, turnover, float(np.mean(autocorrs)) if autocorrs else None


def _quantile_vs_benchmark(daily: pd.DataFrame, factor: str) -> tuple[float | None, float | None]:
    """Q5 相对全池等权基准的日均超额（bp）与 Q5-Q1 日均价差（bp）。"""
    excess_list: list[float] = []
    spread_list: list[float] = []
    for _date, day in daily.groupby(level="date"):
        if day["ret_fwd_1"].notna().sum() < MIN_CROSS_SECTION:
            continue
        bins = _quantile_bins(pd.Series(day[factor]))
        if bins is None:
            continue
        bench = day["ret_fwd_1"].mean()
        top = day.loc[(bins == 5).values, "ret_fwd_1"].mean()
        bottom = day.loc[(bins == 1).values, "ret_fwd_1"].mean()
        if bench == bench and top == top and bottom == bottom:  # 跳过尾部无 T+1 收益日
            excess_list.append((top - bench) * 1e4)
            spread_list.append((top - bottom) * 1e4)
    return (float(np.mean(excess_list)) if excess_list else None,
            float(np.mean(spread_list)) if spread_list else None)


def _ic_by_year(rank_ic_s: pd.Series) -> dict[str, dict]:
    if rank_ic_s.empty:
        return {}
    out = {}
    for year, s in rank_ic_s.groupby(rank_ic_s.index.str[:4]):
        out[year] = {
            "rank_ic_mean": float(s.mean()),
            "rank_ic_ir": float(s.mean() / s.std()) if len(s) > 1 and s.std() > 0 else None,
            "days": int(len(s)),
        }
    return out


def _safe_pearson(a, b) -> float | None:
    """成对剔除 NaN/Inf 后再算 Pearson；样本不足或零方差返回 None。

    必须先剔除 NaN：np.std([nan,...]) 是 NaN，`NaN == 0` 为 False 会绕过
    零方差守卫，随后 corrcoef 返回 NaN 并混进 ic_series → JSON 序列化 500
    （典型来源：停牌股的 ret_fwd_1 为 NaN、或尾部日整列无前瞻收益）。
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.shape != b.shape:
        return None
    mask = np.isfinite(a) & np.isfinite(b)
    a, b = a[mask], b[mask]
    if len(a) < 3:
        return None
    sa, sb = np.std(a), np.std(b)
    if not (math.isfinite(sa) and math.isfinite(sb)) or sa == 0 or sb == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _safe_spearman(a, b) -> float | None:
    paired = pd.DataFrame({"a": a, "b": b}).replace([np.inf, -np.inf], np.nan).dropna()
    return _safe_pearson(paired["a"].rank(), paired["b"].rank())
