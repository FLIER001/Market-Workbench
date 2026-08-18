"""因子库 + 单因子检验（Alphalens 口径，pandas 3.0 薄实现）。

时点纪律：因子值取 T 日收盘可算出的量 × T+1 起的前瞻收益，杜绝同日成交。
# ponytail: 检验用 pandas groupby 横截面实现，全 A 20 年单因子一次约 10-20s；要批量扫描再上矩阵化/DuckDB
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import factor_data

FORWARD_HORIZONS = [1, 5, 10, 20]

MIN_CROSS_SECTION = 30  # 横截面样本少于此数的日子跳过（evaluate 与回测候选共用）

_index_ret_cache: pd.Series | None = None


def _index_ret_aligned(df: pd.DataFrame) -> pd.Series | None:
    """市场（沪深300）日收益对齐到面板行序。构建数据里没存指数时返回 None。"""
    global _index_ret_cache
    idx_close = factor_data.load_index_close()
    if idx_close.empty:
        return None
    if _index_ret_cache is None or not _index_ret_cache.index.equals(idx_close.index):
        _index_ret_cache = idx_close.pct_change()
    m = df["date"].map(_index_ret_cache)
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

    close, open_, high, low = df["close"], df["open"], df["high"], df["low"]
    ret_1d = g["close"].transform(lambda s: s.pct_change())
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
    vol_g = g["volume"]

    def max_ret(window: int) -> pd.Series:
        """MAX 效应（Bali et al. 2011 JFE）：近 n 日最大单日收益（彩票偏好，高值未来收益低）。"""
        return ret_1d.groupby(df["code"], sort=False).rolling(window).max().reset_index(level=0, drop=True)

    def dist_52w_high() -> pd.Series:
        """52 周高点距离（George & Hwang 2004 JF）：close/252 日最高收盘 - 1（接近高点→动量强）。"""
        def _d(s: pd.Series) -> pd.Series:
            return s / s.rolling(252, min_periods=120).max() - 1.0
        return close_g.transform(_d)

    def overnight(window: int) -> pd.Series:
        """隔夜收益（Lou-Polk-Skouras 2019 RFS）：Σ open/prev_close-1，情绪/散户主导段。"""
        def _o(o: pd.Series, c: pd.Series) -> pd.Series:
            return (o / c.shift(1) - 1.0).rolling(window).sum()
        return pd.concat([open_, df["close"]], axis=1, keys=["o", "c"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _o(x["o"], x["c"])).reset_index(level=0, drop=True).sort_index()

    def intraday(window: int) -> pd.Series:
        """日内收益（Lou-Polk-Skouras 2019）：Σ close/open-1，机构主导段（A股 T+1 下的反向锚）。"""
        def _i(c: pd.Series, o: pd.Series) -> pd.Series:
            return (c / o - 1.0).rolling(window).sum()
        return pd.concat([df["close"], open_], axis=1, keys=["c", "o"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _i(x["c"], x["o"])).reset_index(level=0, drop=True).sort_index()

    def frog(window: int, quant: float = 0.2) -> pd.Series:
        """青蛙效应（Wu et al. 2022 JFE）：近 n 日收益在 n*2 日分布中的分位（第一 Pond 跳跃）。"""
        def _f(s: pd.Series) -> pd.Series:
            r = s.pct_change()
            long_win = r.rolling(window * 2, min_periods=window).quantile(quant)
            return (r.rolling(window).sum() < long_win).astype(float)
        return close_g.transform(_f)

    def abnormal_turnover(window: int) -> pd.Series:
        """异常换手（Datar-Naik-Radcliffe 1998 / Liu 2006 变体）：近 n 日均成交额 / 250 日均成交额。"""
        short_m = amount.groupby(df["code"], sort=False).rolling(window).mean().reset_index(level=0, drop=True)
        long_m = amount.groupby(df["code"], sort=False).rolling(250, min_periods=120).mean().reset_index(level=0, drop=True)
        return short_m / long_m

    def hl_spread(window: int) -> pd.Series:
        """Parkinson 高低价波动率（Corwin-Schultz 前置）：σ² = Σ(ln(high/low))²/(4ln2·n)。"""
        def _p(h: pd.Series, l: pd.Series) -> pd.Series:
            return ((np.log(h / l) ** 2).rolling(window).sum() / (4 * np.log(2) * window)) ** 0.5
        return pd.concat([high, low], axis=1, keys=["h", "l"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _p(x["h"], x["l"])).reset_index(level=0, drop=True).sort_index()

    def corr_market(window: int) -> pd.Series:
        """与市场相关（Malkiel-Xu 2006 / Herskovic et al. 2016 JF）：个股收益与指数收益的滚动相关。"""
        idx_ret = _index_ret_aligned(df)
        if idx_ret is None:
            return pd.Series(np.nan, index=df.index)
        def _c(r: pd.Series, m: pd.Series) -> pd.Series:
            return r.rolling(window).corr(m)
        return pd.concat([ret_1d, idx_ret], axis=1, keys=["r", "m"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _c(x["r"], x["m"])).reset_index(level=0, drop=True).sort_index()

    def beta_market(window: int) -> pd.Series:
        """市场贝塔（Frazzini-Pedersen 2014 JFE BAB）：低贝塔异象，低 β 风险调整后收益更高。"""
        idx_ret = _index_ret_aligned(df)
        if idx_ret is None:
            return pd.Series(np.nan, index=df.index)
        def _b(r: pd.Series, m: pd.Series) -> pd.Series:
            var = m.rolling(window).var()
            cov = r.rolling(window).cov(m)
            return cov / var.where(var > 0)
        return pd.concat([ret_1d, idx_ret], axis=1, keys=["r", "m"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _b(x["r"], x["m"])).reset_index(level=0, drop=True).sort_index()

    def idio_vol(window: int) -> pd.Series:
        """特质波动（Ang et al. 2006 JF）：对市场回归的残差 std（高特质波动→低收益，谜题）。"""
        idx_ret = _index_ret_aligned(df)
        if idx_ret is None:
            return pd.Series(np.nan, index=df.index)
        def _iv(r: pd.Series, m: pd.Series) -> pd.Series:
            beta = r.rolling(window).cov(m) / m.rolling(window).var().where(lambda v: v > 0)
            resid = r - beta * m
            return resid.rolling(window).std()
        return pd.concat([ret_1d, idx_ret], axis=1, keys=["r", "m"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _iv(x["r"], x["m"])).reset_index(level=0, drop=True).sort_index()

    def coskew(window: int) -> pd.Series:
        """协偏度（Harvey-Siddique 2000 JF）：个股与市场收益的滚动协偏（系统性偏度定价）。"""
        idx_ret = _index_ret_aligned(df)
        if idx_ret is None:
            return pd.Series(np.nan, index=df.index)
        def _cs(r: pd.Series, m: pd.Series) -> pd.Series:
            m2 = (m ** 2).rolling(window).mean()
            m3 = (m ** 3).rolling(window).mean()
            rm2 = (r * m ** 2).rolling(window).mean()
            var = m.rolling(window).var()
            denom = (m3 - m2 ** 2).where(lambda v: v != 0)
            return (rm2 - r.rolling(window).mean() * m2) / denom
        return pd.concat([ret_1d, idx_ret], axis=1, keys=["r", "m"]).groupby(df["code"], sort=False) \
            .apply(lambda x: _cs(x["r"], x["m"])).reset_index(level=0, drop=True).sort_index()

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
        return ret_1d.groupby(df["code"], sort=False).rolling(window).skew().reset_index(level=0, drop=True)

    def ret_kurt(window: int) -> pd.Series:
        """收益峰度（尾部风险，Dittmar 2002 二阶偏好）。"""
        return ret_1d.groupby(df["code"], sort=False).rolling(window).kurt().reset_index(level=0, drop=True)

    def max_min_gap(window: int) -> pd.Series:
        """极值差（Bali et al. MAX-MIN 组合口径）：近 n 日最大收益 - 最小收益。"""
        rmax = ret_1d.groupby(df["code"], sort=False).rolling(window).max().reset_index(level=0, drop=True)
        rmin = ret_1d.groupby(df["code"], sort=False).rolling(window).min().reset_index(level=0, drop=True)
        return rmax - rmin

    def mom_quality(window: int) -> pd.Series:
        """动量质量（Israel-Moskowitz 2013 JF「信息离散度」：平滑上涨 > 忽上忽下）。"""
        def _q(s: pd.Series) -> pd.Series:
            r = s.pct_change()
            sign_sum = np.sign(r).rolling(window).sum()
            abs_sum = r.abs().rolling(window).sum().where(lambda v: v > 0)
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
        return v.groupby(df["code"], sort=False).rolling(window).std().reset_index(level=0, drop=True)

    def max_corr(window: int) -> pd.Series:
        """同上 corr_market 的短窗口版（15 日）：短期联动（涨跌停潮中的传导）。"""
        return corr_market(window)

    all_factors = {
        # 动量/反转
        "mom20": mom(5, 20),
        "mom60": mom(5, 60),
        "mom120": mom(5, 120),
        "rev5": g["close"].transform(lambda s: -(s / s.shift(5) - 1.0)),  # 5 日反转：跌得多 → 值大
        "dist_52w_high": dist_52w_high(),
        "frog20": frog(20),
        "mom_quality60": mom_quality(60),
        "trend60": trend_strength(60),
        # 波动/风险
        "vol20": vol(20),
        "vol60": vol(60),
        "downside_vol20": downside_vol(20),
        "hl_vol20": hl_spread(20),
        "idio_vol60": idio_vol(60),
        "vol_of_vol20": vol_of_vol(20),
        "ret_skew60": ret_skew(60),
        "ret_kurt60": ret_kurt(60),
        "max_ret20": max_ret(20),
        "maxmin20": max_min_gap(20),
        # 市场结构
        "beta60": beta_market(60),
        "corr_market60": corr_market(60),
        "coskew60": coskew(60),
        # 流动性/量
        "amihud20": amihud(20),
        "illiq_shock": illiq_shock(),
        "abnormal_turnover20": abnormal_turnover(20),
        "amount_stab20": amount_stability(20),
        "turnover_stability": turnover_stab(),
        "volume_trend20": volume_trend(20),
        # 日内结构
        "overnight20": overnight(20),
        "intraday20": intraday(20),
    }

    out = df[["date", "code"]].copy()
    for name in (names or list(all_factors)):
        out[name] = all_factors[name]
    out["ret_fwd_1"] = g["close"].transform(lambda s: s.shift(-1) / s - 1.0)
    for h in FORWARD_HORIZONS[1:]:
        out[f"ret_fwd_{h}"] = g["close"].transform(lambda s, h=h: s.shift(-h) / s - 1.0)
    # T+1 起的收益序列（组合回测与分组收益按日聚合用）
    out["ret_next_1"] = g["close"].transform(lambda s: s.shift(-1) / s - 1.0)
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
        saved = {f["id"]: f for f in factor_expr.list_custom()}
        return saved.get(fid, {}).get("name", fid)
    if factor.startswith("fin_"):
        return FIN_FACTOR_META.get(factor, factor)
    return FACTOR_META.get(factor, factor)


# ---------------------------------------------------------------------------
# PIT 财务因子（业绩报表口径派生；按公告日可见，杜绝前视）
# ---------------------------------------------------------------------------

FIN_FACTOR_META = {
    "fin_ep": "EP 盈收益率 = TTM 净利/市值（Fama-French 1992 价值）",
    "fin_ep_ocf": "OCF 盈利率 = TTM 每股经营现金流/收盘价（更抗操纵的便宜）",
    "fin_sue": "SUE 标准化未预期盈余（Bernard-Thomas 1989 盈余动量）",
    "fin_accruals": "应计质量代理 = (净利-经营现金流TTM)/总资产(净利代理)",
    "fin_gross_margin": "毛利率（Novy-Marx 2013 质量 proxy）",
    "fin_roe_ttm": "ROE TTM（质量因子，近四季加总）",
    "fin_rev_grow_ttm": "营收 TTM 同比增长（成长）",
    "fin_profit_grow_ttm": "净利 TTM 同比增长（成长）",
    "fin_earnings_stab": "盈利稳定性 = 近 8 季单季 EPS 的 均值/std（质量）",
    "fin_rev_stab": "营收稳定性 = 近 8 季单季营收 均值/std（质量）",
    "fin_delta_roe": "ROE 环比改善（质量动量）",
    "fin_ocf_to_profit": "经营现金流/净利（应计质量，高=利润含金量高）",
}


def _fin_factor_frame(panel: pd.DataFrame, factor: str) -> pd.DataFrame:
    """PIT 财务因子：以 as_of 宽表为基础派生，对齐回 compute_factors 的输出结构。"""
    import factor_pit

    base = compute_factors(panel, [])
    trade_dates = base.index.get_level_values("date").unique().tolist()
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
            return pit.xs(name, axis=1, level=1).reindex(close_w.index)[close_w.columns]
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
    out["fin_roe_ttm"] = roe  # 单期 ROE 近似（加权 ROE 本身已年化倾向）
    out["fin_rev_grow_ttm"] = rev_yoy
    out["fin_profit_grow_ttm"] = profit_yoy
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
    """从 PIT 长表做单季化 + SUE/应计/稳定性/ΔROE。

    业绩报表为累计口径：单季 = 本期累计 - 上期累计（Q1 直接用）。not_yet_q4 陷阱：年报→一季报
    跨年时上期累计取 0（年报重置）。字段无总资产/股本，分母用近 4 季均收盘价×股数不可得——
    应计用「单季净利-单季经营现金流」除以股价（每股价差口径），稳定性用单季序列的 CV。
    """
    fund = fund.sort_values(["code", "report_date"]).copy()

    def quarterize(g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy()
        g["prev_rd"] = g["report_date"].shift(1)
        # 累计口径：同一年内本期累计-上期累计；跨年（12-31 → 03-31）时 Q1 单季 = 累计本身
        same_year = g["report_date"].str[:4] == g["prev_rd"].str[:4].fillna("")
        for col in ("net_profit", "revenue", "ocf_ps", "eps"):
            q_col = f"q_{col}"
            g[q_col] = g[col]
            prev = g[col].shift(1)
            g.loc[same_year, q_col] = g.loc[same_year, col] - prev[same_year]
        g["roe_prev"] = g["roe"].shift(1)
        g["eps_std8"] = g["q_eps"].rolling(8, min_periods=6).std()
        g["eps_mean8"] = g["q_eps"].rolling(8, min_periods=6).mean()
        g["rev_std8"] = g["q_revenue"].rolling(8, min_periods=6).std()
        g["rev_mean8"] = g["q_revenue"].rolling(8, min_periods=6).mean()
        # SUE（Bernard-Thomas）：单季 EPS - 4 期前单季 EPS，除以单季 EPS 的 8 季 std
        g["sue"] = (g["q_eps"] - g["q_eps"].shift(4)) / g["eps_std8"].where(g["eps_std8"] > 1e-9)
        # 应计（每股价差口径）：单季净利/股 - 单季经营现金流。无股本 → 用 eps 与 ocf_ps 的差近似
        g["accr"] = g["q_eps"] - g["q_ocf_ps"]
        return g

    parts = {c: quarterize(g) for c, g in fund.groupby("code", sort=False)}
    fund_q = pd.concat(parts.values())

    # 稳定性 / ΔROE 派生列
    fund_q["earn_stab"] = fund_q["eps_mean8"] / fund_q["eps_std8"].where(fund_q["eps_std8"] > 1e-9)
    fund_q["rev_stab"] = fund_q["rev_mean8"] / fund_q["rev_std8"].where(fund_q["rev_std8"] > 1e-9)
    fund_q["droe"] = fund_q["roe"] - fund_q["roe_prev"]

    def to_wide(col: str) -> pd.DataFrame:
        """报告期序列 → 公告日可见的交易日宽表（向量化：所有股票一次性 asof）。"""
        sub = fund_q.dropna(subset=["notice_date"]) \
            .sort_values("notice_date") \
            .drop_duplicates(subset=["code", "notice_date"], keep="last")
        # 长表 → (公告日, code) 透视，再按公告日对交易日 asof（每行取最近已公告值）
        pv = sub.pivot_table(index="notice_date", columns="code", values=col, aggfunc="last")
        pv.index = pd.DatetimeIndex(pv.index)
        dates_idx = pd.DatetimeIndex(close_w.index)
        # pandas 3: DataFrame 无直接 asof；逐列 searchsorted 仍比逐股 groupby 快百倍
        pos = pv.index.searchsorted(dates_idx, side="right") - 1
        valid = pos >= 0
        if pv.empty:
            return pd.DataFrame(np.nan, index=close_w.index, columns=close_w.columns)
        safe_pos = np.clip(pos, 0, len(pv.index) - 1)
        aligned = pv.to_numpy()[safe_pos].astype(float)
        aligned[~valid] = np.nan
        wide = pd.DataFrame(aligned, index=close_w.index, columns=pv.columns)
        return wide.reindex(index=close_w.index, columns=close_w.columns)

    sue = to_wide("sue")
    accr = to_wide("accr")
    return sue, accr, to_wide("earn_stab"), to_wide("rev_stab"), to_wide("droe")


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
    saved = {f["id"]: f for f in factor_expr.list_custom()}
    if fid not in saved:
        raise KeyError(f"自定义因子 {fid} 不存在")
    ast = factor_expr.compile_expr(saved[fid]["expr"])
    fields = factor_expr.build_fields(panel)
    fund_fields = _fund_wide_fields(fields["close"].index.tolist())
    fw = factor_expr.evaluate(ast, fields, fund_fields)  # date×code 宽表
    base = compute_factors(panel, [])  # 复用前瞻收益/上市天数的计算口径
    base[factor] = fw.stack().reindex(base.index)
    return base


def _fund_wide_fields(trade_dates: list[str]) -> dict[str, pd.DataFrame] | None:
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

def evaluate(factor: str, start: str | None = None, end: str | None = None,
             min_days_listed: int = 60) -> dict:
    """Alphalens 口径单因子检验。返回 IC/分组/换手/分年指标。"""
    panel = factor_data.load_panel(start, end)
    factors = _factor_frame(panel, factor)
    factors = factors[factors["days_since_list"] >= min_days_listed]

    daily = factors.dropna(subset=[factor])
    if daily.empty:
        raise ValueError(f"因子 {factor} 在给定区间无有效样本")

    ic_series = {}
    rank_ic_series = {}
    grouped = daily.groupby(level="date")
    n_days = 0
    for date, day in grouped:
        if len(day) < MIN_CROSS_SECTION:  # 横截面样本太少，跳过
            continue
        f = day[factor].to_numpy()
        ic = _safe_pearson(f, day["ret_fwd_1"].to_numpy())
        rank_ic = _safe_spearman(f, day["ret_fwd_1"].to_numpy())
        if ic is None:
            continue
        ic_series[date] = ic
        rank_ic_series[date] = rank_ic
        n_days += 1

    ic_s = pd.Series(ic_series).sort_index()
    rank_ic_s = pd.Series(rank_ic_series).sort_index()

    # IC 衰减：不同前瞻期的 Rank IC
    decay = {}
    for h in FORWARD_HORIZONS:
        col = f"ret_fwd_{h}"
        vals = {}
        for date, day in daily.groupby(level="date"):
            if len(day) < MIN_CROSS_SECTION:
                continue
            r = _safe_spearman(day[factor].to_numpy(), day[col].to_numpy())
            if r is not None:
                vals[date] = r
        s = pd.Series(vals)
        decay[h] = {
            "rank_ic_mean": float(s.mean()),
            "rank_ic_ir": float(s.mean() / s.std()) if len(s) > 1 and s.std() > 0 else None,
        }

    # 五分组（按因子值分位，Q5=因子值最大）
    quantile_ret, turnover, rank_autocorr = _quantile_stats(daily, factor)
    long_excess, ls_spread = _quantile_vs_benchmark(daily, factor)

    by_year = _ic_by_year(rank_ic_s)

    return {
        "factor": factor,
        "factor_name": factor_name_of(factor),
        "data_version": factor_data.data_version(),
        "start": str(daily.index.get_level_values("date").min()),
        "end": str(daily.index.get_level_values("date").max()),
        "n_days": n_days,
        "avg_coverage": float(daily.groupby(level="date").size().mean()),
        "ic": {
            "ic_mean": float(ic_s.mean()),
            "rank_ic_mean": float(rank_ic_s.mean()),
            "rank_ic_ir": float(rank_ic_s.mean() / rank_ic_s.std()) if rank_ic_s.std() > 0 else None,
            "rank_ic_positive_ratio": float((rank_ic_s > 0).mean()),
            "ic_std": float(rank_ic_s.std()),
        },
        "ic_series": {"dates": list(rank_ic_s.index), "values": [round(float(v), 4) for v in rank_ic_s.values]},
        "ic_decay": decay,
        "quantile_returns": quantile_ret,       # Q1..Q5 → 日均收益（bp）
        "quantile_turnover": turnover,          # Q1..Q5 → 日均换手
        "long_excess_bp": long_excess,          # Q5 组日均超额（bp）
        "long_short_bp": ls_spread,             # Q5-Q1 日均值（bp）
        "rank_autocorr": rank_autocorr,
        "by_year": by_year,
        "biases": factor_data.BIAS_LABELS,
        "timing_note": "因子值取 T 日收盘，配对 T+1 起前瞻收益",
    }


def _quantile_stats(daily: pd.DataFrame, factor: str) -> tuple[dict, dict, float]:
    """五分组日均收益（bp）、分组换手、因子秩自相关（相邻两日秩的 Pearson）。"""
    ret_by_q: dict[str, list[float]] = {f"Q{i}": [] for i in range(1, 6)}
    rank_series: dict[str, pd.Series] = {}
    prev_rank: pd.Series | None = None
    prev_bins: pd.Series | None = None
    autocorrs: list[float] = []
    turnover_acc: dict[str, list[float]] = {f"Q{i}": [] for i in range(1, 6)}

    for date, day in daily.groupby(level="date"):
        if len(day) < MIN_CROSS_SECTION:
            prev_rank, prev_bins = None, None
            continue
        ranks = day[factor].rank()
        rank_series[date] = ranks
        bins = pd.qcut(day[factor].rank(method="first"), 5, labels=False) + 1  # 1..5
        # 分组收益 = 该组股票 T+1 日收益的等权均值（ret_fwd_1 即 T→T+1 收益）。
        # 尾部日无 T+1 收益（全 NaN → mean 为 NaN），跳过防污染整个均值。
        for q in range(1, 6):
            mask = bins == q
            mean_ret = day.loc[mask.values, "ret_fwd_1"].mean()
            if mean_ret == mean_ret:  # not NaN
                ret_by_q[f"Q{q}"].append(float(mean_ret) * 1e4)
            if prev_bins is not None:
                cur_members = set(bins.index[mask.values].get_level_values("code"))
                prev_members = set(prev_bins.index[prev_bins.values == q].get_level_values("code"))
                union = cur_members | prev_members
                if union:
                    turnover_acc[f"Q{q}"].append(
                        1.0 - len(cur_members & prev_members) / len(union)
                    )
        if prev_rank is not None:
            # 两组秩的索引是 (date,code) MultiIndex，join 前先去掉 date 层（每天 code 唯一）
            joined = pd.concat([prev_rank.droplevel("date"), ranks.droplevel("date")],
                               axis=1, join="inner").dropna()
            if len(joined) > MIN_CROSS_SECTION:
                autocorrs.append(_safe_pearson(joined.iloc[:, 0], joined.iloc[:, 1]) or 0.0)
        prev_rank, prev_bins = ranks, bins

    quantile_ret = {k: float(np.mean(v)) if v else None for k, v in ret_by_q.items()}
    turnover = {k: float(np.mean(v)) if v else None for k, v in turnover_acc.items()}
    return quantile_ret, turnover, float(np.mean(autocorrs)) if autocorrs else None


def _quantile_vs_benchmark(daily: pd.DataFrame, factor: str) -> tuple[float, float]:
    """Q5 相对全池等权基准的日均超额（bp）与 Q5-Q1 日均价差（bp）。"""
    excess_list: list[float] = []
    spread_list: list[float] = []
    for date, day in daily.groupby(level="date"):
        if len(day) < MIN_CROSS_SECTION:
            continue
        bins = pd.qcut(day[factor].rank(method="first"), 5, labels=False) + 1
        bench = day["ret_fwd_1"].mean()
        top = day.loc[(bins == 5).values, "ret_fwd_1"].mean()
        bottom = day.loc[(bins == 1).values, "ret_fwd_1"].mean()
        if bench == bench and top == top and bottom == bottom:  # 跳过尾部无 T+1 收益日
            excess_list.append((top - bench) * 1e4)
            spread_list.append((top - bottom) * 1e4)
    return float(np.mean(excess_list)), float(np.mean(spread_list))


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
    if len(a) < 3:
        return None
    sa, sb = np.std(a), np.std(b)
    if sa == 0 or sb == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _safe_spearman(a, b) -> float | None:
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    return _safe_pearson(ra, rb)
