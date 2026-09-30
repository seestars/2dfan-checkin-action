"""2dfan.com 签到模块 — 使用 Selenium + undetected-chromedriver 自动化 Chrome。

流程：启动 stealth Chrome → CDP 预置 session cookie → 打开 /checkin
     → 通过 Cloudflare（Turnstile 由 uc 伪装 + 有头模式自动解决）
     → 读取 status.json → 完成人机验证（Turnstile 自动解决 / 阿里云滑块自动拖动）
     → 点击确认提交 POST /checkin（页面内 hook 捕获响应）

签到页面为 Vue（Naive UI）应用，签到接口：
    GET  /checkin/status.json   签到状态（checked / serial_checkins / checkins_count ...）
    GET  /checkin/history.json  签到日历
    POST /checkin               JSON body：{"aliyun_captcha_verify_param": "..."}
                                或 {"cf-turnstile-response": "..."}

注意：Cloudflare 对 headless Chrome 检测严格，CI 环境请用 Xvfb 跑有头模式
     （见 .github/workflows/checkin.yml），仅在本机调试时可用 HEADLESS=true。
"""

import json
import logging
import math
import os
import random
import time

import undetected_chromedriver as uc
from selenium.common.exceptions import JavascriptException, WebDriverException
from selenium.webdriver.common.by import By

logger = logging.getLogger(__name__)

CHECKIN_URL = "https://2dfan.com/checkin"
_CF_TITLES = ("Just a moment", "请稍候")
_CF_WAIT = 90              # 等待 Cloudflare 挑战通过（秒）
_CAPTCHA_WAIT = 30         # 等待人机验证组件出现（秒）
_CONFIRM_WAIT = 15         # 滑块后等待确认按钮可用（秒）
_POST_WAIT = 20            # 点击确认后等待 POST 响应（秒）
_SLIDER_RETRIES = 3
_SUBMIT_RETRIES = 2

# ── 注入页面的 JS ─────────────────────────────────────────────────

# hook fetch / XHR，记录 POST /checkin 的响应到 window.__checkinResponses
_HOOK_JS = """
window.__checkinResponses = [];
(function () {
  function record(method, url, status, body) {
    try {
      if (String(method).toUpperCase() === 'POST'
          && String(url).indexOf('/checkin') !== -1) {
        window.__checkinResponses.push({status: status, body: body});
      }
    } catch (e) {}
  }
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      var method = (init && init.method)
        || (input && input.method) || 'GET';
      var url = typeof input === 'string' ? input
        : (input && input.url) || '';
      return origFetch.apply(this, arguments).then(function (resp) {
        if (String(method).toUpperCase() === 'POST') {
          resp.clone().text().then(function (t) {
            record(method, url, resp.status, t);
          });
        }
        return resp;
      });
    };
  }
  var origOpen = XMLHttpRequest.prototype.open;
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__method = method;
    this.__url = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    var xhr = this;
    xhr.addEventListener('load', function () {
      record(xhr.__method, xhr.__url, xhr.status, xhr.responseText);
    });
    return origSend.apply(this, arguments);
  };
})();
"""

# 页面内异步 fetch，由 execute_async_script 回调返回
_FETCH_JS = """
const path = arguments[0];
const cb = arguments[arguments.length - 1];
fetch(path, {
    headers: {Accept: 'application/json'},
    credentials: 'same-origin'
}).then(r => r.text().then(body => cb({status: r.status, body: body}))
).catch(e => cb({status: 0, body: String(e)}));
"""

# 人机验证组件状态
_CAPTCHA_STATE_JS = """
const ts = document.querySelector('input[name="cf-turnstile-response"]');
const slider = document.getElementById('aliyunCaptcha-sliding-slider');
const sr = slider ? slider.getBoundingClientRect() : null;
return {
    turnstileToken: ts ? ts.value : null,
    sliderVisible: !!(sr && sr.width > 0 && sr.height > 0)
};
"""

# 确认按钮状态 + 滑块失败提示
_CONFIRM_STATE_JS = """
const btn = [...document.querySelectorAll('button')].find(
    b => b.textContent.trim() === '确认');
const fail = document.getElementById('aliyunCaptcha-sliding-failTip');
return {
    enabled: btn ? !btn.disabled : false,
    fail: fail ? fail.textContent.trim() : ''
};
"""

# Turnstile widget iframe 位置（checkbox 在 iframe 内左侧约 30px）
_TURNSTILE_RECT_JS = """
const f = [...document.querySelectorAll('iframe')]
    .find(x => x.src.includes('challenges.cloudflare.com'));
if (!f) return null;
const r = f.getBoundingClientRect();
return {x: r.x, y: r.y, w: r.width, h: r.height};
"""

# 滑块与滑轨的视口坐标
_SLIDER_BOX_JS = """
const h = document.getElementById('aliyunCaptcha-sliding-slider');
const t = document.getElementById('aliyunCaptcha-sliding-body');
if (!h || !t) return null;
const hb = h.getBoundingClientRect();
const tb = t.getBoundingClientRect();
return {
    x0: hb.x + hb.width / 2,
    y0: hb.y + hb.height / 2,
    start: hb.x + hb.width,
    end: tb.x + tb.width
};
"""


class _StealthChrome(uc.Chrome):
    """uc.Chrome 的 __del__ 会在手动 quit 后重复清理，
    在解释器关闭阶段抛出 OSError（WinError 6）噪音，这里静默处理。"""

    def __del__(self):
        try:
            super().__del__()
        except Exception:
            pass


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


# ── CDP 鼠标（视口坐标，时序由 Python 端控制）────────────────────


def _cdp_mouse(driver, mtype: str, x: float, y: float) -> None:
    params: dict = {"type": mtype, "x": float(x), "y": float(y)}
    if mtype == "mouseMoved":
        params["button"] = "none"
    else:
        params["button"] = "left"
        params["clickCount"] = 1
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", params)


# ── Turnstile 复选框点击 ─────────────────────────────────────────


def _click_turnstile_checkbox(driver) -> bool:
    """点击 Turnstile widget 复选框（iframe 内左侧约 30px、垂直居中）。"""
    rect = driver.execute_script(_TURNSTILE_RECT_JS)
    if not rect or rect["w"] <= 0 or rect["h"] <= 0:
        return False
    x = rect["x"] + 30
    y = rect["y"] + rect["h"] / 2
    _cdp_mouse(driver, "mouseMoved", x, y)
    time.sleep(random.uniform(0.1, 0.25))
    _cdp_mouse(driver, "mousePressed", x, y)
    time.sleep(random.uniform(0.05, 0.12))
    _cdp_mouse(driver, "mouseReleased", x, y)
    logger.info("已点击 Turnstile 复选框")
    return True


# ── Cloudflare 挑战处理 ───────────────────────────────────────────


def _wait_cf(driver, timeout: int = _CF_WAIT) -> bool:
    clicked = False
    for sec in range(timeout):
        try:
            title = driver.title
        except WebDriverException:
            title = ""
        if not any(k in (title or "") for k in _CF_TITLES):
            if sec:
                logger.info("Cloudflare 验证通过（%d 秒）", sec)
            return True
        # 伪装良好的有头浏览器通常自动通过；超过 12s 未通过则尝试点 checkbox
        if not clicked and sec >= 12:
            clicked = _click_turnstile_checkbox(driver)
        if sec % 10 == 0:
            logger.info("等待 Cloudflare 验证... (%d/%ds)", sec, timeout)
        time.sleep(1)
    return False


# ── 状态读取 ─────────────────────────────────────────────────────


def _get_status(driver) -> dict:
    raw = driver.execute_async_script(_FETCH_JS, "/checkin/status.json")
    if raw.get("status") != 200:
        raise RuntimeError(
            f"获取签到状态失败: {raw.get('status')} {raw.get('body')}"
        )
    return json.loads(raw["body"])


# ── 阿里云滑块拖动 ───────────────────────────────────────────────


def _drag_slider(driver) -> None:
    """拟人拖动阿里云无缺口滑块，失败自动重试。"""
    for attempt in range(1, _SLIDER_RETRIES + 1):
        logger.info("拖动阿里云滑块（第 %d/%d 次）", attempt, _SLIDER_RETRIES)

        try:
            box = driver.execute_script(_SLIDER_BOX_JS)
        except JavascriptException:
            box = None
        if not box:
            time.sleep(1)
            continue

        x0, y0 = box["x0"], box["y0"]
        distance = box["end"] - box["start"] + 2

        _cdp_mouse(driver, "mouseMoved", x0, y0)
        time.sleep(random.uniform(0.1, 0.3))
        _cdp_mouse(driver, "mousePressed", x0, y0)

        # 先快后慢的 ease-out 轨迹 + 轻微抖动，模拟真人
        steps = random.randint(35, 50)
        for i in range(1, steps + 1):
            p = i / steps
            ease = 1 - (1 - p) ** 3
            x = x0 + distance * ease + random.uniform(-1, 1)
            y = y0 + math.sin(p * math.pi) * 2 + random.uniform(-1, 1)
            _cdp_mouse(driver, "mouseMoved", x, y)
            time.sleep(random.uniform(0.012, 0.04))

        _cdp_mouse(driver, "mouseMoved", x0 + distance, y0)
        time.sleep(0.15)
        _cdp_mouse(driver, "mouseReleased", x0 + distance, y0)

        # 等待确认按钮可用（captchaReady）
        for _ in range(_CONFIRM_WAIT):
            state = driver.execute_script(_CONFIRM_STATE_JS)
            if state["enabled"]:
                logger.info("滑块验证通过")
                return
            if state["fail"]:
                logger.warning("滑块验证失败: %s", state["fail"])
                break
            time.sleep(1)

    raise RuntimeError("阿里云滑块验证多次失败")


# ── 人机验证 ─────────────────────────────────────────────────────


def _solve_captcha(driver) -> None:
    """等待 Turnstile 自动解决；Turnstile 失败切换为阿里云滑块后自动拖动。"""
    clicked = False
    for sec in range(_CAPTCHA_WAIT):
        state = driver.execute_script(_CAPTCHA_STATE_JS)
        if state.get("turnstileToken"):
            logger.info("Turnstile 已自动解决（%ds）", sec)
            return
        if state.get("sliderVisible"):
            logger.info("Turnstile 未通过，已切换阿里云滑块")
            _drag_slider(driver)
            return
        # 超过 8s 无 token，尝试点击 widget 中的 checkbox（仅一次）
        if not clicked and sec >= 8:
            clicked = _click_turnstile_checkbox(driver)
        if sec % 5 == 0:
            logger.info("等待人机验证组件... (%d/%ds)", sec, _CAPTCHA_WAIT)
        time.sleep(1)
    raise RuntimeError("人机验证组件未出现")


# ── 提交签到 ─────────────────────────────────────────────────────


def _click_confirm(driver) -> dict | None:
    """点击弹窗「确认」，返回页面 hook 捕获的 POST /checkin 响应。"""
    driver.execute_script("window.__checkinResponses = [];")
    btn = driver.find_element(
        By.XPATH, "//button[normalize-space()='确认']"
    )
    try:
        btn.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", btn)

    for _ in range(_POST_WAIT):
        resps = driver.execute_script("return window.__checkinResponses;")
        if resps:
            r = resps[-1]
            try:
                data = json.loads(r["body"])
            except (json.JSONDecodeError, TypeError):
                data = None
            return {"status": r["status"], "body": r["body"], "json": data}
        time.sleep(1)
    return None


# ── 主入口 ───────────────────────────────────────────────────────


def checkin(user_id: str, session_cookie: str) -> CheckinResult | None:
    """
    执行 2dfan.com 签到。

    Returns:
        CheckinResult  签到成功（含累计/连续天数、积分）
        None           今日已签到，无需操作
    Raises:
        RuntimeError   Cloudflare 超时 / 人机验证失败 / 签到提交失败
    """
    headless = os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes")

    options = uc.ChromeOptions()
    options.add_argument("--window-size=1280,900")
    options.add_argument("--no-first-run")

    driver = None
    try:
        driver = _StealthChrome(options=options, headless=headless)
        driver.set_page_load_timeout(120)
        driver.set_script_timeout(30)

        # 导航前通过 CDP 预置 session cookie（无需先访问域名）
        driver.execute_cdp_cmd("Network.setCookie", {
            "name": "_project_hgc_session",
            "value": session_cookie,
            "domain": ".2dfan.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        })
        # 每个文档加载前注入响应捕获 hook
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": _HOOK_JS}
        )

        driver.get(CHECKIN_URL)
        if not _wait_cf(driver):
            raise RuntimeError("Cloudflare 验证超时")
        time.sleep(2)  # 等待 Vue 渲染与接口请求

        logger.info("页面: %s", driver.current_url)
        status = _get_status(driver)
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
        btns = driver.find_elements(By.CSS_SELECTOR, "button.n-button--large-type")
        if not btns:
            raise RuntimeError("找不到签到按钮，页面可能未正确加载")
        btns[0].click()

        for round_ in range(1, _SUBMIT_RETRIES + 1):
            _solve_captcha(driver)
            result = _click_confirm(driver)

            if result and result["status"] == 200 and result["json"]:
                j = result["json"]
                logger.info("签到成功响应: %s", j)
                return CheckinResult(
                    checkins_count=j.get("checkins_count", -1),
                    serial_checkins=j.get("serial_checkins", -1),
                    user_points=j.get("user_points", -1),
                )

            # hook 未捕获响应时，回查状态确认是否实际已签到
            fallback = None
            try:
                latest = _get_status(driver)
                if latest.get("checked"):
                    fallback = CheckinResult(
                        checkins_count=latest.get("checkins_count", -1),
                        serial_checkins=latest.get("serial_checkins", -1),
                    )
            except Exception:
                pass
            if fallback:
                logger.info("未捕获 POST 响应，但状态显示已签到")
                return fallback

            body = result["body"] if result else "<未捕获 POST 响应>"
            code = result["status"] if result else "-"
            logger.warning(
                "提交失败（%d/%d）: status=%s body=%.200s",
                round_, _SUBMIT_RETRIES, code, body,
            )

        raise RuntimeError("签到提交多次失败")

    except Exception:
        # 保存调试快照
        if driver is not None:
            try:
                base = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    f"debug_{user_id}",
                )
                driver.save_screenshot(base + ".png")
                with open(base + ".html", "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                logger.info("已保存调试快照: %s.png/.html", base)
            except Exception:
                pass
        raise
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
