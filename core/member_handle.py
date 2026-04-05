from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.message.components import At
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from ..utils import format_time, get_nickname

if TYPE_CHECKING:
    from ..main import QQAdminPlugin


class MemberHandle:
    def __init__(self, plugin: QQAdminPlugin):
        self.plugin = plugin

    async def get_group_member_list(self, event: AiocqhttpMessageEvent):
        """查看群友信息，人数太多时可能会处理失败"""
        await event.send(event.plain_result("获取中..."))
        group_id = event.get_group_id()
        members_data = await event.bot.get_group_member_list(group_id=int(group_id))
        info_list = [
            (
                f"{format_time(member['join_time'])}："
                f"【{member['level']}】"
                f"{member['user_id']}-"
                f"{member['nickname']}"
            )
            for member in members_data
        ]
        info_list.sort(key=lambda x: datetime.strptime(x.split("：")[0], "%Y-%m-%d"))
        info_str = "进群时间：【等级】QQ-昵称\n\n"
        info_str += "\n\n".join(info_list)
        url = await self.plugin.text_to_image(info_str)
        await event.send(event.image_result(url))

    async def clear_group_member(
        self,
        event: AiocqhttpMessageEvent,
        inactive_days: int = 30,
        under_level: int = 10,
    ):
        """/清理群友 未发言天数 群等级"""
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()

        try:
            members_data = await event.bot.get_group_member_list(group_id=int(group_id))
        except Exception as e:
            await event.send(event.plain_result(f"获取群成员信息失败：{e}"))
            return

        threshold_ts = int(datetime.now().timestamp()) - inactive_days * 86400
        clear_ids: list[int] = []
        info_lines: list[str] = []

        # 获取配置：是否跳过有专属头衔的成员
        skip_special = getattr(self.plugin.cfg, 'clear_member_skip_special_title', False)
        logger.info(f"[清理群友] 跳过专属头衔开关: {skip_special}")

        for member in members_data:
            last_sent = member.get("last_sent_time", 0)
            level = int(member.get("level", 0))
            user_id = member.get("user_id", "")
            nickname = member.get("nickname", "（无昵称）")

            # 兼容不同协议端的头衔字段名：优先 title（标准 OneBot），其次 special_title
            if skip_special:
                # 获取头衔，OneBot v11 标准字段为 title
                title = member.get("title", "") or member.get("special_title", "")
                if title and title.strip():
                    logger.debug(f"[清理群友] 成员 {nickname}({user_id}) 拥有专属头衔 '{title}'，跳过清理")
                    continue

            if last_sent < threshold_ts and level < under_level:
                clear_ids.append(user_id)
                last_active_str = format_time(last_sent)
                info_lines.append(
                    f"- **{last_active_str}**｜**{level}**级｜`{user_id}` - {nickname}"
                )

        if not clear_ids:
            await event.send(event.plain_result("无符合条件的群友"))
            return

        # 按发言时间排序
        info_lines.sort(key=lambda x: datetime.strptime(x.split("**")[1], "%Y-%m-%d"))

        info_str = (
            f"### 共 **{len(clear_ids)}** 位群友 **{inactive_days}** 天内无发言，群等级低于 **{under_level}** 级\n\n"
            + "\n".join(info_lines)
            + "\n\n### 请发送 **确认清理** 或 **取消清理** 来处理这些群友！"
        )

        url = await self.plugin.text_to_image(info_str)
        await event.send(event.image_result(url))

        await event.send(event.chain_result([At(qq=cid) for cid in clear_ids]))

        @session_waiter(timeout=60)
        async def empty_mention_waiter(
            controller: SessionController, event: AiocqhttpMessageEvent
        ):
            if group_id != event.get_group_id() or sender_id != event.get_sender_id():
                return

            if event.message_str == "取消清理":
                await event.send(event.plain_result("清理群友任务已取消"))
                controller.stop()
                return

            if event.message_str == "确认清理":
                msg_list = []
                for clear_id in clear_ids:
                    try:
                        target_name = await get_nickname(event, user_id=clear_id)
                        await event.bot.set_group_kick(
                            group_id=int(group_id),
                            user_id=int(clear_id),
                            reject_add_request=False,
                        )
                        msg_list.append(f"✅ 已将 {target_name}({clear_id}) 踢出本群")
                    except Exception as e:
                        msg_list.append(f"❌ 踢出 {target_name}({clear_id}) 失败")
                        logger.error(f"踢出 {target_name}({clear_id}) 失败：{e}")

                if msg_list:
                    await event.send(event.plain_result("\n".join(msg_list)))
                controller.stop()

        try:
            await empty_mention_waiter(event)
        except TimeoutError:
            await event.send(event.plain_result("等待超时！"))
        except Exception as e:
            logger.error("清理群友任务出错: " + str(e))
        finally:
            event.stop_event()