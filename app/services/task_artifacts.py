"""Safe reading and writing of persistent files in the task directory."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from app.utils import utils


def _script_file(task_id: str) -> Path:
    """Return the task script manifest path and reuse the unified task directory creation logic."""
    return Path(utils.task_dir(task_id)) / "script.json"


def _write_json_atomic(target: Path, payload: Mapping[str, Any]) -> None:
    """
    Atomicly write JSON in the target directory to avoid process interruption and leaving half a file.

    The temporary file and the target file must be in the same directory to ensure that ``os.replace``
    Atomic replacement semantics are maintained in local file systems and Docker mount directories. It will not be modified until the write is successful.
    Existing files; in case of exception, only the temporary files created this time will be cleaned up, and the error will be handed over to the caller to decide whether
    Affect the main process.
    """
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(
                payload,
                temp_file,
                ensure_ascii=False,
                indent=4,
                default=lambda value: value.__dict__,
            )
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_script_data(task_id: str, payload: Mapping[str, Any]) -> None:
    """Create or completely replace a task's ``script.json`` manifest."""
    _write_json_atomic(_script_file(task_id), payload)


def patch_script_data(task_id: str, **updates: Any) -> bool:
    """
    Supplement the task list while retaining the original fields, and return False on failure.

    The source of the material is auxiliary diagnostic information and cannot be caused by file permissions, temporary disk abnormalities or historical file damage.
    Block video generation. Therefore, this entry will record the complete exception and be downgraded; the first time the task list is created, it will still be used.
    ``write_script_data``, the main process decides how to handle when the basic task data fails to be written.
    """
    try:
        target = _script_file(task_id)
        with target.open("r", encoding="utf-8") as script_file:
            payload = json.load(script_file)
        if not isinstance(payload, dict):
            raise ValueError("task script data must be a JSON object")

        payload.update(updates)
        _write_json_atomic(target, payload)
        return True
    except FileNotFoundError:
        # ``download_videos`` may also be called independently by tests, scripts or third-party code. At this time, there is no
        # To-do lists are a normal scenario and should not create warnings or create incomplete files for auxiliary records.
        logger.debug(
            f"skip task script update because script.json does not exist: "
            f"task_id={task_id}"
        )
        return False
    except Exception as exc:
        logger.warning(
            "failed to update task script data: "
            f"task_id={task_id}, fields={sorted(updates)}, "
            f"error={type(exc).__name__}, detail={exc}"
        )
        return False
