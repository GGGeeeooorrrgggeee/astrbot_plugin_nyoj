from __future__ import annotations

from typing import Dict, List, Sequence

from config import PluginConfig
from models import RankingEntry


class RankingService:
    """Build live rankings and compare them with saved ones."""

    def __init__(self, repository):
        self.repository = repository

    async def get_latest_ranking(
        self,
        config: PluginConfig,
        after_date: str | None = None,
    ) -> List[RankingEntry]:
        source_rows = await self.repository.get_rank_source_rows(
            after_date or config.ranking_after_date
        )
        blacklist = {
            username.strip()
            for username in config.ranking_username_blacklist
            if username.strip()
        }
        if blacklist:
            source_rows = [row for row in source_rows if str(row[1]).strip() not in blacklist]
        return self._build_rankings(source_rows, config.ranking_limit)

    @staticmethod
    def _build_rankings(source_rows: Sequence[tuple], limit: int) -> List[RankingEntry]:
        ranking: List[RankingEntry] = []
        threshold_ac = None

        for index, row in enumerate(source_rows, start=1):
            uuid, username, ac_count = row[:3]
            gender = row[3] if len(row) > 3 else ""
            if len(ranking) < limit:
                ranking.append(
                    RankingEntry(
                        rank=index,
                        uuid=uuid,
                        username=username,
                        ac_count=ac_count,
                        gender=str(gender or ""),
                    )
                )
                if len(ranking) == limit:
                    threshold_ac = ac_count
                continue

            if threshold_ac is not None and ac_count == threshold_ac:
                ranking.append(
                    RankingEntry(
                        rank=index,
                        uuid=uuid,
                        username=username,
                        ac_count=ac_count,
                        gender=str(gender or ""),
                    )
                )
                continue
            break

        return ranking

    @staticmethod
    def is_changed(
        old_ranking: Sequence[RankingEntry],
        new_ranking: Sequence[RankingEntry],
    ) -> bool:
        return RankingService.has_rank_movement(old_ranking, new_ranking)

    @staticmethod
    def needs_cache_update(
        old_ranking: Sequence[RankingEntry],
        new_ranking: Sequence[RankingEntry],
    ) -> bool:
        if len(old_ranking) != len(new_ranking):
            return True

        old_map: Dict[str, RankingEntry] = {item.uuid: item for item in old_ranking}
        new_map: Dict[str, RankingEntry] = {item.uuid: item for item in new_ranking}

        if set(old_map.keys()) != set(new_map.keys()):
            return True

        for uuid, old_entry in old_map.items():
            new_entry = new_map[uuid]
            if (
                old_entry.rank != new_entry.rank
                or old_entry.ac_count != new_entry.ac_count
                or old_entry.username != new_entry.username
            ):
                return True
        return False

    @staticmethod
    def has_rank_movement(
        old_ranking: Sequence[RankingEntry],
        new_ranking: Sequence[RankingEntry],
    ) -> bool:
        old_map: Dict[str, RankingEntry] = {item.uuid: item for item in old_ranking}
        for user in new_ranking:
            old_user = old_map.get(user.uuid)
            if old_user is None:
                continue
            if user.rank < old_user.rank or user.rank > old_user.rank:
                return True
        return False
