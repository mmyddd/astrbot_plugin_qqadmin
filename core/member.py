from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .api import QQOfficialApi

if TYPE_CHECKING:
    from ..main import QQAdminPlugin

# 官方文档：群成员批量移除单次最多 20 个
REMOVE_CHUNK = 20
# 两次批量请求之间的间隔，避免触发频控（30 QPM）
REMOVE_INTERVAL = 2.0

ROLE_MAP = {
    "owner": "群主",
    "admin": "管理员",
    "member": "成员",
}


def format_ts(value: Any, fmt: str = "%Y-%m-%d") -> str:
    """把 RFC3339 时间字符串或时间戳格式化为 YYYY-MM-DD"""
    if not value:
        return "未知"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime(fmt)
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).strftime(fmt)
    except ValueError:
        return str(value)


def member_name(member: dict, fallback: str = "（无昵称）") -> str:
    return str(member.get("username") or fallback)


async def get_group_member_list(plugin: QQAdminPlugin, event: AstrMessageEvent):
    """查看群友信息（官方 API 只提供昵称、角色、入群时间、是否为机器人）"""
    group_id = event.get_group_id()
    if not group_id:
        yield event.plain_result("本指令仅限群聊使用")
        return

    await event.send(event.plain_result("获取中..."))
    try:
        members = await QQOfficialApi.get_members(event, group_id)
    except Exception as e:
        yield event.plain_result(f"获取群成员信息失败：{e}")
        return

    if not members:
        yield event.plain_result("未获取到群成员信息")
        return

    members.sort(key=lambda m: str(m.get("joined_at") or ""))
    lines = [f"共 {len(members)} 位群友：", "", "进群时间｜角色｜昵称"]
    for member in members:
        role = ROLE_MAP.get(str(member.get("member_role")), str(member.get("member_role")))
        flag = "（机器人）" if member.get("bot") else ""
        lines.append(
            f"{format_ts(member.get('joined_at'))}｜{role}｜{member_name(member)}{flag}"
        )

    yield event.plain_result("\n".join(lines))


async def collect_candidates(
    plugin: QQAdminPlugin,
    event: AstrMessageEvent,
    group_id: str,
    inactive_days: int,
) -> tuple[list[dict], list[dict]]:
    """按「未发言天数」筛选普通成员。

    Returns:
        (待清理列表, 本地无发言记录而被跳过的列表)
    """
    members = await QQOfficialApi.get_members(event, group_id)
    activity = await plugin.store.get_activity(group_id)

    threshold = time.time() - inactive_days * 86400
    clear_list: list[dict] = []
    unknown_list: list[dict] = []

    for member in members:
        openid = str(member.get("member_openid") or "")
        if not openid or member.get("bot"):
            continue
        # 群主与管理员无法被移除，直接跳过
        if str(member.get("member_role")) != "member":
            continue

        record = activity.get(openid)
        if not record:
            unknown_list.append(
                {
                    "member_openid": openid,
                    "username": member_name(member),
                    "last_active": None,
                    "joined_at": member.get("joined_at"),
                }
            )
            continue

        last_active = float(record.get("last_active") or 0)
        if last_active >= threshold:
            continue

        clear_list.append(
            {
                "member_openid": openid,
                "username": member_name(member),
                "last_active": last_active,
                "joined_at": member.get("joined_at"),
            }
        )

    clear_list.sort(key=lambda m: m.get("last_active") or 0)
    unknown_list.sort(key=lambda m: str(m.get("joined_at") or ""))
    return clear_list, unknown_list


async def clear_group_member(
    plugin: QQAdminPlugin,
    event: AstrMessageEvent,
    days: str | int | None = None,
    confirm: bool = False,
):
    """清理群友 <未发言天数>：先列出候选，二次确认后再批量移除"""
    group_id = event.get_group_id()
    if not group_id:
        yield event.plain_result("本指令仅限群聊使用")
        return

    limit = int(getattr(plugin.cfg, "inactive_days_limit", 365) or 365)
    inactive_days = days
    if inactive_days in (None, ""):
        inactive_days = int(await plugin.store.get(group_id, "inactive_days", 30))
    try:
        inactive_days = int(inactive_days)
    except (TypeError, ValueError):
        yield event.plain_result(f"未发言天数请填写 1~{limit} 之间的整数")
        return
    if inactive_days < 1 or inactive_days > limit:
        yield event.plain_result(f"未发言天数请填写 1~{limit} 之间的整数")
        return
    days = inactive_days

    if confirm:
        async for result in _run_clear(plugin, event, group_id, days):
            yield result
        return

    try:
        clear_list, unknown_list = await collect_candidates(
            plugin, event, group_id, days
        )
    except Exception as e:
        yield event.plain_result(f"获取群成员信息失败：{e}")
        return

    if not clear_list:
        tip = f"没有超过 **{days}** 天未发言的普通群友"
        if unknown_list:
            tip += f"（另有 {len(unknown_list)} 位群友本地无发言记录，已跳过）"
        yield event.plain_result(tip)
        return

    lines = [
        f"共 **{len(clear_list)}** 位群友 **{days}** 天内无发言（仅统计普通成员）：",
        "",
    ]
    for member in clear_list[:50]:
        lines.append(
            f"- **{format_ts(member.get('last_active'))}**｜{member.get('username')}"
            f"（{member.get('member_openid')}）"
        )
    if len(clear_list) > 50:
        lines.append(f"- …… 还有 {len(clear_list) - 50} 位")
    if unknown_list:
        lines.append("")
        lines.append(f"另有 **{len(unknown_list)}** 位群友本地没有发言记录，已跳过")
    lines.append("")
    lines.append(f"确认清理请发送 **清理群友 {days} 确认**，取消请忽略本条消息")

    yield event.plain_result("\n".join(lines))


async def _run_clear(
    plugin: QQAdminPlugin,
    event: AstrMessageEvent,
    group_id: str,
    days: int,
):
    """二次确认后执行批量移除"""
    await event.send(event.plain_result("清理中..."))
    try:
        clear_list, _ = await collect_candidates(plugin, event, group_id, days)
    except Exception as e:
        yield event.plain_result(f"获取群成员信息失败：{e}")
        return

    if not clear_list:
        yield event.plain_result(f"没有超过 **{days}** 天未发言的普通群友")
        return

    names = {m["member_openid"]: m["username"] for m in clear_list}
    openids = [m["member_openid"] for m in clear_list]
    chunk_size = max(1, min(int(getattr(plugin.cfg, "batch_size", REMOVE_CHUNK)), REMOVE_CHUNK))

    failed: list[str] = []
    for index in range(0, len(openids), chunk_size):
        if index:
            await asyncio.sleep(REMOVE_INTERVAL)
        chunk = openids[index : index + chunk_size]
        try:
            data = await QQOfficialApi.batch_remove_members(event, group_id, chunk)
        except Exception as e:
            logger.error(f"[清理群友] 批量移除失败：{e}")
            failed.extend(chunk)
            continue

        result = data if isinstance(data, dict) else {}
        if result.get("remove_members_result") not in (None, "success"):
            logger.warning(f"[清理群友] 批量移除返回异常：{result}")
            failed.extend(chunk)
            continue
        failed.extend(str(o) for o in (result.get("add_to_member_blacklist_fail_openids") or []))

    success = len(openids) - len(failed)
    lines = [f"清理完成：成功 {success} 位，失败 {len(failed)} 位"]
    if failed:
        shown = "、".join(f"{names.get(o, o)}({o})" for o in failed[:20])
        lines.append(f"失败名单：{shown}")
        if len(failed) > 20:
            lines.append(f"…… 共 {len(failed)} 位")
    lines.append(f"（按 {days} 天未发言筛选，仅清理普通成员）")

    yield event.plain_result("\n".join(lines))
