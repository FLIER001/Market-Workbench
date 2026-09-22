"""快照能力探测回归测试。

背景：各模块的 warm() 原先写成 `payload.get("schema_version") == N`，把「快照可用」
绑死在 payload 结构不变上。任何一次结构演进都会让整张快照被判废，进程重启后首个
请求要走全量同步冷建（黄金实测 50s、油价 1-2 分钟），前端整段停在「首次计算中」。

逐个模块验证两件事：
1. schema_version 被改掉后，加载器仍返回可用载荷并打 degraded 标记；
2. 缺关键字段时必须判废 —— 放宽版本门不等于接受坏数据。

测试自造合成快照并重定向模块路径，不依赖 ~/.market-workbench 下的真实数据
（conftest.py 会把 MW_DATA_DIR 指向临时目录，那里本来就是空的）。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache_runtime  # noqa: E402
import fund_pfs  # noqa: E402
import industry_chain  # noqa: E402
import oil  # noqa: E402
import plate_scores  # noqa: E402
import sector_scores  # noqa: E402
import sw_level2_scores  # noqa: E402
import timing_alloc  # noqa: E402


# ---------------------------------------------------------------------------
# usable_snapshot 本体
# ---------------------------------------------------------------------------

def test_usable_snapshot_requires_capability_fields():
    assert cache_runtime.usable_snapshot(None, ("a",)) is None
    assert cache_runtime.usable_snapshot([], ("a",)) is None
    assert cache_runtime.usable_snapshot({}, ("a",)) is None
    assert cache_runtime.usable_snapshot({"a": []}, ("a",)) is None
    assert cache_runtime.usable_snapshot({"a": 1}, ("a",)) == {"a": 1}


def test_usable_snapshot_supports_nested_paths():
    value = {"timing": {"regime": "neutral"}}
    assert cache_runtime.usable_snapshot(value, ("timing.regime",)) == value
    assert cache_runtime.usable_snapshot(value, ("timing.missing",)) is None
    assert cache_runtime.usable_snapshot({"timing": "not-a-dict"}, ("timing.regime",)) is None


def test_usable_snapshot_degrades_instead_of_discarding_on_version_mismatch():
    """版本不符 → 降级（打 degraded），而不是作废。这是核心契约。"""
    value = {"schema_version": 99, "indicators": [1]}
    out = cache_runtime.usable_snapshot(value, ("indicators",), schema_version=3)
    assert out is not None and out.get("degraded") is True
    assert value.get("degraded") is None, "不得污染调用方持有的原对象"


def test_usable_snapshot_version_match_has_no_degraded_mark():
    value = {"schema_version": 3, "indicators": [1]}
    out = cache_runtime.usable_snapshot(value, ("indicators",), schema_version=3)
    assert out is not None and "degraded" not in out


# ---------------------------------------------------------------------------
# 各模块加载器
# ---------------------------------------------------------------------------

# name, 加载器, 快照路径所在的模块属性（第一个为主路径，其余重定向到不存在处）,
# 关键字段（点号表嵌套）
_CASES = [
    ("oil", oil._warm, ("_SNAPSHOT",), "indicators"),
    ("timing_alloc", timing_alloc._load_snapshot, ("_SNAPSHOT_FILE",), "timing.regime"),
    ("plate_scores", plate_scores._load_cache,
     ("_PRIMARY_CACHE_FILE", "_FALLBACK_CACHE_FILE"), "boards"),
    ("sector_scores", sector_scores._load_cache,
     ("_PRIMARY_CACHE_FILE", "_FALLBACK_CACHE_FILE"), "industries"),
    ("sw_level2_scores", sw_level2_scores._load_cache,
     ("_PRIMARY_CACHE_FILE", "_FALLBACK_CACHE_FILE"), "industries"),
    ("fund_pfs", fund_pfs._load_cache, ("_CACHE_FILE",), "rows"),
]

_CASE_IDS = [case[0] for case in _CASES]


def _payload_for(key_path: str, schema_version: int) -> dict:
    """按关键字段路径造一份最小可用快照。"""
    parts = key_path.split(".")
    node: object = [{"stub": 1}]
    for part in reversed(parts):
        node = {part: node}
    payload = dict(node)  # type: ignore[arg-type]
    payload["schema_version"] = schema_version
    return payload


class _Redirected:
    """把模块的多条快照路径重定向到临时副本，fallback 一律指向不存在的路径。

    fallback 必须一起重定向：sector_scores 等模块有真实的 fallback 文件，
    只改主路径会被 fallback 兜住，测不到目标分支。
    """

    def __init__(self, module, attrs: tuple[str, ...], primary: str, absent: str):
        self.module = module
        self.attrs = attrs
        self.primary = primary
        self.absent = absent
        self.saved: dict[str, str] = {}

    def __enter__(self):
        for index, attr in enumerate(self.attrs):
            self.saved[attr] = getattr(self.module, attr)
            setattr(self.module, attr, self.primary if index == 0 else self.absent)
        return self

    def __exit__(self, *exc):
        for attr, value in self.saved.items():
            setattr(self.module, attr, value)
        return False


def _run(loader, attrs: tuple[str, ...], payload: dict, tmp_path, present_primary=True):
    module = sys.modules[loader.__module__]
    primary = tmp_path / "snapshot.json"
    if present_primary:
        with open(primary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    absent = tmp_path / "__absent_fallback__.json"
    with _Redirected(module, attrs, str(primary), str(absent)):
        return loader()


@pytest.mark.parametrize("name,loader,attrs,key", _CASES, ids=_CASE_IDS)
def test_loader_degrades_when_schema_version_changes(name, loader, attrs, key, tmp_path):
    payload = _payload_for(key, schema_version=99999)
    out = _run(loader, attrs, payload, tmp_path)

    assert out is not None, f"{name}：版本号变化后快照被判废，冷建会落到用户请求路径"
    assert out.get("degraded") is True, f"{name}：降级快照未打 degraded 标记"


@pytest.mark.parametrize("name,loader,attrs,key", _CASES, ids=_CASE_IDS)
def test_loader_accepts_matching_version_without_degraded(name, loader, attrs, key, tmp_path):
    """版本相符时照常接受，且不打降级标记（避免误伤正常路径）。"""
    expected = {
        "oil": 1, "timing_alloc": timing_alloc._SCHEMA_VERSION,
        "plate_scores": plate_scores._SCHEMA_VERSION,
        "sector_scores": sector_scores._SCHEMA_VERSION,
        "sw_level2_scores": sw_level2_scores._SCHEMA_VERSION,
        "fund_pfs": fund_pfs._SCHEMA_VERSION,
    }[name]
    payload = _payload_for(key, schema_version=expected)
    out = _run(loader, attrs, payload, tmp_path)

    assert out is not None, f"{name}：版本相符却被拒"
    assert "degraded" not in out, f"{name}：版本相符却打了降级标记"


@pytest.mark.parametrize("name,loader,attrs,key", _CASES, ids=_CASE_IDS)
def test_loader_still_rejects_snapshot_without_key_fields(name, loader, attrs, key, tmp_path):
    """缺关键字段必须判废 —— 放宽版本门不等于接受坏数据。"""
    payload = _payload_for(key, schema_version=99999)
    payload.pop(key.split(".")[0], None)
    out = _run(loader, attrs, payload, tmp_path)

    assert out is None, f"{name}：缺关键字段 {key} 却仍被接受"


@pytest.mark.parametrize("name,loader,attrs,key", _CASES, ids=_CASE_IDS)
def test_loader_returns_none_when_snapshot_absent(name, loader, attrs, key, tmp_path):
    """快照不存在时保持返回 None（真实冷启动路径不受影响）。"""
    out = _run(loader, attrs, {}, tmp_path, present_primary=False)
    assert out is None


def test_industry_chain_loader_degrades(tmp_path, monkeypatch):
    payload = _payload_for("structure.nodes", schema_version=99999)
    primary = tmp_path / "chain.json"
    with open(primary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    monkeypatch.setattr(industry_chain, "_chain_cache_files", lambda chain: (str(primary),))

    out = industry_chain._load_chain_cache({})
    assert out is not None and out.get("degraded") is True
    assert out.get("structure", {}).get("nodes")


def test_industry_chain_loader_rejects_without_nodes(tmp_path, monkeypatch):
    payload = {"schema_version": 99999, "structure": {"nodes": []}}
    primary = tmp_path / "chain.json"
    with open(primary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    monkeypatch.setattr(industry_chain, "_chain_cache_files", lambda chain: (str(primary),))

    assert industry_chain._load_chain_cache({}) is None
