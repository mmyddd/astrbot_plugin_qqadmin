from __future__ import annotations

import re

from astrbot.api.event import AstrMessageEvent

# QQ 官方群聊中 @ 的两种呈现：
#   1) 消息内容里的 <@member_openid>（旧协议）或 <qqbot-at-user id="..." />（新协议）
#   2) AstrBot 解析后消息链中的 At 组件
AT_USER_RE = re.compile(r'<qqbot-at-user\s+id="([^"]+)"\s*/>|<@!?([^>\s]+)>')

# 用占位符把 @ 标记替换掉，避免 openid 参与分词
PLACEHOLDER = "\x00{}\x00"
PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")
SPLIT_RE = re.compile(r"[\s,，、]+")

# 增 / 减 关键字（+ / - 是它们的简写）
ADD_WORDS = {"添加", "增加", "加", "新增", "加入", "+", "＋"}
DEL_WORDS = {"删除", "移除", "删", "减", "去掉", "去除", "-", "－"}


def extract_mentions(text: str) -> list[str]:
    """按出现顺序取出文本中的 @ 对象 openid（不含 @全体）"""
    ids: list[str] = []
    for match in AT_USER_RE.finditer(text or ""):
        value = (match.group(1) or match.group(2) or "").strip()
        if value and value.lower() != "all" and value not in ids:
            ids.append(value)
    return ids


def get_mention_openids(event: AstrMessageEvent) -> list[str]:
    """取出本条消息里被 @ 的群成员 openid"""
    openids: list[str] = []

    # 消息链里的 At 组件
    try:
        for seg in event.get_messages() or []:
            qq = getattr(seg, "qq", None)
            if qq is None:
                continue
            value = str(qq).strip()
            if value and value.lower() not in ("all", "qq_official") and value not in openids:
                openids.append(value)
    except Exception:
        pass

    # 原始内容里的 @ 标记
    raw = getattr(event.message_obj, "raw_message", None)
    content = getattr(raw, "content", None) if raw is not None else None
    for value in extract_mentions(str(content or "")):
        if value not in openids:
            openids.append(value)

    return openids


def mask_mentions(raw: str) -> tuple[str, list[str]]:
    """把 @ 标记替换成占位符，返回 (替换后的文本, 被 @ 的 openid 列表)"""
    ids: list[str] = []
    parts: list[str] = []
    cursor = 0
    for match in AT_USER_RE.finditer(raw or ""):
        value = (match.group(1) or match.group(2) or "").strip()
        parts.append((raw or "")[cursor : match.start()])
        if value and value.lower() != "all":
            parts.append(PLACEHOLDER.format(len(ids)))
            ids.append(value)
        cursor = match.end()
    parts.append((raw or "")[cursor:])
    return "".join(parts), ids


def parse_whitelist_args(raw: str) -> list[tuple[str | None, str]]:
    """解析「清理白名单」的参数。

    支持：
        清理白名单 添加 @AAA      把 @AAA 加入白名单
        清理白名单 删除 @AAA      把 @AAA 移出白名单
        清理白名单 +@AAA -@BBB    + / - 为「添加 / 删除」的简写
        清理白名单 添加 张三      也支持直接写昵称或 member_openid
        清理白名单 张三 李四      不带动作关键字则整体覆写

    Returns:
        [(动作, 值)]，动作为 "+"（添加）、"-"（删除）或 None（覆写）。
    """
    text, ids = mask_mentions(raw)
    result: list[tuple[str | None, str]] = []
    action: str | None = None

    for token in SPLIT_RE.split(text):
        token = token.strip()
        if not token:
            continue

        # 动作关键字：作用于其后所有目标
        if token in ADD_WORDS:
            action = "+"
            continue
        if token in DEL_WORDS:
            action = "-"
            continue

        # 前缀简写：+@AAA / -@AAA
        sign = action
        if token[0] in "+-＋－":
            sign = "-" if token[0] in "-－" else "+"
            token = token[1:].strip()
            if not token:
                continue

        match = PLACEHOLDER_RE.fullmatch(token) or PLACEHOLDER_RE.search(token)
        if match is not None:
            value = ids[int(match.group(1))]
        else:
            value = token.lstrip("@＠").strip()

        if value:
            result.append((sign, value))

    return result
