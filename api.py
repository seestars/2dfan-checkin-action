"""2dfan.com 签到模块 — 使用 Selenium + undetected-chromedriver 自动化 Chrome。

流程：启动 stealth Chrome → CDP 预置 session cookie → 打开 /checkin
     → 通过 Cloudflare（Turnstile 由 uc 伪装 + 有头模式自动解决）
     → 读取 status.json → 先完成页面级 Turnstile（若有）→ 点击签到
     → 弹窗时完成人机验证（Turnstile / 阿里云滑块；站点未启用验证则跳过）
     → 提交 POST /checkin（页面内 hook 捕获响应；422 需验证时自动补验后重提）

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
import os
import random
import re
import subprocess
import sys
import time

import undetected_chromedriver as uc
from selenium.common.exceptions import JavascriptException, WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

logger = logging.getLogger(__name__)

CHECKIN_URL = "https://2dfan.com/checkin"
_CF_TITLES = ("Just a moment", "请稍候")
_CF_WAIT = 90              # 等待 Cloudflare 挑战通过（秒）
_CAPTCHA_WAIT = 30         # 等待人机验证组件出现（秒）
_CONFIRM_WAIT = 16         # 滑块后等待确认按钮可用（每 0.5s 轮询）
_POST_WAIT = 20            # 点击确认后等待 POST 响应（秒）
_MODAL_WAIT = 10           # 等待人机验证弹窗挂载（每 0.5s 轮询）
_SLIDER_RETRIES = 3
_SUBMIT_RETRIES = 3        # 每轮重开弹窗、获取全新验证组件

# ── 注入页面的 JS ─────────────────────────────────────────────────

# hook fetch / XHR：
#   window.__checkinResponses — POST /checkin 的请求体 + 响应（请求体用于判断
#                               提交时验证码参数是否真的存在，不记录完整 token）
#   window.__captchaNet       — 阿里云验证码相关的跨域请求，便于定位风控拒绝
_HOOK_JS = """
window.__checkinResponses = [];
window.__captchaNet = [];
(function () {
  function recordCheckin(url, reqBody, status, body) {
    try {
      if (String(url).indexOf('/checkin') !== -1) {
        window.__checkinResponses.push({
          status: status,
          reqBody: reqBody == null ? null : String(reqBody).slice(0, 2000),
          body: body == null ? null : String(body).slice(0, 2000)
        });
      }
    } catch (e) {}
  }
  function recordCaptchaNet(method, url, status, body) {
    try {
      var u = String(url);
      if (u.indexOf('2dfan.com') === -1
          && (u.indexOf('aliyun') !== -1 || u.indexOf('captcha') !== -1
              || u.indexOf('nvc') !== -1)) {
        window.__captchaNet.push({
          method: String(method).toUpperCase(),
          url: u.slice(0, 300),
          status: status,
          body: body == null ? null : String(body).slice(0, 500)
        });
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
      var reqBody = init && init.body;
      return origFetch.apply(this, arguments).then(function (resp) {
        if (String(method).toUpperCase() === 'POST') {
          resp.clone().text().then(function (t) {
            recordCheckin(url, reqBody, resp.status, t);
          }).catch(function () {});
        }
        resp.clone().text().then(function (t) {
          recordCaptchaNet(method, url, resp.status, t);
        }).catch(function () {});
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
  XMLHttpRequest.prototype.send = function (body) {
    var xhr = this;
    xhr.addEventListener('load', function () {
      var t = '';
      try { t = xhr.responseText; } catch (e) {}
      if (String(xhr.__method).toUpperCase() === 'POST') {
        recordCheckin(xhr.__url, body, xhr.status, t);
      }
      recordCaptchaNet(xhr.__method, xhr.__u || xhr.__url, xhr.status, t);
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

# 无弹窗场景直接 POST /checkin；若页面已有 Turnstile token 则一并带上
_DIRECT_POST_JS = """
const cb = arguments[arguments.length - 1];
const meta = document.querySelector('meta[name="csrf-token"]');
const headers = {
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'X-Requested-With': 'XMLHttpRequest'
};
if (meta && meta.content) headers['X-CSRF-Token'] = meta.content;
const body = {};
const ts = document.querySelector(
    'input[name="cf-turnstile-response"], input[id*="cf-chl"][id$="_response"]'
);
if (ts && ts.value) body['cf-turnstile-response'] = ts.value;
const aliyun = document.querySelector('input[name="aliyun_captcha_verify_param"]');
if (aliyun && aliyun.value) body['aliyun_captcha_verify_param'] = aliyun.value;
fetch('/checkin', {
    method: 'POST',
    headers: headers,
    credentials: 'same-origin',
    body: JSON.stringify(body)
}).then(r => r.text().then(body => cb({status: r.status, body: body}))
).catch(e => cb({status: 0, body: String(e)}));
"""

# 人机验证组件状态（captchaPresent 为假表示站点暂未启用验证，可直接确认）
# 新版 Cloudflare widget 用 cf-chl-widget-* 命名，id 不含 "turnstile"；
# token 输入框存在（哪怕 value 为空）即视为验证组件已挂载。
_CAPTCHA_STATE_JS = """
const ts = document.querySelector(
    'input[name="cf-turnstile-response"], input[id*="cf-chl"][id$="_response"]'
);
const slider = document.getElementById('aliyunCaptcha-sliding-slider');
const sr = slider ? slider.getBoundingClientRect() : null;
const tsWidget = !!document.querySelector(
    '[id*="turnstile"], [class*="turnstile"], [class*="cf-turnstile"]'
    + ', [id*="cf-chl"], [class*="cf-chl"], [data-sitekey]'
);
const tsIframe = [...document.querySelectorAll('iframe')].some(x => {
    const s = x.src || '';
    return s.includes('challenges.cloudflare.com')
        || s.includes('turnstile')
        || (x.id || '').includes('turnstile')
        || (x.id || '').includes('cf-chl')
        || String(x.className || '').includes('turnstile');
});
return {
    turnstileToken: ts ? ts.value : null,
    sliderVisible: !!(sr && sr.width > 0 && sr.height > 0),
    modalMounted: !!document.querySelector('.n-modal'),
    captchaPresent: !!(
        document.querySelector('.captcha-render-area')
        || tsWidget
        || tsIframe
        || (sr && sr.width > 0 && sr.height > 0)
        || ts
    )
};
"""

# 确认按钮状态（限定在验证弹窗内）+ 滑块失败提示
_CONFIRM_STATE_JS = """
const modal = [...document.querySelectorAll('.n-modal')].pop();
const btn = modal ? (
    [...modal.querySelectorAll('button')].find(
        b => b.textContent.trim() === '确认')
    || modal.querySelector('.n-button--primary-type')
) : null;
const fail = document.getElementById('aliyunCaptcha-sliding-failTip');
return {
    enabled: btn ? !btn.disabled : false,
    fail: fail ? fail.textContent.trim() : ''
};
"""

# Turnstile widget 位置（checkbox 在 widget 内左侧约 30px）
# 新版 widget 容器 id 形如 cf-chl-widget-XXXX（响应 input 为 ..._response）
_TURNSTILE_RECT_JS = """
const input = document.querySelector(
    'input[name="cf-turnstile-response"], input[id*="cf-chl"][id$="_response"]'
);
let c = document.querySelector(
    '[id^="cf-chl-widget"]:not([id$="_response"])'
);
if (!c && input) {
    // 沿 input 向上找尺寸接近 Turnstile 的盒子（约 300×65）
    let p = input.parentElement;
    while (p && p !== document.body) {
        const r = p.getBoundingClientRect();
        if (r.width > 180 && r.width < 500 && r.height > 40 && r.height < 120) {
            c = p;
            break;
        }
        p = p.parentElement;
    }
    if (!c) c = input.parentElement;
}
if (!c) {
    c = document.querySelector(
        '.cf-turnstile, [data-sitekey]'
        + ', [id*="turnstile"], [class*="cf-turnstile"], [id*="cf-chl"]'
    );
}
if (!c) {
    c = [...document.querySelectorAll('iframe')].find(x => {
        const s = x.src || '';
        return s.includes('challenges.cloudflare.com')
            || s.includes('turnstile')
            || (x.id || '').includes('cf-chl');
    });
}
if (!c) {
    // 可见 iframe 兜底（src 可能为空，用尺寸识别 Turnstile）
    c = [...document.querySelectorAll('iframe')].find(x => {
        const r = x.getBoundingClientRect();
        return r.width > 180 && r.height > 40 && r.height < 140;
    });
}
if (!c) return null;
c.scrollIntoView && c.scrollIntoView({block: 'center'});
const r = c.getBoundingClientRect();
if (r.width <= 0 || r.height <= 0) return null;
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


# ── Chrome 版本检测 ──────────────────────────────────────────────


def _detect_chrome_major() -> int | None:
    """检测本机 Chrome 主版本号。

    undetected-chromedriver 默认下载最新 Stable 的 chromedriver，不检测
    本地 Chrome 版本；当系统 Chrome 落后最新版一个大版本（如 CI runner
    镜像）时会 session not created，因此显式检测后传 version_main。
    检测失败返回 None，由 uc 回退到最新版。
    """
    if sys.platform.startswith(("linux", "cygwin")):
        for exe in ("google-chrome", "google-chrome-stable",
                    "chromium", "chromium-browser"):
            try:
                out = subprocess.check_output(
                    [exe, "--version"], stderr=subprocess.DEVNULL, timeout=15
                ).decode()
            except (OSError, subprocess.SubprocessError):
                continue
            m = re.search(r"(\d+)\.", out)
            if m:
                return int(m.group(1))
        return None

    if sys.platform == "darwin":
        exe = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        try:
            out = subprocess.check_output(
                [exe, "--version"], stderr=subprocess.DEVNULL, timeout=15
            ).decode()
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None

    # Windows：Chrome Application 目录下有以完整版本号命名的文件夹
    roots = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        app_dir = os.path.join(root, "Google", "Chrome", "Application")
        if not os.path.isdir(app_dir):
            continue
        for name in os.listdir(app_dir):
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", name):
                return int(name.split(".")[0])
    return None


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
    """点击 Turnstile widget 复选框（widget 内左侧约 30px、垂直居中）。"""
    rect = driver.execute_script(_TURNSTILE_RECT_JS)
    if not rect or rect["w"] <= 0 or rect["h"] <= 0:
        logger.info("未定位到 Turnstile widget")
        return False
    x = rect["x"] + 30
    y = rect["y"] + rect["h"] / 2
    logger.info(
        "点击 Turnstile 复选框 @(%.0f,%.0f) widget=(%.0f,%.0f %.0fx%.0f)",
        x, y, rect["x"], rect["y"], rect["w"], rect["h"],
    )
    _cdp_mouse(driver, "mouseMoved", x, y)
    time.sleep(random.uniform(0.1, 0.25))
    _cdp_mouse(driver, "mousePressed", x, y)
    time.sleep(random.uniform(0.05, 0.12))
    _cdp_mouse(driver, "mouseReleased", x, y)
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


def _drag_path(x0: float, y0: float, distance: float):
    """生成拟人拖动轨迹，返回 (轨迹名称, [(x, y, 停留秒), ...])。

    三种轨迹随机切换，避免固定轨迹被风控指纹化：
      ease      先快后慢一次到位
      overshoot 轻微冲过头再回拉（真人常见动作）
      stall     中途停顿后继续
    """
    profile = random.choices(
        ("ease", "overshoot", "stall"), weights=(5, 3, 2)
    )[0]
    points: list[tuple[float, float, float]] = []
    drift = 0.0  # y 轴随机游走累计偏移

    def push(x: float, lo: float = 0.012, hi: float = 0.04) -> None:
        nonlocal drift
        drift += random.uniform(-0.9, 0.9)
        drift = max(-3.5, min(3.5, drift))
        points.append((x, y0 + drift, random.uniform(lo, hi)))

    if profile == "overshoot":
        target = distance * random.uniform(1.02, 1.045)
        steps = random.randint(26, 38)
        for i in range(1, steps + 1):
            p = i / steps
            push(x0 + target * (1 - (1 - p) ** 3))
        points.append((x0 + target, y0 + drift, random.uniform(0.18, 0.45)))
        back = random.randint(7, 12)
        for i in range(1, back + 1):
            p = i / back
            push(x0 + target + (distance - target) * p, 0.015, 0.04)
    elif profile == "stall":
        split = random.uniform(0.55, 0.72)
        steps1 = random.randint(16, 24)
        for i in range(1, steps1 + 1):
            p = i / steps1
            push(x0 + distance * split * (1 - (1 - p) ** 2))
        points.append(
            (x0 + distance * split, y0 + drift, random.uniform(0.35, 0.9))
        )
        steps2 = random.randint(16, 26)
        for i in range(1, steps2 + 1):
            p = i / steps2
            base = distance * split
            push(x0 + base + (distance - base) * (1 - (1 - p) ** 3))
    else:
        steps = random.randint(30, 48)
        for i in range(1, steps + 1):
            p = i / steps
            push(x0 + distance * (1 - (1 - p) ** 3))

    return profile, points


def _drag_once(driver, box: dict) -> str:
    """执行一次滑块拖动，返回轨迹名称。"""
    x0, y0 = box["x0"], box["y0"]
    distance = box["end"] - box["start"] + random.uniform(0, 2)
    profile, points = _drag_path(x0, y0, distance)

    # 从附近随机位置移入滑块（真人光标不会直接出现在手柄上）
    _cdp_mouse(driver, "mouseMoved",
               x0 + random.uniform(-70, 70), y0 + random.uniform(-30, 30))
    time.sleep(random.uniform(0.05, 0.15))
    _cdp_mouse(driver, "mouseMoved",
               x0 + random.uniform(-25, 25), y0 + random.uniform(-10, 10))
    time.sleep(random.uniform(0.05, 0.15))
    _cdp_mouse(driver, "mouseMoved", x0, y0)
    time.sleep(random.uniform(0.08, 0.25))  # 按下前的短暂犹豫

    _cdp_mouse(driver, "mousePressed", x0, y0)
    for x, y, delay in points:
        _cdp_mouse(driver, "mouseMoved", x, y)
        time.sleep(delay)

    final_x = points[-1][0]
    _cdp_mouse(driver, "mouseMoved", final_x, y0)
    time.sleep(random.uniform(0.1, 0.2))
    _cdp_mouse(driver, "mouseReleased", final_x, y0)
    return profile


def _wait_slider_ready(driver):
    """滑块释放后等待确认按钮可用。

    Returns:
        (True, "")      按钮连续可用（排除“假就绪”）
        (False, fail)   出现滑块失败提示
        (None, "")      等待超时
    """
    enabled_for = 0
    for _ in range(_CONFIRM_WAIT):
        state = driver.execute_script(_CONFIRM_STATE_JS)
        if state["fail"]:
            return False, state["fail"]
        if state["enabled"]:
            enabled_for += 1
            if enabled_for >= 2:
                return True, ""
        time.sleep(0.5)
    return None, ""


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

        profile = _drag_once(driver, box)
        logger.info("本次拖动轨迹: %s", profile)

        ok, msg = _wait_slider_ready(driver)
        if ok is True:
            logger.info("滑块验证通过")
            return
        if ok is False:
            logger.warning("滑块验证失败: %s，准备重试", msg)
            time.sleep(random.uniform(0.5, 1.2))

    raise RuntimeError("阿里云滑块验证多次失败")


# ── 人机验证 ─────────────────────────────────────────────────────


def _solve_captcha(driver) -> bool:
    """完成人机验证；站点暂未启用验证时直接放行。

    - captchaPresent 为假且出现「确认」按钮：视为无需验证，返回 True
    - Turnstile 自动出 token / 切换阿里云滑块：走原有求解逻辑
    - 弹窗未挂载：返回 False（调用方应重开弹窗重试）
    """
    clicked = False
    no_captcha_seen = 0
    for sec in range(_CAPTCHA_WAIT):
        state = driver.execute_script(_CAPTCHA_STATE_JS)
        if not state.get("modalMounted"):
            if _checkin_response_ready(driver):
                return True
            return False
        if state.get("turnstileToken"):
            logger.info("Turnstile 已自动解决（%ds）", sec)
            return True
        if state.get("sliderVisible"):
            logger.info("Turnstile 未通过，已切换阿里云滑块")
            _drag_slider(driver)
            return True
        if not state.get("captchaPresent"):
            no_captcha_seen += 1
            if no_captcha_seen >= 3:
                confirm = driver.execute_script(_CONFIRM_STATE_JS)
                if confirm.get("enabled"):
                    logger.info("无人机验证组件，直接确认")
                    return True
        else:
            no_captcha_seen = 0
        # 超过 8s 无 token，尝试点击 widget 中的 checkbox（仅一次）
        if not clicked and sec >= 8 and state.get("captchaPresent"):
            clicked = _click_turnstile_checkbox(driver)
        if sec % 5 == 0:
            logger.info("等待人机验证组件... (%d/%ds)", sec, _CAPTCHA_WAIT)
        time.sleep(1)
    return False


# ── 人机验证弹窗生命周期 ─────────────────────────────────────────


def _modal_open(driver) -> bool:
    """验证/确认弹窗是否已挂载（Naive UI 默认 display-directive=if，关闭即卸载）。"""
    return bool(driver.execute_script(
        "return !!document.querySelector('.captcha-render-area, .n-modal');"
    ))


def _checkin_response_ready(driver) -> bool:
    """页面 hook 是否已捕获 POST /checkin 响应（签到按钮直提场景）。"""
    try:
        resps = driver.execute_script("return window.__checkinResponses;") or []
    except WebDriverException:
        return False
    return bool(resps)


def _take_checkin_response(driver) -> dict | None:
    """取出 hook 捕获的最近一次 POST /checkin 响应（与 _click_confirm 同构）。"""
    resps = driver.execute_script("return window.__checkinResponses;") or []
    if not resps:
        return None
    r = resps[-1]
    try:
        data = json.loads(r["body"])
    except (json.JSONDecodeError, TypeError):
        data = None
    return {
        "status": r["status"],
        "body": r["body"],
        "reqBody": r.get("reqBody"),
        "json": data,
    }


def _post_checkin_direct(driver) -> dict | None:
    """无弹窗/无验证场景，页面内直接 POST /checkin（空 JSON body）。"""
    raw = driver.execute_async_script(_DIRECT_POST_JS)
    if not raw or raw.get("status") == 0:
        logger.warning("直接 POST /checkin 失败: %s", raw)
        return None
    try:
        data = json.loads(raw["body"])
    except (json.JSONDecodeError, TypeError):
        data = None
    return {"status": raw["status"], "body": raw["body"], "reqBody": None, "json": data}


def _wait_page_turnstile(driver, timeout: int = 20) -> bool:
    """确保页面级 Turnstile 已通过（token 就绪）再签到。

    服务端对陌生 IP（如 GitHub Actions）强制要求 cf-turnstile-response；
    组件是页面上的「Verify you are human」复选框，必须先点掉拿到 token。
    页面无 Turnstile 组件时视为无需验证，直接返回 True。
    """
    # 先快速判断有无验证组件（组件可能仍在挂载，短等 1.5s 再确认）
    time.sleep(1.5)
    state = driver.execute_script(_CAPTCHA_STATE_JS)
    if (
        not state.get("captchaPresent")
        and not state.get("sliderVisible")
        and not state.get("turnstileToken")
    ):
        logger.info("页面无 Turnstile/验证组件，视为无需验证")
        return True
    if state.get("turnstileToken"):
        logger.info("页面 Turnstile token 已就绪")
        return True

    clicks = 0
    for sec in range(timeout):
        state = driver.execute_script(_CAPTCHA_STATE_JS)
        if state.get("turnstileToken"):
            logger.info("页面 Turnstile token 已就绪（%ds）", sec)
            return True
        if state.get("sliderVisible"):
            logger.info("页面出现阿里云滑块，自动拖动")
            _drag_slider(driver)
            return True
        if clicks < 2 and sec in (1, 8):
            if _click_turnstile_checkbox(driver):
                clicks += 1
        if sec % 5 == 0:
            logger.info("等待页面 Turnstile... (%d/%ds)", sec, timeout)
        time.sleep(1)
    logger.info("页面 Turnstile 等待超时（%ds）", timeout)
    _log_captcha_dom(driver)
    return False


def _open_captcha_modal(driver) -> bool:
    """点击签到页「签到」按钮。

    Returns:
        True   弹窗已打开，或点击后已直接产生 POST /checkin 响应
        False  两者都未出现（调用方决定是否报错）
    """
    btns = driver.find_elements(By.CSS_SELECTOR, "button.n-button--large-type")
    if not btns:
        raise RuntimeError("找不到签到按钮，页面可能未正确加载")
    _wait_page_turnstile(driver)
    try:
        btns[0].click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", btns[0])

    for _ in range(_MODAL_WAIT):
        if _modal_open(driver) or _checkin_response_ready(driver):
            return True
        time.sleep(0.5)
    return False


def _close_captcha_modal(driver) -> None:
    """关闭残留的验证弹窗：优先右上角关闭按钮，其次 Esc。"""
    if not _modal_open(driver):
        return
    try:
        closers = driver.find_elements(
            By.CSS_SELECTOR,
            ".n-modal .n-base-close, .n-modal .n-card-header__close",
        )
        for closer in closers:
            if closer.is_displayed():
                closer.click()
                break
    except WebDriverException:
        pass

    for _ in range(_MODAL_WAIT):
        if not _modal_open(driver):
            return
        time.sleep(0.3)

    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except WebDriverException:
        pass
    for _ in range(_MODAL_WAIT):
        if not _modal_open(driver):
            return
        time.sleep(0.3)


# ── 提交签到 ─────────────────────────────────────────────────────


def _click_confirm(driver) -> dict | None:
    """点击弹窗「确认」，返回页面 hook 捕获的 POST /checkin 响应。"""
    driver.execute_script("window.__checkinResponses = [];")

    btn = None
    candidates = driver.find_elements(
        By.CSS_SELECTOR, ".n-modal .n-button--primary-type"
    )
    if candidates:
        btn = candidates[0]
    if btn is None:
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
            return {
                "status": r["status"],
                "body": r["body"],
                "reqBody": r.get("reqBody"),
                "json": data,
            }
        time.sleep(1)
    return None


def _summarize_submit(result: dict) -> str:
    """把一次提交结果压缩成一行日志（不输出完整验证码 token）。"""
    parts = [f"status={result['status']} body={str(result['body'])[:120]}"]
    req = result.get("reqBody")
    if req:
        try:
            data = json.loads(req)
            fields = ", ".join(
                f"{k}=<{len(str(v))}字符>" for k, v in data.items()
            )
            parts.append("提交字段: " + fields)
        except (json.JSONDecodeError, TypeError):
            parts.append(f"reqBody={str(req)[:120]}")
    return "; ".join(parts)


def _log_captcha_net(driver) -> None:
    """输出阿里云验证相关的跨域请求摘要，辅助定位风控拒绝。"""
    try:
        net = driver.execute_script("return window.__captchaNet;") or []
    except WebDriverException:
        net = []
    for item in net[-10:]:
        logger.info(
            "验证网络: %s %s status=%s body=%.150s",
            item.get("method"), item.get("url"),
            item.get("status"), item.get("body") or "",
        )


_CAPTCHA_DOM_JS = """
const pick = (sel) => [...document.querySelectorAll(sel)].map(e => ({
    tag: e.tagName, id: e.id,
    cls: String(e.className).slice(0, 80),
    text: (e.textContent || '').trim().slice(0, 40),
    visible: !!(e.offsetWidth || e.offsetHeight),
    valLen: e.value ? String(e.value).length : 0
}));
return {
    turnstileInputs: pick('input[name="cf-turnstile-response"], input[id*="cf-chl"]'),
    widgets: pick('[id^="cf-chl-widget"], .cf-turnstile, [data-sitekey]'),
    iframes: [...document.querySelectorAll('iframe')].map(f => {
        const r = f.getBoundingClientRect();
        return {src: (f.src || '').slice(0, 80), id: f.id, w: r.width, h: r.height};
    }),
    captcha: pick('.captcha-render-area, .n-modal, [id*="captcha"], [id*="aliyun"]'),
    largeBtns: pick('button.n-button--large-type')
};
"""


def _log_captcha_dom(driver) -> None:
    """输出页面验证相关 DOM 摘要，便于区分「无验证」和「验证未加载」。"""
    try:
        info = driver.execute_script(_CAPTCHA_DOM_JS) or {}
    except WebDriverException as e:
        logger.warning("读取验证 DOM 失败: %s", e)
        return
    logger.info("验证 DOM: %s", json.dumps(info, ensure_ascii=False)[:500])


def _wait_required_captcha(driver) -> bool:
    """服务端返回 422「请完成验证」后，等待并完成验证组件。

    覆盖页面级 Turnstile 复选框（截图里那类「Verify you are human」）
    以及弹窗内验证；组件可能在首次失败后才挂载。
    """
    logger.info("服务端要求验证，等待验证组件挂载...")
    retried = False
    clicks = 0
    for sec in range(_CAPTCHA_WAIT):
        state = driver.execute_script(_CAPTCHA_STATE_JS)
        if state.get("turnstileToken"):
            logger.info("Turnstile token 已就绪（%ds）", sec)
            return True
        if state.get("sliderVisible"):
            logger.info("出现阿里云滑块（%ds）", sec)
            _drag_slider(driver)
            return True
        if state.get("modalMounted") and state.get("captchaPresent"):
            logger.info("验证弹窗已挂载（%ds），开始求解", sec)
            return _solve_captcha(driver)
        # 页面级 Turnstile 复选框（不在弹窗内）
        if state.get("captchaPresent") and not state.get("modalMounted"):
            if clicks < 2 and sec in (1, 8):
                if _click_turnstile_checkbox(driver):
                    clicks += 1
        # 422 后页面可能只弹提示；再点一次签到以唤起验证弹窗
        if not retried and sec == 10:
            retried = True
            try:
                btns = driver.find_elements(
                    By.CSS_SELECTOR, "button.n-button--large-type"
                )
                if btns and not btns[0].get_attribute("disabled"):
                    logger.info("未见验证组件，再次点击签到按钮唤起验证")
                    btns[0].click()
            except WebDriverException:
                pass
        if sec % 5 == 0:
            _log_captcha_dom(driver)
            logger.info("等待验证组件... (%d/%ds)", sec, _CAPTCHA_WAIT)
        time.sleep(1)
    _log_captcha_dom(driver)
    return False


# ── 主入口 ───────────────────────────────────────────────────────


def _find_local_chromedriver() -> str | None:
    """优先使用本地 chromedriver，避免 uc 每次联网下载（网络不稳时会失败）。

    查找顺序：环境变量 CHROMEDRIVER_PATH → 项目下 .chromedriver/chromedriver*.exe
    """
    env_path = os.environ.get("CHROMEDRIVER_PATH", "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chromedriver")
    if os.path.isdir(base):
        for root, _dirs, files in os.walk(base):
            for name in files:
                if name.lower() in ("chromedriver.exe", "chromedriver"):
                    return os.path.join(root, name)
    return None


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

    version_main = _detect_chrome_major()
    logger.info("本机 Chrome 主版本: %s", version_main or "未检测到（用最新）")

    local_driver = _find_local_chromedriver()
    if local_driver:
        logger.info("使用本地 chromedriver: %s", local_driver)

    options = uc.ChromeOptions()
    options.add_argument("--window-size=1280,900")
    options.add_argument("--no-first-run")

    driver = None
    try:
        driver = _StealthChrome(
            options=options,
            driver_executable_path=local_driver,
            headless=headless,
            version_main=version_main,
        )
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
            "签到状态: checked=%s 连续=%s 累计=%s skip_captcha=%s",
            status.get("checked"),
            status.get("serial_checkins"),
            status.get("checkins_count"),
            status.get("skip_captcha"),
        )

        if status.get("checked"):
            logger.info("今日已签到")
            return None

        last_detail = "未知错误"
        for round_ in range(1, _SUBMIT_RETRIES + 1):
            result = None
            try:
                # 页面在点击确认时无论 POST 成败都会关闭弹窗，
                # 因此每轮都（重新）打开弹窗，获取全新的验证组件
                driver.execute_script("window.__checkinResponses = [];")
                if not _modal_open(driver):
                    _open_captcha_modal(driver)

                if _checkin_response_ready(driver) and not _modal_open(driver):
                    # 签到按钮直提：点击后已产生 POST 响应
                    result = _take_checkin_response(driver)
                    logger.info("签到按钮直提，已捕获 POST 响应")
                elif _modal_open(driver):
                    if not _solve_captcha(driver):
                        if _checkin_response_ready(driver):
                            result = _take_checkin_response(driver)
                        else:
                            raise RuntimeError("人机验证组件未出现")
                    else:
                        result = _click_confirm(driver)
                else:
                    # 无弹窗无响应：站点未启用验证时可能不弹窗，直接 POST 兜底
                    logger.info("无验证弹窗，尝试直接 POST /checkin")
                    result = _post_checkin_direct(driver)
                    if result is None:
                        raise RuntimeError("人机验证弹窗未打开")

                # 服务端要求验证但未带上 token：等待并完成验证后重新确认
                if (
                    result
                    and result["status"] == 422
                    and "验证" in str(result.get("body") or "")
                ):
                    logger.info("提交被拒（422 需验证）: %s", result.get("body"))
                    if _wait_required_captcha(driver):
                        if _modal_open(driver):
                            result = _click_confirm(driver) or result
                        else:
                            # 页面级 Turnstile 已就绪：重新提交（body 带 token）
                            logger.info("Turnstile 已就绪，重新提交签到")
                            driver.execute_script("window.__checkinResponses = [];")
                            _open_captcha_modal(driver)
                            if _checkin_response_ready(driver):
                                result = _take_checkin_response(driver)
                            # 按钮未带 token 时用直提（显式写入 cf-turnstile-response）
                            if not (
                                result
                                and result["status"] == 200
                                and result.get("json")
                            ):
                                direct = _post_checkin_direct(driver)
                                if direct:
                                    result = direct
                    else:
                        _log_captcha_dom(driver)
            except Exception as captcha_err:
                last_detail = str(captcha_err)
                _close_captcha_modal(driver)

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

            detail = _summarize_submit(result) if result else last_detail
            last_detail = detail
            logger.warning(
                "提交失败（%d/%d）: %s", round_, _SUBMIT_RETRIES, detail
            )
            if not result:
                _close_captcha_modal(driver)

        _log_captcha_net(driver)
        raise RuntimeError(f"签到提交多次失败: {last_detail}")

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
