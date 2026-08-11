from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import aiosqlite

from models import LocalUserProfile, NotifyCandidate, RankingEntry

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class SQLiteRepository:
    """All local database operations for the plugin."""

    def __init__(self, database_path: str):
        self.database_path = database_path

    def _connect(self):
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        return aiosqlite.connect(self.database_path)

    # ------------------------------------------------------------------ #
    #  Schema initialisation                                             #
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_sync_time DATETIME
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS config_state (
                    name  TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_info (
                    uuid      TEXT PRIMARY KEY,
                    username  TEXT,
                    email     TEXT,
                    gender    TEXT,
                    gmt_create TEXT,
                    last_update DATETIME
                )
                """
            )
            await self._ensure_column(db, "user_info", "gender", "TEXT")
            await self._ensure_column(db, "user_info", "last_update", "DATETIME")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_ac_stats (
                    uid                     TEXT PRIMARY KEY,
                    ac_count                INTEGER NOT NULL,
                    last_update             DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_notified_ac_count  INTEGER DEFAULT 0,
                    last_notify_time        DATETIME
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_ac_detail (
                    uid TEXT,
                    pid INTEGER,
                    PRIMARY KEY (uid, pid)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_ranking (
                    rank     INTEGER,
                    uuid     TEXT,
                    username TEXT,
                    ac_count INTEGER
                )
                """
            )
            await db.commit()

    # ------------------------------------------------------------------ #
    #  Sync state                                                        #
    # ------------------------------------------------------------------ #

    async def get_last_sync_time(self) -> Optional[str]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT last_sync_time FROM sync_state WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else None

    async def update_sync_time(self, now_time: str) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO sync_state (id, last_sync_time)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET last_sync_time = excluded.last_sync_time
                """,
                (now_time,),
            )
            await db.commit()

    async def get_config_state(self, name: str) -> Optional[str]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT value FROM config_state WHERE name = ?",
                (name,),
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else None

    async def set_config_state(self, name: str, value: str) -> None:
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO config_state (name, value)
                VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = excluded.value
                """,
                (name, value),
            )
            await db.commit()

    async def get_debug_snapshot(self) -> Dict[str, object]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT last_sync_time FROM sync_state WHERE id = 1"
            ) as cursor:
                sync_row = await cursor.fetchone()

            snapshot: Dict[str, object] = {
                "database_path": self.database_path,
                "last_sync_time": sync_row[0] if sync_row else "",
            }

            for table_name in (
                "user_info",
                "user_ac_detail",
                "user_ac_stats",
                "user_ranking",
            ):
                async with db.execute(f"SELECT COUNT(*) FROM {table_name}") as cursor:
                    row = await cursor.fetchone()
                snapshot[f"{table_name}_count"] = row[0] if row else 0

            async with db.execute(
                "SELECT MAX(last_update) FROM user_info"
            ) as cursor:
                row = await cursor.fetchone()
            snapshot["max_user_update_time"] = row[0] if row else ""

            async with db.execute(
                "SELECT MAX(last_update) FROM user_ac_stats"
            ) as cursor:
                row = await cursor.fetchone()
            snapshot["max_ac_update_time"] = row[0] if row else ""

            async with db.execute(
                "SELECT MAX(last_notify_time) FROM user_ac_stats"
            ) as cursor:
                row = await cursor.fetchone()
            snapshot["max_notify_time"] = row[0] if row else ""

        return snapshot

    # ------------------------------------------------------------------ #
    #  User info                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _ensure_column(
        db: aiosqlite.Connection,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
            columns = {row[1] async for row in cursor}
        if column_name not in columns:
            await db.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
            )

    async def upsert_user_info(self, rows: Sequence[dict], track_update: bool = True) -> int:
        async with self._connect() as db:
            update_time = datetime.now(BEIJING_TZ) if track_update else None
            for row in rows:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO user_info (
                        uuid, username, email, gender, gmt_create, last_update
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.get("uuid", ""),
                        row.get("username", ""),
                        row.get("email", ""),
                        row.get("gender", ""),
                        row.get("gmt_create", ""),
                        update_time,
                    ),
                )
            await db.commit()
        return len(rows)

    async def get_username(self, uid: str) -> Optional[str]:
        async with self._connect() as db:
            async with db.execute(
                "SELECT username FROM user_info WHERE uuid = ?", (uid,)
            ) as cursor:
                result = await cursor.fetchone()
        return result[0] if result else None

    async def get_genders_by_usernames(self, usernames: Sequence[str]) -> Dict[str, str]:
        names = [str(username).strip() for username in usernames if str(username).strip()]
        if not names:
            return {}
        placeholders = ", ".join(["?"] * len(names))
        genders: Dict[str, str] = {}
        async with self._connect() as db:
            async with db.execute(
                f"""
                SELECT username, COALESCE(gender, '')
                FROM user_info
                WHERE username IN ({placeholders})
                """,
                names,
            ) as cursor:
                async for username, gender in cursor:
                    genders[str(username)] = str(gender or "")
        return genders

    async def find_user_by_email(self, email: str) -> Optional[LocalUserProfile]:
        return await self._find_user(
            "LOWER(u.email) = LOWER(?)",
            (email,),
        )

    async def find_user_by_identifier(self, identifier: str) -> Optional[LocalUserProfile]:
        identifier = identifier.strip()
        if "@" in identifier:
            return await self.find_user_by_email(identifier)
        return await self._find_user(
            "u.uuid = ? OR u.username = ?",
            (identifier, identifier),
        )

    async def _find_user(
        self,
        where_clause: str,
        params: tuple[object, ...],
    ) -> Optional[LocalUserProfile]:
        async with self._connect() as db:
            async with db.execute(
                f"""
                SELECT
                    u.uuid,
                    u.username,
                    COALESCE(u.email, ''),
                    COALESCE(u.gender, ''),
                    COALESCE(u.gmt_create, ''),
                    COALESCE(s.ac_count, 0)
                FROM user_info u
                LEFT JOIN user_ac_stats s ON u.uuid = s.uid
                WHERE {where_clause}
                LIMIT 1
                """,
                params,
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return LocalUserProfile(
            uuid=row[0],
            username=row[1],
            email=row[2],
            gender=row[3],
            gmt_create=row[4],
            ac_count=row[5],
        )

    # ------------------------------------------------------------------ #
    #  AC stats                                                          #
    # ------------------------------------------------------------------ #

    async def apply_ac_updates(
        self,
        ac_counts: Dict[str, set],
        track_update: bool = True,
    ) -> int:
        """Apply incremental AC counts, returning the number of touched users."""
        touched = 0
        update_time = datetime.now(BEIJING_TZ) if track_update else None
        async with self._connect() as db:
            for uid, new_pids in ac_counts.items():
                existing_pids: set = set()
                async with db.execute(
                    "SELECT pid FROM user_ac_detail WHERE uid = ?", (uid,)
                ) as cursor:
                    async for row in cursor:
                        existing_pids.add(row[0])

                fresh_pids = new_pids - existing_pids

                for pid in fresh_pids:
                    await db.execute(
                        "INSERT OR IGNORE INTO user_ac_detail (uid, pid) VALUES (?, ?)",
                        (uid, pid),
                    )

                if fresh_pids:
                    async with db.execute(
                        "SELECT ac_count FROM user_ac_stats WHERE uid = ?", (uid,)
                    ) as cursor:
                        row = await cursor.fetchone()
                    prev = row[0] if row else 0
                    new_count = prev + len(fresh_pids)

                    await db.execute(
                        """
                        INSERT INTO user_ac_stats (uid, ac_count, last_update)
                        VALUES (?, ?, ?)
                        ON CONFLICT(uid) DO UPDATE SET
                            ac_count = excluded.ac_count,
                            last_update = excluded.last_update
                        """,
                        (uid, new_count, update_time),
                    )
                    touched += 1

            await db.commit()
        return touched

    # ------------------------------------------------------------------ #
    #  Notifications                                                     #
    # ------------------------------------------------------------------ #

    async def find_users_to_notify(self, threshold: int) -> List[NotifyCandidate]:
        candidates: List[NotifyCandidate] = []
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT uid, ac_count, last_notified_ac_count
                FROM user_ac_stats
                WHERE (ac_count - COALESCE(last_notified_ac_count, 0)) >= ?
                """,
                (threshold,),
            ) as cursor:
                async for uid, current_ac, last_notified_ac in cursor:
                    candidates.append(
                        NotifyCandidate(
                            uid=uid,
                            current_ac=current_ac,
                            increase_ac=current_ac - last_notified_ac,
                        )
                    )
        return candidates

    async def reset_notification_baseline(self) -> int:
        """Make the current AC count the notification baseline for every user."""
        async with self._connect() as db:
            cursor = await db.execute(
                """
                UPDATE user_ac_stats
                SET last_notified_ac_count = ac_count
                WHERE COALESCE(last_notified_ac_count, -1) != ac_count
                """
            )
            await db.commit()
            return cursor.rowcount

    async def clear_recent_data_times(self) -> None:
        """Clear data-change markers after a manual initialization baseline."""
        async with self._connect() as db:
            await db.execute("UPDATE user_info SET last_update = NULL")
            await db.execute("UPDATE user_ac_stats SET last_update = NULL, last_notify_time = NULL")
            await db.commit()

    async def mark_notified(self, candidates: Iterable[NotifyCandidate]) -> None:
        now = datetime.now(BEIJING_TZ)
        async with self._connect() as db:
            for candidate in candidates:
                await db.execute(
                    """
                    UPDATE user_ac_stats
                    SET last_notified_ac_count = ?,
                        last_notify_time = ?
                    WHERE uid = ?
                    """,
                    (candidate.current_ac, now, candidate.uid),
                )
            await db.commit()

    # ------------------------------------------------------------------ #
    #  Ranking                                                           #
    # ------------------------------------------------------------------ #

    async def generate_user_rank(
        self,
        after_date: str,
        limit: int,
        username_blacklist: List[str] | None = None,
    ) -> int:
        """Persist the current ranking into user_ranking, returning the count."""
        blacklist = {username.strip() for username in (username_blacklist or []) if username.strip()}
        async with self._connect() as db:
            await db.execute("DELETE FROM user_ranking")

            async with db.execute(
                """
                SELECT u.uuid, u.username, s.ac_count
                FROM user_info u
                JOIN user_ac_stats s ON u.uuid = s.uid
                WHERE datetime(u.gmt_create) > datetime(?)
                ORDER BY s.ac_count DESC, u.username ASC, u.uuid ASC
                """,
                (after_date,),
            ) as cursor:
                rank = 1
                last_ac_count = 1
                async for row in cursor:
                    uuid, username, ac_count = row
                    if str(username).strip() in blacklist:
                        continue
                    if ac_count < last_ac_count:
                        break
                    await db.execute(
                        """
                        INSERT INTO user_ranking (rank, uuid, username, ac_count)
                        VALUES (?, ?, ?, ?)
                        """,
                        (rank, uuid, username, ac_count),
                    )
                    if rank == limit:
                        last_ac_count = ac_count
                    rank += 1

            await db.commit()
            return rank - 1

    async def get_saved_ranking(self) -> List[RankingEntry]:
        ranking: List[RankingEntry] = []
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT rank, uuid, username, ac_count
                FROM user_ranking
                ORDER BY rank ASC
                """
            ) as cursor:
                async for row in cursor:
                    ranking.append(
                        RankingEntry(
                            rank=row[0],
                            uuid=row[1],
                            username=row[2],
                            ac_count=row[3],
                        )
                    )
        return ranking

    async def get_rank_source_rows(self, after_date: str) -> List[Tuple[str, str, int, str]]:
        rows: List[Tuple[str, str, int, str]] = []
        async with self._connect() as db:
            async with db.execute(
                """
                SELECT u.uuid, u.username, s.ac_count, COALESCE(u.gender, '')
                FROM user_info u
                JOIN user_ac_stats s ON u.uuid = s.uid
                WHERE datetime(u.gmt_create) > datetime(?)
                ORDER BY s.ac_count DESC, u.username ASC, u.uuid ASC
                """,
                (after_date,),
            ) as cursor:
                async for row in cursor:
                    rows.append((row[0], row[1], row[2], row[3]))
        return rows
