"""日志入口：统一 logger 命名与兜底输出。

后端此前完全没有 logging：运行期的关键事件（首启快照、子进程重算失败、更新重启、
看门狗触发）全靠 print，没有级别、没有时间戳，也无法按级别筛选或落盘。

约定：
- 各模块 `log = logutil.get(__name__)`，logger 名为 `market_workbench.<module>`。
- 没有配置过日志的进程（评分重算子进程、MCP server、单独跑的脚本）里，INFO 也会
  输出到 stderr：`get()` 会给 `market_workbench` 挂一个兜底 handler。
- ASGI 服务启动时调用 `configure()`：配置根 handler，并摘掉兜底 handler 以免双份输出。
- 命令行工具的用户可见输出（add_user.py 的交互提示、研究脚本的 `__main__` 转储）
  仍然用 print，那是程序输出而非日志。
"""
from __future__ import annotations

import logging
import sys

ROOT = "market_workbench"
_FALLBACK_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_fallback_handler: logging.Handler | None = None


def get(name: str | None = None) -> logging.Logger:
    logger = logging.getLogger(ROOT if not name else f"{ROOT}.{name}")
    _ensure_visible()
    return logger


def _ensure_visible() -> None:
    """没有任何 handler 时挂一个 INFO 级的 stderr handler。

    logging 的 lastResort 只输出 WARNING 及以上：子进程里的进度与失败原因用
    print 的时候看得到，换成 log.info 后若没有这个兜底就会消失。
    """
    global _fallback_handler
    root = logging.getLogger(ROOT)
    if not root.handlers and _fallback_handler is None:
        _fallback_handler = logging.StreamHandler(sys.stderr)
        _fallback_handler.setFormatter(logging.Formatter(_FALLBACK_FORMAT))
        root.addHandler(_fallback_handler)
    root.setLevel(logging.INFO)


def configure(level: int = logging.INFO) -> None:
    """ASGI 服务启动时调用一次。uvicorn 已配好根 handler 时只调级别，不重复加。"""
    logging_root = logging.getLogger()
    if not logging_root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FALLBACK_FORMAT))
        logging_root.addHandler(handler)
    logging_root.setLevel(level)
    logging.getLogger(ROOT).setLevel(level)
    global _fallback_handler
    if _fallback_handler is not None:
        logging.getLogger(ROOT).removeHandler(_fallback_handler)
        _fallback_handler = None
