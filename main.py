import random
import re

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.star.filter.event_message_type import EventMessageType

from .config import PluginConfig
from .core import (
    QQOfficialApi,
    QQOfficialMessageEvent,
    clear_group_member,
    describe_whitelist,
    get_group_member_list,
    parse_whitelist_args,
)
from .data import ActivityStore
from .permission import (
    PermLevel,
    perm_manager,
    perm_required,
)
from .utils import ADMIN_HELP, print_logo


class QQAdminPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.store = ActivityStore(self.cfg)
        self.api = QQOfficialApi(self.cfg)

    async def initialize(self):
        await self.store.init()
        perm_manager.lazy_init(self.cfg)
        if random.random() < 0.01:
            print_logo()

    # ====================== 群友活跃记录 ======================

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
    )
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def record_activity(self, event: QQOfficialMessageEvent):
        """监听群消息，记录每位群友的最后发言时间（官方 API 不提供该字段）"""
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if not group_id or not sender_id or sender_id == event.get_self_id():
            return
        try:
            await self.store.touch(
                group_id=group_id,
                member_openid=sender_id,
                username=event.get_sender_name(),
            )
        except Exception as e:
            logger.error(f"[清理群友] 记录群友活跃度失败：{e}")

    # ====================== 身份自检 ======================

    @filter.command("我的信息", alias={"我是谁"})
    async def my_info(self, event: QQOfficialMessageEvent):
        """查看当前平台下的身份标识（排查权限问题用）"""
        sender_id = event.get_sender_id() or "（未知）"
        group_id = event.get_group_id() or "（私聊）"
        platform = event.get_platform_name()

        role = await perm_manager.get_group_role(event)
        role_text = {
            "owner": "群主",
            "admin": "管理员",
            "member": "成员",
        }.get(str(role), "未知（官方成员接口不可用）")

        is_super = await perm_manager.is_superuser(event)
        lines = [
            "【我的信息】",
            f"- 平台：{platform}",
            f"- 我的 OpenID：{sender_id}",
            f"- 本群 OpenID：{group_id}",
            f"- 本群身份：{role_text}",
            f"- 是否超管：{'是' if is_super else '否'}",
        ]
        if not is_super:
            lines.append("")
            lines.append(
                "如需超管权限：把上面的 OpenID 填进 AstrBot 的「管理员 ID」列表"
            )
        yield event.plain_result("\n".join(lines))

    # ====================== 群成员工具 ======================

    @filter.command("群友信息", desc="查看群友信息")
    @perm_required(perm_key="get_group_member_list")
    async def get_group_member_list(self, event: QQOfficialMessageEvent):
        async for result in get_group_member_list(self, event):
            yield result

    @filter.command("清理群友")
    @perm_required(perm_key="clear_group_member")
    async def clear_group_member(
        self,
        event: QQOfficialMessageEvent,
        days: str | None = None,
        joined_days: str | None = None,
        confirm: str | None = None,
    ):
        "清理群友 <未发言天数> <入群天数>；确认执行后在末尾加 确认"
        raw_days = (days or "").strip()
        raw_joined = (joined_days or "").strip()
        raw_confirm = (confirm or "").strip()

        is_confirm = raw_confirm == "确认" or raw_days == "确认" or raw_joined == "确认"
        # 参数个数不固定时，确认词可能落在任意位置
        if raw_days == "确认":
            raw_days = ""
        if raw_joined == "确认":
            raw_joined = ""

        async for result in clear_group_member(
            self,
            event,
            days=raw_days or None,
            joined_days=raw_joined or None,
            confirm=is_confirm,
        ):
            yield result

    @filter.command("清理白名单", alias={"免清理名单"})
    @perm_required(perm_key="clear_whitelist")
    async def clear_whitelist(self, event: QQOfficialMessageEvent):
        "清理白名单 | 清理白名单 添加/删除 @群友（不带动作则覆写）"
        raw: str = event.message_str.partition(" ")[2].strip()
        current = self.cfg.get_whitelist()

        if not raw:
            if current:
                shown = await describe_whitelist(self, event, current)
                yield event.plain_result(
                    "【清理白名单】以下群友不会被清理：\n"
                    + "\n".join(f"- {i}" for i in shown)
                )
            else:
                yield event.plain_result(
                    "【清理白名单】为空，可用 \"清理白名单 添加 @群友\" 添加"
                )
            return

        args = parse_whitelist_args(raw)
        if not args:
            yield event.plain_result(
                "没解析到要处理的群友，试试：清理白名单 添加 @群友"
            )
            return

        if any(sign is not None for sign, _ in args):
            add = [value for sign, value in args if sign == "+"]
            remove = {value.lower() for sign, value in args if sign == "-"}
            merged = [item for item in current if item.lower() not in remove]
            existed = {item.lower() for item in merged}
            for value in add:
                if value.lower() not in existed:
                    merged.append(value)
                    existed.add(value.lower())
        else:
            merged = [value for _, value in args]

        self.cfg.save_whitelist(merged)
        if merged:
            shown = await describe_whitelist(self, event, merged)
            yield event.plain_result(
                f"【清理白名单】已更新，共 {len(merged)} 位：\n"
                + "\n".join(f"- {i}" for i in shown)
            )
        else:
            yield event.plain_result("【清理白名单】已清空")

    # ====================== 群管配置 ======================

    @filter.command("群管配置", alias={"群管设置"})
    @perm_required(perm_key="set_config", group_only=False)
    async def set_config(self, event: QQOfficialMessageEvent):
        """群管配置 <配置串>，留空则为查询"""
        raw: str = event.message_str.partition(" ")[2].strip()
        gid = event.get_group_id() or event.get_sender_id()
        if raw:
            await self.store.import_cn_lines(gid, raw)
        config_str = await self.store.export_cn_lines(gid)
        title = "【群管配置】更新:" if raw else "【群管配置】"
        yield event.plain_result(f"{title}\n{config_str}")

    @filter.command("群管重置")
    @perm_required(perm_key="reset_config", group_only=False)
    async def reset_config(
        self, event: QQOfficialMessageEvent, target: str | None = None
    ):
        """群管重置 | 群管重置 all（含清空活跃记录）"""
        gid = event.get_group_id() or event.get_sender_id()

        if target == "all":
            if not await perm_manager.is_superuser(event):
                yield event.plain_result("你没超管权限，只能重置本群：群管重置")
                return
            await self.store.reset_to_default()
            await self.store.clear_activity()
            yield event.plain_result("已重置所有群的群管配置，并清空群友活跃记录")
            return

        await self.store.reset_to_default(gid)
        await self.store.clear_activity(gid)
        yield event.plain_result("已重置本群的群管配置，并清空本群活跃记录")

    @filter.command("群管帮助")
    async def qq_admin_help(self, event: QQOfficialMessageEvent):
        """查看群管帮助"""
        yield event.plain_result(ADMIN_HELP)

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        await self.store.close()
        logger.info("插件 astrbot_plugin_qqadmin 已优雅关闭")
