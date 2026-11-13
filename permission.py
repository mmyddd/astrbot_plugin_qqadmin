from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from enum import IntEnum
from functools import wraps
from typing import Any, cast

from astrbot import logger
from astrbot.api.event import AstrMessageEvent

from .config import PluginConfig


class PermLevel(IntEnum):
    """
    定义用户的权限等级。数字越小，权限越高。
    官方 API 不提供群成员角色以外的信息，因此只有 超管 / 成员 两级判定。
    """

    SUPERUSER = 0
    MEMBER = 1
    UNKNOWN = 2

    def __str__(self):
        return {
            PermLevel.SUPERUSER: "超管",
            PermLevel.MEMBER: "成员",
            PermLevel.UNKNOWN: "未知/无权限",
        }.get(self, "未知/无权限")

    @classmethod
    def from_str(cls, perm_str: str):
        mapping = {
            "超管": cls.SUPERUSER,
            "成员": cls.MEMBER,
            "未知": cls.UNKNOWN,
            "无权限": cls.UNKNOWN,
        }
        return mapping.get(perm_str, cls.UNKNOWN)


class PermissionManager:
    _initialized = False

    def __init__(self):
        self.cfg: PluginConfig | None = None
        self.perms: dict[str, PermLevel] | None = None

    def lazy_init(self, config: PluginConfig):
        if self._initialized:
            return
        self.cfg = config
        self.perms = {k: PermLevel.from_str(v) for k, v in self.cfg.perms.items()}
        self._initialized = True

    async def is_superuser(self, event: AstrMessageEvent) -> bool:
        sender_id = event.get_sender_id()
        return bool(self.cfg and sender_id and str(sender_id) in self.cfg.admins_id)

    async def get_perm_level(self, event: AstrMessageEvent) -> PermLevel:
        if await self.is_superuser(event):
            return PermLevel.SUPERUSER
        if event.get_sender_id():
            return PermLevel.MEMBER
        return PermLevel.UNKNOWN

    async def perm_block(
        self,
        event: AstrMessageEvent,
        perm_key: str,
        group_only: bool = True,
    ) -> str | None:
        if group_only and not event.get_group_id():
            return "本指令仅限群聊使用"

        user_level = await self.get_perm_level(event)
        # 未指定权限，则默认至少需要成员权限
        required_level = (self.perms or {}).get(perm_key, PermLevel.MEMBER)

        if user_level > required_level:
            return f"你没{required_level}权限"

        return None


perm_manager = PermissionManager()


def perm_required(perm_key: str | None = None, group_only: bool = True):
    """
    权限检查装饰器。
    :param perm_key: 可选。用户执行命令所需的最低权限键名，默认使用被装饰函数的函数名。
    :param group_only: 是否仅限群聊使用。
    """

    def decorator(
        func: Callable[..., AsyncGenerator[Any, Any] | Awaitable[Any]],
    ) -> Callable[..., AsyncGenerator[Any, Any]]:
        actual_perm_key = perm_key or func.__name__

        @wraps(func)
        async def wrapper(
            plugin_instance: Any,
            event: AstrMessageEvent,
            *args: Any,
            **kwargs: Any,
        ) -> AsyncGenerator[Any, Any]:
            # 权限管理未初始化
            if not perm_manager._initialized:
                logger.error(
                    f"PermissionManager 未初始化（尝试访问权限项：{actual_perm_key}）"
                )
                yield event.plain_result("内部错误：权限系统未正确加载")
                event.stop_event()
                return

            result = await perm_manager.perm_block(
                event, perm_key=actual_perm_key, group_only=group_only
            )
            if result:
                yield event.plain_result(result)
                event.stop_event()
                return

            # 执行原始方法
            if inspect.isasyncgenfunction(func):
                async for item in func(plugin_instance, event, *args, **kwargs):
                    yield item
            else:
                await cast(
                    Awaitable[Any], func(plugin_instance, event, *args, **kwargs)
                )

        return wrapper

    return decorator
