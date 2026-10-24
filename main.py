import asyncio
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

IDLE_SECONDS = 4.0      # 多久没有新消息，就认为用户说完了
MAX_WAIT_SECONDS = 25.0  # 单批最长等待时间
TICK_SECONDS = 0.3

MENTION_RE = re.compile(r"@[^\s(]*\(\d+\)")


def _clean(text: str) -> str:
    """去掉 @xxx(123456) 前缀和多余的标点空白。"""
    return MENTION_RE.sub("", text or "").strip(" \t\r\n,，")


@register(
    "astrbot_plugin_merge_msgs",
    "QT-666",
    "连续消息合并：等用户把话说完再统一回复，避免一条消息回一句",
    "1.0.0",
)
class MergeMessagesPlugin(Star):
    """把短时间内连发的多条消息合并成一条，再交给模型。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # key = 会话 + 发送者，value = {"texts": [(序号, 文本)], "last": 时间}
        self._pending: dict[str, dict] = {}

    @staticmethod
    def _order(event: AstrMessageEvent) -> int:
        try:
            return int(getattr(event.message_obj, "message_id", 0))
        except Exception:
            return 0

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def merge_messages(self, event: AstrMessageEvent):
        text = _clean(event.message_str)
        if not text or text.startswith("/"):
            return

        try:
            key = f"{event.unified_msg_origin}:{event.get_sender_id()}"
        except Exception:
            return

        state = self._pending.get(key)
        if state is not None:
            # 已经有人在等：并入，并让这条消息不再单独触发回复
            state["texts"].append((self._order(event), text))
            state["last"] = time.monotonic()
            event.stop_event()
            return

        state = {"texts": [(self._order(event), text)], "last": time.monotonic()}
        self._pending[key] = state
        started = time.monotonic()
        try:
            while True:
                await asyncio.sleep(TICK_SECONDS)
                now = time.monotonic()
                if now - state["last"] >= IDLE_SECONDS:
                    break
                if now - started >= MAX_WAIT_SECONDS:
                    break
        finally:
            self._pending.pop(key, None)

        if len(state["texts"]) <= 1:
            return

        ordered = sorted(state["texts"], key=lambda item: item[0])
        parts: list[str] = []
        for _, item in ordered:
            if item and (not parts or parts[-1] != item):
                parts.append(item)

        merged = "，".join(parts)
        logger.info(f"[merge-msgs] 合并 {len(parts)} 条 -> {merged[:100]}")
        event.message_str = merged
        try:
            event.message_obj.message_str = merged
            event.message_obj.message.append(Plain(text=merged))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[merge-msgs] 注入合并文本失败: {exc}")
