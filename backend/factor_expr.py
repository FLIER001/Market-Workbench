"""因子公式引擎：字段 + 时序/截面算子的小型表达式语言（解析执行，无 eval）。

语法示例：
  -close/ts_delta(close,5)                          # 5 日反转（价差口径）
  cs_rank(ts_mean(amount,20)/ts_mean(amount,120))   # 成交额放大（截面分位）
  -cs_zscore(ts_std(ret,20))                        # 低波动（截面标准化）
字段（腾讯前复权日线派生）：close/open/high/low/volume/amount/vwap/ret
时序算子（按个股逐列）：ts_mean/ts_std/ts_sum/ts_max/ts_min(x,n)、ts_delta(x,n)、
  ts_corr(x,y,n)、滞后写法 x[n]
截面算子（按日逐行）：cs_rank(x)、cs_zscore(x)
标量函数：abs/log/sign/sqrt；四则 + - * /、负号、括号

执行口径：宽表（date×code）逐列滚动 = 个股时序，逐行 = 横截面；窗口 min_periods=n
（严格窗口，与内置因子一致）。公式只用 T 日及以前数据，T+1 起配前瞻收益。
# ponytail: 小语言只覆盖价量因子；要 DSL 化/多因子合成/更多字段（财务）再扩
"""
from __future__ import annotations

import json
import os
import re
import time

import numpy as np
import pandas as pd

import factor_data

MAX_EXPR_LEN = 300
MAX_NODES = 80
MAX_WINDOW = 500

FIELDS: dict[str, str] = {
    # 原始 OHLCV
    "close": "前复权收盘价",
    "open": "前复权开盘价",
    "high": "前复权最高价",
    "low": "前复权最低价",
    "volume": "成交量（股）",
    "amount": "成交额（元）",
    "vwap": "成交均价 = amount/volume",
    "ret": "日收益率 = close/close[-1]-1",
    # K 线形态结构（Alpha158 KMID 家族；分母为 close，量纲统一）
    "kmid": "K 线中部 = (close-open)/close",
    "klen": "K 线长度 = (high-low)/close",
    "kup": "上影线 = (high-max(open,close))/close",
    "klow": "下影线 = (min(open,close)-low)/close",
    "ksft": "实体方向 = 2*(close-open)/(high-low+1e-12)",
    # 当日相对位置（Alpha158 ROC/CLOSE2 家族）
    "open0": "开盘相对昨收 = open/close[1]",
    "high0": "最高相对昨收 = high/close[1]",
    "low0": "最低相对昨收 = low/close[1]",
    "vwap0": "均价相对昨收 = vwap/close[1]",
    # 多周期动量（Alpha158 ROC，比手写 close/close[n]-1 更直观）
    "ret5": "5 日收益 = close/close[5]-1",
    "ret10": "10 日收益",
    "ret20": "20 日收益",
    "ret60": "60 日收益",
    "ret120": "120 日收益",
    # 多周期量比（Alpha158 VROC/VSTD 家族）
    "vol_ratio5": "5 日量比 = volume/ts_mean(volume,5)",
    "vol_ratio20": "20 日量比 = volume/ts_mean(volume,20)",
    "turnover20": "20 日日均成交额（元）",
    # PIT 财务字段（业绩报表口径；按公告日对齐——NOTICE_DATE ≤ T 的最新报告期）
    "fn_revenue": "营业总收入（元，最新已公告报告期）",
    "fn_net_profit": "归母净利润（元）",
    "fn_eps": "每股收益 EPS",
    "fn_eps_deducted": "扣非每股收益",
    "fn_bps": "每股净资产",
    "fn_roe": "加权平均 ROE（%，单季口径）",
    "fn_rev_yoy": "营收同比增长（%）",
    "fn_profit_yoy": "净利润同比增长（%）",
    "fn_gross_margin": "销售毛利率（%）",
    "fn_ocf_ps": "每股经营现金流",
}

# 财务字段：因子构建时走 PIT 对齐（factors._factor_frame 注入 fn_ 前缀宽表）
FUND_FIELDS = {
    "fn_revenue": "revenue", "fn_net_profit": "net_profit", "fn_eps": "eps",
    "fn_eps_deducted": "eps_deducted", "fn_bps": "bps", "fn_roe": "roe",
    "fn_rev_yoy": "rev_yoy", "fn_profit_yoy": "profit_yoy",
    "fn_gross_margin": "gross_margin", "fn_ocf_ps": "ocf_ps",
}

TS_FUNCS = {
    "ts_mean", "ts_std", "ts_sum", "ts_max", "ts_min", "ts_delta", "ts_corr",
    "ts_rank", "ts_skew", "ts_kurt", "ts_cov", "ts_scale", "ts_rsv",
}
CS_FUNCS = {"cs_rank", "cs_zscore"}
SCALAR_FUNCS = {"abs", "log", "sign", "sqrt"}

OPS_DOC = [
    {"name": "ts_mean(x,n) / ts_std / ts_sum / ts_max / ts_min", "desc": "按个股 n 日滚动均值/标准差/和/最大/最小"},
    {"name": "ts_delta(x,n)", "desc": "x - x[n]，n 日变化"},
    {"name": "x[n]", "desc": "x 滞后 n 日（如 close[1] 昨收）"},
    {"name": "ts_corr(x,y,n) / ts_cov(x,y,n)", "desc": "x 与 y 的 n 日滚动相关系数 / 协方差"},
    {"name": "ts_rank(x,n)", "desc": "x 在最近 n 日中的分位（0-1）"},
    {"name": "ts_skew(x,n) / ts_kurt(x,n)", "desc": "n 日滚动偏度 / 超额峰度（分布形态）"},
    {"name": "ts_scale(x,n)", "desc": "(x-min)/(max-min)，n 日窗口内归一位置"},
    {"name": "ts_rsv(x,n)", "desc": "随机指标 RSV = (x-低点)/(高点-低点)，n 日"},
    {"name": "cs_rank(x) / cs_zscore(x)", "desc": "按日横截面排名 / z-score 标准化"},
    {"name": "abs / log / sign / sqrt", "desc": "标量函数（log/sqrt 对非正数为缺失）"},
    {"name": "+ - * /（负号、括号）", "desc": "四则运算；除以 0 为缺失"},
]

# ---------------------------------------------------------------------------
# 词法 + 语法（递归下降，AST 为嵌套 tuple）
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|([+\-*/()\[\],]))"
)


def tokenize(src: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            raise ValueError(f"公式在位置 {pos} 附近有无法识别的字符：{src[pos:pos + 8]!r}")
        num, name, sym = m.groups()
        if num:
            tokens.append(("num", num))
        elif name:
            tokens.append(("id", name))
        elif sym:
            tokens.append(("sym", sym))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.i = 0
        self.nodes = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise ValueError("公式不完整（意外结束）")
        self.i += 1
        return tok

    def _bump(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ValueError(f"公式过于复杂（超过 {MAX_NODES} 个节点）")

    def parse(self):
        node = self.expr()
        if self.peek() is not None:
            raise ValueError(f"公式在 {self.tokens[self.i][1]!r} 附近有多余内容")
        return node

    def expr(self) -> tuple:
        node = self.term()
        while (tok := self.peek()) and tok == ("sym", "+") or (tok := self.peek()) and tok == ("sym", "-"):
            op = self.next()[1]
            node = self._bump_or(("bin", op, node, self.term()))
        return node

    def term(self) -> tuple:
        node = self.unary()
        while (tok := self.peek()) and tok[0] == "sym" and tok[1] in ("*", "/"):
            op = self.next()[1]
            node = self._bump_or(("bin", op, node, self.unary()))
        return node

    def unary(self) -> tuple:
        tok = self.peek()
        if tok == ("sym", "-"):
            self.next()
            return self._bump_or(("neg", self.unary()))
        if tok == ("sym", "+"):
            self.next()
            return self.unary()
        return self.postfix()

    def postfix(self) -> tuple:
        node = self.primary()
        while (tok := self.peek()) and tok == ("sym", "["):
            self.next()
            lag_tok = self.next()
            if lag_tok[0] != "num" or not float(lag_tok[1]).is_integer() or int(lag_tok[1]) < 1:
                raise ValueError("滞后 [n] 里的 n 必须是正整数")
            close_tok = self.next()
            if close_tok != ("sym", "]"):
                raise ValueError("滞后写法缺少右括号 ]")
            node = self._bump_or(("lag", node, int(lag_tok[1])))
        return node

    def primary(self) -> tuple:
        kind, text = self.next()
        if kind == "num":
            return self._bump_or(("num", float(text)))
        if kind == "sym" and text == "(":
            node = self.expr()
            if self.next() != ("sym", ")"):
                raise ValueError("括号不匹配")
            return node
        if kind == "id":
            if (self.peek() == ("sym", "(")):
                return self._call(text)
            if text not in FIELDS:
                raise ValueError(f"未知字段 {text!r}（可用：{'、'.join(FIELDS)}）")
            return self._bump_or(("field", text))
        raise ValueError(f"意外的符号 {text!r}")

    def _call(self, name: str) -> tuple:
        self.next()  # (
        args: list[tuple] = []
        if self.peek() != ("sym", ")"):
            args.append(self.expr())
            while self.peek() == ("sym", ","):
                self.next()
                args.append(self.expr())
        if self.next() != ("sym", ")"):
            raise ValueError(f"{name}(...) 缺少右括号")

        known = TS_FUNCS | CS_FUNCS | SCALAR_FUNCS
        if name not in known:
            raise ValueError(f"未知函数 {name}（可用：{'、'.join(sorted(known))}）")
        if name in TS_FUNCS:
            want = 3 if name in ("ts_corr", "ts_cov") else 2
            if len(args) != want:
                raise ValueError(f"{name} 需要 {want - 1} 个序列参数和一个窗口 n")
            if args[-1][0] != "num" or not float(args[-1][1]).is_integer() or not (1 <= int(args[-1][1]) <= MAX_WINDOW):
                raise ValueError(f"{name} 的窗口 n 必须是 1-{MAX_WINDOW} 的整数")
        elif name in SCALAR_FUNCS and len(args) != 1:
            raise ValueError(f"{name} 只接受 1 个参数")
        elif name in CS_FUNCS and len(args) != 1:
            raise ValueError(f"{name} 只接受 1 个参数")
        return self._bump_or(("call", name, args))

    def _bump_or(self, node: tuple) -> tuple:
        self._bump()
        return node


def compile_expr(expr: str) -> tuple:
    """解析 + 静态校验，返回 AST。任何非法输入抛 ValueError（不执行任何代码）。"""
    if not expr or not expr.strip():
        raise ValueError("公式为空")
    if len(expr) > MAX_EXPR_LEN:
        raise ValueError(f"公式过长（>{MAX_EXPR_LEN} 字符）")
    if "import" in expr or "exec" in expr or "eval" in expr or "__" in expr:
        raise ValueError("公式里不允许出现 import/exec/eval/__")
    return _Parser(tokenize(expr)).parse()


# ---------------------------------------------------------------------------
# 执行（宽表：index=date, columns=code）
# ---------------------------------------------------------------------------

def build_fields(panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """日线面板 → 字段宽表字典（价量层；财务/宏观字段无 point-in-time 历史，不纳入）。"""
    wide: dict[str, pd.DataFrame] = {}
    for f in ("close", "open", "high", "low", "volume", "amount"):
        wide[f] = panel.pivot(index="date", columns="code", values=f).sort_index()
    close, open_, high, low = wide["close"], wide["open"], wide["high"], wide["low"]
    vol = wide["volume"]
    wide["vwap"] = wide["amount"] / vol.replace(0, np.nan)
    wide["ret"] = close.pct_change()

    # K 线形态（Alpha158 KMID 家族）
    wide["kmid"] = (close - open_) / close
    wide["klen"] = (high - low) / close
    wide["kup"] = (high - open_.combine(close, np.maximum)) / close
    wide["klow"] = (open_.combine(close, np.minimum) - low) / close
    wide["ksft"] = 2 * (close - open_) / (high - low + 1e-12)

    # 当日相对昨收位置
    prev = close.shift(1)
    wide["open0"] = open_ / prev
    wide["high0"] = high / prev
    wide["low0"] = low / prev
    wide["vwap0"] = wide["vwap"] / prev

    # 多周期动量与量比
    for n in (5, 10, 20, 60, 120):
        wide[f"ret{n}"] = close / close.shift(n) - 1
    wide["vol_ratio5"] = vol / vol.rolling(5).mean()
    wide["vol_ratio20"] = vol / vol.rolling(20).mean()
    wide["turnover20"] = wide["amount"].rolling(20).mean()
    return wide


def evaluate(ast: tuple, fields: dict[str, pd.DataFrame], fund_fields: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """在字段宽表上执行 AST，返回 date×code 的因子宽表。

    fund_fields：PIT 财务字段宽表（fn_* 前缀），由调用方按公告日对齐后注入；未注入时
    公式引用 fn_* 字段会报错（提示先构建财务数据），不会静默给 NaN。
    """
    w = dict(fields)
    if fund_fields:
        w.update(fund_fields)
    needed_fund = _collect_fields(ast) & set(FUND_FIELDS)
    if needed_fund and not fund_fields:
        raise ValueError(f"公式用到财务字段 {sorted(needed_fund)}：需先构建 PIT 财务数据")
    out = _eval(ast, w)
    if not isinstance(out, pd.DataFrame):
        # 纯标量表达式（如 1+2、abs(5)、1/0）→ 没有任何字段引用，无意义
        raise ValueError("公式结果不含任何字段（全是常数）")
    return out.replace([np.inf, -np.inf], np.nan)


def _collect_fields(node: tuple) -> set[str]:
    """递归收集 AST 里引用的字段名。"""
    kind = node[0]
    if kind == "field":
        return {node[1]}
    if kind in ("num",):
        return set()
    if kind == "neg":
        return _collect_fields(node[1])
    if kind == "bin":
        return _collect_fields(node[2]) | _collect_fields(node[3])
    if kind == "lag":
        return _collect_fields(node[1])
    if kind == "call":
        out: set[str] = set()
        for a in node[2]:
            out |= _collect_fields(a)
        return out
    return set()


def _eval(node: tuple, w: dict[str, pd.DataFrame]):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "field":
        return w[node[1]]
    if kind == "neg":
        return -_eval(node[1], w)
    if kind == "bin":
        op, l, r = node[1], _eval(node[2], w), _eval(node[3], w)
        if op == "+":
            return l + r
        if op == "-":
            return l - r
        if op == "*":
            return l * r
        if isinstance(r, (int, float)) and r == 0:
            return np.nan  # 标量除零：缺失而非异常（DataFrame 除零 pandas 自己出 inf/NaN）
        return l / r
    if kind == "lag":
        x = _eval(node[1], w)
        return x.shift(node[2], axis=0) if isinstance(x, pd.DataFrame) else x
    if kind == "call":
        return _call(node[1], node[2], w)
    raise ValueError(f"未知节点 {kind}")


def _call(name: str, args: list[tuple], w: dict[str, pd.DataFrame]):
    if name in CS_FUNCS:
        x = _eval(args[0], w)
        if not isinstance(x, pd.DataFrame):
            raise ValueError(f"{name} 只能作用于字段/序列表达式")
        if name == "cs_rank":
            return x.rank(axis=1)
        # pandas 3：DataFrame 与行 Series 运算必须显式 axis=0，否则按列对齐全为 NaN
        std = x.std(axis=1)
        return x.sub(x.mean(axis=1), axis=0).div(std.where(std > 0), axis=0)
    if name in SCALAR_FUNCS:
        x = _eval(args[0], w)
        if not isinstance(x, pd.DataFrame):
            raise ValueError(f"{name} 只能作用于字段/序列表达式")
        if name == "abs":
            return x.abs()
        if name == "sign":
            return np.sign(x)
        if name == "log":
            return np.log(x.where(x > 0))
        return np.sqrt(x.where(x > 0))
    # 时序算子（逐列）：输入必须是序列（字段表达式）；标量入参给可读错误
    x = _eval(args[0], w)
    if name in TS_FUNCS and not isinstance(x, pd.DataFrame):
        raise ValueError(f"{name} 只能作用于字段/序列表达式")
    if name == "ts_corr":
        y = _eval(args[1], w)
        if not isinstance(y, pd.DataFrame):
            raise ValueError("ts_corr 的第二个参数必须是字段/序列表达式")
        return x.rolling(int(args[2][1])).corr(y)
    if name == "ts_cov":
        y = _eval(args[1], w)
        if not isinstance(y, pd.DataFrame):
            raise ValueError("ts_cov 的第二个参数必须是字段/序列表达式")
        return x.rolling(int(args[2][1])).cov(y)
    if name == "ts_rank":
        # 当日值在最近 n 日中的滚动分位（含当日）
        n = int(args[1][1])
        return x.rolling(n).apply(lambda s: s.rank().iloc[-1] / len(s), raw=False)
    if name == "ts_skew":
        return x.rolling(int(args[1][1])).skew()
    if name == "ts_kurt":
        return x.rolling(int(args[1][1])).kurt()
    if name == "ts_scale":
        r = x.rolling(int(args[1][1]))
        lo, hi = r.min(), r.max()
        return (x - lo) / (hi - lo + 1e-12)
    if name == "ts_rsv":
        r = x.rolling(int(args[1][1]))
        lo, hi = r.min(), r.max()
        return (x - lo) / (hi - lo + 1e-12)
    x = _eval(args[0], w)
    n = int(args[1][1])
    if name == "ts_mean":
        return x.rolling(n).mean()
    if name == "ts_std":
        return x.rolling(n).std()
    if name == "ts_sum":
        return x.rolling(n).sum()
    if name == "ts_max":
        return x.rolling(n).max()
    if name == "ts_min":
        return x.rolling(n).min()
    if name == "ts_delta":
        return x - x.shift(n)
    raise ValueError(f"未实现的函数 {name}")


# ---------------------------------------------------------------------------
# 自定义因子存取（~/.vibe-research/factor/custom_factors.json）
# ---------------------------------------------------------------------------

_CUSTOM_FILE = os.path.join(factor_data.DATA_DIR, "custom_factors.json")


def list_custom() -> list[dict]:
    """已保存自定义因子。读取时清扫 __tmp_ 前缀的临时试跑因子（删除请求失败/关页残留）。"""
    try:
        with open(_CUSTOM_FILE, encoding="utf-8") as f:
            data = json.load(f)
        factors = [f for f in data.get("factors", []) if not str(f.get("id", "")).startswith("__tmp_")]
    except (OSError, ValueError):
        return []
    return sorted(factors, key=lambda x: x.get("created_at", ""))


def save_custom(fid: str, name: str, expr: str) -> dict:
    compile_expr(expr)  # 保存前静态校验
    if not fid or len(fid) > 32 or not re.fullmatch(r"[A-Za-z0-9_]+", fid):
        raise ValueError("因子 id 只能是字母/数字/下划线，≤32 字符")
    if not name or len(name) > 40:
        raise ValueError("因子名称不能为空且 ≤40 字符")
    factors = [f for f in list_custom() if f["id"] != fid]
    factors.append({"id": fid, "name": name, "expr": expr, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    os.makedirs(factor_data.DATA_DIR, exist_ok=True)
    tmp = _CUSTOM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"factors": factors}, f, ensure_ascii=False)
    os.replace(tmp, _CUSTOM_FILE)
    return {"id": fid, "name": name, "expr": expr}


def delete_custom(fid: str) -> None:
    factors = [f for f in list_custom() if f["id"] != fid]
    os.makedirs(factor_data.DATA_DIR, exist_ok=True)
    tmp = _CUSTOM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"factors": factors}, f, ensure_ascii=False)
    os.replace(tmp, _CUSTOM_FILE)


def fields_doc() -> dict:
    return {
        "fields": [{"name": k, "desc": v} for k, v in FIELDS.items()],
        "ops": OPS_DOC,
        "examples": [
            {"expr": "-close/ts_delta(close,5)", "desc": "5 日反转（价差口径）"},
            {"expr": "cs_rank(ts_mean(amount,20)/ts_mean(amount,120))", "desc": "成交额近 20 日相对 120 日放大（截面分位）"},
            {"expr": "-cs_zscore(ts_std(ret,20))", "desc": "低波动（20 日波动率的横截面负 z-score）"},
        ],
    }
