"""QQ 官方机器人消息事件类型。

AstrBot 的 qq_official / qq_official_webhook 适配器分别使用
QQOfficialMessageEvent 与 QQOfficialWebhookMessageEvent 两个事件类。
这里做一个安全的导入封装，便于类型标注与统一使用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
        QQOfficialMessageEvent,
    )
else:
    try:
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
            QQOfficialMessageEvent,
        )
    except Exception:  # pragma: no cover - 适配器不在时退化为基类
        from astrbot.api.event import AstrMessageEvent as QQOfficialMessageEvent

__all__ = ["QQOfficialMessageEvent"]
