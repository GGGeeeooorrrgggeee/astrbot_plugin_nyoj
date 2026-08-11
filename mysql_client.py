from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Dict, List, Sequence, Set, Tuple

import aiomysql
from astrbot.api import logger

from config import PluginConfig


class OJMySQLClient:
    """Read-only client for the remote NYOJ MySQL database."""

    def __init__(self, config: PluginConfig):
        self.config = config

    def _connect_kwargs(self) -> dict:
        return {
            "host": self.config.mysql_host,
            "port": self.config.mysql_port,
            "user": self.config.mysql_user,
            "password": self.config.mysql_password,
            "db": self.config.mysql_database,
        }

    def _connect_kwargs_with_timeout(self, timeout_seconds: int) -> dict:
        kwargs = self._connect_kwargs()
        # aiomysql supports connect_timeout, while query execution is bounded
        # by asyncio.wait_for in the caller.
        kwargs["connect_timeout"] = timeout_seconds
        return kwargs

    async def fetch_users(self) -> List[dict]:
        """Fetch all user_info rows from MySQL."""
        logger.info(
            "%s MySQL 开始读取 user_info: host=%s port=%s db=%s user=%s password_set=%s",
            self.config.oj_name,
            self.config.mysql_host,
            self.config.mysql_port,
            self.config.mysql_database,
            self.config.mysql_user or "<empty>",
            bool(self.config.mysql_password),
        )
        conn = await aiomysql.connect(**self._connect_kwargs())
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT
                        u.uuid,
                        u.username,
                        u.email,
                        u.gender,
                        u.gmt_create
                    FROM user_info u
                    """
                )
                rows = await cur.fetchall()
                logger.info("%s MySQL user_info 读取完成: rows=%s", self.config.oj_name, len(rows))
        finally:
            conn.close()
        return rows

    async def fetch_incremental_ac(self, last_sync_time: str | None) -> Dict[str, Set[int]]:
        """Fetch incremental AC records since *last_sync_time*.

        Returns a mapping uid -> set(pid). When *last_sync_time* is falsy
        the entire user_acproblem table is read.
        """
        logger.info(
            "%s MySQL 开始读取 user_acproblem: host=%s port=%s db=%s last_sync=%s",
            self.config.oj_name,
            self.config.mysql_host,
            self.config.mysql_port,
            self.config.mysql_database,
            last_sync_time or "<full>",
        )
        counts: Dict[str, Set[int]] = defaultdict(set)
        conn = await aiomysql.connect(**self._connect_kwargs())
        try:
            async with conn.cursor() as cur:
                if last_sync_time:
                    await cur.execute(
                        "SELECT uid, pid FROM user_acproblem WHERE gmt_modified > %s",
                        (last_sync_time,),
                    )
                else:
                    await cur.execute("SELECT uid, pid FROM user_acproblem")
                async for uid, pid in cur:
                    counts[str(uid)].add(int(pid))
                total_pids = sum(len(pids) for pids in counts.values())
                logger.info(
                    "%s MySQL user_acproblem 读取完成: users=%s records=%s",
                    self.config.oj_name,
                    len(counts),
                    total_pids,
                )
        finally:
            conn.close()
        return counts

    async def fetch_daily_rank_rows(
        self,
        start_time: str,
        end_time: str,
        username_blacklist: Sequence[str],
        timeout_seconds: int,
    ) -> List[Tuple[str, str, int, str]]:
        """Fetch accepted problem counts for the configured daily time window."""
        timeout_seconds = max(int(timeout_seconds or 30), 1)
        blacklist = [name.strip() for name in username_blacklist if name.strip()]
        logger.info(
            "%s MySQL 开始读取每日榜单: start=%s end=%s timeout=%s blacklist=%s",
            self.config.oj_name,
            start_time,
            end_time,
            timeout_seconds,
            len(blacklist),
        )

        async def query() -> List[Tuple[str, str, int, str]]:
            blacklist_clause = ""
            params: list[object] = [start_time, end_time]
            if blacklist:
                placeholders = ", ".join(["%s"] * len(blacklist))
                blacklist_clause = f"AND ui.username NOT IN ({placeholders})"
                params.extend(blacklist)

            sql = f"""
                WITH user_problem_stat AS (
                    SELECT
                        uid,
                        pid,
                        LAST_VALUE(status) OVER (
                            PARTITION BY uid, pid
                            ORDER BY submit_time
                            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                        ) AS final_status,
                        MAX(IF(status = 0, submit_time, NULL)) OVER (
                            PARTITION BY uid, pid
                        ) AS last_accept_time
                    FROM judge
                ),
                distinct_up AS (
                    SELECT DISTINCT uid, pid, final_status, last_accept_time
                    FROM user_problem_stat
                ),
                user_cnt AS (
                    SELECT
                        t.uid,
                        ui.username,
                        ui.gender,
                        COUNT(DISTINCT t.pid) AS cnt
                    FROM distinct_up t
                    INNER JOIN user_info ui ON t.uid = ui.uuid
                    WHERE
                        t.final_status = 0
                        AND t.last_accept_time >= %s
                        AND t.last_accept_time <= %s
                        {blacklist_clause}
                    GROUP BY ui.username, ui.gender, t.uid
                )
                SELECT uid, username, cnt, gender
                FROM user_cnt
                ORDER BY cnt DESC, username ASC, uid ASC
            """

            conn = await aiomysql.connect(
                **self._connect_kwargs_with_timeout(timeout_seconds)
            )
            try:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
            finally:
                conn.close()
            return [
                (str(uid), str(username), int(cnt), str(gender or ""))
                for uid, username, cnt, gender in rows
            ]

        try:
            rows = await asyncio.wait_for(query(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"每日榜单查询超时，超过 {timeout_seconds}s") from exc

        logger.info("%s MySQL 每日榜单读取完成: rows=%s", self.config.oj_name, len(rows))
        return rows

    async def fetch_user_profile_extra(self, uuid: str) -> dict:
        """Fetch avatar, roles, and latest judge status for one user."""
        logger.info("%s MySQL 开始读取用户扩展信息: uuid=%s", self.config.oj_name, uuid)
        conn = await aiomysql.connect(**self._connect_kwargs())
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT
                        t.avatar_url,
                        t.role_list,
                        t.last_login_time,
                        j.submit_time AS latest_submit_time,
                        j.status AS latest_status_code
                    FROM (
                        SELECT
                            ui.avatar AS avatar_url,
                            GROUP_CONCAT(DISTINCT r.role SEPARATOR ',') AS role_list,
                            ui.uuid,
                            (
                                SELECT MAX(s.gmt_create)
                                FROM session s
                                WHERE s.uid = ui.uuid
                            ) AS last_login_time
                        FROM user_info ui
                        LEFT JOIN user_role ur ON ui.uuid = ur.uid
                        LEFT JOIN role r ON ur.role_id = r.id
                        WHERE ui.uuid = %s
                        GROUP BY ui.uuid
                    ) t
                    LEFT JOIN judge j
                        ON j.uid = t.uuid
                        AND j.submit_time = (
                            SELECT MAX(submit_time) FROM judge WHERE uid = t.uuid
                        )
                    LIMIT 1
                    """,
                    (uuid,),
                )
                row = await cur.fetchone()
                logger.info("%s MySQL 用户扩展信息读取完成: found=%s", self.config.oj_name, bool(row))
        finally:
            conn.close()
        return row or {}
