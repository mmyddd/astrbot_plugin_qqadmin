from __future__ import annotations

import inspect
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from enum import IntEnum
from functools import wraps
from typing import Any, cast

from astrbot import logger
from astrbot.api.event import AstrMessageEvent

from .config import PluginConfig
from .core.api import QQOfficialApi


class PermLevel(IntEnum):
    """
    定义用户的权限等级。数字越小，权限越高。

    群主 / 管理员来自官方成员接口的 member_role 字段
    （owner / admin / member），与执行踢人时用的是同一份数据。
    """

    SUPERUSER = 0
    OWNER = 1
    ADMIN = 2
    MEMBER = 3
    UNKNOWN = 4

    def __str__(self):
        return {
            PermLevel.SUPERUSER: "超管",
            PermLevel.OWNER: "群主",
            PermLevel.ADMIN: "管理员",
            PermLevel.MEMBER: "成员",
            PermLevel.UNKNOWN: "未知/无权限",
        }.get(self, "未知/无权限")

    @classmethod
    def from_str(cls, perm_str: str):
        mapping = {
            "超管": cls.SUPERUSER,
            "群主": cls.OWNER,
            "管理员": cls.ADMIN,
            "成员": cls.MEMBER,
            "未知": cls.UNKNOWN,
            "无权限": cls.UNKNOWN,
        }
        return mapping.get(perm_str, cls.UNKNOWN)


class RoleLookup:
    """一次角色查询的结果：角色本身 + 接口是否可用"""

    __slots__ = ("role", "available")

    def __init__(self, role: str | None, available: bool):
        self.role = role
        self.available = available

    def __bool__(self) -> bool:
        return bool(self.role)


class PermissionManager:
    _initialized = False

    def __init__(self):
        self.cfg: PluginConfig | None = None
        self.perms: dict[str, PermLevel] | None = None
        # 群成员角色缓存：(group_id, member_openid) -> (角色, 过期时间, 接口是否可用)
        # 命中正角色的缓存 5 分钟，未取到角色的负缓存 1 分钟，避免频繁打官方接口
        self._role_cache: dict[tuple[str, str], tuple[str | None, float, bool]] = {}
        self._role_ttl = 300.0
        self._role_negative_ttl = 60.0

    def lazy_init(self, config: PluginConfig):
        if self._initialized:
            return
        self.cfg = config
        self.perms = {k: PermLevel.from_str(v) for k, v in self.cfg.perms.items()}
        self._initialized = True

    # ====================== 超管判定 ======================

    async def is_superuser(self, event: AstrMessageEvent) -> bool:
        """超管 = AstrBot 全局 admins_id（与插件改造前保持一致）。

        两个来源都会认：
        1. AstrBot 唤醒阶段已经比对过 admins_id，命中时会把 event.role 置为 admin；
        2. 这里再比对一次 admins_id，忽略大小写，兼容官方接口下发的 OpenID。
        """
        if getattr(event, "role", None) == "admin":
            return True

        if not self.cfg:
            return False

        sender_id = str(event.get_sender_id() or "").strip().lower()
        if not sender_id:
            return False

        candidates = {str(i).strip().lower() for i in self.cfg.admins_id}
        return sender_id in candidates

    # ====================== 群成员角色 ======================

    async def lookup_role(
        self, event: AstrMessageEvent, member_openid: str | None = None
    ) -> RoleLookup:
        """读取群成员角色（owner / admin / member），带进程内缓存"""
        group_id = event.get_group_id()
        target = str(member_openid or event.get_sender_id() or "")
        if not group_id or not target:
            return RoleLookup(None, False)

        key = (str(group_id), target)
        cached = self._role_cache.get(key)
        if cached is not None and cached[1] > time.time():
            return RoleLookup(cached[0], cached[2])

        try:
            role = await QQOfficialApi.get_member_role(event, group_id, target)
        except Exception as e:
            # 接口不可用（例如白名单未开通、频控、网络异常）时降级处理：
            # 不阻断成员即可执行的指令，但也不会因此获得管理员权限
            logger.warning(f"[权限] 获取群成员角色失败，按普通成员处理：{e}")
            self._role_cache[key] = (None, time.time() + self._role_negative_ttl, False)
            return RoleLookup(None, False)

        available = role is not None
        ttl = self._role_ttl if available else self._role_negative_ttl
        self._role_cache[key] = (role, time.time() + ttl, available)
        return RoleLookup(role, available)

    async def get_group_role(
        self, event: AstrMessageEvent, member_openid: str | None = None
    ) -> str | None:
        """只取角色值，不关心接口可用性"""
        return (await self.lookup_role(event, member_openid)).role

    def clear_role_cache(self):
        """成员角色变化后（进群/退群/改管理员）可主动清空缓存"""
        self._role_cache.clear()

    # ====================== 等级解析 ======================

    async def get_perm_level(self, event: AstrMessageEvent) -> PermLevel:
        return (await self.resolve_level(event))[0]

    async def resolve_level(
        self, event: AstrMessageEvent
    ) -> tuple[PermLevel, RoleLookup]:
        """解析发送者等级，同时返回角色查询结果，便于给出准确的报错文案"""
        if await self.is_superuser(event):
            return PermLevel.SUPERUSER, RoleLookup("superuser", True)

        if not event.get_sender_id():
            return PermLevel.UNKNOWN, RoleLookup(None, False)

        lookup = await self.lookup_role(event)
        match lookup.role:
            case "owner":
                return PermLevel.OWNER, lookup
            case "admin":
                return PermLevel.ADMIN, lookup
            case "member":
                return PermLevel.MEMBER, lookup
            case _:
                # 私聊，或角色接口不可用时，按普通成员处理：
                # 保证「成员」级指令仍可用，同时不会误放行管理员级指令
                return PermLevel.MEMBER, lookup

    async def perm_block(
        self,
        event: AstrMessageEvent,
        perm_key: str,
        group_only: bool = True,
    ) -> str | None:
        if group_only and not event.get_group_id():
            return "本指令仅限群聊使用"

        if not event.get_sender_id():
            return "无法识别你的身份，请稍后再试"

        user_level, lookup = await self.resolve_level(event)

        # 未指定权限，则默认至少需要成员权限
        required_level = (self.perms or {}).get(perm_key, PermLevel.MEMBER)

        if user_level <= required_level:
            return None

        if not lookup.available and event.get_group_id():
            logger.warning(
                f"[权限] 无法确认 {event.get_sender_id()} 的群身份，"
                f"该用户需要 {required_level} 权限才能执行 {perm_key}"
            )
            return (
                f"你没{required_level}权限，且官方成员接口暂时取不到你的群身份"
                "（通常是未开通白名单导致的 11253）。\n"
                "可发送「我的信息」查看自己的 OpenID，"
                "再把它加进 AstrBot 的 admins_id（全局管理员）"
            )

        return f"你没{required_level}权限"


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
