from __future__ import annotations

import sys
import inspect
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_nyoj"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from config import PluginConfig
from paths import PluginPaths
from runtime import PluginRuntime
from scheduler import PluginScheduler


@register(
    PLUGIN_NAME,
    "George",
    "NYOJ 排行榜同步、过题提醒与榜单推送插件",
    "1.0.0",
    "https://github.com/GGGeeeooorrrgggeee/astrbot_plugin_nyoj",
)
class NyojRankPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.runtime = PluginRuntime(self._plugin_paths())
        self.scheduler = PluginScheduler(
            user_sync_job=self._scheduled_user_sync,
            ac_sync_job=self._scheduled_ac_sync,
            daily_ranking_job=self._scheduled_daily_ranking,
        )
        self._last_user_sync_status = "尚未执行"
        self._last_ac_sync_status = "尚未执行"
        self._last_daily_ranking_status = "尚未执行"
        self._last_notify_status = "尚未触发"
        self._last_broadcast_status = "尚未推送"
        self._last_broadcast_details: list[str] = []
        self._scheduler_start_status = "尚未启动"
        self._start_scheduler_from_config()

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """Initialize DB and start scheduled jobs after AstrBot is fully loaded."""
        try:
            await self.runtime.initialize(self._plugin_config())
            await self._restore_recent_statuses()
            self._start_scheduler_from_config()
        except Exception as exc:
            logger.error("%s 插件初始化失败: %s", self._oj_name(), exc)
            await self._broadcast_error(
                "插件初始化",
                exc,
                "请检查 data 目录、SQLite 文件权限和插件配置。",
            )
            raise
        logger.info("%s 排行榜插件已启动，定时任务已开启。", self._oj_name())

    # ------------------------------------------------------------------ #
    #  Commands (admin)                                                  #
    # ------------------------------------------------------------------ #

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nyoj初始化")
    async def update_data(self, event: AstrMessageEvent):
        """\u624b\u52a8\u6267\u884c\u7528\u6237\u4fe1\u606f\u548c AC \u521d\u59cb\u5316"""
        cfg = self._plugin_config()
        yield event.plain_result("\u7528\u6237\u521d\u59cb\u5316\u5f00\u59cb......")
        try:
            await self.runtime.initialize(cfg)
            saved_count = await self.runtime.refresh_user_info(
                cfg,
                progress=None,
                track_update=False,
            )
        except Exception as exc:
            logger.error("%s \u7528\u6237\u521d\u59cb\u5316\u5931\u8d25: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} \u7528\u6237\u521d\u59cb\u5316", exc))
            return

        yield event.plain_result(f"\u7528\u6237\u521d\u59cb\u5316\u6210\u529f\uff01\u66f4\u65b0\u7528\u6237\u6570={saved_count}\u3002")

        yield event.plain_result("AC\u521d\u59cb\u5316\u5f00\u59cb......")
        try:
            result = await self.runtime.sync_ac_cycle(
                cfg,
                progress=None,
                reset_notify_baseline=True,
                track_ac_update=False,
            )
            await self.runtime.repository.clear_recent_data_times()
        except Exception as exc:
            logger.error("%s AC\u521d\u59cb\u5316\u5931\u8d25: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} AC\u521d\u59cb\u5316", exc))
            return

        yield event.plain_result(
            "AC\u521d\u59cb\u5316\u6210\u529f\uff01"
            f"\u66f4\u65b0\u7528\u6237\u6570={result.synced_ac_user_count}\u3002"
        )
        await self._broadcast_ac_result(
            result,
            send_debug=False,
        )
        yield event.plain_result(f"{self._oj_name()}\u521d\u59cb\u5316\u5b8c\u6210\uff01")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启查询非公开赛")
    async def allow_private_contest_rank(self, event: AstrMessageEvent):
        """开启比赛榜单和查询题目查询非公开赛"""
        try:
            save_note = await self._set_config_value("nyoj_api.allow_private_contest_rank", True)
        except Exception as exc:
            logger.error("%s 开启查询非公开赛失败: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} 开启查询非公开赛", exc))
            return
        yield event.plain_result(f"已开启查询非公开赛！{save_note}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭查询非公开赛")
    async def disallow_private_contest_rank(self, event: AstrMessageEvent):
        """关闭比赛榜单和查询题目查询非公开赛"""
        try:
            save_note = await self._set_config_value("nyoj_api.allow_private_contest_rank", False)
        except Exception as exc:
            logger.error("%s 关闭查询非公开赛失败: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} 关闭查询非公开赛", exc))
            return
        yield event.plain_result(f"已关闭查询非公开赛！{save_note}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启过题通知")
    async def enable_ac_notify(self, event: AstrMessageEvent):
        """开启过题通知"""
        try:
            save_note = await self._set_config_value("notification.notify_enabled", True)
        except Exception as exc:
            logger.error("%s 开启过题通知失败: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} 开启过题通知", exc))
            return
        yield event.plain_result(f"已开启过题通知！{save_note}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭过题通知")
    async def disable_ac_notify(self, event: AstrMessageEvent):
        """关闭过题通知"""
        try:
            save_note = await self._set_config_value("notification.notify_enabled", False)
        except Exception as exc:
            logger.error("%s 关闭过题通知失败: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} 关闭过题通知", exc))
            return
        yield event.plain_result(f"已关闭过题通知！{save_note}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("更改榜单基础人数")
    async def set_ranking_limit(self, event: AstrMessageEvent):
        """更改榜单基础人数并刷新本地榜单缓存"""
        raw_value = self._command_args(event.message_str, "更改榜单基础人数")
        try:
            value = int(raw_value)
        except ValueError:
            yield event.plain_result("❌ 格式错误，请使用：更改榜单基础人数 数字")
            return
        cfg = self._plugin_config()
        if value <= 0:
            yield event.plain_result("❌ 榜单基础人数必须大于 0。")
            return
        if value > cfg.manual_ranking_limit_max:
            yield event.plain_result(
                f"❌ 榜单基础人数不能超过当前榜单最大人数 {cfg.manual_ranking_limit_max}。"
            )
            return
        try:
            save_note = await self._set_config_value("ranking.ranking_limit", value)
            cfg = replace(cfg, ranking_limit=value)
            await self._refresh_ranking_cache(cfg)
        except Exception as exc:
            logger.error("%s 更改榜单基础人数失败: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} 更改榜单基础人数", exc))
            return
        yield event.plain_result(f"已更改榜单基础人数为 {value}，榜单缓存已刷新！{save_note}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("更改榜单最大人数")
    async def set_manual_ranking_limit_max(self, event: AstrMessageEvent):
        """更改手动查询榜单最大人数"""
        raw_value = self._command_args(event.message_str, "更改榜单最大人数")
        try:
            value = int(raw_value)
        except ValueError:
            yield event.plain_result("❌ 格式错误，请使用：更改榜单最大人数 数字")
            return
        cfg = self._plugin_config()
        if value <= 0:
            yield event.plain_result("❌ 榜单最大人数必须大于 0。")
            return
        if value < cfg.ranking_limit:
            yield event.plain_result(
                f"❌ 榜单最大人数不能小于当前榜单基础人数 {cfg.ranking_limit}。"
            )
            return
        try:
            save_note = await self._set_config_value("ranking.manual_ranking_limit_max", value)
        except Exception as exc:
            logger.error("%s 更改榜单最大人数失败: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} 更改榜单最大人数", exc))
            return
        yield event.plain_result(f"已更改榜单最大人数为 {value}！{save_note}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("添加黑名单")
    async def add_ranking_blacklist(self, event: AstrMessageEvent):
        """添加榜单用户黑名单并刷新本地榜单缓存"""
        username = self._command_args(event.message_str, "添加黑名单")
        if not username:
            yield event.plain_result("❌ 格式错误，请使用：添加黑名单 用户名")
            return
        cfg = self._plugin_config()
        blacklist = list(cfg.ranking_username_blacklist)
        if username in blacklist:
            yield event.plain_result(f"❌ {username} 已在黑名单中。")
            return
        blacklist.append(username)
        try:
            save_note = await self._set_config_value("ranking.ranking_username_blacklist", blacklist)
            cfg = replace(cfg, ranking_username_blacklist=blacklist)
            await self._refresh_ranking_cache(cfg)
        except Exception as exc:
            logger.error("%s 添加黑名单失败: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} 添加黑名单", exc))
            return
        yield event.plain_result(f"已添加黑名单：{username}，榜单缓存已刷新！{save_note}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除黑名单")
    async def remove_ranking_blacklist(self, event: AstrMessageEvent):
        """删除榜单用户黑名单并刷新本地榜单缓存"""
        username = self._command_args(event.message_str, "删除黑名单")
        if not username:
            yield event.plain_result("❌ 格式错误，请使用：删除黑名单 用户名")
            return
        cfg = self._plugin_config()
        blacklist = list(cfg.ranking_username_blacklist)
        if username not in blacklist:
            yield event.plain_result(f"❌ {username} 不在黑名单中。")
            return
        blacklist = [item for item in blacklist if item != username]
        try:
            save_note = await self._set_config_value("ranking.ranking_username_blacklist", blacklist)
            cfg = replace(cfg, ranking_username_blacklist=blacklist)
            await self._refresh_ranking_cache(cfg)
        except Exception as exc:
            logger.error("%s 删除黑名单失败: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} 删除黑名单", exc))
            return
        yield event.plain_result(f"已删除黑名单：{username}，榜单缓存已刷新！{save_note}")

    @filter.command("查询黑名单")
    async def show_ranking_blacklist(self, event: AstrMessageEvent):
        """查询当前排行榜用户黑名单"""
        blacklist = [
            username
            for username in self._plugin_config().ranking_username_blacklist
            if username
        ]
        if not blacklist:
            yield event.plain_result("当前黑名单为空。")
            return
        lines = [f"当前黑名单（{len(blacklist)}）："]
        lines.extend(
            f"{index}. {username}"
            for index, username in enumerate(blacklist, start=1)
        )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("同步用户")
    async def update_users(self, event: AstrMessageEvent):
        """手动同步用户信息"""
        cfg = self._plugin_config()
        yield event.plain_result("用户同步开始......")
        try:
            await self.runtime.initialize(cfg)
            saved_count = await self.runtime.refresh_user_info(cfg, progress=None)
        except Exception as exc:
            logger.error("%s 手动更新用户信息失败: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} 用户同步", exc))
            return
        yield event.plain_result(f"用户同步成功！更新用户数={saved_count}。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("db状态")
    async def show_sync_status(self, event: AstrMessageEvent):
        """\u67e5\u770b\u672c\u5730 SQLite \u540c\u6b65\u72b6\u6001"""
        try:
            snapshot = await self.runtime.get_sync_debug_status()
        except Exception as exc:
            logger.error("%s \u8bfb\u53d6\u540c\u6b65\u72b6\u6001\u5931\u8d25: %s", self._oj_name(), exc)
            yield event.plain_result(
                self._error_text(
                    "\u8bfb\u53d6\u540c\u6b65\u72b6\u6001",
                    exc,
                    "\u8bf7\u68c0\u67e5\u672c\u5730 SQLite \u6587\u4ef6\u662f\u5426\u5b58\u5728\u4e14\u53ef\u8bfb\u3002",
                )
            )
            return

        def display_time(value: object) -> str:
            text = str(value or "").replace("T", " ")
            return text[:19] if text else "\u65e0"

        none_text = "\u65e0"
        lines = [
            f"SQLite\uff1a{Path(str(snapshot.get('database_path', 'nyoj_rank.db'))).name}",
            f"last_sync_time\uff1a{snapshot.get('last_sync_time') or none_text}",
            f"user_info\uff1a{snapshot.get('user_info_count', 0)}",
            f"user_ac_detail\uff1a{snapshot.get('user_ac_detail_count', 0)}",
            f"user_ac_stats\uff1a{snapshot.get('user_ac_stats_count', 0)}",
            f"user_ranking\uff1a{snapshot.get('user_ranking_count', 0)}",
            f"\u6700\u8fd1\u7528\u6237\u6570\u636e\u66f4\u65b0\u65f6\u95f4\uff1a{display_time(snapshot.get('max_user_update_time'))}",
            f"\u6700\u8fd1AC\u6570\u636e\u66f4\u65b0\u65f6\u95f4\uff1a{display_time(snapshot.get('max_ac_update_time'))}",
            f"\u6700\u8fd1\u901a\u77e5\u6570\u636e\u66f4\u65b0\u65f6\u95f4\uff1a{display_time(snapshot.get('max_notify_time'))}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("同步AC")
    async def trigger_ac_sync(self, event: AstrMessageEvent):
        """\u624b\u52a8\u540c\u6b65 AC \u589e\u91cf\u5e76\u6267\u884c\u8fc7\u9898\u901a\u77e5\u548c\u699c\u5355\u5347\u964d\u68c0\u6d4b"""
        cfg = self._plugin_config()
        yield event.plain_result("AC\u540c\u6b65\u5f00\u59cb......")
        try:
            await self.runtime.initialize(cfg)
            result = await self.runtime.sync_ac_cycle(cfg, progress=None)
        except Exception as exc:
            logger.error("%s \u624b\u52a8\u89e6\u53d1AC\u540c\u6b65\u5931\u8d25: %s", self._oj_name(), exc)
            yield event.plain_result(self._safe_error_text(f"{self._oj_name()} AC\u540c\u6b65", exc))
            return

        yield event.plain_result(
            "AC\u540c\u6b65\u6210\u529f\uff01"
            f"\u66f4\u65b0\u7528\u6237\u6570={result.synced_ac_user_count}\uff0c"
            f"\u901a\u77e5\u56fe\u7247\u6570={len(result.notifications)}\uff0c"
            f"\u6392\u884c\u699c\u5347\u964d={result.ranking_changed}\u3002"
        )
        await self._broadcast_ac_result(
            result,
            send_debug=False,
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("测试推送")
    async def test_broadcast(self, event: AstrMessageEvent):
        """\u6d4b\u8bd5\u5411\u914d\u7f6e\u4e2d\u7684\u6240\u6709\u7fa4\u53d1\u9001\u4e00\u6761\u6d88\u606f"""
        targets = self._broadcast_targets()
        if not targets:
            yield event.plain_result("❌ 当前没有配置推送目标群号。")
            return

        ok_count = 0
        fail_count = 0
        details = []
        for umo in targets:
            try:
                send_result = await self.context.send_message(
                    umo,
                    MessageChain().message(f"{self._oj_name()} \u63a8\u9001\u6d4b\u8bd5\uff1a\u8fd9\u662f\u4e00\u6761\u6d4b\u8bd5\u6d88\u606f\u3002"),
                )
                if send_result is False:
                    fail_count += 1
                    details.append(f"\u5931\u8d25\uff1a{umo}\uff0csend_message \u8fd4\u56de False")
                else:
                    ok_count += 1
                    details.append(
                        f"\u6210\u529f\uff1a{umo}\uff0c\u8fd4\u56de={self._short_text(repr(send_result), 80)}"
                    )
            except Exception as exc:
                fail_count += 1
                logger.error("%s \u6d4b\u8bd5\u63a8\u9001\u5931\u8d25: %s, error=%s", self._oj_name(), umo, exc)
                details.append(
                    f"\u5931\u8d25\uff1a{umo}\uff0c{type(exc).__name__}\uff1a{self._short_text(str(exc), 80)}"
                )

        yield event.plain_result(
            "\n".join(
                [f"测试完成！成功={ok_count}，失败={fail_count}。", *details]
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("推送目标")
    async def show_broadcast_targets(self, event: AstrMessageEvent):
        """查看当前解析后的推送目标"""
        targets = self._broadcast_targets()
        if not targets:
            yield event.plain_result("❌ 当前没有配置推送目标群号。")
            return
        yield event.plain_result(
            "当前推送目标：\n" + "\n".join(targets)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("定时状态")
    async def show_broadcast_status(self, event: AstrMessageEvent):
        """查看定时任务配置、下次运行时间和最近执行状态"""
        await self.runtime.initialize(self._plugin_config())
        await self._restore_recent_statuses()
        yield event.plain_result(self._push_status_text())

    # ------------------------------------------------------------------ #
    #  Command (everyone)                                                #
    # ------------------------------------------------------------------ #

    @filter.command("nyoj")
    async def show_rank(self, event: AstrMessageEvent):
        """查看当前 OJ 排行榜"""
        cfg = self._plugin_config()
        try:
            after_date, ranking_limit, date_from_arg, has_args = self._parse_nyoj_args(
                event.message_str,
                cfg,
            )
        except ValueError as exc:
            yield event.plain_result(f"\u274c {exc}")
            return
        query_cfg = replace(
            cfg,
            ranking_after_date=after_date,
            ranking_limit=ranking_limit,
            ranking_title=(
                f"{after_date}之后注册的用户总排行"
                if date_from_arg
                else cfg.ranking_title
            ),
        )
        try:
            await self.runtime.render_current_ranking(query_cfg, show_legend=not has_args)
        except Exception as exc:
            logger.error("渲染榜单失败: %s", exc)
            yield event.plain_result(
                self._error_text(
                    "生成排行榜",
                    exc,
                    "请检查 MySQL 配置、排行榜起始日期和当前同步状态。",
                )
            )
            return
        yield event.image_result(str(self.runtime.paths.data_root / "current_rank.png"))

    @filter.command("比赛榜单")
    async def show_contest_rank(self, event: AstrMessageEvent):
        """按比赛名称查看比赛排行榜"""
        cfg = self._plugin_config()
        try:
            contest_name, ranking_limit = self._parse_contest_args(event.message_str, cfg)
        except ValueError as exc:
            yield event.plain_result(f"\u274c {exc}")
            return

        query_cfg = replace(cfg, ranking_limit=ranking_limit)
        try:
            await self.runtime.render_contest_ranking(query_cfg, contest_name)
        except Exception as exc:
            logger.error("%s 查询比赛榜单失败: %s", self._oj_name(), exc)
            yield event.plain_result(
                self._error_text(
                    "查询比赛榜单",
                    exc,
                    "请检查 NYOJ 配置和比赛名称。",
                )
            )
            return
        yield event.image_result(str(self.runtime.paths.data_root / "contest_rank.png"))

    @filter.command("每日榜单")
    async def show_daily_rank(self, event: AstrMessageEvent):
        """查看配置时间段内的每日榜单"""
        cfg = self._plugin_config()
        try:
            result = await self.runtime.render_daily_ranking(cfg, manual_query=True)
        except Exception as exc:
            logger.error("%s 每日榜单查询失败: %s", self._oj_name(), exc)
            yield event.plain_result(
                self._error_text(
                    "每日榜单",
                    exc,
                    "请检查 MySQL 配置、judge 表、时间段配置、黑名单和查询超时时间。",
                )
            )
            return
        text = (
            "每日榜单\n"
            f"时间段：{result.start_time} 至 {result.end_time}"
        )
        yield event.image_result(result.image_path)

    @filter.command("查询题目")
    async def show_problem(self, event: AstrMessageEvent):
        """查询某场比赛中的题目并生成题面图片"""
        cfg = self._plugin_config()
        try:
            contest_name, display_id = self._parse_problem_args(event.message_str)
        except ValueError as exc:
            yield event.plain_result(f"\u274c {exc}")
            return

        try:
            result = await self.runtime.query_problem(cfg, contest_name, display_id)
        except Exception as exc:
            logger.error("%s 题目查询失败: %s", self._oj_name(), exc)
            yield event.plain_result(
                self._error_text(
                    "题目查询",
                    exc,
                    "请检查 NYOJ 网站地址、登录账号、密码、比赛名称、题号和服务器 Chromium 状态。",
                )
            )
            return

        text = (
            f"题目名称：{result.problem_title}\n"
            f"通过：{result.ac_count}\n"
            f"总数：{result.total_count}\n"
            f"AC通过率：{result.acceptance_rate:.2f}%\n"
            f"题目网址：{result.problem_url}\n"
            "题面如下："
        )
        await event.send(MessageChain().message(text).file_image(result.image_path))

    @filter.command("查询用户")
    async def show_user_profile(self, event: AstrMessageEvent):
        """查询用户主页卡片"""
        cfg = self._plugin_config()
        try:
            raw_target, force_email = self._parse_user_profile_target(event)
        except ValueError as exc:
            yield event.plain_result(f"\u274c {exc}")
            return

        try:
            lookup_value, lookup_by_email = await self._resolve_user_profile_lookup(
                event,
                raw_target,
                force_email=force_email,
            )
            image_path = await self.runtime.render_user_profile(
                cfg,
                lookup_value,
                lookup_by_email=lookup_by_email,
            )
        except Exception as exc:
            logger.error("%s 查询用户失败: %s", self._oj_name(), exc)
            yield event.plain_result(
                self._error_text(
                    "查询用户",
                    exc,
                    "请确认用户是否存在、账号信息是否已同步，或稍后再试。",
                )
            )
            return
        yield event.image_result(image_path)

    # ------------------------------------------------------------------ #
    #  Scheduled jobs                                                    #
    # ------------------------------------------------------------------ #

    async def _scheduled_user_sync(self) -> None:
        """Sync user info from MySQL on the configured schedule."""
        cfg = self._plugin_config()
        logger.info("%s 定时用户信息同步开始。", self._oj_name())
        try:
            await self.runtime.initialize(cfg)
            saved_count = await self.runtime.refresh_user_info(cfg)
            logger.info("%s 定时用户信息同步完成。", self._oj_name())
            self._last_user_sync_status = f"{self._now_text()} 成功，更新用户数={saved_count}"
        except Exception as exc:
            logger.error("%s 定时用户信息同步失败: %s", self._oj_name(), exc)
            self._last_user_sync_status = (
                f"{self._now_text()} 失败，{type(exc).__name__}：{exc}"
            )
        await self._save_recent_status("last_user_sync", self._last_user_sync_status)

    async def _scheduled_ac_sync(self) -> None:
        """Sync AC data, notify, and push ranking changes on the configured schedule."""
        cfg = self._plugin_config()
        logger.info("%s 定时AC同步开始。", self._oj_name())
        try:
            await self.runtime.initialize(cfg)
            result = await self.runtime.sync_ac_cycle(cfg)
        except Exception as exc:
            logger.error("%s 定时AC同步失败: %s", self._oj_name(), exc)
            self._last_ac_sync_status = (
                f"{self._now_text()} 失败，{type(exc).__name__}：{exc}"
            )
            await self._save_recent_status("last_ac_sync", self._last_ac_sync_status)
            return

        logger.info(
            "%s 定时AC同步完成: synced=%s notifications=%s ranking_changed=%s",
            self._oj_name(),
            result.synced_ac_user_count,
            len(result.notifications),
            result.ranking_changed,
        )

        status = await self._broadcast_ac_result(
            result,
            send_debug=False,
        )
        self._last_ac_sync_status = (
            f"{self._now_text()} 成功，更新用户数={result.synced_ac_user_count}，"
            f"通知图片数={len(result.notifications)}，排行榜升降={result.ranking_changed}，"
            f"推送目标={status['total']}，成功={status['ok']}，失败={status['fail']}"
        )
        await self._save_recent_status("last_ac_sync", self._last_ac_sync_status)

    async def _scheduled_daily_ranking(self) -> None:
        """Send daily ranking for the configured time window."""
        cfg = self._plugin_config()
        logger.info("%s 定时每日榜单开始。", self._oj_name())
        try:
            await self.runtime.initialize(cfg)
            result = await self.runtime.render_daily_ranking(cfg)
        except Exception as exc:
            logger.error("%s 定时每日榜单失败: %s", self._oj_name(), exc)
            self._last_daily_ranking_status = (
                f"{self._now_text()} 失败，{type(exc).__name__}：{exc}"
            )
            await self._save_recent_status("last_daily_ranking", self._last_daily_ranking_status)
            return

        status = await self._broadcast_daily_ranking_result(result)
        self._last_daily_ranking_status = (
            f"{self._now_text()} 成功，时间段={result.start_time} 至 {result.end_time}，"
            f"推送目标={status['total']}，成功={status['ok']}，失败={status['fail']}"
        )
        await self._save_recent_status("last_daily_ranking", self._last_daily_ranking_status)
    async def _broadcast_daily_ranking_result(self, result: object) -> dict[str, object]:
        status = await self._broadcast(
            MessageChain()
            .message(
                f"定时自动推送 {self._oj_name()} 每日榜单：\n"
                f"时间段：{result.start_time} 至 {result.end_time}"
            )
            .file_image(result.image_path),
            label="每日榜单",
        )
        self._remember_broadcast_status(status)
        return status

    async def _broadcast_ac_result(
        self,
        result: object,
        send_debug: bool = True,
    ) -> dict[str, object]:
        status = {"total": 0, "ok": 0, "fail": 0, "details": []}
        notify_total = 0
        notify_ok = 0
        notify_fail = 0
        for index, rendered in enumerate(result.notifications, start=1):
            rendered.image_path = await self.runtime.renderer.make_img(
                rendered.username,
                rendered.increase_ac,
            )
            image_label = self._image_debug_label(
                f"过题通知图片#{index} uid={rendered.uid}",
                rendered.image_path,
            )
            image_status = await self._broadcast(
                MessageChain().file_image(rendered.image_path),
                label=image_label,
            )
            notify_total += image_status.get("total", 0)
            notify_ok += image_status.get("ok", 0)
            notify_fail += image_status.get("fail", 0)
            status = self._merge_broadcast_status(status, image_status)
            if send_debug and image_status["fail"]:
                fallback_status = await self._broadcast_text(
                    f"{self._oj_name()}调试：过题通知图片发送失败\n"
                    f"{self._image_file_info(rendered.image_path)}",
                    label="过题通知图片失败告警",
                )
                status = self._merge_broadcast_status(status, fallback_status)

        if result.notifications:
            self._last_notify_status = (
                f"{self._now_text()} 通知图片数={len(result.notifications)}，"
                f"推送目标={notify_total}，成功={notify_ok}，失败={notify_fail}"
            )
            await self._save_recent_status("last_notify", self._last_notify_status)
        if result.ranking_changed and result.ranking_image_path:
            image_label = self._image_debug_label(
                "排行榜升降图片",
                result.ranking_image_path,
            )
            image_status = await self._broadcast(
                MessageChain().file_image(result.ranking_image_path),
                label=image_label,
            )
            status = self._merge_broadcast_status(status, image_status)
            if send_debug and image_status["fail"]:
                fallback_status = await self._broadcast_text(
                    f"{self._oj_name()}调试：排行榜升降图片发送失败\n"
                    f"{self._image_file_info(result.ranking_image_path)}",
                    label="排行榜升降图片失败告警",
                )
                status = self._merge_broadcast_status(status, fallback_status)
        elif send_debug and result.ranking_changed:
            fallback_status = await self._broadcast_text(
                f"{self._oj_name()}调试：排行榜升降=True，但没有生成排行榜图片路径。",
                label="排行榜图片缺失告警",
            )
            status = self._merge_broadcast_status(status, fallback_status)

        self._remember_broadcast_status(status)
        return status

    async def _broadcast(
        self,
        chain: MessageChain,
        label: str = "消息",
    ) -> dict[str, object]:
        """Send a message chain to all configured broadcast targets."""
        targets = self._broadcast_targets()
        ok_count = 0
        fail_count = 0
        details = []
        if not targets:
            self._last_broadcast_status = f"{self._now_text()} {label} 未推送：没有配置推送目标"
            self._last_broadcast_details = []
            logger.warning("%s 推送跳过：未配置 broadcast_targets", self._oj_name())
            return {"total": 0, "ok": 0, "fail": 0, "details": []}

        for umo in targets:
            try:
                send_result = await self.context.send_message(umo, chain)
                if send_result is False:
                    fail_count += 1
                    details.append(f"{label}失败：{umo}，send_message 返回 False")
                else:
                    ok_count += 1
                    details.append(
                        f"{label}成功：{umo}，返回={self._short_text(repr(send_result), 80)}"
                    )
            except Exception as exc:
                fail_count += 1
                details.append(
                    f"{label}失败：{umo}，{type(exc).__name__}：{self._short_text(str(exc), 80)}"
                )
                logger.error("向 %s 推送消息失败: %s", umo, exc)
        status = {
            "total": len(targets),
            "ok": ok_count,
            "fail": fail_count,
            "details": details,
        }
        self._remember_broadcast_status(status)
        return status

    async def _broadcast_text(
        self,
        text: str,
        label: str = "文字消息",
    ) -> dict[str, object]:
        return await self._broadcast(MessageChain().message(text), label=label)

    async def _broadcast_error(
        self,
        action: str,
        exc: Exception,
        suggestion: str = "",
    ) -> dict[str, object]:
        text = self._error_text(action, exc, suggestion)
        return await self._broadcast(MessageChain().message(text), label=f"{action}错误消息")

    def _start_scheduler_from_config(self) -> None:
        cfg = self._plugin_config()
        try:
            self.scheduler.start(
                user_sync_enabled=cfg.user_sync_schedule_enabled,
                ac_sync_enabled=cfg.ac_sync_schedule_enabled,
                daily_ranking_enabled=cfg.daily_ranking_enabled,
                user_sync_schedule_time=cfg.user_sync_schedule_time,
                ac_sync_schedule_time=cfg.ac_sync_schedule_time,
                daily_ranking_send_time=cfg.daily_ranking_send_time,
                oj_name=cfg.oj_name,
            )
        except Exception as exc:
            self._scheduler_start_status = f"失败：{type(exc).__name__}：{exc}"
            logger.error("%s 定时任务启动失败: %s", self._oj_name(), exc)
            return
        self._scheduler_start_status = f"{self._now_text()} 已按配置启动"

    @staticmethod
    def _error_text(action: str, exc: Exception, suggestion: str = "") -> str:
        return NyojRankPlugin._safe_error_text(action, exc, suggestion)

    @staticmethod
    def _safe_error_text(action: str, exc: Exception, suggestion: str = "") -> str:
        if "database is locked" in str(exc).lower():
            return f"\u274c {action}\u5931\u8d25: database is locked"
        detail = NyojRankPlugin._sanitize_error_detail(str(exc))
        if not detail:
            detail = "\u65e0\u66f4\u591a\u9519\u8bef\u4fe1\u606f"
        text = f"\u274c {action}\u5931\u8d25\uff08{type(exc).__name__}\uff09\uff1a{detail}"
        suggestion_text = NyojRankPlugin._sanitize_error_detail(suggestion)
        if suggestion_text:
            text += f"\n{suggestion_text}"
        return text

    @staticmethod
    def _sanitize_error_detail(text: str) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        text = text.replace("\r", " ").replace("\n", " ")
        text = re.sub(r"[A-Za-z]:[\\/][^\s'\"\uff0c\uff1b\uff1a\u3002]+", "\u8def\u5f84\u5df2\u9690\u85cf", text)
        text = re.sub(r"/[^\s'\"\uff0c\uff1b\uff1a\u3002]+", "\u8def\u5f84\u5df2\u9690\u85cf", text)
        text = re.sub(
            r"\b[\w.-]+\.(?:py|db|sqlite|sqlite3|png|jpg|jpeg|gif|webp|html|htm|ttf|ttc|yaml|yml|json)\b",
            "\u6587\u4ef6\u5df2\u9690\u85cf",
            text,
            flags=re.IGNORECASE,
        )
        table_names = (
            "user_info",
            "user_ac_detail",
            "user_ac_stats",
            "user_ranking",
            "sync_state",
            "config_state",
            "judge",
            "user_role",
            "role",
            "session",
        )
        for name in table_names:
            text = re.sub(
                rf"\b{re.escape(name)}\b\s*\u8868?",
                "\u6570\u636e\u8868",
                text,
                flags=re.IGNORECASE,
            )
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _command_args(message: str, command: str) -> str:
        text = NyojRankPlugin._strip_command_prefix(message, command)
        if text is not None:
            return text
        return ""

    @staticmethod
    def _strip_command_prefix(message: str, command: str) -> str | None:
        text = (message or "").strip()
        command_text = re.escape(command)
        pattern = rf"^(?:[^\w\u4e00-\u9fff]+)?{command_text}(?:\s+|$)"
        match = re.match(pattern, text)
        if match:
            return text[match.end():].strip()
        return None

    async def _refresh_ranking_cache(self, cfg: PluginConfig) -> None:
        await self.runtime.initialize(cfg)
        await self.runtime.repository.generate_user_rank(
            cfg.ranking_after_date,
            cfg.ranking_limit,
            cfg.ranking_username_blacklist,
        )
        await self.runtime.repository.set_config_state(
            "ranking_blacklist_key",
            self.runtime._ranking_blacklist_key(cfg.ranking_username_blacklist),
        )

    def _plugin_config(self) -> PluginConfig:
        cfg = PluginConfig.from_mapping(self.config)
        oj_name = cfg.oj_name or "NYOJ"
        if hasattr(self, "runtime"):
            self.runtime.oj_name = oj_name
        if hasattr(self, "scheduler"):
            self.scheduler.oj_name = oj_name
        return cfg

    def _oj_name(self) -> str:
        runtime_name = getattr(getattr(self, "runtime", None), "oj_name", "")
        if runtime_name:
            return runtime_name
        return PluginConfig.from_mapping(self.config).oj_name or "NYOJ"

    async def _set_config_value(self, key: str, value: object) -> str:
        try:
            if "." in key:
                self._set_nested_config_value(key, value)
            else:
                self.config[key] = value
        except Exception:
            setter = getattr(self.config, "set", None)
            if not callable(setter):
                raise
            setter(key, value)

        for method_name in (
            "save",
            "save_config",
            "save_conf",
            "save_to_file",
            "save_config_to_file",
        ):
            method = getattr(self.config, method_name, None)
            if not callable(method):
                continue
            result = method()
            if inspect.isawaitable(result):
                await result
            return ""

        return "\n提示：当前 AstrBot 配置对象未暴露保存方法；如果设置页没有同步变化，请在设置里也确认这个开关。"

    def _set_nested_config_value(self, key: str, value: object) -> None:
        parts = [part for part in key.split(".") if part]
        if len(parts) < 2:
            self.config[key] = value
            return

        current = self.config
        for part in parts[:-1]:
            try:
                child = current.get(part)
            except AttributeError:
                child = current[part] if part in current else None
            if not isinstance(child, dict):
                child = {}
                current[part] = child
            current = child
        current[parts[-1]] = value

    @staticmethod
    def _parse_nyoj_args(message: str, config: PluginConfig) -> tuple[str, int, bool, bool]:
        text = NyojRankPlugin._strip_command_prefix(message, "nyoj")
        if text is None:
            text = (message or "").strip()
        if not text:
            return config.ranking_after_date, config.ranking_limit, False, False

        parts = text.split()
        if len(parts) > 2:
            raise ValueError("参数格式错误，请使用：nyoj 2026-06-01 50")

        after_date = config.ranking_after_date
        ranking_limit = config.ranking_limit
        date_from_arg = False
        limit_from_arg = False
        for part in parts:
            if NyojRankPlugin._is_date_text(part):
                after_date = part
                date_from_arg = True
                continue
            if part.isdigit():
                ranking_limit = int(part)
                limit_from_arg = True
                continue
            raise ValueError("参数格式错误，请使用：nyoj 2026-06-01 50")

        max_limit = getattr(config, "manual_ranking_limit_max", 100)
        if limit_from_arg and ranking_limit > max_limit:
            raise ValueError(f"排行榜人数不能超过配置上限：{max_limit}")
        return after_date, ranking_limit, date_from_arg, True

    @staticmethod
    def _parse_contest_args(message: str, config: PluginConfig) -> tuple[str, int]:
        text = NyojRankPlugin._strip_command_prefix(message, "比赛榜单")
        if text is None:
            text = (message or "").strip()

        if not text:
            raise ValueError("比赛榜单第一个参数必须是比赛名，例如：比赛榜单 新生赛")

        parts = text.split()
        ranking_limit = config.ranking_limit
        if parts[-1].isdigit():
            ranking_limit = int(parts[-1])
            parts = parts[:-1]
            max_limit = getattr(config, "manual_ranking_limit_max", 100)
            if ranking_limit > max_limit:
                raise ValueError(f"比赛榜单人数不能超过配置上限：{max_limit}")

        contest_name = " ".join(parts).strip()
        if not contest_name:
            raise ValueError("比赛榜单第一个参数必须是比赛名，人数只能放在比赛名后面，例如：比赛榜单 新生赛 50")
        return contest_name, ranking_limit

    @staticmethod
    def _parse_problem_args(message: str) -> tuple[str, str]:
        text = NyojRankPlugin._strip_command_prefix(message, "查询题目")
        if text is None:
            text = (message or "").strip()

        parts = text.split()
        if len(parts) < 2:
            raise ValueError("参数格式错误，请使用：查询题目 比赛名 题号")

        display_id = parts[-1].strip()
        contest_name = " ".join(parts[:-1]).strip()
        if not contest_name or not display_id:
            raise ValueError("参数格式错误，比赛名和题号都不能为空，例如：查询题目 新生赛 A")
        return contest_name, display_id

    def _parse_user_profile_target(self, event: AstrMessageEvent) -> tuple[str, bool]:
        text = NyojRankPlugin._strip_command_prefix(event.message_str, "查询用户")
        if text is None:
            text = (event.message_str or "").strip()
        if not text:
            mentioned_qq = self._extract_at_qq(event)
            if mentioned_qq:
                return f"@{mentioned_qq}", True
            raise ValueError("参数格式错误，请使用：查询用户 用户名，或在群里使用：查询用户 @某人")
        match = re.search(r"(?:\[CQ:at,qq=\d+\]|\[At:\d+\])", text)
        if match:
            return match.group(0), True

        mentioned_qq = self._extract_at_qq(event)
        if mentioned_qq:
            return f"@{mentioned_qq}", True
        return text.split()[0], False

    async def _resolve_user_profile_lookup(
        self,
        event: AstrMessageEvent,
        raw_target: str,
        force_email: bool = False,
    ) -> tuple[str, bool]:
        if force_email:
            qq = self._target_to_qq(event, raw_target)
            if qq:
                return f"{qq}@qq.com", True

        if await self.runtime.user_exists_by_identifier(raw_target):
            return raw_target, False

        qq = self._target_to_qq(event, raw_target)
        if qq:
            return f"{qq}@qq.com", True
        return raw_target, False

    def _extract_at_qq(self, event: AstrMessageEvent) -> str:
        text = getattr(event, "message_str", "") or ""
        match = re.search(r"(?:\[CQ:at,qq=|\[At:)(\d+)\]?", text)
        if match:
            return match.group(1)

        sources = [
            event,
            getattr(event, "message_obj", None),
            getattr(event, "message_chain", None),
            getattr(event, "chain", None),
            getattr(event, "message", None),
        ]
        seen: set[int] = set()
        for source in sources:
            qq = self._extract_at_qq_from_object(source, seen, 0)
            if qq:
                return qq
        return ""

    def _extract_at_qq_from_object(
        self,
        obj: object,
        seen: set[int],
        depth: int,
    ) -> str:
        if obj is None or depth > 5:
            return ""
        object_id = id(obj)
        if object_id in seen:
            return ""
        seen.add(object_id)

        if isinstance(obj, dict):
            for key in ("raw_message", "message_str", "plain_text", "text"):
                value = obj.get(key)
                if isinstance(value, str):
                    match = re.search(r"(?:\[CQ:at,qq=|\[At:)(\d+)\]?", value)
                    if match:
                        return match.group(1)
            type_text = str(
                obj.get("type") or obj.get("message_type") or obj.get("name") or ""
            ).lower()
            if "at" in type_text:
                data = obj.get("data")
                sources = [obj, data] if isinstance(data, dict) else [obj]
                for source in sources:
                    for key in ("qq", "user_id", "target", "id"):
                        value = source.get(key)
                        if value and str(value).isdigit():
                            return str(value)
            for value in obj.values():
                qq = self._extract_at_qq_from_object(value, seen, depth + 1)
                if qq:
                    return qq
            return ""

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                qq = self._extract_at_qq_from_object(item, seen, depth + 1)
                if qq:
                    return qq
            return ""

        if "at" in obj.__class__.__name__.lower():
            for attr in ("qq", "user_id", "target", "id"):
                value = getattr(obj, attr, None)
                if value and str(value).isdigit():
                    return str(value)

        for attr in ("raw_message", "message_str", "plain_text", "text"):
            value = getattr(obj, attr, None)
            if isinstance(value, str):
                match = re.search(r"(?:\[CQ:at,qq=|\[At:)(\d+)\]?", value)
                if match:
                    return match.group(1)

        for attr in ("data", "message", "messages", "chain", "components"):
            value = getattr(obj, attr, None)
            if value is None or callable(value):
                continue
            qq = self._extract_at_qq_from_object(value, seen, depth + 1)
            if qq:
                return qq
        return ""

    def _target_to_qq(self, event: AstrMessageEvent, raw_target: str) -> str:
        target = raw_target.strip()
        if target == "自己":
            return self._extract_sender_qq(event)

        match = re.fullmatch(r"(?:\[CQ:at,qq=|\[At:)(\d+)\]?", target)
        if match:
            return match.group(1)

        match = re.fullmatch(r"@(\d+)", target)
        if match:
            return match.group(1)

        return ""

    def _extract_sender_qq(self, event: AstrMessageEvent) -> str:
        sources = [
            event,
            getattr(event, "message_obj", None),
            getattr(event, "sender", None),
            getattr(getattr(event, "message_obj", None), "sender", None),
        ]
        for source in sources:
            qq = self._extract_sender_qq_from_object(source, set(), 0)
            if qq:
                return qq
        return ""

    def _extract_sender_qq_from_object(self, obj: object, seen: set[int], depth: int) -> str:
        if obj is None or depth > 5:
            return ""
        obj_id = id(obj)
        if obj_id in seen:
            return ""
        seen.add(obj_id)

        if isinstance(obj, dict):
            for key in ("sender_id", "user_id", "qq", "id"):
                value = obj.get(key)
                if value and str(value).isdigit():
                    return str(value)
            sender = obj.get("sender")
            qq = self._extract_sender_qq_from_object(sender, seen, depth + 1)
            if qq:
                return qq
            return ""

        for attr in ("sender_id", "user_id", "qq", "id"):
            value = getattr(obj, attr, None)
            if value and str(value).isdigit():
                return str(value)

        for attr in ("sender", "message_obj", "data"):
            value = getattr(obj, attr, None)
            if value is None or callable(value):
                continue
            qq = self._extract_sender_qq_from_object(value, seen, depth + 1)
            if qq:
                return qq
        return ""

    @staticmethod
    def _is_date_text(text: str) -> bool:
        if len(text) != 10 or text[4] != "-" or text[7] != "-":
            return False
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return False
        return True

    def _broadcast_targets(self) -> list[str]:
        cfg = self._plugin_config()
        platform_ids = self._active_platform_ids()
        default_platform_id = self._preferred_broadcast_platform_id(
            platform_ids,
            cfg.broadcast_platform_id,
        )
        default_message_type = cfg.broadcast_message_type or "GroupMessage"
        targets = []
        for target in cfg.broadcast_targets:
            target = str(target).strip()
            if not target:
                continue
            if ":" not in target and target.isdigit():
                target = f"{default_platform_id}:{default_message_type}:{target}"
            else:
                target = self._fix_broadcast_platform(
                    target,
                    platform_ids,
                    cfg.broadcast_platform_id,
                )
            targets.append(target)
        return targets

    @staticmethod
    def _preferred_broadcast_platform_id(
        platform_ids: list[str],
        configured_platform_id: str = "OJbot",
    ) -> str:
        configured_platform_id = (configured_platform_id or "").strip()
        if configured_platform_id:
            return configured_platform_id
        for platform_id in platform_ids:
            if platform_id.lower() != "webchat":
                return platform_id
        return platform_ids[0] if platform_ids else "OJbot"

    def _active_platform_ids(self) -> list[str]:
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return []

        raw_instances = []
        for attr in ("platform_insts", "platforms", "insts", "instances"):
            value = getattr(manager, attr, None)
            if value is None:
                continue
            if callable(value):
                try:
                    value = value()
                except TypeError:
                    continue
            if isinstance(value, dict):
                raw_instances.extend(value.values())
            elif isinstance(value, (list, tuple, set)):
                raw_instances.extend(value)

        ids = []
        for inst in raw_instances:
            platform_id = self._extract_platform_id(inst)
            if platform_id and platform_id not in ids:
                ids.append(platform_id)
        return ids

    @staticmethod
    def _extract_platform_id(inst: object) -> str:
        meta = getattr(inst, "meta", None)
        if callable(meta):
            try:
                meta = meta()
            except TypeError:
                meta = None

        for source in (meta, inst):
            if source is None:
                continue
            for attr in ("id", "name", "platform_id"):
                value = getattr(source, attr, None)
                if value:
                    return str(value)
        return ""

    def _fix_broadcast_platform(
        self,
        target: str,
        platform_ids: list[str],
        configured_platform_id: str = "OJbot",
    ) -> str:
        parts = target.split(":", 2)
        if len(parts) != 3:
            return target
        platform_id, message_type, session_id = parts
        if platform_id in platform_ids:
            return target
        if message_type == "GroupMessage" and session_id.isdigit():
            return f"{self._preferred_broadcast_platform_id(platform_ids, configured_platform_id)}:{message_type}:{session_id}"
        return target

    @staticmethod
    def _merge_broadcast_status(
        left: dict[str, object],
        right: dict[str, object],
    ) -> dict[str, object]:
        return {
            "total": int(left.get("total", 0)) + int(right.get("total", 0)),
            "ok": int(left.get("ok", 0)) + int(right.get("ok", 0)),
            "fail": int(left.get("fail", 0)) + int(right.get("fail", 0)),
            "details": list(left.get("details", [])) + list(right.get("details", [])),
        }

    def _remember_broadcast_status(self, status: dict[str, object]) -> None:
        self._last_broadcast_status = (
            f"{self._now_text()} 目标={status.get('total', 0)}，"
            f"成功={status.get('ok', 0)}，失败={status.get('fail', 0)}"
        )
        self._last_broadcast_details = [str(detail) for detail in status.get("details", [])]

    @staticmethod
    def _image_debug_label(label: str, image_path: str) -> str:
        return f"{label}（{NyojRankPlugin._image_file_info(image_path)}）"

    @staticmethod
    def _image_file_info(image_path: str) -> str:
        path = Path(image_path)
        try:
            exists = path.exists()
            size = path.stat().st_size if exists else 0
        except OSError as exc:
            return f"file={path.name}，读取失败={type(exc).__name__}:{exc}"
        return f"file={path.name}，exists={exists}，size={size}"

    @staticmethod
    def _short_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    @staticmethod
    def _now_text() -> str:
        return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

    async def _restore_recent_statuses(self) -> None:
        keys = {
            "last_user_sync": "_last_user_sync_status",
            "last_ac_sync": "_last_ac_sync_status",
            "last_daily_ranking": "_last_daily_ranking_status",
            "last_notify": "_last_notify_status",
        }
        for key, attr in keys.items():
            value = await self.runtime.repository.get_config_state(f"status.{key}")
            if value:
                setattr(self, attr, value)

    async def _save_recent_status(self, key: str, value: str) -> None:
        await self.runtime.repository.set_config_state(f"status.{key}", value)

    def _push_status_text(self) -> str:
        scheduler = getattr(self, "scheduler", None)
        inner = getattr(scheduler, "scheduler", None)
        cfg = self._plugin_config()

        user_job = inner.get_job("getUserInfo") if inner is not None else None
        ac_job = inner.get_job("getUserAC") if inner is not None else None
        daily_job = inner.get_job("getDailyRank") if inner is not None else None

        def format_next_run(job) -> str:
            next_run_time = getattr(job, "next_run_time", None)
            if next_run_time is None:
                return "\u65e0"
            return next_run_time.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

        unknown_text = "\u672a\u8bc6\u522b"
        lines = [
            f"\u5f53\u524d\u5e73\u53f0ID\uff1a{', '.join(self._active_platform_ids()) or unknown_text}",
            f"\u5f53\u524d\u63a8\u9001\u76ee\u6807\u6570\uff1a{len(self._broadcast_targets())}",
            "",
            f"\u542f\u7528\u7528\u6237\u5b9a\u65f6\u540c\u6b65\uff1a{cfg.user_sync_schedule_enabled}",
            f"\u7528\u6237\u540c\u6b65\u65f6\u95f4\uff1a{cfg.user_sync_schedule_time}",
            f"\u6700\u8fd1\u7528\u6237\u540c\u6b65\uff1a{self._last_user_sync_status}",
            f"\u7528\u6237\u540c\u6b65\u4e0b\u6b21\u8fd0\u884c\uff1a{format_next_run(user_job)}",
            "",
            f"\u542f\u7528AC\u5b9a\u65f6\u540c\u6b65\uff1a{cfg.ac_sync_schedule_enabled}",
            f"AC\u540c\u6b65\u65f6\u95f4\uff1a{cfg.ac_sync_schedule_time}",
            f"\u6700\u8fd1AC\u540c\u6b65\uff1a{self._last_ac_sync_status}",
            f"AC\u540c\u6b65\u4e0b\u6b21\u8fd0\u884c\uff1a{format_next_run(ac_job)}",
            "",
            f"\u542f\u7528\u6bcf\u65e5\u699c\u5355\u5b9a\u65f6\u63a8\u9001\uff1a{cfg.daily_ranking_enabled}",
            f"\u6bcf\u65e5\u699c\u5355\u53d1\u9001\u65f6\u95f4\uff1a{cfg.daily_ranking_send_time}",
            f"\u6bcf\u65e5\u699c\u5355\u7edf\u8ba1\u65f6\u95f4\u6bb5\uff1a{cfg.daily_ranking_window_start_time} \u81f3 {cfg.daily_ranking_window_end_time}",
            f"\u6700\u8fd1\u6bcf\u65e5\u699c\u5355\uff1a{self._last_daily_ranking_status}",
            f"\u6bcf\u65e5\u699c\u5355\u4e0b\u6b21\u8fd0\u884c\uff1a{format_next_run(daily_job)}",
            "",
            f"\u542f\u7528\u8fc7\u9898\u901a\u77e5\uff1a{cfg.notify_enabled}",
            f"\u6700\u8fd1\u8fc7\u9898\u901a\u77e5\uff1a{self._last_notify_status}",
        ]
        return "\n".join(lines)

    def _plugin_paths(self) -> PluginPaths:
        plugin_root = Path(__file__).resolve().parent
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            data_root = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except Exception as exc:
            logger.warning("无法获取 AstrBot 数据目录，尝试根据插件目录推断: %s", exc)
            data_root = self._guess_data_root(plugin_root)
        return PluginPaths.from_root(plugin_root, data_root=data_root)

    @staticmethod
    def _guess_data_root(plugin_root: Path) -> Path:
        plugin_root = plugin_root.resolve()
        if plugin_root.parent.name == "plugins" and plugin_root.parent.parent.name == "data":
            return plugin_root.parent.parent / "plugin_data" / PLUGIN_NAME
        return plugin_root / "data"

    async def terminate(self):
        """Clean up scheduler when plugin is unloaded."""
        await self.scheduler.shutdown()
