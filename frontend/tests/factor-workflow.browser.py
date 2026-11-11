"""Real browser/API/worker smoke check, using isolated synthetic bars and accounts.

Run with an existing Python environment containing Playwright. No production files
or external data services are used. The frontend must already be built.
"""
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "backend/.venv/bin/python"
SCREENSHOTS = ROOT / "outputs/factor-builder-2026-10-22"
SERVER = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import numpy as np
import pandas as pd
root = Path(sys.argv[1]); folder = root / "factor"; folder.mkdir()
rng = np.random.default_rng(43)
dates = pd.bdate_range("2019-01-02", periods=900).strftime("%Y-%m-%d").tolist()
parts = []
for i in range(60):
    price = (10 + i / 3) * np.exp(np.cumsum(rng.normal(0.0002, 0.012, len(dates))))
    parts.append(pd.DataFrame({"date": dates, "code": f"{600000+i:06d}", "open": price,
        "high": price*1.01, "low": price*.99, "close": price, "volume": 1e6, "amount": price*1e6}))
panel = pd.concat(parts, ignore_index=True)
panel.to_csv(folder / "bars.csv.gz", index=False, compression="gzip")
pd.DataFrame({"code": [f"{600000+i:06d}" for i in range(60)], "name": ["Synthetic" for _ in range(60)]}).to_csv(folder / "instruments.csv.gz", index=False, compression="gzip")
(folder / "calendar.json").write_text(json.dumps({"dates": dates, "index_close": {d: 3000+i for i,d in enumerate(dates)}}))
(folder / "catalog.json").write_text(json.dumps({"built_at": "synthetic-ui-check", "stocks": 60, "rows": len(panel), "date_min": dates[0], "date_max": dates[-1]}))
import users
users.register("factor-ui", "synthetic-test-only")
users.register("other-ui", "synthetic-test-only")
import app
app._start_background_jobs = lambda: None
import uvicorn
uvicorn.run(app.app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
'''


def check():
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mw-factor-browser-") as temp:
        folder = Path(temp)
        script = folder / "server.py"
        script.write_text(SERVER, encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = {**os.environ, "MW_DATA_DIR": temp, "MW_USERS_DB": str(folder / "users.db"),
               "MW_REPORTS_DIR": str(folder / "reports"), "MW_API_KEY": ""}
        with (folder / "server.log").open("w+", encoding="utf-8") as log:
            proc = subprocess.Popen([str(PYTHON), str(script), temp, str(port), str(ROOT / "backend")],
                                    cwd=ROOT / "backend", env=env, stdout=log, stderr=log)
            try:
                def api(path, method="GET", body=None, token=None):
                    data = None if body is None else json.dumps(body).encode()
                    headers = {"Content-Type": "application/json"}
                    if token:
                        headers["X-VR-User-Token"] = token
                    request = Request(base + "/api" + path, data=data, method=method, headers=headers)
                    with urlopen(request, timeout=30) as response:
                        payload = json.load(response)
                    return payload.get("data", payload)

                deadline = time.monotonic() + 30
                while True:
                    try:
                        api("/health")
                        break
                    except URLError:
                        if proc.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            raise AssertionError(log.read())
                        time.sleep(.2)
                login = api("/auth/login", "POST", {"username": "factor-ui", "password": "synthetic-test-only"})
                token = login["token"]
                sid = None

                def detail():
                    return api(f"/factor/research/{sid}", token=token)

                def wait_job(kind, count):
                    end = time.monotonic() + 60
                    while time.monotonic() < end:
                        jobs = sorted((r for r in detail()["runs"] if r["kind"] == kind), key=lambda r: r["created_at"])
                        if len(jobs) >= count and jobs[-1]["status"] in ("completed", "failed", "cancelled"):
                            assert jobs[-1]["status"] == "completed", jobs[-1].get("error")
                            return jobs[-1]
                        time.sleep(.2)
                    raise AssertionError("worker did not finish")

                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.add_init_script(f"localStorage.setItem('vr-auth-token',{json.dumps(token)});localStorage.setItem('vr-auth-user',JSON.stringify({{id:{login['user_id']},username:'factor-ui'}}));")
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base + "/factors")
                    page.locator("summary").filter(has_text="数据：").first.wait_for()
                    builder = page.locator("form").filter(has=page.get_by_role("heading", name="构建因子"))
                    # 首次运行只需模板/参数；模拟研究创建成功、实验提交失败。
                    failed = {"once": False}
                    def reject_first(route):
                        if route.request.method == "POST" and not failed["once"]:
                            failed["once"] = True
                            route.fulfill(status=400, content_type="application/json", body=json.dumps({"detail": "模拟实验提交失败"}))
                        else:
                            route.continue_()
                    page.route("**/api/factor/research/*/runs", reject_first)
                    builder.get_by_role("button", name="运行检验", exact=True).click()
                    builder.get_by_role("alert").filter(has_text="模拟实验提交失败").wait_for()
                    assert len(api("/factor/research", token=token)) == 1
                    sid = page.url.split("#factor-study=")[1]
                    builder.get_by_role("button", name="另存版本并运行", exact=True).click()
                    first = wait_job("evaluate", 1)
                    assert len(api("/factor/research", token=token)) == 1
                    assert first["definition"] == {"expr": "close / close[20] - 1", "direction": "high"}
                    page.get_by_role("button", name="刷新", exact=True).click()
                    page.get_by_role("button", name="设为基线", exact=True).first.wait_for()
                    page.get_by_role("button", name="设为基线", exact=True).first.click()
                    builder.get_by_label("回看窗口（交易日）").fill("60")
                    page.get_by_text("右侧为上次已运行版本，当前修改尚未检验。", exact=True).wait_for()
                    # 继续修改旧版本时，可取消覆盖未提交的60日草稿。
                    page.once("dialog", lambda dialog: dialog.dismiss())
                    page.get_by_role("button", name="继续修改", exact=True).first.click()
                    assert builder.get_by_label("回看窗口（交易日）").input_value() == "60"
                    # 首次公式入口带入当前模板；取消返回模板保留公式。
                    builder.get_by_role("button", name="写公式 / 内置因子", exact=True).click()
                    assert builder.get_by_role("textbox").first.input_value() == "close / close[60] - 1"
                    page.once("dialog", lambda dialog: dialog.dismiss())
                    builder.get_by_role("button", name="选模板", exact=True).click()
                    assert builder.get_by_role("textbox").first.input_value() == "close / close[60] - 1"
                    page.once("dialog", lambda dialog: dialog.accept())
                    builder.get_by_role("button", name="选模板", exact=True).click()
                    builder.get_by_role("button", name="另存版本并运行", exact=True).click()
                    second = wait_job("evaluate", 2)
                    assert first["definition"]["expr"] == "close / close[20] - 1"
                    assert second["definition"]["expr"] == "close / close[60] - 1"
                    page.get_by_role("button", name="刷新", exact=True).click()
                    page.get_by_label("选择 20日价格动量", exact=True).check()
                    page.get_by_label("选择 60日价格动量", exact=True).check()
                    page.locator("h1").scroll_into_view_if_needed()
                    page.screenshot(path=str(SCREENSHOTS / "01-builder.png"), full_page=True)
                    page.get_by_role("button", name="对比选中", exact=True).click()
                    comparison = wait_job("compare", 1)
                    assert comparison["result"]["common"]["by_horizon"]["5"]["paired_rows"] > 0
                    assert len(comparison["result"]["correlation"]["matrix"]) == 2
                    page.get_by_role("button", name="刷新", exact=True).click()
                    page.get_by_role("button", name="共同样本", exact=True).wait_for()
                    page.screenshot(path=str(SCREENSHOTS / "02-compare.png"), full_page=True)
                    page.get_by_role("button", name="锁定选中实验", exact=True).click()
                    page.get_by_role("dialog").get_by_role("button", name="锁定", exact=True).click()
                    page.get_by_role("button", name="记录结论", exact=True).click()
                    assert detail()["study"]["freezes"]
                    page.get_by_role("button", name="运行验证期", exact=True).click()
                    validation = wait_job("evaluate", 3)
                    assert validation["phase"] == "validation"
                    page.get_by_role("button", name="刷新", exact=True).click()
                    page.get_by_role("button", name="研究结论", exact=True).click()
                    page.get_by_role("button", name="查看留出期", exact=True).click()
                    page.get_by_role("dialog").get_by_role("button", name="查看留出期", exact=True).click()
                    holdout = wait_job("evaluate", 4)
                    assert holdout["phase"] == "holdout"
                    assert detail()["study"]["holdout_used_at"] is not None
                    page.get_by_role("button", name="刷新", exact=True).click()
                    page.get_by_role("button", name="研究结论", exact=True).click()
                    page.get_by_label("备注（原因和下一步）", exact=True).fill("合成行情验收：继续验证，不判断实盘有效。")
                    page.get_by_role("button", name="保存结论", exact=True).click()
                    page.reload()
                    page.get_by_role("button", name="研究结论", exact=True).click()
                    assert detail()["study"]["decisions"][-1]["note"] == "合成行情验收：继续验证，不判断实盘有效。"
                    page.screenshot(path=str(SCREENSHOTS / "03-decision.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.wait_for_timeout(200)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(SCREENSHOTS / "04-mobile.png"), full_page=True)
                    builder.get_by_label("回看窗口（交易日）").fill("123")
                    other_login = api("/auth/login", "POST", {"username": "other-ui", "password": "synthetic-test-only"})
                    page.evaluate("token => localStorage.setItem('vr-auth-token', token)", other_login["token"])
                    page.wait_for_timeout(1300)
                    assert builder.get_by_label("回看窗口（交易日）").input_value() == "20"
                    assert not errors, errors
                    browser.close()
                other = api("/auth/login", "POST", {"username": "other-ui", "password": "synthetic-test-only"})
                assert api("/factor/research", token=other["token"]) == []
                try:
                    api(f"/factor/research/{sid}", token=other["token"])
                    raise AssertionError("another account read the study")
                except HTTPError as error:
                    assert error.code == 404
                print("PASS: real HTTP + SQLite + calculation subprocess + browser; isolated synthetic input only")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    check()
