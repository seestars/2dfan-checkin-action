"""Telegram 消息推送模块（仅使用标准库，无额外依赖）。

环境变量：
    TELEGRAM_BOT_TOKEN  BotFather 创建机器人获得的 token
    TELEGRAM_CHAT_ID    接收消息的 chat_id，多个用英文逗号分隔
"""

import logging
import os
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 20


def send_telegram(message: str) -> bool:
    """推送一条 HTML 格式消息；未配置或全部失败时返回 False。"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_ids:
        logger.info("未配置 Telegram（TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID），跳过推送")
        return False

    all_ok = True
    for chat_id in chat_ids.split(","):
        chat_id = chat_id.strip()
        if not chat_id:
            continue

        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")

        try:
            with urllib.request.urlopen(
                urllib.request.Request(_API.format(token=token), data=payload),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    logger.info("Telegram 推送成功 -> %s", chat_id)
                else:
                    all_ok = False
                    logger.warning("Telegram 返回非 200: %s", resp.status)
        except Exception as e:
            all_ok = False
            logger.warning("Telegram 推送失败 -> %s: %s", chat_id, e)

    return all_ok
