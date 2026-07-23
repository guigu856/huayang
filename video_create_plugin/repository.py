from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import PluginError
from .models import ArtifactEnvelope, FreezeRecord, StageRun, TaskRun


class WorkflowRepository:
    """持久化工作流状态并提供事务化状态转换。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_task(self, task: TaskRun, stage: StageRun) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id, payload) VALUES (?, ?)",
                (task.task_id, task.model_dump_json()),
            )
            connection.execute(
                "INSERT INTO stages(stage_run_id, task_id, stage_type, payload) "
                "VALUES (?, ?, ?, ?)",
                (stage.stage_run_id, stage.task_id, stage.stage_type, stage.model_dump_json()),
            )

    def get_task(self, task_id: str) -> TaskRun:
        row = self._fetch_one("SELECT payload FROM tasks WHERE task_id = ?", (task_id,))
        if row is None:
            raise PluginError("task_not_found", "任务不存在")
        return TaskRun.model_validate_json(row[0])

    def list_tasks(self, *, limit: int = 200, offset: int = 0) -> list[TaskRun]:
        rows = self._fetch_all(
            "SELECT payload FROM tasks ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (_validated_limit(limit), _validated_offset(offset)),
        )
        return [TaskRun.model_validate_json(row[0]) for row in rows]

    def count_tasks(self) -> int:
        row = self._fetch_one("SELECT COUNT(*) FROM tasks", ())
        return 0 if row is None else int(row[0])

    def get_stage(self, stage_run_id: str) -> StageRun:
        row = self._fetch_one("SELECT payload FROM stages WHERE stage_run_id = ?", (stage_run_id,))
        if row is None:
            raise PluginError("stage_not_found", "阶段不存在")
        return StageRun.model_validate_json(row[0])

    def get_current_stage(self, task: TaskRun) -> StageRun:
        row = self._fetch_one(
            "SELECT payload FROM stages WHERE task_id = ? AND stage_type = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (task.task_id, task.current_stage),
        )
        if row is None:
            raise PluginError("stage_not_found", "任务当前阶段不存在")
        return StageRun.model_validate_json(row[0])

    def list_stages(self, task_id: str) -> list[StageRun]:
        rows = self._fetch_all(
            "SELECT payload FROM stages WHERE task_id = ? ORDER BY rowid", (task_id,)
        )
        return [StageRun.model_validate_json(row[0]) for row in rows]

    def get_artifact(self, artifact_id: str) -> ArtifactEnvelope:
        row = self._fetch_one("SELECT payload FROM artifacts WHERE artifact_id = ?", (artifact_id,))
        if row is None:
            raise PluginError("artifact_not_found", "Artifact 不存在")
        return ArtifactEnvelope.model_validate_json(row[0])

    def list_artifacts(
        self,
        *,
        task_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[ArtifactEnvelope]:
        validated_limit = _validated_limit(limit)
        validated_offset = _validated_offset(offset)
        if task_id is None:
            rows = self._fetch_all(
                "SELECT payload FROM artifacts ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (validated_limit, validated_offset),
            )
        else:
            rows = self._fetch_all(
                "SELECT payload FROM artifacts WHERE task_id = ? "
                "ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (task_id, validated_limit, validated_offset),
            )
        return [ArtifactEnvelope.model_validate_json(row[0]) for row in rows]

    def count_artifacts(self) -> int:
        row = self._fetch_one("SELECT COUNT(*) FROM artifacts", ())
        return 0 if row is None else int(row[0])

    def get_freeze(self, freeze_id: str) -> FreezeRecord:
        row = self._fetch_one("SELECT payload FROM freezes WHERE freeze_id = ?", (freeze_id,))
        if row is None:
            raise PluginError("freeze_not_found", "FreezeRecord 不存在")
        return FreezeRecord.model_validate_json(row[0])

    def get_freeze_for_artifact(self, artifact_id: str) -> FreezeRecord:
        row = self._fetch_one(
            "SELECT payload FROM freezes WHERE artifact_id = ? ORDER BY rowid DESC LIMIT 1",
            (artifact_id,),
        )
        if row is None:
            raise PluginError("artifact_not_approved", "Artifact 尚未冻结")
        return FreezeRecord.model_validate_json(row[0])

    def save_access_grant(
        self,
        *,
        handle_hash: str,
        task_id: str,
        stage_run_id: str,
        task_revision: int,
        stage_revision: int,
        allowed_tools: list[str],
        expires_at: str,
    ) -> None:
        payload = json.dumps(allowed_tools, ensure_ascii=False, separators=(",", ":"))
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO access_grants("
                "handle_hash, task_id, stage_run_id, task_revision, stage_revision, "
                "allowed_tools, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    handle_hash,
                    task_id,
                    stage_run_id,
                    task_revision,
                    stage_revision,
                    payload,
                    expires_at,
                ),
            )

    def get_access_grant(self, handle_hash: str) -> dict[str, Any]:
        row = self._fetch_one(
            "SELECT task_id, stage_run_id, task_revision, stage_revision, "
            "allowed_tools, expires_at FROM access_grants WHERE handle_hash = ?",
            (handle_hash,),
        )
        if row is None:
            raise PluginError("stage_access_invalid", "阶段访问句柄无效")
        return {
            "task_id": row[0],
            "stage_run_id": row[1],
            "task_revision": row[2],
            "stage_revision": row[3],
            "allowed_tools": json.loads(row[4]),
            "expires_at": row[5],
        }

    def submit_artifact(
        self,
        *,
        artifact: ArtifactEnvelope,
        stage: StageRun,
        task: TaskRun,
    ) -> None:
        with self._transaction() as connection:
            self._assert_revisions(connection, task, stage)
            connection.execute(
                "INSERT INTO artifacts(artifact_id, task_id, stage_run_id, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    artifact.artifact_id,
                    artifact.task_id,
                    artifact.stage_run_id,
                    artifact.model_dump_json(),
                ),
            )
            self._update_stage(connection, stage)
            self._update_task(connection, task)
            self._delete_stage_grants(connection, stage.stage_run_id)

    def approve_stage(
        self,
        *,
        artifact: ArtifactEnvelope,
        freeze: FreezeRecord,
        stage: StageRun,
        task: TaskRun,
        next_stage: StageRun | None,
    ) -> None:
        with self._transaction() as connection:
            self._assert_revisions(connection, task, stage)
            connection.execute(
                "UPDATE artifacts SET payload = ? WHERE artifact_id = ?",
                (artifact.model_dump_json(), artifact.artifact_id),
            )
            if connection.total_changes == 0:
                raise PluginError("artifact_not_found", "Artifact 不存在")
            connection.execute(
                "INSERT INTO freezes(freeze_id, artifact_id, task_id, stage_run_id, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    freeze.freeze_id,
                    freeze.artifact_id,
                    freeze.task_id,
                    freeze.stage_run_id,
                    freeze.model_dump_json(),
                ),
            )
            self._update_stage(connection, stage)
            self._update_task(connection, task)
            if next_stage is not None:
                connection.execute(
                    "INSERT INTO stages(stage_run_id, task_id, stage_type, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        next_stage.stage_run_id,
                        next_stage.task_id,
                        next_stage.stage_type,
                        next_stage.model_dump_json(),
                    ),
                )
            self._delete_stage_grants(connection, stage.stage_run_id)

    def reopen_stage(
        self,
        *,
        task: TaskRun,
        stale_stages: list[StageRun],
        new_stage: StageRun,
    ) -> None:
        with self._transaction() as connection:
            stored_task = self._task_from_connection(connection, task.task_id)
            if stored_task.revision + 1 != task.revision:
                raise PluginError("task_revision_conflict", "任务版本冲突")
            for stage in stale_stages:
                self._update_stage(connection, stage)
                self._delete_stage_grants(connection, stage.stage_run_id)
            connection.execute(
                "INSERT INTO stages(stage_run_id, task_id, stage_type, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    new_stage.stage_run_id,
                    new_stage.task_id,
                    new_stage.stage_type,
                    new_stage.model_dump_json(),
                ),
            )
            self._update_task(connection, task)

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode = WAL;
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE IF NOT EXISTS tasks(
                        task_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stages(
                        stage_run_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        stage_type TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                    );
                    CREATE TABLE IF NOT EXISTS artifacts(
                        artifact_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        stage_run_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(task_id),
                        FOREIGN KEY(stage_run_id) REFERENCES stages(stage_run_id)
                    );
                    CREATE TABLE IF NOT EXISTS freezes(
                        freeze_id TEXT PRIMARY KEY,
                        artifact_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        stage_run_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id)
                    );
                    CREATE TABLE IF NOT EXISTS access_grants(
                        handle_hash TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        stage_run_id TEXT NOT NULL,
                        task_revision INTEGER NOT NULL,
                        stage_revision INTEGER NOT NULL,
                        allowed_tools TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    """
                )
        except sqlite3.Error as error:
            raise PluginError("workflow_store_unavailable", "工作流数据库初始化失败") from error

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
        except PluginError:
            raise
        except sqlite3.IntegrityError as error:
            raise PluginError("workflow_conflict", "工作流状态写入冲突") from error
        except sqlite3.Error as error:
            raise PluginError("workflow_store_unavailable", "工作流数据库写入失败") from error

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def _fetch_one(self, statement: str, parameters: tuple[object, ...]) -> tuple[Any, ...] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(statement, parameters).fetchone()
                return None if row is None else tuple(row)
        except sqlite3.Error as error:
            raise PluginError("workflow_store_unavailable", "工作流数据库读取失败") from error

    def _fetch_all(self, statement: str, parameters: tuple[object, ...]) -> list[tuple[Any, ...]]:
        try:
            with self._connect() as connection:
                return [tuple(row) for row in connection.execute(statement, parameters)]
        except sqlite3.Error as error:
            raise PluginError("workflow_store_unavailable", "工作流数据库读取失败") from error

    @staticmethod
    def _update_task(connection: sqlite3.Connection, task: TaskRun) -> None:
        connection.execute(
            "UPDATE tasks SET payload = ? WHERE task_id = ?",
            (task.model_dump_json(), task.task_id),
        )

    @staticmethod
    def _update_stage(connection: sqlite3.Connection, stage: StageRun) -> None:
        connection.execute(
            "UPDATE stages SET payload = ? WHERE stage_run_id = ?",
            (stage.model_dump_json(), stage.stage_run_id),
        )

    @staticmethod
    def _delete_stage_grants(connection: sqlite3.Connection, stage_run_id: str) -> None:
        connection.execute("DELETE FROM access_grants WHERE stage_run_id = ?", (stage_run_id,))

    def _assert_revisions(
        self,
        connection: sqlite3.Connection,
        task: TaskRun,
        stage: StageRun,
    ) -> None:
        stored_task = self._task_from_connection(connection, task.task_id)
        stored_stage = self._stage_from_connection(connection, stage.stage_run_id)
        if stored_task.revision + 1 != task.revision:
            raise PluginError("task_revision_conflict", "任务版本冲突")
        if stored_stage.revision + 1 != stage.revision:
            raise PluginError("stage_revision_conflict", "阶段版本冲突")

    @staticmethod
    def _task_from_connection(connection: sqlite3.Connection, task_id: str) -> TaskRun:
        row = connection.execute(
            "SELECT payload FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise PluginError("task_not_found", "任务不存在")
        return TaskRun.model_validate_json(row[0])

    @staticmethod
    def _stage_from_connection(connection: sqlite3.Connection, stage_run_id: str) -> StageRun:
        row = connection.execute(
            "SELECT payload FROM stages WHERE stage_run_id = ?", (stage_run_id,)
        ).fetchone()
        if row is None:
            raise PluginError("stage_not_found", "阶段不存在")
        return StageRun.model_validate_json(row[0])


def _validated_limit(limit: int) -> int:
    if limit < 1 or limit > 101_000:
        raise PluginError("invalid_request", "分页数量必须在 1 到 101000 之间")
    return limit


def _validated_offset(offset: int) -> int:
    if offset < 0 or offset > 1_000_000:
        raise PluginError("invalid_request", "分页偏移必须在 0 到 1000000 之间")
    return offset
