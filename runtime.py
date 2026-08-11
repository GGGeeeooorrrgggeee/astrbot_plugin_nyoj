from __future__ import annotations


from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Awaitable, Callable, List, Optional, Sequence
from zoneinfo import ZoneInfo

from astrbot.api import logger

from config import PluginConfig
from contest_client import NyojContestClient
from models import DailyRankingResult, NotifyCandidate, ProblemQueryResult, RenderedNotify, SyncCycleResult, UserProfileCard
from mysql_client import OJMySQLClient
from paths import PluginPaths
from problem_client import NyojProblemClient
from profile_card import ProfileCardRenderer
from ranking_service import RankingService
from renderer import ImageRenderer
from repository import SQLiteRepository

ProgressReporter = Callable[[str], Awaitable[None]]
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class PluginRuntime:
    """Orchestrates data sync, notifications, and ranking generation.

    This replaces the scattered logic that was spread across getUserInfo,
    getUserAC, notifyUser, getRank, and pluginSwitch in the original plugin.
    """

    def __init__(self, paths: PluginPaths):
        self.paths = paths
        self.oj_name = "NYOJ"
        self.repository = SQLiteRepository(str(paths.database_path))
        self.ranking_service = RankingService(self.repository)
        self.renderer = ImageRenderer(paths)
        self.profile_renderer = ProfileCardRenderer(paths)

    async def initialize(self, config: PluginConfig | None = None) -> None:
        if config is not None:
            self.oj_name = config.oj_name or "NYOJ"
        logger.info(
            "%s Runtime 初始化: data_root=%s database=%s assets=%s",
            self.oj_name,
            self.paths.data_root,
            self.paths.database_path,
            self.paths.assets_root,
        )
        
        self.paths.ensure()
        await self.repository.initialize()
        logger.info("%s Runtime 初始化完成", self.oj_name)

    # ------------------------------------------------------------------ #
    #  User info sync                                                    #
    # ------------------------------------------------------------------ #

    async def refresh_user_info(
        self,
        config: PluginConfig,
        progress: ProgressReporter | None = None,
        track_update: bool = True,
    ) -> int:
        self._log_config("用户信息同步", config)
        await self._emit(progress, f"本地 SQLite：{self.paths.database_path}")
        await self._emit(
            progress,
            "用户信息同步：准备连接 MySQL "
            f"{config.mysql_host}:{config.mysql_port}，数据库={config.mysql_database}，"
            f"用户={config.mysql_user or '<empty>'}，password_set={bool(config.mysql_password)}",
        )
        client = self._build_mysql_client(config)
        rows = await client.fetch_users()
        await self._emit(progress, f"用户信息同步：MySQL 读取完成，rows={len(rows)}")
        saved_count = await self.repository.upsert_user_info(rows, track_update=track_update)
        await self._emit(progress, f"用户信息同步：写入本地 SQLite 完成，saved={saved_count}")
        logger.info("%s 用户信息同步完成: fetched=%s saved=%s", self.oj_name, len(rows), saved_count)
        return saved_count

    # ------------------------------------------------------------------ #
    #  AC sync + notify + ranking cycle                                  #
    # ------------------------------------------------------------------ #

    async def sync_ac_cycle(
        self,
        config: PluginConfig,
        progress: ProgressReporter | None = None,
        reset_notify_baseline: bool = False,
        track_ac_update: bool = True,
    ) -> SyncCycleResult:
        """Run the full per-minute AC sync cycle.

        Mirrors the original getUserAC_oneminute logic:
        1. Sync incremental AC data from MySQL.
        2. Find users to notify (threshold >= config.notify_threshold).
        3. If notify is disabled, skip notification rendering.
        4. Render notification images for each candidate after the initial baseline sync.
        5. Compare saved vs latest ranking; if changed, persist & render.
        """
        self._log_config("AC 同步周期", config)
        await self._emit(progress, f"本地 SQLite：{self.paths.database_path}")
        client = self._build_mysql_client(config)
        last_sync = await self.repository.get_last_sync_time()
        is_initial_ac_sync = last_sync is None
        now_time = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        logger.info("%s AC 同步开始: last_sync=%s now=%s", self.oj_name, last_sync, now_time)
        await self._emit(
            progress,
            f"AC 同步：开始读取 MySQL，last_sync={last_sync or '<full>'}",
        )

        blacklist_key = self._ranking_blacklist_key(config.ranking_username_blacklist)
        cached_blacklist_key = await self.repository.get_config_state("ranking_blacklist_key")
        if cached_blacklist_key != blacklist_key:
            await self.repository.generate_user_rank(
                config.ranking_after_date,
                config.ranking_limit,
                config.ranking_username_blacklist,
            )
            await self.repository.set_config_state("ranking_blacklist_key", blacklist_key)

        ac_counts = await client.fetch_incremental_ac(last_sync)
        total_records = sum(len(pids) for pids in ac_counts.values())
        await self._emit(
            progress,
            f"AC 同步：MySQL 读取完成，users={len(ac_counts)}，records={total_records}",
        )
        synced_ac_count = await self.repository.apply_ac_updates(
            ac_counts,
            track_update=track_ac_update,
        )
        await self.repository.update_sync_time(now_time)
        await self._emit(
            progress,
            f"AC 同步：写入 SQLite 完成，updated_users={synced_ac_count}，sync_time={now_time}",
        )
        logger.info(
            "%s AC 同步写入完成: mysql_users=%s touched_users=%s new_sync_time=%s",
            self.oj_name,
            len(ac_counts),
            synced_ac_count,
            now_time,
        )

        # --- Notifications ---
        notifications: List[RenderedNotify] = []
        if reset_notify_baseline or is_initial_ac_sync:
            baseline_count = await self.repository.reset_notification_baseline()
            logger.info(
                "%s AC同步通知基线已重置: users=%s initial=%s forced=%s",
                self.oj_name,
                baseline_count,
                is_initial_ac_sync,
                reset_notify_baseline,
            )
            await self._emit(
                progress,
                f"过题通知：已重置通知基线，用户数={baseline_count}",
            )
            candidates: List[NotifyCandidate] = []
        else:
            candidates = await self.repository.find_users_to_notify(config.notify_threshold)
            logger.info(
                "%s 通知候选检查完成: threshold=%s candidates=%s",
                self.oj_name,
                config.notify_threshold,
                len(candidates),
            )
            await self._emit(
                progress,
                f"过题通知：候选用户={len(candidates)}，阈值={config.notify_threshold}",
            )
            if candidates:
                await self.repository.mark_notified(candidates)

        notify_enabled = config.notify_enabled
        logger.info("%s 通知开关状态: enabled=%s", self.oj_name, notify_enabled)
        await self._emit(progress, f"过题通知：开关状态 enabled={notify_enabled}")
        if notify_enabled and candidates and not is_initial_ac_sync:
            for candidate in candidates:
                username = await self.repository.get_username(candidate.uid)
                if not username:
                    logger.info(
                        "%s 过题通知跳过: uid=%s 本地用户信息尚未同步，已记录通知基线",
                        self.oj_name,
                        candidate.uid,
                    )
                    continue
                notifications.append(
                    RenderedNotify(
                        uid=candidate.uid,
                        increase_ac=candidate.increase_ac,
                        username=username,
                        image_path=str(self.paths.data_root / "notify.png"),
                    )
                )
        elif candidates and is_initial_ac_sync:
            logger.info(
                "%s 首次AC同步只记录过题通知基线，不推送历史通知：candidates=%s",
                self.oj_name,
                len(candidates),
            )
        # --- Ranking ---
        old_ranking = await self.repository.get_saved_ranking()
        new_ranking = await self.ranking_service.get_latest_ranking(config)
        old_ranking = await self._attach_ranking_genders(old_ranking)
        new_ranking = await self._attach_ranking_genders(new_ranking)
        ranking_changed = self.ranking_service.is_changed(old_ranking, new_ranking)
        cache_changed = self.ranking_service.needs_cache_update(old_ranking, new_ranking)
        ranking_image_path: Optional[str] = None
        logger.info(
            "%s 排行榜比较完成: old=%s new=%s rank_movement=%s cache_changed=%s",
            self.oj_name,
            len(old_ranking),
            len(new_ranking),
            ranking_changed,
            cache_changed,
        )
        await self._emit(
            progress,
            "排行榜："
            f"old={len(old_ranking)}，new={len(new_ranking)}，"
            f"rank_movement={ranking_changed}，cache_changed={cache_changed}",
        )

        if cache_changed:
            await self.repository.generate_user_rank(
                config.ranking_after_date,
                config.ranking_limit,
                config.ranking_username_blacklist,
            )
            logger.info("%s 排行榜缓存已更新: entries=%s", self.oj_name, len(new_ranking))
            await self._emit(progress, "排行榜：对比缓存已更新")

        if ranking_changed:
            await self.renderer.draw_rank(
                old_ranking,
                new_ranking,
                config.ranking_title,
                output_filename="rank_change.png",
                ranking_after_date=config.ranking_after_date,
            )
            ranking_image_path = str(self.paths.data_root / "rank_change.png")
            logger.info("%s 排行榜变化图片生成完成", self.oj_name)
            await self._emit(progress, "排行榜：检测到排名上升/下降，图片生成完成")

        return SyncCycleResult(
            synced_ac_user_count=synced_ac_count,
            notifications=notifications,
            ranking_changed=ranking_changed,
            ranking_image_path=ranking_image_path,
        )

    async def render_current_ranking(
        self,
        config: PluginConfig,
        progress: ProgressReporter | None = None,
        after_date: str | None = None,
        show_legend: bool = True,
    ) -> str:
        """Render the current live ranking (no comparison), returning base64."""
        ranking_after_date = after_date or config.ranking_after_date
        self._log_config("手动生成排行榜", config)
        await self._emit(progress, f"本地 SQLite：{self.paths.database_path}")
        await self._emit(progress, "排行榜：开始读取本地 SQLite 排名数据")
        ranking = await self.ranking_service.get_latest_ranking(
            config,
            after_date=ranking_after_date,
        )
        ranking = await self._attach_ranking_genders(ranking)
        logger.info("%s 手动生成排行榜: entries=%s title=%s", self.oj_name, len(ranking), config.ranking_title)
        await self._emit(
            progress,
            f"排行榜：读取完成，entries={len(ranking)}，开始生成图片",
        )
        return await self.renderer.draw_rank(
            ranking,
            ranking,
            config.ranking_title,
            output_filename="current_rank.png",
            ranking_after_date=ranking_after_date,
            show_legend=show_legend,
        )

    async def render_contest_ranking(
        self,
        config: PluginConfig,
        contest_name: str,
        progress: ProgressReporter | None = None,
    ) -> str:
        self._log_config("比赛榜单", config)
        await self._emit(progress, f"比赛榜单：准备查询比赛={contest_name}")
        client = NyojContestClient(config, progress=progress)
        ranking = await client.fetch_contest_rank(contest_name)
        ranking = await self._attach_ranking_genders(ranking)
        await self._emit(
            progress,
            f"比赛榜单：读取完成，entries={len(ranking)}，开始生成图片",
        )
        return await self.renderer.draw_static_rank(
            ranking,
            contest_name,
            output_filename="contest_rank.png",
        )

    async def render_daily_ranking(
        self,
        config: PluginConfig,
        progress: ProgressReporter | None = None,
        manual_query: bool = False,
    ) -> DailyRankingResult:
        self._log_config("每日榜单", config)
        start_dt, end_dt = self._daily_ranking_window(
            config,
            manual_query=manual_query,
        )
        start_text = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_text = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        client = self._build_mysql_client(config)
        rows = await client.fetch_daily_rank_rows(
            start_text,
            end_text,
            config.ranking_username_blacklist,
            config.daily_ranking_query_timeout_seconds,
        )
        ranking = self.ranking_service._build_rankings(rows, config.ranking_limit)
        ranking = await self._attach_ranking_genders(ranking)
        title = "每日榜单"
        await self.renderer.draw_static_rank(
            ranking,
            title,
            output_filename="daily_rank.png",
            subtitle=f"时间区间：{start_text} 至 {end_text}",
        )
        return DailyRankingResult(
            title=title,
            start_time=start_text,
            end_time=end_text,
            entry_count=len(ranking),
            image_path=str(self.paths.data_root / "daily_rank.png"),
        )

    async def _attach_ranking_genders(self, ranking: Sequence[object]) -> list[object]:
        """Fill missing gender values from the local user_info table."""
        names = [
            str(getattr(item, "username", "")).strip()
            for item in ranking
            if not str(getattr(item, "gender", "") or "").strip()
            and str(getattr(item, "username", "")).strip()
        ]
        gender_map = await self.repository.get_genders_by_usernames(names)
        enriched: list[object] = []
        for item in ranking:
            gender = str(getattr(item, "gender", "") or "").strip()
            if not gender:
                username = str(getattr(item, "username", "")).strip()
                gender = gender_map.get(username, "")
            try:
                enriched.append(replace(item, gender=gender))
            except TypeError:
                enriched.append(item)
        return enriched

    async def query_problem(
        self,
        config: PluginConfig,
        contest_name: str,
        display_id: str,
    ) -> ProblemQueryResult:
        self._log_config("题目查询", config)
        client = NyojProblemClient(config, self.paths)
        return await client.fetch_problem(contest_name, display_id)

    async def render_user_profile(
        self,
        config: PluginConfig,
        lookup_value: str,
        lookup_by_email: bool = False,
    ) -> str:
        if lookup_by_email:
            local_user = await self.repository.find_user_by_email(lookup_value)
        else:
            local_user = await self.repository.find_user_by_identifier(lookup_value)
        if local_user is None:
            raise RuntimeError("没有找到该用户，请确认用户名或绑定邮箱是否正确。")

        client = self._build_mysql_client(config)
        extra = await client.fetch_user_profile_extra(local_user.uuid)
        avatar = self._absolute_avatar_url(config, str(extra.get("avatar_url") or ""))
        card = UserProfileCard(
            oj_name=config.oj_name,
            username=local_user.username or local_user.uuid,
            gender=self._translate_gender(local_user.gender),
            avatar=avatar,
            registered_at=self._format_time(local_user.gmt_create),
            last_submission_at=self._format_time(extra.get("latest_submit_time")),
            last_submission_result=self._translate_judge_status(extra.get("latest_status_code")),
            last_login_at=self._format_time(
                extra.get("last_login_time"),
                empty_text="暂无登录记录",
            ),
            permission=self._translate_roles(str(extra.get("role_list") or "")),
            total_ac=int(local_user.ac_count or 0),
        )
        return self.profile_renderer.draw(card, output_filename="user_profile.png")

    async def user_exists_by_identifier(self, identifier: str) -> bool:
        return await self.repository.find_user_by_identifier(identifier) is not None

    async def get_sync_debug_status(self) -> dict[str, object]:
        return await self.repository.get_debug_snapshot()

    def _build_mysql_client(self, config: PluginConfig) -> OJMySQLClient:
        if not config.has_mysql_config():
            raise RuntimeError("MySQL 配置不完整，请在 AstrBot 插件配置中填写数据库连接信息。")
        return OJMySQLClient(config)

    @staticmethod
    def _ranking_blacklist_key(values: list[str]) -> str:
        return "\n".join(sorted({value.strip() for value in values if value.strip()}))

    @staticmethod
    def _daily_ranking_window(
        config: PluginConfig,
        manual_query: bool = False,
    ) -> tuple[datetime, datetime]:
        now = datetime.now(BEIJING_TZ)
        today = now.date()
        end_time = PluginRuntime._parse_daily_time(
            config.daily_ranking_window_end_time,
            time(0, 0, 0),
        )
        start = datetime.combine(
            today - timedelta(days=1),
            PluginRuntime._parse_daily_time(
                config.daily_ranking_window_start_time,
                time(0, 0, 0),
            ),
        ).replace(tzinfo=BEIJING_TZ)
        end = datetime.combine(
            today,
            end_time,
        ).replace(tzinfo=BEIJING_TZ)
        if manual_query:
            if now.time() > end_time:
                start += timedelta(days=1)
                end += timedelta(days=1)
            return start, end

        if now < end:
            start -= timedelta(days=1)
            end -= timedelta(days=1)
        return start, end

    @staticmethod
    def _parse_daily_time(value: str, default: time) -> time:
        text = (value or "").strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(text, fmt).time()
            except ValueError:
                continue
        return default

    @staticmethod
    def _log_config(action: str, config: PluginConfig) -> None:
        logger.info(
            "%s %s 配置: mysql_host=%s mysql_port=%s mysql_db=%s mysql_user=%s "
            "password_set=%s broadcast_targets=%s ranking_after_date=%s "
            "ranking_limit=%s notify_threshold=%s",
            config.oj_name or "NYOJ",
            action,
            config.mysql_host or "<empty>",
            config.mysql_port,
            config.mysql_database or "<empty>",
            config.mysql_user or "<empty>",
            bool(config.mysql_password),
            len(config.broadcast_targets),
            config.ranking_after_date,
            config.ranking_limit,
            config.notify_threshold,
        )

    async def _emit(self, progress: ProgressReporter | None, message: str) -> None:
        logger.info("%s 调试输出: %s", self.oj_name, message)
        if progress is not None:
            await progress(message)

    @staticmethod
    def _absolute_avatar_url(config: PluginConfig, avatar: str) -> str:
        avatar = avatar.strip()
        if not avatar:
            return ""
        if avatar.startswith(("http://", "https://", "data:image")):
            return avatar
        if avatar.startswith("/"):
            return f"{config.nyoj_base_url}{avatar}"
        return avatar

    @staticmethod
    def _translate_gender(gender: str) -> str:
        text = str(gender or "").strip().lower()
        mapping = {
            "male": "男",
            "m": "男",
            "男": "男",
            "男性": "男",
            "female": "女",
            "f": "女",
            "女": "女",
            "女性": "女",
            "secrecy": "保密",
            "secret": "保密",
            "保密": "保密",
            "未知": "保密",
            "隐藏": "保密",
        }
        return mapping.get(text, "保密")

    @staticmethod
    def _translate_roles(role_list: str) -> str:
        mapping = {
            "root": "超级管理员",
            "admin": "普通管理员",
            "default_user": "默认普通用户",
            "no_subimit_user": "禁止提交代码",
            "no_submit_user": "禁止提交代码",
            "no_discuss_user": "禁止发帖讨论",
            "mute_user": "禁言",
            "no_submit_no_discuss_user": "禁止提交 + 禁止讨论",
            "no_submit_mute_user": "禁止提交 + 禁言",
            "problem_admin": "题目管理员",
            "contest_account": "比赛账号",
            "team_contest_account": "团队赛账号",
            "coach_admin": "教练管理员",
        }
        roles = [item.strip() for item in role_list.split(",") if item.strip()]
        if not roles:
            return "默认普通用户"
        return "、".join(mapping.get(role, role) for role in roles)

    @staticmethod
    def _translate_judge_status(status_code: object) -> str:
        if status_code is None or status_code == "":
            return "暂无提交"
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            return str(status_code)
        mapping = {
            -4: "Cancelled",
            -3: "Presentation Error",
            -2: "Compile Error",
            -1: "Wrong Answer",
            0: "Accepted",
            1: "Time Limit Exceeded",
            2: "Memory Limit Exceeded",
            3: "Runtime Error",
            4: "System Error",
            5: "Pending",
            6: "Compiling",
            7: "Judging",
            8: "Partial Accepted",
            10: "Submitted Failed",
            15: "No Status",
        }
        return mapping.get(code, f"未知状态({code})")

    @staticmethod
    def _format_time(value: object, empty_text: str = "无") -> str:
        if value is None or value == "":
            return empty_text
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        text = str(value).strip()
        if "." in text:
            text = text.split(".", 1)[0]
        return text or empty_text
