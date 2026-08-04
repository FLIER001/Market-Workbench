"""宏观面扩展数据采集 —— 爬取无法通过现成 API 获取的官方统计指标。

数据源全部为公开发布的官方网站，按指标归并为 6 个采集策略：

  1. NBS 国家统计局（data.stats.gov.cn）：
     PMI 分项 / 核心 CPI / 工业企业利润与库存 / 房地产销售与资金来源 /
     固定资产投资分项（设备工器具）/ 财政支出 / 货币供应。
  2. 人民银行（pbc.gov.cn）：
     社会融资规模存量结构（政府债→私人信用脉冲）、金融机构信贷收支（分部门中长期贷款）、
     银行家问卷调查（贷款需求/审批指数，PDF）。
  3. 财政部（mof.gov.cn）：财政收支（一般公共预算 + 政府性基金）。
  4. CPB 荷兰经济政策分析局：世界贸易量。
  5. akshare 衍生：专项债发行（地方政府债按名称汇总）、国房景气、企业商品价格指数。
  6. 近似代理：票据利率（DR007 代理）、同业存单（AAA 银行债代理）、非银/财政存款（央行信贷收支表近似）。

所有采集函数返回统一结构：{"label","value","forecast","prev","date","hist","unit","source"}，
与 macro.py 现有 investing 格式指标对齐，便于前端统一渲染。

容错策略：每个函数独立 try/except，失败返回 None，由上层缓存（market.py _sub_cached /
_layered_get）做 last-good 回退，页面不空窗。
"""

from __future__ import annotations

import io
import re
import time
from datetime import datetime, timezone, timedelta

import requests

BEIJING = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 15, retries: int = 2, **kw) -> requests.Response:
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def _ym_to_date(ym: str) -> str:
    """'2026年6月' / '2026.06' / '202606' / '2026年第1-2季度' → 'YYYY-MM'。"""
    s = str(ym)
    m = re.match(r"(\d{4})年(\d{1,2})月", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{4})\.(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{4})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"(\d{4})年第?(\d)", s)  # 季度 → 季末月
    if m:
        return f"{m.group(1)}-{int(m.group(2))*3:02d}"
    return s[:10]


def _series_from_rows(date_vals: list[tuple[str, float]], limit: int = 60) -> list[dict]:
    """[(date, value), ...] → 升序 hist 点列。"""
    pts = sorted({d: v for d, v in date_vals if v is not None}.items())
    return [{"date": d, "v": round(float(v), 3)} for d, v in pts[-limit:]]


def _mk(label: str, hist: list[dict], unit: str = "", source: str = "",
        forecast: float | None = None, value_transform=None) -> dict | None:
    """由 hist 点列构建统一指标卡。"""
    if not hist:
        return None
    last = hist[-1]["v"]
    if value_transform:
        last = value_transform(last)
        hist = [{"date": h["date"], "v": value_transform(h["v"])} for h in hist]
    prev = hist[-2]["v"] if len(hist) > 1 else None
    return {
        "label": label,
        "value": round(last, 2),
        "forecast": forecast,
        "prev": round(prev, 2) if prev is not None else None,
        "date": hist[-1]["date"],
        "hist": hist,
        "unit": unit,
        "source": source,
    }


# ---------------------------------------------------------------------------
# 策略 1：NBS 国家统计局
# ---------------------------------------------------------------------------

def _nbs_fetch(kind: str, path: str, period: str = "LAST36"):
    """调 akshare 的 NBS 通用接口，返回 DataFrame（行=指标，列=月份）。"""
    import akshare as ak  # 延迟导入，避免冷启动开销
    try:
        df = ak.macro_china_nbs_nation(kind=kind, path=path, period=period)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return df


def _nbs_row_hist(df, row_keyword: str, value_transform=None) -> list[dict]:
    """从 NBS DataFrame 里按行名关键词取一条月度序列，转成升序 hist。"""
    if df is None or df.empty:
        return []
    mask = df.index.str.contains(row_keyword, na=False)
    if not mask.any():
        return []
    row = df[mask].iloc[0]
    pts = []
    for col, val in row.items():
        d = _ym_to_date(col)
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if value_transform:
            v = value_transform(v)
        pts.append((d, v))
    return _series_from_rows(pts)


# ---- 1a. PMI 分项（框架权重最高的缺口，一次性 6 个指标） ----

def pmi_new_orders() -> dict | None:
    df = _nbs_fetch("月度数据", "采购经理指数 > 制造业采购经理指数")
    return _mk("PMI 新订单", _nbs_row_hist(df, "新订单指数"), source="统计局 PMI 分项")


def pmi_production() -> dict | None:
    df = _nbs_fetch("月度数据", "采购经理指数 > 制造业采购经理指数")
    return _mk("PMI 生产", _nbs_row_hist(df, "生产指数"), source="统计局 PMI 分项")


def pmi_new_export_orders() -> dict | None:
    df = _nbs_fetch("月度数据", "采购经理指数 > 制造业采购经理指数")
    return _mk("PMI 新出口订单", _nbs_row_hist(df, "新出口订单指数"), source="统计局 PMI 分项")


def pmi_finished_inventory() -> dict | None:
    df = _nbs_fetch("月度数据", "采购经理指数 > 制造业采购经理指数")
    return _mk("PMI 产成品库存", _nbs_row_hist(df, "产成品库存指数"), source="统计局 PMI 分项")


def pmi_output_price() -> dict | None:
    df = _nbs_fetch("月度数据", "采购经理指数 > 制造业采购经理指数")
    return _mk("PMI 出厂价格", _nbs_row_hist(df, "出厂价格指数"), source="统计局 PMI 分项")


def pmi_input_price() -> dict | None:
    df = _nbs_fetch("月度数据", "采购经理指数 > 制造业采购经理指数")
    return _mk("PMI 购进价格", _nbs_row_hist(df, "主要原材料购进价格指数"), source="统计局 PMI 分项")


def pmi_full() -> dict:
    """一次拉取全部 PMI 分项，返回 {key: 指标卡}，供上层避免重复请求。"""
    df = _nbs_fetch("月度数据", "采购经理指数 > 制造业采购经理指数")
    out = {}
    specs = {
        "pmi_headline": ("制造业PMI", "制造业采购经理指数"),
        "pmi_new_orders": ("PMI 新订单", "新订单指数"),
        "pmi_production": ("PMI 生产", "生产指数"),
        "pmi_new_export_orders": ("PMI 新出口订单", "新出口订单指数"),
        "pmi_finished_inventory": ("PMI 产成品库存", "产成品库存指数"),
        "pmi_output_price": ("PMI 出厂价格", "出厂价格指数"),
        "pmi_input_price": ("PMI 购进价格", "主要原材料购进价格指数"),
        "pmi_employment": ("PMI 从业人员", "从业人员指数"),
        "pmi_expectation": ("PMI 生产经营预期", "生产经营活动预期指数"),
    }
    for key, (label, kw) in specs.items():
        card = _mk(label, _nbs_row_hist(df, kw), source="统计局 PMI 分项")
        if card:
            out[key] = card
    return out


# ---- 1b. 核心 CPI（不包括食品和能源） ----

def core_cpi() -> dict | None:
    """核心 CPI（不包括食品和能源）同比。NBS 分时间段建节点，需拼接。"""
    base = "价格指数 > 居民消费价格分类指数 (上年同月=100) > 全国居民消费价格分类指数 (上年同月=100)"
    pts = []
    for seg in ("(2026-)", "(2021-2025)"):
        df = _nbs_fetch("月度数据", f"{base} {seg}", period="LAST36")
        if df is not None and not df.empty:
            pts.extend(_row_pts(df, "不包括食品和能源", value_transform=lambda v: round(v - 100, 2)))
    hist = _series_from_rows(pts)
    return _mk("核心 CPI 同比", hist, unit="%", source="统计局 CPI 分类")


def _row_pts(df, row_keyword: str, value_transform=None) -> list:
    """从 NBS DataFrame 取一条序列，返回 [(date, value)] 原始点（供拼接）。"""
    if df is None or df.empty:
        return []
    mask = df.index.str.contains(row_keyword, na=False)
    if not mask.any():
        return []
    row = df[mask].iloc[0]
    pts = []
    for col, val in row.items():
        d = _ym_to_date(col)
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if value_transform:
            v = value_transform(v)
        pts.append((d, v))
    return pts


# ---- 1c. 工业企业利润 + 产成品库存（库存周期） ----

def industrial_profit() -> dict | None:
    df = _nbs_fetch("月度数据", "工业 > 工业企业主要经济指标")
    return _mk("工业企业利润累计同比", _nbs_row_hist(df, "利润总额累计增长"),
               unit="%", source="统计局工业企业效益")


def industrial_inventory() -> dict | None:
    df = _nbs_fetch("月度数据", "工业 > 工业企业主要经济指标")
    return _mk("产成品库存同比", _nbs_row_hist(df, "产成品存货增减"),
               unit="%", source="统计局工业企业效益")


def industrial_revenue() -> dict | None:
    df = _nbs_fetch("月度数据", "工业 > 工业企业主要经济指标")
    return _mk("工业企业营收累计同比", _nbs_row_hist(df, "营业收入累计增长"),
               unit="%", source="统计局工业企业效益")


# ---- 1d. 房地产：销售 + 资金来源 ----

def property_sales_area() -> dict | None:
    df = _nbs_fetch("月度数据", "房地产 > 新建商品房销售面积")
    return _mk("新建商品房销售面积累计同比", _nbs_row_hist(df, "销售面积累计增长"),
               unit="%", source="统计局房地产")


def property_funds() -> dict | None:
    df = _nbs_fetch("月度数据", "房地产 > 房地产开发投资实际到位资金")
    return _mk("房地产开发到位资金累计同比", _nbs_row_hist(df, "资金来源小计累计增长"),
               unit="%", source="统计局房地产")


def property_loans() -> dict | None:
    df = _nbs_fetch("月度数据", "房地产 > 房地产开发投资实际到位资金")
    return _mk("房地产国内贷款累计同比", _nbs_row_hist(df, "国内贷款累计增长"),
               unit="%", source="统计局房地产")


# ---- 1e. 固定资产投资分项（设备工器具购置） ----

def fai_equipment() -> dict | None:
    """设备工器具购置投资累计同比。固定资产投资增速节点路径含全角空格，用 cid 直取。"""
    df = _nbs_fetch_by_cid("aac38c7aa152478ebea254ac412aa0a1")
    return _mk("设备工器具购置投资累计同比", _nbs_row_hist(df, "设备工器具购置"),
               unit="%", source="统计局投资")


def _nbs_fetch_by_cid(cid: str, kind: str = "月度数据", period: str = "LAST36"):
    """按目录叶子节点 cid 直接拉 NBS 数据，绕开路径名解析（全角空格等）。"""
    try:
        from akshare.economic.macro_china_nbs import (
            _post_nbs_es_data, _get_nbs_root_id, _get_nbs_indicators,
            _KIND_CONFIG, _build_nbs_dts, _get_nbs_granularity)
    except Exception:
        return None
    import pandas as pd
    try:
        config = _KIND_CONFIG[kind]
        route = str(config["route"])
        gran = _get_nbs_granularity(kind)
        root_id = _get_nbs_root_id(int(config["code"]), route)
        inds = _get_nbs_indicators(cid, route)
        dts = _build_nbs_dts(period=period, granularity=gran)
        data = _post_nbs_es_data(
            cid=cid, root_id=root_id, route=route,
            indicator_ids=[i["_id"] for i in inds],
            das=[{"text": "全国", "value": "000000000000"}],
            show_type="1", dts=dts)
        if not data:
            return None
        # 转成 DataFrame：行=指标 i_showname，列=月份
        recs = {}
        cols = []
        for period_item in data:
            pname = period_item.get("name", "")
            cols.append(pname)
            for v in period_item.get("values", []):
                name = v.get("i_showname") or v.get("_name") or ""
                val = v.get("value")
                recs.setdefault(name, {})[pname] = val
        df = pd.DataFrame(recs).T
        df = df.reindex(columns=cols)
        return df
    except Exception:
        return None


# ---- 1f. 财政支出（NBS 月度） ----

def fiscal_expenditure() -> dict | None:
    df = _nbs_fetch("月度数据", "财政 > 国家财政预算支出")
    return _mk("国家财政支出累计同比", _nbs_row_hist(df, "财政支出.*累计增长|支出.*累计增长"),
               unit="%", source="统计局财政")


# ---------------------------------------------------------------------------
# 策略 2：人民银行
# ---------------------------------------------------------------------------

_PBC_STATS_INDEX = "http://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html"


def _pbc_latest_year_index() -> str | None:
    """找到最新一年的统计数据汇编页。"""
    try:
        r = _get(_PBC_STATS_INDEX)
        r.encoding = r.apparent_encoding
        m = re.search(r'href="(/diaochatongjisi/116219/116319/(\d{4})ntjsj/index\.html)"', r.text)
        if m:
            return f"http://www.pbc.gov.cn{m.group(1)}"
    except Exception:
        pass
    # 退化为当年
    year = datetime.now(BEIJING).year
    return f"http://www.pbc.gov.cn/diaochatongjisi/116219/116319/{year}ntjsj/index.html"


def _pbc_sub_page(year_index_url: str, sub: str) -> str | None:
    """从年度汇编页找子栏目 URL。"""
    try:
        r = _get(year_index_url)
        r.encoding = r.apparent_encoding
        m = re.search(rf'href="(/diaochatongjisi/116219/116319/\d{{4}}ntjsj/{sub}/index\.html)"', r.text)
        if m:
            return f"http://www.pbc.gov.cn{m.group(1)}"
    except Exception:
        pass
    return None


def _pbc_xlsx_links(page_url: str) -> list[str]:
    """从央行栏目页提取全部 xlsx 附件链接（按 URL 中日期排序，最新在前）。"""
    try:
        r = _get(page_url)
        r.encoding = r.apparent_encoding
        files = re.findall(r'href="(/diaochatongjisi/attachDir/[^"]+\.xlsx?)"', r.text)
        files = sorted(set(files), reverse=True)
        return [f"http://www.pbc.gov.cn{f}" for f in files]
    except Exception:
        return []


def _read_xlsx(url: str):
    import openpyxl
    r = _get(url, timeout=20)
    return openpyxl.load_workbook(io.BytesIO(r.content))


def social_financing_stock() -> dict | None:
    """社融存量（万亿元，含增速）+ 政府债券存量 → 私人信用脉冲近似。

    央行《社会融资规模存量统计表》xlsx。返回主指标卡（社融存量同比），
    私人部门信用脉冲由上层结合存量结构另行计算。
    """
    year_index = _pbc_latest_year_index()
    if not year_index:
        return None
    sub_url = _pbc_sub_page(year_index, "shrzgm")
    if not sub_url:
        return None
    links = _pbc_xlsx_links(sub_url)
    # 找存量统计表（文件名无序，逐个读表头判断）
    for url in links[:6]:
        try:
            wb = _read_xlsx(url)
        except Exception:
            continue
        ws = wb[wb.sheetnames[0]]
        head = " ".join(str(c.value) for row in ws.iter_rows(min_row=1, max_row=4) for c in row if c.value)
        if "存量" not in head:
            continue
        # 解析：第 5 行是月份表头（2026.1, 2026.2...），第 8 行起是项目
        # 月份在第 5 行，奇数列是存量、偶数列是增速
        months = []
        for c in ws[5]:
            v = c.value
            if v and re.match(r"\d{4}\.\d+", str(v)):
                months.append((c.column, str(v)))
        if not months:
            continue
        # 社融存量增速行 & 政府债券行
        stock_growth, gov_growth = [], []
        for row in ws.iter_rows(min_row=6, values_only=False):
            name = str(row[0].value or "")
            if "社会融资规模存量" in name and "AFRE" not in name:
                for col, ym in months:
                    gcell = ws.cell(row=row[0].row, column=col + 1)
                    try:
                        stock_growth.append((_ym_to_date(ym), float(gcell.value)))
                    except (TypeError, ValueError):
                        pass
            if "政府债券" in name:
                for col, ym in months:
                    gcell = ws.cell(row=row[0].row, column=col + 1)
                    try:
                        gov_growth.append((_ym_to_date(ym), float(gcell.value)))
                    except (TypeError, ValueError):
                        pass
        hist = _series_from_rows(stock_growth)
        card = _mk("社融存量同比", hist, unit="%", source="人民银行社融存量")
        if card is not None:
            card["gov_bond_growth_hist"] = _series_from_rows(gov_growth)
            card["xlsx_url"] = url
        return card
    return None


def credit_by_sector() -> dict | None:
    """金融机构信贷收支表 → 住户/企事业中长期贷款余额，算同比。

    返回住户中长期贷款（主卡）+ 企事业中长期贷款序列（附加），
    以及非银存款、财政性存款（策略 6 近似用）。
    """
    year_index = _pbc_latest_year_index()
    if not year_index:
        return None
    sub_url = _pbc_sub_page(year_index, "jrjgxdsztj")
    if not sub_url:
        return None
    links = _pbc_xlsx_links(sub_url)
    for url in links:
        try:
            wb = _read_xlsx(url)
        except Exception:
            continue
        ws = wb[wb.sheetnames[0]]
        head = " ".join(str(c.value) for row in ws.iter_rows(min_row=1, max_row=8) for c in row if c.value)
        # 只取全口径「（存款类）金融机构本外币信贷收支表」，跳过中资大型/中小型银行等子口径
        if "本外币信贷收支" not in head or "中资" in head:
            continue
        # 月份表头自动定位：前 10 行中含 '2026.01' 样式单元格的那一行
        months = []
        for hdr_row in range(1, 11):
            row_months = []
            for c in ws[hdr_row]:
                v = c.value
                if v and re.match(r"\d{4}\.\d+", str(v)):
                    row_months.append((c.column, str(v)))
            if len(row_months) >= 3:
                months = row_months
                break
        if not months:
            continue
        # 行定位：按项目名
        rows_map = {}
        for row in ws.iter_rows(min_row=7, values_only=False):
            name = str(row[0].value or "")
            for key, kw in [("household_ml", "中长期贷"), ("corp_total", "企（"), ]:
                pass
            # 精确匹配有难度，宽松收集关键行
            label = name.replace(" ", "").replace("　", "")
            if label:
                rows_map.setdefault(label, row[0].row)
        # 按区块状态机定位：住户/企事业各自的中长期贷款、票据融资、财政性存款、非银存款
        def _cells(row_idx: int) -> list[tuple[str, float]]:
            pts = []
            for col, ym in months:
                try:
                    pts.append((_ym_to_date(ym), float(ws.cell(row=row_idx, column=col).value)))
                except (TypeError, ValueError):
                    pass
            return pts

        household = []          # 住户中长期贷款
        corp = []               # 企事业中长期贷款
        corp_total = []         # 企事业贷款总额
        bill_financing = []     # 票据融资
        fiscal_dep = []         # 财政性存款
        nonbank_dep = []        # 非银行业金融机构存款
        section = None          # 当前所在区块
        for row in ws.iter_rows(min_row=7, values_only=False):
            name = re.sub(r"\s+", "", str(row[0].value or ""))
            rid = row[0].row
            if "住户贷款" in name:
                section = "household"
            elif "企（事）业单位贷款" in name or "企事业单位贷款" in name:
                section = "corp"
                corp_total = _cells(rid)
            elif "非银行业金融机构贷款" in name:
                section = "nonbank_loan"
            elif "境外贷款" in name or "债券投资" in name:
                section = None
            elif "非银行业金融机构存款" in name or "非银行存款" in name:
                nonbank_dep = _cells(rid)
            elif "财政性存款" in name:
                fiscal_dep = _cells(rid)
            if "中长期贷款" in name:
                if section == "household":
                    household = _cells(rid)
                elif section == "corp":
                    corp = _cells(rid)
            if "票据融资" in name and section == "corp":
                bill_financing = _cells(rid)
        hist = _series_from_rows(household)
        card = _mk("住户中长期贷款余额", hist, unit="亿元", source="人民银行信贷收支")
        if card is not None:
            card["corp_ml_loan_hist"] = _series_from_rows(corp)      # 企事业中长期贷款
            card["corp_total_loan_hist"] = _series_from_rows(corp_total)
            card["bill_financing_hist"] = _series_from_rows(bill_financing)  # 票据融资
            card["fiscal_deposit_hist"] = _series_from_rows(fiscal_dep)
            card["nonbank_deposit_hist"] = _series_from_rows(nonbank_dep)
            card["xlsx_url"] = url
        return card
    return None


def bank_survey() -> dict | None:
    """银行家问卷调查：贷款总体需求指数 + 贷款审批指数（季度，PDF 解析）。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    # 问卷列表页
    list_url = "http://www.pbc.gov.cn/diaochatongjisi/116219/116227/index.html"
    try:
        r = _get(list_url)
        r.encoding = r.apparent_encoding
        # 找最新银行家问卷调查报告页
        pages = re.findall(r'href="(/diaochatongjisi/116219/116227/[^"]+/index\.html)"[^>]*>([^<]*银行家问卷调查报告[^<]*)<', r.text)
        if not pages:
            return None
        page_url = f"http://www.pbc.gov.cn{pages[0][0]}"
        r2 = _get(page_url)
        r2.encoding = r2.apparent_encoding
        pdfs = re.findall(r'href="([^"]+\.pdf)"', r2.text)
        if not pdfs:
            return None
        pdf_url = pdfs[0]
        if not pdf_url.startswith("http"):
            pdf_url = f"http://www.pbc.gov.cn{pdf_url}"
        rp = _get(pdf_url, timeout=25)
        reader = PdfReader(io.BytesIO(rp.content))
        text = ""
        for page in reader.pages[:5]:
            text += (page.extract_text() or "") + "\n"
        # 解析：贷款总体需求指数为 X%；审批指数
        demand = re.search(r"贷款总体需求指数为?\s*([\d.]+)\s*%", text)
        approve = re.search(r"(?:贷款)?审批指数为?\s*([\d.]+)\s*%", text)
        quarter = re.search(r"(\d{4})年(?:第)?([一二三四1-4])季度", pages[0][1] or text)
        val = float(demand.group(1)) if demand else None
        appr = float(approve.group(1)) if approve else None
        if val is None:
            return None
        date_str = ""
        if quarter:
            qmap = {"一": 3, "二": 6, "三": 9, "四": 12, "1": 3, "2": 6, "3": 9, "4": 12}
            date_str = f"{quarter.group(1)}-{qmap.get(quarter.group(2), 12):02d}"
        return {
            "label": "银行贷款需求指数",
            "value": val,
            "forecast": None,
            "prev": None,
            "date": date_str or datetime.now(BEIJING).strftime("%Y-%m"),
            "hist": [{"date": date_str, "v": val}] if date_str else [],
            "unit": "%",
            "source": "人民银行银行家问卷调查",
            "approve_index": appr,
            "pdf_url": pdf_url,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 策略 3：财政部财政收支
# ---------------------------------------------------------------------------

def fiscal_revenue_expenditure() -> dict | None:
    """财政部月度财政收支：一般公共预算支出 + 政府性基金支出（广义财政脉冲近似）。"""
    index_url = "http://www.mof.gov.cn/zhengwuxinxi/caizhengshuju/"
    try:
        r = _get(index_url)
        r.encoding = r.apparent_encoding
        # 最新「财政收支情况」
        links = re.findall(r'href="(http://gks\.mof\.gov\.cn/tongjishuju/[^"]+\.htm)"[^>]*>([^<]*财政收支情况[^<]*)<', r.text)
        if not links:
            # 链接文字可能在下一行
            links = re.findall(r'href="(http://gks\.mof\.gov\.cn/tongjishuju/[^"]+\.htm)"[^>]*>\s*([^<]*财政收支[^<]*)', r.text)
        if not links:
            return None
        page_url = links[0][0]
        r2 = _get(page_url)
        r2.encoding = r2.apparent_encoding
        body = re.sub(r"<[^>]+>", " ", r2.text)
        body = re.sub(r"\s+", " ", body)
        # 一般公共预算支出
        exp = re.search(r"全国一般公共预算支出\s*([\d.]+)\s*亿元[，,]?\s*同比增长?\s*([\d.\-]+)\s*%", body)
        exp_yoy = re.search(r"一般公共预算支出\s*[\d.]+\s*亿元[，,]?\s*同比增长?\s*([\d.\-]+)\s*%", body)
        fund_exp = re.search(r"政府性基金预算支出\s*([\d.]+)\s*亿元[，,]?\s*(?:同比)?(?:增长|下降)\s*([\d.\-]+)\s*%", body)
        # 期间（上半年/1-5月等）
        period_m = re.search(r"(上半年|一季度|1-(\d+)月|(\d+)月份)", body)
        val = float(exp.group(1)) if exp else None
        yoy = None
        if exp and exp.lastindex and exp.lastindex >= 2:
            try:
                yoy = float(exp.group(2))
            except (ValueError, IndexError):
                pass
        if yoy is None and exp_yoy:
            yoy = float(exp_yoy.group(1))
        if val is None and yoy is None:
            return None
        month_str = ""
        if period_m:
            p = period_m.group(0)
            if "上半年" in p or "1-6月" in p:
                month_str = f"{datetime.now(BEIJING).year}-06"
            elif "一季度" in p or "1-3月" in p:
                month_str = f"{datetime.now(BEIJING).year}-03"
            else:
                m2 = re.search(r"1-(\d+)月", p)
                if m2:
                    month_str = f"{datetime.now(BEIJING).year}-{int(m2.group(1)):02d}"
        return {
            "label": "一般公共预算支出同比",
            "value": yoy if yoy is not None else val,
            "forecast": None,
            "prev": None,
            "date": month_str or datetime.now(BEIJING).strftime("%Y-%m"),
            "hist": [],
            "unit": "%" if yoy is not None else "亿元",
            "source": "财政部财政收支",
            "exp_amount": val,
            "fund_exp": fund_exp.groups() if fund_exp else None,
            "page_url": page_url,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 策略 4：CPB 世界贸易量
# ---------------------------------------------------------------------------

def world_trade_volume() -> dict | None:
    """CPB World Trade Monitor：世界贸易量指数（2021=100，季调）。"""
    try:
        import pandas as pd
    except ImportError:
        return None
    try:
        r = _get("https://www.cpb.nl/en/worldtrademonitor/latest")
        m = re.search(r'href="(/system/files/cpbmedia/CPB-World-trade-monitor-[^"]+\.xlsx)"', r.text)
        if not m:
            return None
        xlsx_url = f"https://www.cpb.nl{m.group(1)}"
        rx = _get(xlsx_url, timeout=30)
        df = pd.read_excel(io.BytesIO(rx.content), sheet_name="trade_out", header=None)
        # 月份表头行：含 '2000m01' 的那一行
        hdr = None
        for i in range(min(12, len(df))):
            row = df.iloc[i].astype(str)
            if row.str.contains(r"\d{4}m\d{2}", regex=True).any():
                hdr = df.iloc[i]
                break
        if hdr is None:
            return None
        # World trade 数据行
        wt = None
        for i in range(len(df)):
            if str(df.iloc[i, 1]).strip() == "World trade":
                wt = df.iloc[i]
                break
        if wt is None:
            return None
        pts = []
        for j in range(len(hdr)):
            cell = str(hdr[j])
            ym = re.match(r"(\d{4})m(\d{2})", cell)
            if not ym:
                continue
            try:
                v = float(wt[j])
            except (TypeError, ValueError):
                continue
            pts.append((f"{ym.group(1)}-{ym.group(2)}", v))
        hist = _series_from_rows(pts)
        return _mk("世界贸易量指数", hist, source="CPB World Trade Monitor")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 策略 5：akshare 衍生（专项债 / 已有宏观补充）
# ---------------------------------------------------------------------------

def special_bond_issuance() -> dict | None:
    """新增专项债月度发行额（亿元），按地方政府债名称含「专项」且不含「再融资」汇总。"""
    import akshare as ak
    try:
        df = ak.bond_local_government_issue_cninfo()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        name_col = "债券名称" if "债券名称" in df.columns else "债券简称"
        date_col = "发行起始日"
        amt_col = "实际发行总量"
        df = df.copy()
        df[date_col] = pd_to_datetime(df[date_col])
        mask = df[name_col].str.contains("专项", na=False) & ~df[name_col].str.contains("再融资", na=False)
        sub = df[mask].dropna(subset=[date_col])
        monthly = sub.groupby(sub[date_col].dt.to_period("M"))[amt_col].sum()
        pts = [(str(p), float(v)) for p, v in monthly.items()]
        hist = _series_from_rows(pts, limit=36)
        return _mk("新增专项债发行额", hist, unit="亿元", source="地方政府债发行(专项)")
    except Exception:
        return None


def pd_to_datetime(s):
    import pandas as pd
    return pd.to_datetime(s, errors="coerce")


# ---------------------------------------------------------------------------
# 汇总入口：一次拉全所有扩展指标
# ---------------------------------------------------------------------------

def fetch_all() -> dict:
    """并行拉取全部扩展指标，返回 {key: 指标卡}。单源失败不影响其他。"""
    from concurrent.futures import ThreadPoolExecutor
    specs = {
        # NBS
        "pmi_full": pmi_full,
        "core_cpi": core_cpi,
        "industrial_profit": industrial_profit,
        "industrial_inventory": industrial_inventory,
        "industrial_revenue": industrial_revenue,
        "property_sales_area": property_sales_area,
        "property_funds": property_funds,
        "property_loans": property_loans,
        "fai_equipment": fai_equipment,
        "fiscal_expenditure": fiscal_expenditure,
        # 央行
        "social_financing_stock": social_financing_stock,
        "credit_by_sector": credit_by_sector,
        "bank_survey": bank_survey,
        # 财政部
        "fiscal_revenue_expenditure": fiscal_revenue_expenditure,
        # CPB
        "world_trade_volume": world_trade_volume,
        # 专项债
        "special_bond_issuance": special_bond_issuance,
    }
    out: dict = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {k: pool.submit(fn) for k, fn in specs.items()}
        for k, fut in futures.items():
            try:
                val = fut.result()
            except Exception:
                val = None
            if val is None:
                continue
            if k == "pmi_full" and isinstance(val, dict):
                out.update(val)  # 展开 PMI 分项
            else:
                out[k] = val
    return out


if __name__ == "__main__":
    import json
    data = fetch_all()
    print(json.dumps({k: {kk: (vv if not isinstance(vv, list) else f"{len(vv)}pts")
                          for kk, vv in v.items() if kk in ("label", "value", "date", "hist", "unit", "source")}
                      for k, v in data.items()}, ensure_ascii=False, indent=2))
