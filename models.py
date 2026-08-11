from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class RankingEntry:
    rank: int
    uuid: str
    username: str
    ac_count: int
    gender: str = ""


@dataclass
class NotifyCandidate:
    uid: str
    current_ac: int
    increase_ac: int


@dataclass
class RenderedNotify:
    uid: str
    increase_ac: int
    username: str
    image_path: str


@dataclass
class SyncCycleResult:
    synced_ac_user_count: int = 0
    notifications: List[RenderedNotify] = field(default_factory=list)
    ranking_changed: bool = False
    ranking_image_path: Optional[str] = None


@dataclass
class DailyRankingResult:
    title: str
    start_time: str
    end_time: str
    entry_count: int
    image_path: str


@dataclass
class LocalUserProfile:
    uuid: str
    username: str
    email: str
    gender: str
    gmt_create: str
    ac_count: int = 0


@dataclass
class UserProfileCard:
    oj_name: str
    username: str
    gender: str
    avatar: str
    registered_at: str
    last_submission_at: str
    last_submission_result: str
    last_login_at: str
    permission: str
    total_ac: int


@dataclass
class ProblemQueryResult:
    contest_name: str
    display_id: str
    contest_id: str
    problem_title: str
    problem_url: str
    ac_count: int
    total_count: int
    acceptance_rate: float
    html_path: str
    image_path: str
