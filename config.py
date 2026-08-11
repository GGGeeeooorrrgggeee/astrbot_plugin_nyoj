from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping


def _to_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return default
    return max(parsed, minimum)


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on", "开启"):
            return True
        if text in ("false", "0", "no", "off", "关闭"):
            return False
    return bool(value)


def _section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    return value if isinstance(value, Mapping) else {}


def _get(data: Mapping[str, Any], section: Mapping[str, Any], key: str, default: Any) -> Any:
    return section.get(key, default)


@dataclass
class PluginConfig:
    oj_name: str = "NYOJ"
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = ""
    nyoj_base_url: str = "https://xcpc.nyist.edu.cn"
    nyoj_username: str = ""
    nyoj_password: str = ""
    nyoj_secret_key: str = ""
    allow_private_contest_rank: bool = False
    broadcast_platform_id: str = "OJbot"
    broadcast_message_type: str = "GroupMessage"
    broadcast_targets: List[str] = field(default_factory=list)
    user_sync_schedule_enabled: bool = False
    ac_sync_schedule_enabled: bool = False
    user_sync_schedule_time: str = "01:30"
    ac_sync_schedule_time: str = "45"
    ranking_after_date: str = "2026-06-01"
    ranking_limit: int = 10
    manual_ranking_limit_max: int = 50
    ranking_title: str = "2026 新生总排行"
    ranking_username_blacklist: List[str] = field(default_factory=list)
    daily_ranking_enabled: bool = True
    daily_ranking_window_start_time: str = "00:00:00"
    daily_ranking_window_end_time: str = "00:00:00"
    daily_ranking_send_time: str = "00:00:00"
    daily_ranking_query_timeout_seconds: int = 40
    notify_enabled: bool = True
    notify_threshold: int = 5

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "PluginConfig":
        data = dict(mapping or {})
        basic = _section(data, "basic")
        mysql = _section(data, "mysql")
        nyoj_api = dict(_section(data, "nyoj_api"))
        broadcast = _section(data, "broadcast")
        schedule = _section(data, "schedule")
        ranking = _section(data, "ranking")
        daily_ranking = _section(data, "daily_ranking")
        notification = _section(data, "notification")

        raw_targets = _get(data, broadcast, "broadcast_targets", [])
        if isinstance(raw_targets, str):
            targets = [t.strip() for t in raw_targets.split(",") if t.strip()]
        else:
            targets = [str(t).strip() for t in raw_targets if str(t).strip()]

        raw_ranking_blacklist = _get(data, ranking, "ranking_username_blacklist", [])
        if isinstance(raw_ranking_blacklist, str):
            ranking_blacklist = [
                item.strip()
                for item in raw_ranking_blacklist.split(",")
                if item.strip()
            ]
        else:
            ranking_blacklist = [
                str(item).strip()
                for item in raw_ranking_blacklist
                if str(item).strip()
            ]

        return cls(
            oj_name=str(_get(data, basic, "oj_name", "NYOJ") or "NYOJ").strip(),
            mysql_host=str(_get(data, mysql, "mysql_host", "") or "").strip(),
            mysql_port=_to_int(_get(data, mysql, "mysql_port", 3306), 3306),
            mysql_user=str(_get(data, mysql, "mysql_user", "") or "").strip(),
            mysql_password=str(_get(data, mysql, "mysql_password", "") or ""),
            mysql_database=str(_get(data, mysql, "mysql_database", "") or "").strip(),
            nyoj_base_url=str(
                _get(data, nyoj_api, "nyoj_base_url", "https://xcpc.nyist.edu.cn")
                or "https://xcpc.nyist.edu.cn"
            ).strip().rstrip("/"),
            nyoj_username=str(_get(data, nyoj_api, "nyoj_username", "") or "").strip(),
            nyoj_password=str(_get(data, nyoj_api, "nyoj_password", "") or ""),
            nyoj_secret_key=str(_get(data, nyoj_api, "nyoj_secret_key", "") or "").strip(),
            allow_private_contest_rank=_to_bool(
                _get(data, nyoj_api, "allow_private_contest_rank", False),
                False,
            ),
            broadcast_platform_id=str(
                _get(data, broadcast, "broadcast_platform_id", "OJbot") or "OJbot"
            ).strip(),
            broadcast_message_type=str(
                _get(data, broadcast, "broadcast_message_type", "GroupMessage") or "GroupMessage"
            ).strip(),
            broadcast_targets=targets,
            user_sync_schedule_enabled=_to_bool(
                _get(data, schedule, "user_sync_schedule_enabled", False),
                False,
            ),
            ac_sync_schedule_enabled=_to_bool(
                _get(data, schedule, "ac_sync_schedule_enabled", False),
                False,
            ),
            user_sync_schedule_time=str(
                _get(data, schedule, "user_sync_schedule_time", "01:30") or "01:30"
            ).strip(),
            ac_sync_schedule_time=str(
                _get(data, schedule, "ac_sync_schedule_time", "45") or "45"
            ).strip(),
            ranking_after_date=str(
                _get(data, ranking, "ranking_after_date", "2026-06-01") or "2026-06-01"
            ).strip(),
            ranking_limit=_to_int(_get(data, ranking, "ranking_limit", 10), 10),
            manual_ranking_limit_max=_to_int(
                _get(data, ranking, "manual_ranking_limit_max", 50),
                50,
            ),
            ranking_title=str(
                _get(data, ranking, "ranking_title", "2026 新生总排行") or "2026 新生总排行"
            ),
            ranking_username_blacklist=ranking_blacklist,
            daily_ranking_enabled=_to_bool(
                _get(data, daily_ranking, "daily_ranking_enabled", True),
                True,
            ),
            daily_ranking_window_start_time=str(
                _get(data, daily_ranking, "daily_ranking_window_start_time", "00:00:00")
                or "00:00:00"
            ).strip(),
            daily_ranking_window_end_time=str(
                _get(data, daily_ranking, "daily_ranking_window_end_time", "00:00:00")
                or "00:00:00"
            ).strip(),
            daily_ranking_send_time=str(
                _get(data, daily_ranking, "daily_ranking_send_time", "00:00:00")
                or "00:00:00"
            ).strip(),
            daily_ranking_query_timeout_seconds=_to_int(
                _get(data, daily_ranking, "daily_ranking_query_timeout_seconds", 40),
                40,
            ),
            notify_enabled=_to_bool(
                _get(data, notification, "notify_enabled", True),
                True,
            ),
            notify_threshold=_to_int(_get(data, notification, "notify_threshold", 5), 5),
        )

    def has_mysql_config(self) -> bool:
        return bool(self.mysql_host and self.mysql_port and self.mysql_user and self.mysql_database)

    def has_nyoj_api_config(self) -> bool:
        return bool(
            self.nyoj_base_url
            and self.nyoj_username
            and self.nyoj_password
            and self.nyoj_secret_key
        )
