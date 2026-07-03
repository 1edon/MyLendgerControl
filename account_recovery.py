from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any

import aiosqlite


logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class UserProfile:
    id: int
    current_user_id: int
    recovery_code: Optional[str]
    phone_hash: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    created_at: str
    updated_at: str
    is_merged_into: Optional[int]
    notes: Optional[str] = None


@dataclass(slots=True)
class ImportCandidate:
    profile_id: int
    current_user_id: int
    username: Optional[str]
    username_match_type: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    phone_hash_match: bool
    recovery_code_match: bool
    score: int
    status: str
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SearchResult:
    status: str
    candidates: list[ImportCandidate] = field(default_factory=list)
    selected_candidate: Optional[ImportCandidate] = None
    message: Optional[str] = None


@dataclass(slots=True)
class MigrationResult:
    success: bool
    status: str
    old_user_id: int
    new_user_id: int
    reason: str
    merged_profile_id: Optional[int] = None
    users_merged: bool = False
    categories_moved: int = 0
    categories_relinked: int = 0
    transactions_moved: int = 0
    debts_moved: int = 0
    duplicates_skipped: int = 0
    message: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RecoveryByCodeResult:
    success: bool
    status: str
    code: str
    current_user_id: int
    recovered_from_user_id: Optional[int] = None
    migration_result: Optional[MigrationResult] = None
    message: Optional[str] = None


@dataclass(slots=True)
class SafetyCheckResult:
    allowed: bool
    status: str
    score: int
    message: Optional[str] = None
    candidate_profile_id: Optional[int] = None
    details: dict[str, Any] = field(default_factory=dict)


class RecoveryError(Exception):
    pass


class MigrationConflictError(RecoveryError):
    pass


class SelfMigrationError(RecoveryError):
    pass


class AccountRecoveryService:
    SCORE_RECOVERY_CODE = 100
    SCORE_PHONE_HASH = 80
    SCORE_CURRENT_USERNAME = 50
    SCORE_USERNAME_HISTORY = 40
    SCORE_FIRST_LAST = 25
    SCORE_FIRST_USERNAME = 30

    SAFE_THRESHOLD = 90
    CONFIRM_THRESHOLD = 50
    AMBIGUOUS_THRESHOLD = 30

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        async with await self._connect() as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    current_user_id INTEGER NOT NULL UNIQUE,
                    recovery_code TEXT UNIQUE,
                    phone_hash TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_merged_into INTEGER,
                    notes TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_user_profiles_current_user_id
                ON user_profiles(current_user_id);

                CREATE INDEX IF NOT EXISTS idx_user_profiles_recovery_code
                ON user_profiles(recovery_code);

                CREATE INDEX IF NOT EXISTS idx_user_profiles_phone_hash
                ON user_profiles(phone_hash);

                CREATE TABLE IF NOT EXISTS username_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    observed_at TEXT NOT NULL,
                    FOREIGN KEY(profile_id) REFERENCES user_profiles(id)
                );

                CREATE INDEX IF NOT EXISTS idx_username_history_profile_id
                ON username_history(profile_id);

                CREATE INDEX IF NOT EXISTS idx_username_history_username
                ON username_history(username);

                CREATE TABLE IF NOT EXISTS recovery_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    old_user_id INTEGER,
                    new_user_id INTEGER,
                    profile_id INTEGER,
                    username TEXT,
                    reason TEXT,
                    details_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recovery_events_event_type
                ON recovery_events(event_type);

                CREATE INDEX IF NOT EXISTS idx_recovery_events_old_user_id
                ON recovery_events(old_user_id);

                CREATE INDEX IF NOT EXISTS idx_recovery_events_new_user_id
                ON recovery_events(new_user_id);

                CREATE INDEX IF NOT EXISTS idx_recovery_events_profile_id
                ON recovery_events(profile_id);

                CREATE TABLE IF NOT EXISTS migration_locks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_user_id INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_migration_locks_source_target
                ON migration_locks(source_user_id, target_user_id);

                CREATE TABLE IF NOT EXISTS import_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    current_user_id INTEGER NOT NULL,
                    candidate_profile_id INTEGER NOT NULL,
                    requested_username TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence_score INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_import_requests_current_user_id
                ON import_requests(current_user_id);

                CREATE INDEX IF NOT EXISTS idx_import_requests_candidate_profile_id
                ON import_requests(candidate_profile_id);
            """)
            await db.commit()

    async def register_or_update_user(
        self,
        current_user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
        phone_number: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> UserProfile:
        normalized_username = self.normalize_username(username)
        phone_hash = self.hash_phone(phone_number) if phone_number else None

        async with await self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")

                profile = await self._get_profile_by_user_id(current_user_id, db)
                if profile is None:
                    recovery_code = self.generate_recovery_code()
                    now = utc_now_iso()
                    cur = await db.execute(
                        """
                        INSERT INTO user_profiles
                        (current_user_id, recovery_code, phone_hash, first_name, last_name, created_at, updated_at, is_merged_into, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                        """,
                        (current_user_id, recovery_code, phone_hash, first_name, last_name, now, now, notes),
                    )
                    profile_id = cur.lastrowid

                    if normalized_username:
                        await self._set_current_username(profile_id, normalized_username, db)

                    await self._log_event(
                        db=db,
                        event_type="register",
                        new_user_id=current_user_id,
                        profile_id=profile_id,
                        username=normalized_username,
                        reason="new_user_registered",
                        details={
                            "first_name": first_name,
                            "last_name": last_name,
                            "phone_hash_present": bool(phone_hash),
                        },
                    )

                    if normalized_username:
                        await self._log_event(
                            db=db,
                            event_type="username_observed",
                            new_user_id=current_user_id,
                            profile_id=profile_id,
                            username=normalized_username,
                            reason="initial_username",
                            details={"is_current": True},
                        )

                    await db.commit()
                    created = await self._get_profile_by_profile_id(profile_id)
                    if created is None:
                        raise RecoveryError("Не удалось загрузить созданный профиль")
                    return created

                changed_fields: dict[str, Any] = {}
                if first_name is not None and first_name != profile.first_name:
                    changed_fields["first_name"] = first_name
                if last_name is not None and last_name != profile.last_name:
                    changed_fields["last_name"] = last_name
                if phone_hash is not None and phone_hash != profile.phone_hash:
                    changed_fields["phone_hash"] = phone_hash
                if notes is not None and notes != profile.notes:
                    changed_fields["notes"] = notes
                if not profile.recovery_code:
                    changed_fields["recovery_code"] = self.generate_recovery_code()

                if changed_fields:
                    changed_fields["updated_at"] = utc_now_iso()
                    set_parts = ", ".join(f"{key} = ?" for key in changed_fields.keys())
                    values = list(changed_fields.values()) + [profile.id]
                    await db.execute(
                        f"UPDATE user_profiles SET {set_parts} WHERE id = ?",
                        values,
                    )
                    await self._log_event(
                        db=db,
                        event_type="update_profile",
                        new_user_id=current_user_id,
                        profile_id=profile.id,
                        username=normalized_username,
                        reason="profile_fields_changed",
                        details=changed_fields,
                    )

                current_username = await self._get_current_username(profile.id, db)
                if normalized_username and normalized_username != current_username:
                    await self._set_current_username(profile.id, normalized_username, db)
                    await self._log_event(
                        db=db,
                        event_type="username_observed",
                        new_user_id=current_user_id,
                        profile_id=profile.id,
                        username=normalized_username,
                        reason="username_changed_or_seen",
                        details={"previous_username": current_username},
                    )

                await db.commit()
                updated = await self._get_profile_by_user_id(current_user_id)
                if updated is None:
                    raise RecoveryError("Не удалось загрузить обновленный профиль")
                return updated

            except Exception:
                await db.rollback()
                raise

    async def find_profile_by_user_id(self, user_id: int) -> Optional[UserProfile]:
        return await self._get_profile_by_user_id(user_id)

    async def find_profiles_by_username(self, username: str) -> list[UserProfile]:
        normalized_username = self.normalize_username(username)
        if not normalized_username:
            return []

        async with await self._connect() as db:
            async with db.execute(
                """
                SELECT DISTINCT p.*
                FROM user_profiles p
                JOIN username_history h ON h.profile_id = p.id
                WHERE h.username = ?
                ORDER BY p.id ASC
                """,
                (normalized_username,),
            ) as cur:
                rows = await cur.fetchall()

        return [self._row_to_profile(row) for row in rows if row]

    async def find_profile_by_recovery_code(self, code: str) -> Optional[UserProfile]:
        async with await self._connect() as db:
            async with db.execute(
                "SELECT * FROM user_profiles WHERE recovery_code = ?",
                (code,),
            ) as cur:
                row = await cur.fetchone()
                return self._row_to_profile(row)

    async def get_or_create_recovery_code(self, user_id: int) -> str:
        async with await self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                profile = await self._get_profile_by_user_id(user_id, db)
                if profile is None:
                    raise RecoveryError("Профиль пользователя не найден")

                if profile.recovery_code:
                    await db.commit()
                    return profile.recovery_code

                code = self.generate_recovery_code()
                await db.execute(
                    "UPDATE user_profiles SET recovery_code = ?, updated_at = ? WHERE id = ?",
                    (code, utc_now_iso(), profile.id),
                )
                await self._log_event(
                    db=db,
                    event_type="update_profile",
                    new_user_id=user_id,
                    profile_id=profile.id,
                    reason="recovery_code_generated",
                    details={"recovery_code_created": True},
                )
                await db.commit()
                return code
            except Exception:
                await db.rollback()
                raise

    async def find_candidates_for_import(
        self,
        current_user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        phone_number: Optional[str] = None,
        recovery_code: Optional[str] = None,
    ) -> SearchResult:
        username = self.normalize_username(username)
        phone_hash = self.hash_phone(phone_number) if phone_number else None
        candidates_map: dict[int, ImportCandidate] = {}

        if recovery_code:
            profile = await self.find_profile_by_recovery_code(recovery_code)
            if profile:
                candidate = self._build_candidate(
                    profile=profile,
                    username=username,
                    username_match_type=None,
                    input_first_name=first_name,
                    input_last_name=last_name,
                    phone_hash=phone_hash,
                    recovery_code=recovery_code,
                )
                candidates_map[profile.id] = candidate

        if phone_hash:
            async with await self._connect() as db:
                async with db.execute(
                    "SELECT * FROM user_profiles WHERE phone_hash = ?",
                    (phone_hash,),
                ) as cur:
                    rows = await cur.fetchall()
            for row in rows:
                profile = self._row_to_profile(row)
                if profile and profile.current_user_id != current_user_id:
                    candidate = self._build_candidate(
                        profile=profile,
                        username=username,
                        username_match_type=None,
                        input_first_name=first_name,
                        input_last_name=last_name,
                        phone_hash=phone_hash,
                        recovery_code=recovery_code,
                    )
                    candidates_map[profile.id] = self._merge_candidates(candidates_map.get(profile.id), candidate)

        if username:
            async with await self._connect() as db:
                async with db.execute(
                    """
                    SELECT p.*, h.is_current
                    FROM user_profiles p
                    JOIN username_history h ON h.profile_id = p.id
                    WHERE h.username = ?
                    """,
                    (username,),
                ) as cur:
                    rows = await cur.fetchall()

            for row in rows:
                profile = self._row_to_profile(row)
                if profile and profile.current_user_id != current_user_id:
                    match_type = "current" if row["is_current"] == 1 else "history"
                    candidate = self._build_candidate(
                        profile=profile,
                        username=username,
                        username_match_type=match_type,
                        input_first_name=first_name,
                        input_last_name=last_name,
                        phone_hash=phone_hash,
                        recovery_code=recovery_code,
                    )
                    candidates_map[profile.id] = self._merge_candidates(candidates_map.get(profile.id), candidate)

        if first_name and last_name:
            async with await self._connect() as db:
                async with db.execute(
                    """
                    SELECT *
                    FROM user_profiles
                    WHERE LOWER(COALESCE(first_name, '')) = LOWER(?)
                      AND LOWER(COALESCE(last_name, '')) = LOWER(?)
                    """,
                    (first_name, last_name),
                ) as cur:
                    rows = await cur.fetchall()

            for row in rows:
                profile = self._row_to_profile(row)
                if profile and profile.current_user_id != current_user_id:
                    candidate = self._build_candidate(
                        profile=profile,
                        username=username,
                        username_match_type=None,
                        input_first_name=first_name,
                        input_last_name=last_name,
                        phone_hash=phone_hash,
                        recovery_code=recovery_code,
                    )
                    candidates_map[profile.id] = self._merge_candidates(candidates_map.get(profile.id), candidate)

        candidates = sorted(candidates_map.values(), key=lambda x: (-x.score, x.profile_id))

        if not candidates:
            return SearchResult(
                status="not_found",
                candidates=[],
                message="Подходящие профили не найдены.",
            )

        best = candidates[0]
        same_top = [c for c in candidates if c.score == best.score]

        if best.current_user_id == current_user_id:
            best.status = "already_linked"
            return SearchResult(
                status="already_linked",
                candidates=candidates,
                selected_candidate=best,
                message="Профиль уже связан с текущим аккаунтом.",
            )

        if best.score >= self.SAFE_THRESHOLD and len(same_top) == 1 and best.recovery_code_match:
            best.status = "safe_to_migrate"
            return SearchResult(
                status="safe_to_migrate",
                candidates=candidates,
                selected_candidate=best,
                message="Найден надежный кандидат для безопасной миграции.",
            )

        if best.score >= self.CONFIRM_THRESHOLD:
            if len(same_top) > 1:
                for c in same_top:
                    c.status = "ambiguous"
                return SearchResult(
                    status="ambiguous",
                    candidates=candidates,
                    message="Найдено несколько похожих профилей. Требуется ручное подтверждение.",
                )

            best.status = "needs_confirmation"
            return SearchResult(
                status="needs_confirmation",
                candidates=candidates,
                selected_candidate=best,
                message="Найден кандидат, требуется подтверждение.",
            )

        if best.score >= self.AMBIGUOUS_THRESHOLD and len(candidates) > 1:
            for c in candidates:
                c.status = "ambiguous"
            return SearchResult(
                status="ambiguous",
                candidates=candidates,
                message="Есть несколько слабых совпадений, автоматическая миграция запрещена.",
            )

        return SearchResult(
            status="not_found",
            candidates=candidates,
            message="Недостаточно надежных совпадений для импорта.",
        )

    async def request_manual_import_by_username(
        self,
        current_user_id: int,
        username_input: str,
    ) -> SearchResult:
        normalized_username = self.normalize_username(username_input)
        if not normalized_username:
            return SearchResult(
                status="not_found",
                message="Username не указан.",
            )

        current_profile = await self.find_profile_by_user_id(current_user_id)
        result = await self.find_candidates_for_import(
            current_user_id=current_user_id,
            username=normalized_username,
            first_name=current_profile.first_name if current_profile else None,
            last_name=current_profile.last_name if current_profile else None,
        )

        async with await self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._log_event(
                    db=db,
                    event_type="manual_import_requested",
                    new_user_id=current_user_id,
                    username=normalized_username,
                    reason="manual_import_by_username",
                    details={
                        "status": result.status,
                        "candidate_count": len(result.candidates),
                    },
                )

                for candidate in result.candidates:
                    await db.execute(
                        """
                        INSERT INTO import_requests
                        (current_user_id, candidate_profile_id, requested_username, status, confidence_score, details_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            current_user_id,
                            candidate.profile_id,
                            normalized_username,
                            "pending",
                            candidate.score,
                            json.dumps(
                                {
                                    "status": candidate.status,
                                    "reasons": candidate.reasons,
                                    "match_type": candidate.username_match_type,
                                },
                                ensure_ascii=False,
                            ),
                            utc_now_iso(),
                            utc_now_iso(),
                        ),
                    )

                await db.commit()
            except Exception:
                await db.rollback()
                raise

        if result.status == "safe_to_migrate":
            result.status = "needs_confirmation"
            if result.selected_candidate:
                result.selected_candidate.status = "needs_confirmation"
            result.message = "Импорт по одному username требует ручного подтверждения."

        return result

    async def confirm_manual_import(
        self,
        current_user_id: int,
        candidate_profile_id: int,
    ) -> MigrationResult:
        candidate_profile = await self._get_profile_by_profile_id(candidate_profile_id)
        if candidate_profile is None:
            return MigrationResult(
                success=False,
                status="not_found",
                old_user_id=0,
                new_user_id=current_user_id,
                reason="manual_import_confirm",
                message="Кандидат не найден.",
            )

        if candidate_profile.current_user_id == current_user_id:
            return MigrationResult(
                success=False,
                status="already_linked",
                old_user_id=current_user_id,
                new_user_id=current_user_id,
                reason="manual_import_confirm",
                message="Профиль уже привязан к текущему user_id.",
            )

        async with await self._connect() as db:
            async with db.execute(
                """
                SELECT *
                FROM import_requests
                WHERE current_user_id = ? AND candidate_profile_id = ? AND status = 'pending'
                ORDER BY id DESC
                LIMIT 1
                """,
                (current_user_id, candidate_profile_id),
            ) as cur:
                latest_request = await cur.fetchone()

        if latest_request is None:
            return MigrationResult(
                success=False,
                status="not_found",
                old_user_id=candidate_profile.current_user_id,
                new_user_id=current_user_id,
                reason="manual_import_confirm",
                message="Нет ожидающего запроса на импорт для этого кандидата.",
            )

        async with await self._connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "UPDATE import_requests SET status = ?, updated_at = ? WHERE id = ?",
                    ("confirmed", utc_now_iso(), latest_request["id"]),
                )
                await self._log_event(
                    db=db,
                    event_type="manual_import_confirmed",
                    old_user_id=candidate_profile.current_user_id,
                    new_user_id=current_user_id,
                    profile_id=candidate_profile_id,
                    reason="user_confirmed_import",
                    details={"import_request_id": latest_request["id"]},
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        return await self.migrate_user_data(
            old_user_id=candidate_profile.current_user_id,
            new_user_id=current_user_id,
            reason="manual_import_confirmed",
            force=False,
        )

    async def recover_by_code(
        self,
        current_user_id: int,
        recovery_code: str,
    ) -> RecoveryByCodeResult:
        profile = await self.find_profile_by_recovery_code(recovery_code)
        if profile is None:
            await self._log_event(
                event_type="recovery_lookup",
                new_user_id=current_user_id,
                reason="invalid_recovery_code",
                details={"recovery_code": recovery_code},
            )
            return RecoveryByCodeResult(
                success=False,
                status="not_found",
                code=recovery_code,
                current_user_id=current_user_id,
                message="Recovery code не найден.",
            )

        if profile.current_user_id == current_user_id:
            return RecoveryByCodeResult(
                success=False,
                status="already_linked",
                code=recovery_code,
                current_user_id=current_user_id,
                recovered_from_user_id=profile.current_user_id,
                message="Recovery code уже относится к текущему аккаунту.",
            )

        await self._log_event(
            event_type="recovery_lookup",
            old_user_id=profile.current_user_id,
            new_user_id=current_user_id,
            profile_id=profile.id,
            reason="valid_recovery_code",
            details={},
        )

        migration_result = await self.migrate_user_data(
            old_user_id=profile.current_user_id,
            new_user_id=current_user_id,
            reason="recover_by_code",
            force=False,
        )

        if migration_result.success:
            await self._log_event(
                event_type="recovered_by_code",
                old_user_id=profile.current_user_id,
                new_user_id=current_user_id,
                profile_id=profile.id,
                reason="recover_by_code_success",
                details={"migration_status": migration_result.status},
            )

        return RecoveryByCodeResult(
            success=migration_result.success,
            status=migration_result.status,
            code=recovery_code,
            current_user_id=current_user_id,
            recovered_from_user_id=profile.current_user_id,
            migration_result=migration_result,
            message=migration_result.message,
        )

    async def migrate_user_data(
        self,
        old_user_id: int,
        new_user_id: int,
        reason: str,
        force: bool = False,
    ) -> MigrationResult:
        if old_user_id == new_user_id:
            return MigrationResult(
                success=False,
                status="self_migration_forbidden",
                old_user_id=old_user_id,
                new_user_id=new_user_id,
                reason=reason,
                message="Нельзя переносить данные в самого себя.",
            )

        async with await self._connect() as db:
            lock_id: Optional[int] = None
            try:
                await db.execute("BEGIN IMMEDIATE")

                old_profile = await self._get_profile_by_user_id(old_user_id, db)
                new_profile = await self._get_profile_by_user_id(new_user_id, db)

                if old_profile is None:
                    await db.rollback()
                    return MigrationResult(
                        success=False,
                        status="not_found",
                        old_user_id=old_user_id,
                        new_user_id=new_user_id,
                        reason=reason,
                        message="Старый профиль не найден.",
                    )

                if old_profile.is_merged_into is not None and not force:
                    await db.rollback()
                    return MigrationResult(
                        success=False,
                        status="already_linked",
                        old_user_id=old_user_id,
                        new_user_id=new_user_id,
                        reason=reason,
                        merged_profile_id=old_profile.id,
                        message="Профиль уже был объединен ранее. Повторная миграция запрещена без force=True.",
                    )

                async with db.execute(
                    """
                    SELECT *
                    FROM migration_locks
                    WHERE source_user_id = ? AND target_user_id = ?
                      AND status IN ('started', 'completed')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (old_user_id, new_user_id),
                ) as cur:
                    existing_lock = await cur.fetchone()

                if existing_lock and existing_lock["status"] == "completed" and not force:
                    await db.rollback()
                    return MigrationResult(
                        success=False,
                        status="already_linked",
                        old_user_id=old_user_id,
                        new_user_id=new_user_id,
                        reason=reason,
                        merged_profile_id=old_profile.id,
                        message="Такая миграция уже выполнялась ранее.",
                    )

                cur = await db.execute(
                    """
                    INSERT INTO migration_locks (source_user_id, target_user_id, status, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (old_user_id, new_user_id, "started", utc_now_iso()),
                )
                lock_id = cur.lastrowid

                await self._log_event(
                    db=db,
                    event_type="migration_started",
                    old_user_id=old_user_id,
                    new_user_id=new_user_id,
                    profile_id=old_profile.id,
                    reason=reason,
                    details={"force": force, "lock_id": lock_id},
                )

                users_merged = await self._merge_users_table(db, old_user_id, new_user_id)
                category_mapping, categories_moved, categories_relinked = await self._merge_categories_table(
                    db, old_user_id, new_user_id
                )
                transactions_moved, duplicates_skipped_tx = await self._merge_transactions_table(
                    db, old_user_id, new_user_id, category_mapping
                )
                debts_moved, duplicates_skipped_debt = await self._merge_debts_table(
                    db, old_user_id, new_user_id
                )

                duplicates_skipped = duplicates_skipped_tx + duplicates_skipped_debt

                if new_profile is None:
                    await db.execute(
                        "UPDATE user_profiles SET current_user_id = ?, updated_at = ? WHERE id = ?",
                        (new_user_id, utc_now_iso(), old_profile.id),
                    )
                else:
                    await self._merge_recovery_profiles(db, old_profile, new_profile)

                await db.execute(
                    "UPDATE user_profiles SET is_merged_into = ?, updated_at = ? WHERE id = ?",
                    (new_user_id, utc_now_iso(), old_profile.id),
                )

                await db.execute(
                    "UPDATE migration_locks SET status = ? WHERE id = ?",
                    ("completed", lock_id),
                )

                result = MigrationResult(
                    success=True,
                    status="safe_to_migrate",
                    old_user_id=old_user_id,
                    new_user_id=new_user_id,
                    reason=reason,
                    merged_profile_id=old_profile.id,
                    users_merged=users_merged,
                    categories_moved=categories_moved,
                    categories_relinked=categories_relinked,
                    transactions_moved=transactions_moved,
                    debts_moved=debts_moved,
                    duplicates_skipped=duplicates_skipped,
                    message="Миграция успешно завершена.",
                    details={
                        "lock_id": lock_id,
                        "category_mapping_size": len(category_mapping),
                    },
                )

                await self._log_event(
                    db=db,
                    event_type="migration_completed",
                    old_user_id=old_user_id,
                    new_user_id=new_user_id,
                    profile_id=old_profile.id,
                    reason=reason,
                    details={
                        "users_merged": users_merged,
                        "categories_moved": categories_moved,
                        "categories_relinked": categories_relinked,
                        "transactions_moved": transactions_moved,
                        "debts_moved": debts_moved,
                        "duplicates_skipped": duplicates_skipped,
                        "lock_id": lock_id,
                    },
                )

                await db.commit()
                return result

            except Exception as exc:
                logger.exception("Ошибка при миграции данных %s -> %s", old_user_id, new_user_id)
                try:
                    if lock_id is not None:
                        await db.execute(
                            "UPDATE migration_locks SET status = ? WHERE id = ?",
                            ("failed", lock_id),
                        )
                    await self._log_event(
                        db=db,
                        event_type="migration_failed",
                        old_user_id=old_user_id,
                        new_user_id=new_user_id,
                        reason=reason,
                        details={"error": str(exc), "lock_id": lock_id},
                    )
                finally:
                    await db.rollback()

                return MigrationResult(
                    success=False,
                    status="migration_failed",
                    old_user_id=old_user_id,
                    new_user_id=new_user_id,
                    reason=reason,
                    message=f"Ошибка миграции: {exc}",
                )

    async def check_migration_safety(
        self,
        old_user_id: int,
        new_user_id: int,
        candidate_profile_id: Optional[int] = None,
        score: int = 0,
        force: bool = False,
    ) -> SafetyCheckResult:
        if old_user_id == new_user_id:
            return SafetyCheckResult(
                allowed=False,
                status="self_migration_forbidden",
                score=score,
                candidate_profile_id=candidate_profile_id,
                message="Нельзя объединять аккаунт сам в себя.",
            )

        old_profile = await self.find_profile_by_user_id(old_user_id)
        if old_profile is None:
            return SafetyCheckResult(
                allowed=False,
                status="not_found",
                score=score,
                candidate_profile_id=candidate_profile_id,
                message="Старый профиль не найден.",
            )

        if old_profile.is_merged_into is not None and not force:
            return SafetyCheckResult(
                allowed=False,
                status="already_linked",
                score=score,
                candidate_profile_id=candidate_profile_id,
                message="Профиль уже объединен ранее.",
            )

        async with await self._connect() as db:
            async with db.execute(
                """
                SELECT *
                FROM migration_locks
                WHERE source_user_id = ? AND target_user_id = ?
                  AND status = 'completed'
                ORDER BY id DESC
                LIMIT 1
                """,
                (old_user_id, new_user_id),
            ) as cur:
                existing_lock = await cur.fetchone()

        if existing_lock and not force:
            return SafetyCheckResult(
                allowed=False,
                status="already_linked",
                score=score,
                candidate_profile_id=candidate_profile_id,
                message="Миграция уже выполнялась ранее.",
            )

        if score >= self.SAFE_THRESHOLD:
            return SafetyCheckResult(
                allowed=True,
                status="safe_to_migrate",
                score=score,
                candidate_profile_id=candidate_profile_id,
                message="Миграция разрешена автоматически.",
            )

        if score >= self.CONFIRM_THRESHOLD:
            return SafetyCheckResult(
                allowed=False,
                status="needs_confirmation",
                score=score,
                candidate_profile_id=candidate_profile_id,
                message="Нужно ручное подтверждение.",
            )

        return SafetyCheckResult(
            allowed=False,
            status="ambiguous",
            score=score,
            candidate_profile_id=candidate_profile_id,
            message="Недостаточно надежности для миграции.",
        )

    def generate_recovery_code(self) -> str:
        return f"RC-{secrets.token_urlsafe(24)}"

    @staticmethod
    def hash_phone(phone_number: str) -> str:
        normalized = "".join(ch for ch in phone_number if ch.isdigit())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def normalize_username(username: Optional[str]) -> Optional[str]:
        if username is None:
            return None
        username = username.strip().lower()
        if username.startswith("@"):
            username = username[1:]
        return username or None

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        return db

    async def _log_event(
        self,
        event_type: str,
        old_user_id: Optional[int] = None,
        new_user_id: Optional[int] = None,
        profile_id: Optional[int] = None,
        username: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
        db: Optional[aiosqlite.Connection] = None,
    ) -> None:
        payload = json.dumps(details or {}, ensure_ascii=False)

        if db is not None:
            await db.execute(
                """
                INSERT INTO recovery_events
                (event_type, old_user_id, new_user_id, profile_id, username, reason, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_type, old_user_id, new_user_id, profile_id, username, reason, payload, utc_now_iso()),
            )
            return

        async with await self._connect() as local_db:
            await local_db.execute(
                """
                INSERT INTO recovery_events
                (event_type, old_user_id, new_user_id, profile_id, username, reason, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_type, old_user_id, new_user_id, profile_id, username, reason, payload, utc_now_iso()),
            )
            await local_db.commit()

    async def _get_profile_by_user_id(
        self,
        user_id: int,
        db: Optional[aiosqlite.Connection] = None,
    ) -> Optional[UserProfile]:
        query = "SELECT * FROM user_profiles WHERE current_user_id = ?"

        if db is not None:
            async with db.execute(query, (user_id,)) as cur:
                row = await cur.fetchone()
                return self._row_to_profile(row)

        async with await self._connect() as local_db:
            async with local_db.execute(query, (user_id,)) as cur:
                row = await cur.fetchone()
                return self._row_to_profile(row)

    async def _get_profile_by_profile_id(
        self,
        profile_id: int,
        db: Optional[aiosqlite.Connection] = None,
    ) -> Optional[UserProfile]:
        query = "SELECT * FROM user_profiles WHERE id = ?"

        if db is not None:
            async with db.execute(query, (profile_id,)) as cur:
                row = await cur.fetchone()
                return self._row_to_profile(row)

        async with await self._connect() as local_db:
            async with local_db.execute(query, (profile_id,)) as cur:
                row = await cur.fetchone()
                return self._row_to_profile(row)

    async def _get_current_username(
        self,
        profile_id: int,
        db: Optional[aiosqlite.Connection] = None,
    ) -> Optional[str]:
        query = """
            SELECT username
            FROM username_history
            WHERE profile_id = ? AND is_current = 1
            ORDER BY id DESC
            LIMIT 1
        """

        if db is not None:
            async with db.execute(query, (profile_id,)) as cur:
                row = await cur.fetchone()
                return row["username"] if row else None

        async with await self._connect() as local_db:
            async with local_db.execute(query, (profile_id,)) as cur:
                row = await cur.fetchone()
                return row["username"] if row else None

    async def _set_current_username(
        self,
        profile_id: int,
        username: str,
        db: aiosqlite.Connection,
    ) -> None:
        username = self.normalize_username(username)
        if not username:
            return

        await db.execute(
            "UPDATE username_history SET is_current = 0 WHERE profile_id = ?",
            (profile_id,),
        )

        async with db.execute(
            """
            SELECT id
            FROM username_history
            WHERE profile_id = ? AND username = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (profile_id, username),
        ) as cur:
            row = await cur.fetchone()

        if row:
            await db.execute(
                """
                UPDATE username_history
                SET is_current = 1, observed_at = ?
                WHERE id = ?
                """,
                (utc_now_iso(), row["id"]),
            )
        else:
            await db.execute(
                """
                INSERT INTO username_history (profile_id, username, is_current, observed_at)
                VALUES (?, ?, 1, ?)
                """,
                (profile_id, username, utc_now_iso()),
            )

    def _row_to_profile(self, row: Optional[aiosqlite.Row]) -> Optional[UserProfile]:
        if row is None:
            return None
        return UserProfile(
            id=row["id"],
            current_user_id=row["current_user_id"],
            recovery_code=row["recovery_code"],
            phone_hash=row["phone_hash"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            is_merged_into=row["is_merged_into"],
            notes=row["notes"],
        )

    def _build_candidate(
        self,
        profile: UserProfile,
        username: Optional[str],
        username_match_type: Optional[str],
        input_first_name: Optional[str],
        input_last_name: Optional[str],
        phone_hash: Optional[str],
        recovery_code: Optional[str],
    ) -> ImportCandidate:
        reasons: list[str] = []
        score = 0

        recovery_code_match = bool(recovery_code and profile.recovery_code == recovery_code)
        phone_hash_match = bool(phone_hash and profile.phone_hash and profile.phone_hash == phone_hash)

        if recovery_code_match:
            score += self.SCORE_RECOVERY_CODE
            reasons.append("Совпал recovery code")

        if phone_hash_match:
            score += self.SCORE_PHONE_HASH
            reasons.append("Совпал phone_hash")

        if username and username_match_type == "current":
            score += self.SCORE_CURRENT_USERNAME
            reasons.append("Совпал текущий username")

        if username and username_match_type == "history":
            score += self.SCORE_USERNAME_HISTORY
            reasons.append("Совпал username из истории")

        if input_first_name and input_last_name:
            if (profile.first_name or "").strip().lower() == input_first_name.strip().lower() and (
                profile.last_name or ""
            ).strip().lower() == input_last_name.strip().lower():
                score += self.SCORE_FIRST_LAST
                reasons.append("Совпали first_name + last_name")

        if input_first_name and username:
            if (profile.first_name or "").strip().lower() == input_first_name.strip().lower():
                score += self.SCORE_FIRST_USERNAME
                reasons.append("Совпали first_name + username")

        status = "not_found"
        if score >= self.SAFE_THRESHOLD and recovery_code_match:
            status = "safe_to_migrate"
        elif score >= self.CONFIRM_THRESHOLD:
            status = "needs_confirmation"
        elif score >= self.AMBIGUOUS_THRESHOLD:
            status = "ambiguous"

        return ImportCandidate(
            profile_id=profile.id,
            current_user_id=profile.current_user_id,
            username=username,
            username_match_type=username_match_type,
            first_name=profile.first_name,
            last_name=profile.last_name,
            phone_hash_match=phone_hash_match,
            recovery_code_match=recovery_code_match,
            score=score,
            status=status,
            reasons=reasons,
        )

    def _merge_candidates(
        self,
        old: Optional[ImportCandidate],
        new: ImportCandidate,
    ) -> ImportCandidate:
        if old is None:
            return new

        merged_reasons = list(dict.fromkeys(old.reasons + new.reasons))
        merged_score = max(old.score, new.score)

        phone_hash_match = old.phone_hash_match or new.phone_hash_match
        recovery_code_match = old.recovery_code_match or new.recovery_code_match

        if recovery_code_match and merged_score < self.SCORE_RECOVERY_CODE:
            merged_score = self.SCORE_RECOVERY_CODE
        elif phone_hash_match and merged_score < self.SCORE_PHONE_HASH:
            merged_score = max(merged_score, self.SCORE_PHONE_HASH)

        status = "not_found"
        if recovery_code_match and merged_score >= self.SAFE_THRESHOLD:
            status = "safe_to_migrate"
        elif merged_score >= self.CONFIRM_THRESHOLD:
            status = "needs_confirmation"
        elif merged_score >= self.AMBIGUOUS_THRESHOLD:
            status = "ambiguous"

        return ImportCandidate(
            profile_id=old.profile_id,
            current_user_id=old.current_user_id,
            username=new.username or old.username,
            username_match_type=new.username_match_type or old.username_match_type,
            first_name=old.first_name,
            last_name=old.last_name,
            phone_hash_match=phone_hash_match,
            recovery_code_match=recovery_code_match,
            score=merged_score,
            status=status,
            reasons=merged_reasons,
        )

    async def _merge_users_table(
        self,
        db: aiosqlite.Connection,
        old_user_id: int,
        new_user_id: int,
    ) -> bool:
        old_user = await self._get_finance_user(db, old_user_id)
        new_user = await self._get_finance_user(db, new_user_id)

        if old_user is None and new_user is None:
            return False

        if old_user is not None and new_user is None:
            await db.execute(
                """
                INSERT INTO users (user_id, timezone, last_bot_message_id, created_at)
                VALUES (?, ?, NULL, ?)
                """,
                (new_user_id, old_user.get("timezone"), old_user.get("created_at")),
            )
            await db.execute("DELETE FROM users WHERE user_id = ?", (old_user_id,))
            return True

        if old_user is not None and new_user is not None:
            old_timezone = old_user.get("timezone")
            new_timezone = new_user.get("timezone")

            if (not new_timezone) and old_timezone:
                await db.execute(
                    "UPDATE users SET timezone = ? WHERE user_id = ?",
                    (old_timezone, new_user_id),
                )

            await db.execute(
                "UPDATE users SET last_bot_message_id = NULL WHERE user_id = ?",
                (new_user_id,),
            )
            await db.execute("DELETE FROM users WHERE user_id = ?", (old_user_id,))
            return True

        return False

    async def _merge_categories_table(
        self,
        db: aiosqlite.Connection,
        old_user_id: int,
        new_user_id: int,
    ) -> tuple[dict[int, int], int, int]:
        async with db.execute(
            "SELECT * FROM categories WHERE user_id = ? ORDER BY id ASC",
            (old_user_id,),
        ) as cur:
            old_categories = await cur.fetchall()

        mapping: dict[int, int] = {}
        moved = 0
        relinked = 0

        for cat in old_categories:
            async with db.execute(
                """
                SELECT * FROM categories
                WHERE user_id = ? AND type = ? AND LOWER(name) = LOWER(?)
                LIMIT 1
                """,
                (new_user_id, cat["type"], cat["name"]),
            ) as cur:
                existing = await cur.fetchone()

            if existing:
                mapping[cat["id"]] = existing["id"]
                relinked += 1
            else:
                await db.execute(
                    "UPDATE categories SET user_id = ? WHERE id = ?",
                    (new_user_id, cat["id"]),
                )
                mapping[cat["id"]] = cat["id"]
                moved += 1

        return mapping, moved, relinked

    async def _merge_transactions_table(
        self,
        db: aiosqlite.Connection,
        old_user_id: int,
        new_user_id: int,
        category_mapping: dict[int, int],
    ) -> tuple[int, int]:
        columns = await self._get_table_columns(db, "transactions")
        has_comment = "comment" in columns

        async with db.execute(
            "SELECT * FROM transactions WHERE user_id = ? ORDER BY id ASC",
            (old_user_id,),
        ) as cur:
            old_transactions = await cur.fetchall()

        moved = 0
        duplicates_skipped = 0

        for tr in old_transactions:
            new_category_id = category_mapping.get(tr["category_id"]) if tr["category_id"] is not None else None

            if has_comment:
                async with db.execute(
                    """
                    SELECT id FROM transactions
                    WHERE user_id = ?
                      AND type = ?
                      AND COALESCE(category_id, -1) = COALESCE(?, -1)
                      AND amount = ?
                      AND COALESCE(comment, '') = COALESCE(?, '')
                      AND created_at = ?
                    LIMIT 1
                    """,
                    (
                        new_user_id,
                        tr["type"],
                        new_category_id,
                        tr["amount"],
                        tr["comment"],
                        tr["created_at"],
                    ),
                ) as cur:
                    exists = await cur.fetchone()
            else:
                async with db.execute(
                    """
                    SELECT id FROM transactions
                    WHERE user_id = ?
                      AND type = ?
                      AND COALESCE(category_id, -1) = COALESCE(?, -1)
                      AND amount = ?
                      AND created_at = ?
                    LIMIT 1
                    """,
                    (
                        new_user_id,
                        tr["type"],
                        new_category_id,
                        tr["amount"],
                        tr["created_at"],
                    ),
                ) as cur:
                    exists = await cur.fetchone()

            if exists:
                await db.execute("DELETE FROM transactions WHERE id = ?", (tr["id"],))
                duplicates_skipped += 1
                continue

            await db.execute(
                "UPDATE transactions SET user_id = ?, category_id = ? WHERE id = ?",
                (new_user_id, new_category_id, tr["id"]),
            )
            moved += 1

        return moved, duplicates_skipped

    async def _merge_debts_table(
        self,
        db: aiosqlite.Connection,
        old_user_id: int,
        new_user_id: int,
    ) -> tuple[int, int]:
        columns = await self._get_table_columns(db, "debts")
        has_comment = "comment" in columns

        async with db.execute(
            "SELECT * FROM debts WHERE user_id = ? ORDER BY id ASC",
            (old_user_id,),
        ) as cur:
            old_debts = await cur.fetchall()

        moved = 0
        duplicates_skipped = 0

        for debt in old_debts:
            if has_comment:
                async with db.execute(
                    """
                    SELECT id FROM debts
                    WHERE user_id = ?
                      AND operation_type = ?
                      AND person_name = ?
                      AND amount = ?
                      AND COALESCE(comment, '') = COALESCE(?, '')
                      AND created_at = ?
                    LIMIT 1
                    """,
                    (
                        new_user_id,
                        debt["operation_type"],
                        debt["person_name"],
                        debt["amount"],
                        debt["comment"],
                        debt["created_at"],
                    ),
                ) as cur:
                    exists = await cur.fetchone()
            else:
                async with db.execute(
                    """
                    SELECT id FROM debts
                    WHERE user_id = ?
                      AND operation_type = ?
                      AND person_name = ?
                      AND amount = ?
                      AND created_at = ?
                    LIMIT 1
                    """,
                    (
                        new_user_id,
                        debt["operation_type"],
                        debt["person_name"],
                        debt["amount"],
                        debt["created_at"],
                    ),
                ) as cur:
                    exists = await cur.fetchone()

            if exists:
                await db.execute("DELETE FROM debts WHERE id = ?", (debt["id"],))
                duplicates_skipped += 1
                continue

            await db.execute(
                "UPDATE debts SET user_id = ? WHERE id = ?",
                (new_user_id, debt["id"]),
            )
            moved += 1

        return moved, duplicates_skipped

    async def _merge_recovery_profiles(
        self,
        db: aiosqlite.Connection,
        old_profile: UserProfile,
        new_profile: UserProfile,
    ) -> None:
        payload: dict[str, Any] = {}

        if not new_profile.phone_hash and old_profile.phone_hash:
            payload["phone_hash"] = old_profile.phone_hash
        if not new_profile.first_name and old_profile.first_name:
            payload["first_name"] = old_profile.first_name
        if not new_profile.last_name and old_profile.last_name:
            payload["last_name"] = old_profile.last_name
        if not new_profile.recovery_code and old_profile.recovery_code:
            payload["recovery_code"] = old_profile.recovery_code

        if payload:
            payload["updated_at"] = utc_now_iso()
            set_parts = ", ".join(f"{key} = ?" for key in payload.keys())
            values = list(payload.values()) + [new_profile.id]
            await db.execute(
                f"UPDATE user_profiles SET {set_parts} WHERE id = ?",
                values,
            )

        async with db.execute(
            "SELECT username, observed_at FROM username_history WHERE profile_id = ?",
            (old_profile.id,),
        ) as cur:
            old_usernames = await cur.fetchall()

        async with db.execute(
            "SELECT username FROM username_history WHERE profile_id = ?",
            (new_profile.id,),
        ) as cur:
            new_usernames = await cur.fetchall()

        existing_usernames = {row["username"] for row in new_usernames}

        for row in old_usernames:
            if row["username"] not in existing_usernames:
                await db.execute(
                    """
                    INSERT INTO username_history (profile_id, username, is_current, observed_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    (new_profile.id, row["username"], row["observed_at"]),
                )

    async def _get_finance_user(
        self,
        db: aiosqlite.Connection,
        user_id: int,
    ) -> Optional[dict[str, Any]]:
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _get_table_columns(
        self,
        db: aiosqlite.Connection,
        table_name: str,
    ) -> set[str]:
        async with db.execute(f"PRAGMA table_info({table_name})") as cur:
            rows = await cur.fetchall()
            return {row["name"] for row in rows}
