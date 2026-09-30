import html
import json
import logging
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

from api import checkin
from notify import send_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class Account:
    user_id: str
    session: str


def load_accounts() -> list[Account]:
    accounts_json = os.environ.get("ACCOUNTS")
    if not accounts_json:
        logger.error("请在 .env 中配置 ACCOUNTS")
        sys.exit(1)
    raw = json.loads(accounts_json)
    return [Account(user_id=str(a["user_id"]), session=a["session"]) for a in raw]


def main() -> int:
    load_dotenv()
    accounts = load_accounts()
    logger.info("共 %d 个账号待签到", len(accounts))

    # (Account, 日志消息, 状态 success/done/fail)
    results: list[tuple[Account, str, str]] = []

    for i, acc in enumerate(accounts, 1):
        logger.info("── 账号 %d/%d (ID: %s) ──", i, len(accounts), acc.user_id)
        try:
            result = checkin(acc.user_id, acc.session)
            if result:
                msg = (
                    f"签到成功！累计: {result.checkins_count}, "
                    f"连续: {result.serial_checkins}"
                )
                if result.user_points >= 0:
                    msg += f", 积分: {result.user_points}"
                status = "success"
            else:
                msg = "今日已签到，无需操作"
                status = "done"
            logger.info("[%s] %s", acc.user_id, msg)
            results.append((acc, msg, status))
        except Exception as e:
            msg = f"签到失败: {e}"
            logger.error("[%s] %s", acc.user_id, msg)
            results.append((acc, msg, "fail"))

    logger.info("── 签到汇总 ──")
    for acc, msg, _ in results:
        logger.info("  [%s] %s", acc.user_id, msg)

    # ── Telegram 推送 ──
    icons = {"success": "✅", "done": "ℹ️", "fail": "❌"}
    lines = [
        "<b>2DFan 签到报告</b>",
        f"共 {len(results)} 个账号",
    ]
    for acc, msg, status in results:
        lines.append(
            f"{icons[status]} <code>{html.escape(acc.user_id)}</code> "
            f"{html.escape(msg)}"
        )
    send_telegram("\n".join(lines))

    failed = sum(1 for _, _, s in results if s == "fail")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
