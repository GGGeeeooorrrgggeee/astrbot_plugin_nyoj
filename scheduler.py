from __future__ import annotations

from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import logger

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class PluginScheduler:
    """APScheduler wrapper for configured NYOJ jobs."""

    def __init__(
        self,
        user_sync_job: Callable[[], Awaitable[None]],
        ac_sync_job: Callable[[], Awaitable[None]],
        daily_ranking_job: Callable[[], Awaitable[None]],
    ):
        self.user_sync_job = user_sync_job
        self.ac_sync_job = ac_sync_job
        self.daily_ranking_job = daily_ranking_job
        self.oj_name = "NYOJ"
        self.user_sync_schedule_time = ""
        self.ac_sync_schedule_time = ""
        self.daily_ranking_send_time = ""
        self.scheduler = AsyncIOScheduler(
            timezone=BEIJING_TZ,
            job_defaults={"coalesce": True, "max_instances": 1},
        )
        self.started = False

    def start(
        self,
        user_sync_enabled: bool = True,
        ac_sync_enabled: bool = True,
        daily_ranking_enabled: bool = False,
        user_sync_schedule_time: str = "00:30",
        ac_sync_schedule_time: str = "00",
        daily_ranking_send_time: str = "00:00:00",
        oj_name: str = "NYOJ",
    ) -> None:
        self.oj_name = oj_name or "NYOJ"
        user_minute, user_second = self._parse_mmss(user_sync_schedule_time)
        ac_second = self._parse_second(ac_sync_schedule_time)
        daily_hour, daily_minute, daily_second = self._parse_hhmmss(daily_ranking_send_time)

        self.user_sync_schedule_time = (user_sync_schedule_time or "00:30").strip()
        self.ac_sync_schedule_time = (ac_sync_schedule_time or "00").strip()
        self.daily_ranking_send_time = (daily_ranking_send_time or "00:00:00").strip()

        if user_sync_enabled:
            self.scheduler.add_job(
                self._run_user_sync,
                "cron",
                minute=str(user_minute),
                second=str(user_second),
                id="getUserInfo",
                replace_existing=True,
            )
        elif self.scheduler.get_job("getUserInfo") is not None:
            self.scheduler.remove_job("getUserInfo")

        if ac_sync_enabled:
            self.scheduler.add_job(
                self._run_ac_sync,
                "cron",
                second=str(ac_second),
                id="getUserAC",
                replace_existing=True,
            )
        elif self.scheduler.get_job("getUserAC") is not None:
            self.scheduler.remove_job("getUserAC")

        if daily_ranking_enabled:
            self.scheduler.add_job(
                self._run_daily_ranking,
                "cron",
                hour=str(daily_hour),
                minute=str(daily_minute),
                second=str(daily_second),
                id="getDailyRank",
                replace_existing=True,
            )
        elif self.scheduler.get_job("getDailyRank") is not None:
            self.scheduler.remove_job("getDailyRank")
            self.daily_ranking_send_time = ""

        if not self.started:
            self.scheduler.start()
            self.started = True

    async def shutdown(self) -> None:
        if not self.started:
            return
        self.scheduler.shutdown(wait=False)
        self.started = False

    async def _run_user_sync(self) -> None:
        try:
            await self.user_sync_job()
        except Exception as exc:
            logger.error("%s 用户同步任务失败: %s", self.oj_name, exc)

    async def _run_ac_sync(self) -> None:
        try:
            await self.ac_sync_job()
        except Exception as exc:
            logger.error("%s AC 同步任务失败: %s", self.oj_name, exc)

    async def _run_daily_ranking(self) -> None:
        try:
            await self.daily_ranking_job()
        except Exception as exc:
            logger.error("%s 每日榜单任务失败: %s", self.oj_name, exc)

    @staticmethod
    def _parse_mmss(value: str) -> tuple[int, int]:
        text = (value or "00:30").strip()
        parts = text.split(":")
        try:
            minute = int(parts[0])
            second = int(parts[1]) if len(parts) > 1 else 0
        except (TypeError, ValueError, IndexError):
            logger.warning("用户同步时间格式错误，使用默认 00:30：%s", value)
            return 0, 30
        if not (0 <= minute <= 59 and 0 <= second <= 59):
            logger.warning("用户同步时间超出范围，使用默认 00:30：%s", value)
            return 0, 30
        return minute, second

    @staticmethod
    def _parse_second(value: str) -> int:
        text = (value or "00").strip()
        try:
            second = int(text)
        except (TypeError, ValueError):
            logger.warning("AC 同步时间格式错误，使用默认 00：%s", value)
            return 0
        if not 0 <= second <= 59:
            logger.warning("AC 同步时间超出范围，使用默认 00：%s", value)
            return 0
        return second

    @staticmethod
    def _parse_hhmmss(value: str) -> tuple[int, int, int]:
        text = (value or "00:00:00").strip()
        parts = text.split(":")
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            second = int(parts[2]) if len(parts) > 2 else 0
        except (TypeError, ValueError, IndexError):
            logger.warning("每日榜单时间格式错误，使用默认 00:00:00：%s", value)
            return 0, 0, 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            logger.warning("每日榜单时间超出范围，使用默认 00:00:00：%s", value)
            return 0, 0, 0
        return hour, minute, second
