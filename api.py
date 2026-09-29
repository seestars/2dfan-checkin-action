"""2dfan.com 签到模块 — 使用 Playwright 自动化 Chrome 完成签到。

流程：启动 Chrome → 设置 session cookie → 打开 /checkin → 通过 Cloudflare
     → 读取 status.json → 完成人机验证（Turnstile 自动解决 / 阿里云滑块自动拖动）
     → 点击确认提交 POST /checkin

签到页面为 Vue（Naive UI）应用，签到接口：
    GET  /checkin/status.json   签到状态（checked / serial_checkins / checkins_count ...）
    GET  /checkin/history.json  签到日历
    POST /checkin               JSON body：{"aliyun_captcha_verify_param": "..."}
                                或 {"cf-turnstile-response": "..."}
"""

import json
import logging
import math
import os
import random

from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeoutError
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

CHECKIN_URL = "https://2dfan.com/checkin"
_CF_TITLES = ("Just a moment", "请稍候")
_CAPTCHA_WAIT = 30          # 等待人机验证组件出现（秒）
_CONFIRM_WAIT = 15          # 滑块后等待确认按钮可用（秒）
_SLIDER_RETRIES = 3
_SUBMIT_RETRIES = 2

# ── 页面内 fetch，自动携带 cookie ──────────────────────────────────

_FETCH_JS = """
async (path) => {
    const r = await fetch(path, {
        headers: {Accept: 'application/json'},
        credentials: 'same-origin'
    });
    return {status: r.status, body: await r.text()};
}
"""

# 人机验证组件状态
_CAPTCHA_STATE_JS = """
() => {
    const ts = document.querySelector('input[name="cf-turnstile-response"]');
    const slider = document.getElementById('aliyunCaptcha-sliding-slider');
    const sr = slider ? slider.getBoundingClientRect() : null;
    return {
        turnstileToken: ts ? ts.value : null,
        sliderVisible: !!(sr && sr.width > 0 && sr.height > 0)
    };
}
"""

# 确认按钮状态 + 滑块失败提示
_CONFIRM_STATE_JS = """
() => {
    const btn = [...document.querySelectorAll('button')].find(
        b => b.textContent.trim() === '确认');
    const fail = document.getElementById('aliyunCaptcha-sliding-failTip');
    return {
        enabled: btn ? !btn.disabled : false,
        fail: fail ? fail.textContent.trim() : ''
    };
}
"""


class CheckinResult:
    def __init__(
        self,
        checkins_count: int = -1,
        serial_checkins: int = -1,
        user_points: int = -1,
    ):
        self.checkins_count = checkins_count
        self.serial_checkins = serial_checkins
        self.user_points = user_points


# ── Cloudflare 挑战处理 ───────────────────────────────────────────


async def _wait_cf(page, timeout: int = 90) -> bool:
    for sec in range(timeout):
        title = await page.title()
        if not any(k in (title or "") for k in _CF_TITLES):
            if sec:
                logger.info("Cloudflare 验证通过（%d 秒）", sec)
            return True
        if sec % 10 == 0:
            logger.info("等待 Cloudflare 验证... (%d/%ds)", sec, timeout)
        await page.wait_for_timeout(1000)
    return False


# ── 状态读取 ─────────────────────────────────────────────────────


async def _get_status(page) -> dict:
    raw = await page.evaluate(_FETCH_JS, "/checkin/status.json")
    if raw.get("status") != 200:
        raise RuntimeError(f"获取签到状态失败: {raw.get('status')} {raw.get('body')}")
    return json.loads(raw["body"])


# ── 阿里云滑块拖动 ───────────────────────────────────────────────


async def _drag_slider(page) -> None:
    """拟人拖动阿里云无缺口滑块，失败自动重试。"""
    for attempt in range(1, _SLIDER_RETRIES + 1):
        logger.info("拖动阿里云滑块（第 %d/%d 次）", attempt, _SLIDER_RETRIES)

        handle = page.locator("#aliyunCaptcha-sliding-slider")
        track = page.locator("#aliyunCaptcha-sliding-body")
        hbox = await handle.bounding_box()
        tbox = await track.bounding_box()
        if not hbox or not tbox:
            await page.wait_for_timeout(1000)
            continue

        x0 = hbox["x"] + hbox["width"] / 2
        y0 = hbox["y"] + hbox["height"] / 2
        distance = (tbox["x"] + tbox["width"]) - (hbox["x"] + hbox["width"]) + 2

        await page.mouse.move(x0, y0)
        await page.mouse.down()

        # 先快后慢的 ease-out 轨迹 + 轻微抖动，模拟真人
        steps = random.randint(35, 50)
        for i in range(1, steps + 1):
            p = i / steps
            ease = 1 - (1 - p) ** 3
            x = x0 + distance * ease + random.uniform(-1, 1)
            y = y0 + math.sin(p * math.pi) * 2 + random.uniform(-1, 1)
            await page.mouse.move(x, y)
            await page.wait_for_timeout(random.randint(12, 40))

        await page.mouse.move(x0 + distance, y0)
        await page.wait_for_timeout(150)
        await page.mouse.up()

        # 等待确认按钮可用（captchaReady）
        for _ in range(_CONFIRM_WAIT):
            state = await page.evaluate(_CONFIRM_STATE_JS)
            if state["enabled"]:
                logger.info("滑块验证通过")
                return
            if state["fail"]:
                logger.warning("滑块验证失败: %s", state["fail"])
                break
            await page.wait_for_timeout(1000)

    raise RuntimeError("阿里云滑块验证多次失败")


# ── 人机验证 ─────────────────────────────────────────────────────


async def _solve_captcha(page) -> None:
    """等待 Turnstile 自动解决；Turnstile 失败切换为阿里云滑块后自动拖动。"""
    for sec in range(_CAPTCHA_WAIT):
        state = await page.evaluate(_CAPTCHA_STATE_JS)
        token = state.get("turnstileToken")
        if token:
            logger.info("Turnstile 已自动解决（%ds）", sec)
            return
        if state.get("sliderVisible"):
            logger.info("Turnstile 未通过，已切换阿里云滑块")
            await _drag_slider(page)
            return
        if sec % 5 == 0:
            logger.info("等待人机验证组件... (%d/%ds)", sec, _CAPTCHA_WAIT)
        await page.wait_for_timeout(1000)
    raise RuntimeError("人机验证组件未出现")


# ── 提交签到 ─────────────────────────────────────────────────────


async def _click_confirm(page) -> dict:
    """点击弹窗「确认」并捕获 POST /checkin 响应。"""
    try:
        async with page.expect_response(
            lambda r: r.request.method == "POST" and "/checkin" in r.url,
            timeout=30000,
        ) as resp_info:
            await page.get_by_role("button", name="确认").click()
        resp = await resp_info.value
    except PWTimeoutError:
        raise RuntimeError("签到提交超时（未捕获 POST 响应）")

    body = await resp.text()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = None
    return {"status": resp.status, "body": body, "json": data}


# ── 主入口 ───────────────────────────────────────────────────────


async def checkin(user_id: str, session_cookie: str) -> CheckinResult | None:
    """
    执行 2dfan.com 签到。

    Returns:
        CheckinResult  签到成功（含累计/连续天数、积分）
        None           今日已签到，无需操作
    Raises:
        RuntimeError   Cloudflare 超时 / 人机验证失败 / 签到提交失败
    """
    headless = os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes")

    async with async_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            browser = await p.chromium.launch(
                headless=headless,
                channel="chrome",
                args=launch_args,
            )
        except PWError:
            logger.info("未找到系统 Chrome，改用 Playwright 内置 Chromium")
            browser = await p.chromium.launch(
                headless=headless,
                args=launch_args,
            )
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900}
            )
            await context.add_cookies([{
                "name": "_project_hgc_session",
                "value": session_cookie,
                "domain": ".2dfan.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }])
            page = await context.new_page()

            await page.goto(CHECKIN_URL, wait_until="domcontentloaded",
                            timeout=120000)
            if not await _wait_cf(page):
                raise RuntimeError("Cloudflare 验证超时")
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeoutError:
                pass

            logger.info("页面: %s", page.url)
            status = await _get_status(page)
            logger.info(
                "签到状态: checked=%s 连续=%s 累计=%s",
                status.get("checked"),
                status.get("serial_checkins"),
                status.get("checkins_count"),
            )

            if status.get("checked"):
                logger.info("今日已签到")
                return None

            # 打开人机验证弹窗
            btn = page.locator("button.n-button--large-type").first
            if await btn.count() == 0:
                raise RuntimeError("找不到签到按钮，页面可能未正确加载")
            await btn.click()

            for round_ in range(1, _SUBMIT_RETRIES + 1):
                await _solve_captcha(page)
                result = await _click_confirm(page)

                if result["status"] == 200 and result["json"]:
                    j = result["json"]
                    logger.info("签到成功响应: %s", j)
                    return CheckinResult(
                        checkins_count=j.get("checkins_count", -1),
                        serial_checkins=j.get("serial_checkins", -1),
                        user_points=j.get("user_points", -1),
                    )

                logger.warning(
                    "提交失败（%d/%d）: status=%s body=%.200s",
                    round_, _SUBMIT_RETRIES, result["status"], result["body"],
                )

            raise RuntimeError("签到提交多次失败")

        except Exception:
            # 保存调试快照
            try:
                debug_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    f"debug_{user_id}.html",
                )
                await page.screenshot(path=debug_path.replace(".html", ".png"))
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(await page.content())
                logger.info("已保存调试快照: %s", debug_path)
            except Exception:
                pass
            raise
        finally:
            await browser.close()
