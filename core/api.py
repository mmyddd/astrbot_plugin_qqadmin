from __future__ import annotations

from typing import Any

from astrbot.api import logger
from botpy.http import Route

from ..config import PluginConfig

# 官方文档：该接口为「邀请制(白名单)」能力，未获得权限的机器人会返回 11253
PERMISSION_DENIED_HINT = (
    "官方接口返回 11253：应用无接口访问权限。"
    "群成员列表/群成员批量移除属于官方开放平台的内邀(白名单)能力，"
    "需要先在 QQ 开放平台申请开通后才能使用。"
)


class QQOfficialApi:
    """QQ 机器人官方 API v2 封装（群成员管理相关）"""

    def __init__(self, config: PluginConfig):
        self.cfg = config

    @staticmethod
    def get_client(event: Any):
        """从事件中取出 botpy Client（qq_official 与 qq_official_webhook 均适用）"""
        return getattr(event, "bot", None)

    @classmethod
    def get_http(cls, event: Any):
        """获取 botpy 的 BotHttp（自带 access_token 刷新与鉴权）"""
        client = cls.get_client(event)
        api = getattr(client, "api", None)
        http = getattr(api, "_http", None)
        if http is None:
            raise RuntimeError("当前平台不是 QQ 官方机器人接口，无法调用官方 API")
        return http

    @classmethod
    async def request(
        cls,
        event: Any,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        """调用官方 API；遇到 11253 白名单错误时给出可读提示"""
        http = cls.get_http(event)
        route = Route(method, path)
        try:
            return await http.request(route, **kwargs)
        except Exception as e:
            message = str(e)
            if "11253" in message or "无接口访问权限" in message:
                logger.error(f"[清理群友] {PERMISSION_DENIED_HINT}")
                raise RuntimeError(PERMISSION_DENIED_HINT) from e
            raise

    @classmethod
    async def get_members(cls, event: Any, group_openid: str) -> list[dict]:
        """获取群成员列表（每次最多 30 条，自动翻页）"""
        members: list[dict] = []
        cursor = ""
        while True:
            payload: dict[str, Any] = {}
            if cursor:
                payload["cursor"] = cursor
            data = await cls.request(
                event,
                "GET",
                "/v2/groups/{group_openid}/members",
                json=payload,
            )
            if not isinstance(data, dict):
                break
            members.extend(data.get("members") or [])
            cursor = str(data.get("next_cursor") or "")
            if not cursor:
                break
        return members

    @classmethod
    async def get_member_info(
        cls, event: Any, group_openid: str, member_openid: str
    ) -> dict:
        """获取单个群成员信息（含 member_role：owner / admin / member）"""
        data = await cls.request(
            event,
            "GET",
            "/v2/groups/{group_openid}/members/{member_openid}",
            json={},
        )
        return data if isinstance(data, dict) else {}

    @classmethod
    async def get_member_role(
        cls, event: Any, group_openid: str, member_openid: str
    ) -> str | None:
        """获取群成员角色，单成员接口不可用时回退到成员列表"""
        try:
            info = await cls.get_member_info(event, group_openid, member_openid)
            role = info.get("member_role")
            if role:
                return str(role)
        except Exception:
            pass

        try:
            for member in await cls.get_members(event, group_openid):
                if str(member.get("member_openid") or "") == str(member_openid):
                    role = member.get("member_role")
                    return str(role) if role else None
        except Exception:
            pass
        return None

    @classmethod
    async def batch_remove_members(
        cls,
        event: Any,
        group_openid: str,
        member_openids: list[str],
        add_to_member_blacklist: bool = False,
    ) -> dict:
        """群成员批量移除（单次最多 20 个）"""
        return await cls.request(
            event,
            "POST",
            "/v2/groups/{group_openid}/batch_remove_members",
            json={
                "member_openids": member_openids,
                "add_to_member_blacklist": add_to_member_blacklist,
            },
        )

    async def close(self):
        """连接由 AstrBot 平台适配器管理，这里无需处理"""
        return None
