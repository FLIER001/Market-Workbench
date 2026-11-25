"""特定站点的 TLS 校验修复：补全不完整的证书链，替代 verify=False。

背景：申万研究（www.swsresearch.com）的服务器只下发叶子证书，缺中间证书
「GeoTrust G2 TLS CN RSA4096 SHA256 2022 CA1」。macOS 钥匙串会按 AIA 自动补全，
Linux / OpenSSL（容器、CI）则报 `unable to get local issuer certificate`。
此前的做法是 `verify=False`，等于对中间人完全敞开：行业分类与月报一旦被篡改，
会直接污染行业评分。

做法：把缺的中间证书随仓库带上（data/swsresearch_intermediate_ca.crt，来源为该证书
AIA 指向的 http://cacerts.digicert.cn/...crt），与 certifi 根证书拼成专用 CA 包。
校验仍然完整：叶子 → 中间（包内）→ DigiCert Global Root G2（certifi）。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent / "data"
_intermediate_paths: list[Path] = [
    _BUNDLE_DIR / "swsresearch_intermediate_ca.crt",
]
_built: dict[str, str] = {}


def ca_bundle(name: str = "default", *extra_pem: Path) -> str:
    """返回一个 CA 包文件路径：certifi 根证书 + 指定的中间证书。

    `name` 只是缓存键；同一组输入只会落一次盘。
    """
    key = "|".join([name, *(str(p) for p in extra_pem)])
    if key in _built:
        return _built[key]

    import certifi

    parts = [Path(certifi.where()).read_text(encoding="utf-8")]
    for pem in extra_pem:
        if not pem.exists():  # 文件缺失时退回标准根证书：宁可校验失败，也不关闭校验
            continue
        parts.append(pem.read_text(encoding="utf-8"))
    payload = "\n".join(part.strip() + "\n" for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    target = Path(tempfile.gettempdir()) / f"mw-ca-bundle-{name}-{digest}.pem"
    if not target.exists():
        fd, tmp = tempfile.mkstemp(prefix=f"mw-ca-bundle-{name}-", dir=str(tempfile.gettempdir()))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, target)  # 多进程同时构建时原子落盘，内容相同
    _built[key] = str(target)
    return _built[key]


def swsresearch_verify() -> str:
    """传给 requests 的 verify 参数：申万研究的专用 CA 包。"""
    return ca_bundle("swsresearch", *_intermediate_paths)
