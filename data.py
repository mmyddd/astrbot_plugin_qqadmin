import asyncio
import json
import time

import aiosqlite

from astrbot.api import logger

from .config import PluginConfig


class ActivityStore:
    """
    群管配置 + 群友活跃记录（官方 API 没有「最后发言时间」和「群等级」，
    这里通过监听群消息，在本地记录每位群友的最后发言时间）
    """

    # ====================== 字段中英文映射 ======================
    FIELD_MAP = {
        "inactive_days": "未发言天数",
        "joined_days": "入群天数",
    }

    REVERSE_FIELD_MAP = {v: k for k, v in FIELD_MAP.items()}

    # ================================================================

    def __init__(self, config: PluginConfig):
        self.db_path = config.db_path
        self.default_cfg: dict = config.default

        self._conn: aiosqlite.Connection | None = None
        self._cache: dict[str, dict] = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()

        # 活跃度写入节流：同一群友 60 秒内只落库一次，避免每条群消息都写盘
        self._touch_cache: dict[tuple[str, str], float] = {}
        self._touch_interval = 60.0

    # ============================== 初始化 ==============================

    async def init(self):
        async with self._init_lock:
            if self._initialized:
                return

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self.db_path))
            self._conn.row_factory = aiosqlite.Row

            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
            """)
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS member_activity (
                    group_id TEXT NOT NULL,
                    member_openid TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    last_active REAL NOT NULL,
                    PRIMARY KEY (group_id, member_openid)
                );
            """)
            await self._conn.commit()

            async with self._conn.execute("SELECT group_id, data FROM groups;") as cur:
                async for row in cur:
                    try:
                        self._cache[row["group_id"]] = json.loads(row["data"])
                    except Exception:
                        logger.exception("解析 group 数据失败: %s", row["group_id"])

            self._initialized = True
            logger.info("QQAdminDB initialized (%d groups)", len(self._cache))

    async def _save_to_db(self, gid: str, data):
        if not self._conn:
            raise RuntimeError("请先 init()")

        await self._conn.execute(
            """
            INSERT INTO groups(group_id, data)
            VALUES (?, ?)
            ON CONFLICT(group_id) DO UPDATE SET data=excluded.data;
            """,
            (gid, json.dumps(data, ensure_ascii=False)),
        )
        await self._conn.commit()

    # ============================== 群配置 ==============================

    async def ensure_group(self, gid: str):
        """确保存在群配置，若没有则按 default_cfg 初始化"""
        if gid not in self._cache:
            self._cache[gid] = json.loads(json.dumps(self.default_cfg))
            await self._save_to_db(gid, self._cache[gid])

    async def all(self, gid: str) -> dict:
        """获取整个配置，并自动补齐 default_cfg 的字段"""
        await self.ensure_group(gid)
        data = self._cache[gid]

        changed = False
        for k, v in self.default_cfg.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
                changed = True

        if changed:
            await self._save_to_db(gid, data)

        return data

    async def get(self, gid: str, field: str, default=None):
        """读字段，不存在则补齐 default"""
        await self.ensure_group(gid)
        data = self._cache[gid]

        if field not in data:
            data[field] = json.loads(json.dumps(default))
            await self._save_to_db(gid, data)

        return data[field]

    async def set(self, gid: str, field: str, value):
        """写入字段"""
        await self.ensure_group(gid)
        self._cache[gid][field] = value
        await self._save_to_db(gid, self._cache[gid])

    async def reset_to_default(self, gid: str | None = None):
        """把指定群（或全部群）配置恢复成 default_cfg"""
        targets = [gid] if gid else list(self._cache.keys())
        for g in targets:
            self._cache[g] = json.loads(json.dumps(self.default_cfg))
            await self._save_to_db(g, self._cache[g])

        logger.info(f"群聊{gid}的群管配置已重置为默认值")

    # ====================== 群友活跃记录 ======================

    async def touch(self, group_id: str, member_openid: str, username: str = ""):
        """记录（或刷新）某位群友的最后发言时间"""
        if not self._conn:
            raise RuntimeError("请先 init()")

        key = (str(group_id), str(member_openid))
        now = time.time()
        if now - self._touch_cache.get(key, 0.0) < self._touch_interval:
            return
        self._touch_cache[key] = now

        await self._conn.execute(
            """
            INSERT INTO member_activity(group_id, member_openid, username, last_active)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, member_openid) DO UPDATE SET
                username=excluded.username,
                last_active=excluded.last_active;
            """,
            (str(group_id), str(member_openid), username or "", now),
        )
        await self._conn.commit()

    async def get_activity(self, group_id: str) -> dict[str, dict]:
        """获取本群全部活跃记录：{member_openid: {"username": str, "last_active": float}}"""
        if not self._conn:
            raise RuntimeError("请先 init()")

        result: dict[str, dict] = {}
        async with self._conn.execute(
            "SELECT member_openid, username, last_active FROM member_activity WHERE group_id = ?;",
            (str(group_id),),
        ) as cur:
            async for row in cur:
                result[row["member_openid"]] = {
                    "username": row["username"],
                    "last_active": float(row["last_active"]),
                }
        return result

    async def clear_activity(self, group_id: str | None = None):
        """清空活跃记录（指定群或全部群）"""
        if not self._conn:
            raise RuntimeError("请先 init()")

        if group_id:
            await self._conn.execute(
                "DELETE FROM member_activity WHERE group_id = ?;", (str(group_id),)
            )
            for key in [k for k in self._touch_cache if k[0] == str(group_id)]:
                self._touch_cache.pop(key, None)
        else:
            await self._conn.execute("DELETE FROM member_activity;")
            self._touch_cache.clear()
        await self._conn.commit()

    # ============================== 关闭 ==============================

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None
            self._initialized = False

    # ====================== 中文展示、读回 ======================

    async def export_cn_lines(self, gid: str) -> str:
        """以中文键名 + 多行文本形式输出群配置"""
        data = await self.all(gid)
        lines = []
        for eng_key, value in data.items():
            cn_key = self.FIELD_MAP.get(eng_key, eng_key)
            lines.append(f"{cn_key}: {value}")
        return "\n".join(lines)

    async def import_cn_lines(self, gid: str, text: str) -> dict:
        """解析用户提交的中文多行文本并写回 DB"""
        await self.ensure_group(gid)
        data = self._cache[gid]

        for line in text.splitlines():
            if ":" not in line:
                continue

            cn_key, raw_v = line.split(":", 1)
            cn_key = cn_key.strip()
            raw_v = raw_v.strip()

            eng_key = self.REVERSE_FIELD_MAP.get(cn_key)
            if not eng_key:
                continue

            old_val = data.get(eng_key)
            if isinstance(old_val, int) and not isinstance(old_val, bool):
                try:
                    data[eng_key] = int(raw_v)
                except ValueError:
                    continue
            else:
                data[eng_key] = raw_v

        await self._save_to_db(gid, data)
        return data
