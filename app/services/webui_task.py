import threading
from collections import deque

from loguru import logger

from app.config import config
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.models import const
from app.models.schema import VideoParams
from app.services import state as sm
from app.services import task as tm
from app.services.loomloom import LoomLoomConfirmedVideoRequest
from app.utils.logging_utils import format_log_record, log_scope_thread_id


# The configuration of WebUI is stored in a process-level global dictionary. The original synchronization implementation will hold during the full build
# runtime_config_lock, so different browser sessions are actually executed serially. Here the number of concurrency is fixed
# It is 1, which not only maintains the original configuration consistency, but also prevents multiple threads from waiting meaninglessly outside the configuration lock.
_task_manager = InMemoryTaskManager(
    max_concurrent_tasks=1,
    max_queued_tasks=max(1, int(config.app.get("max_queued_tasks", 100))),
)
_task_logs: dict[str, deque[str]] = {}
_task_logs_lock = threading.RLock()
_MAX_LOG_TASKS = 20
_MAX_LOG_RECORDS_PER_TASK = 1000
# Streamlit cannot directly push component updates by background threads, but can only be polled through Fragment. 0.5 seconds
# It is enough to make the WebUI log close to the real-time output of the terminal, but will not continue to occupy browser resources like high-frequency refresh.
TASK_LOG_REFRESH_INTERVAL_SECONDS = 0.5


def _append_task_log(task_id: str, message: str) -> None:
    """Save a limited number of logs per task for safe polling by Streamlit Fragments."""
    with _task_logs_lock:
        records = _task_logs.get(task_id)
        if records is None:
            # Only keep the logs of the most recent tasks to prevent the WebUI service from continuing to occupy memory after running for a long time.
            # dict maintains the insertion order; the task log is only used for interface diagnosis, and eliminating the earliest record does not affect the task.
            if len(_task_logs) >= _MAX_LOG_TASKS:
                oldest_task_id = next(iter(_task_logs))
                _task_logs.pop(oldest_task_id, None)
            records = deque(maxlen=_MAX_LOG_RECORDS_PER_TASK)
            _task_logs[task_id] = records
        records.append(message.rstrip())


def get_task_logs(task_id: str) -> list[str]:
    """Return a log snapshot to avoid holding locks used by background threads during page rendering."""
    with _task_logs_lock:
        return list(_task_logs.get(task_id, ()))


def _run_generation(
    task_id: str,
    params: VideoParams,
    capture_logs: bool,
    voice_preview: dict | None = None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None = None,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
) -> dict:
    """
    Execute the existing video pipeline in a background thread.

    Loguru's sink is a process-level resource, so it must be filtered by the current worker thread. Otherwise run simultaneously
    API tasks or other page logs are mixed with the current task. Tasks are download, fragment encoding and heartbeat initiated
    Auxiliary threads are assigned to worker threads through bind_log_scope, and their logs must also be collected, otherwise
    These time-consuming phases have no output in the WebUI. The page only reads ordinary list snapshots and does not read from the background
    The thread accesses Streamlit session_state to avoid the root cause of delta path confusion during refresh.
    """
    log_handler_id = None
    worker_thread_id = threading.get_ident()
    try:
        if capture_logs:
            log_handler_id = logger.add(
                lambda message: _append_task_log(task_id, str(message)),
                level="DEBUG",
                format=format_log_record,
                colorize=False,
                filter=lambda record: (
                    log_scope_thread_id(record["thread"].id) == worker_thread_id
                ),
            )

        # The full task still uses the original configuration lock, preventing another WebUI session from modifying it mid-build
        # Process-level configurations such as providers and keys cause different settings to be used before and after the same video.
        with config.runtime_config_lock():
            return tm.start(
                task_id=task_id,
                params=params,
                voice_preview=voice_preview,
                loomloom_video_request=loomloom_video_request,
                voxcpm_reference_audio=voxcpm_reference_audio,
                voxcpm_prompt_audio=voxcpm_prompt_audio,
                voxcpm_prompt_text=voxcpm_prompt_text,
            )
    except Exception as exc:
        # tm.start is already responsible for converting pipeline exceptions into failure status; here additional protection log sink,
        # WebUI wrapper layers such as configuration locks. Any background thread exception must leave the final state and cannot let the task
        # The manager keeps showing "Building" permanently after the worker thread exits.
        error = f"{type(exc).__name__}: {exc}"
        failure = {
            "task_id": task_id,
            "state": const.TASK_STATE_FAILED,
            "progress": 0,
            "failed_stage": "webui_worker",
            "error": error,
        }
        sm.state.update_task(
            task_id,
            state=failure["state"],
            progress=failure["progress"],
            failed_stage=failure["failed_stage"],
            error=failure["error"],
        )
        logger.exception(
            f"unexpected WebUI generation worker failure, "
            f"task_id={task_id}, error={exc}"
        )
        return failure
    finally:
        if log_handler_id is not None:
            try:
                logger.remove(log_handler_id)
            except ValueError:
                logger.debug(
                    f"WebUI task log handler already removed: task_id={task_id}"
                )


def submit_generation(
    task_id: str,
    params: VideoParams,
    capture_logs: bool = True,
    voice_preview: dict | None = None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None = None,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
) -> None:
    """
    Register and submit the WebUI video generation task, and return immediately after the call.

    Task status must be written before the thread is started. In this way, the task can be queried when the current script execution of the page ends.
    Browser refreshes or WebSocket reconnects also do not rely on placeholders for old pages in memory.
    """
    task_params = params.model_copy(deep=True)
    # The preview payload only contains immutable audio paths, parameter snapshots, and a read-only subtitles timeline. Copy the outer dictionary,
    # This prevents subsequent reruns of the page from affecting tasks already submitted to the background queue when replacing cached fields.
    voice_preview_snapshot = dict(voice_preview) if voice_preview else None
    # Reference audio belongs only to this queued request. It is deliberately
    # separate from VideoParams so it cannot reach task history, presets, state
    # persistence, or logs. ``bytes`` is immutable; make an explicit snapshot
    # before the background worker starts so later WebUI reruns cannot share
    # mutable upload state with this task.
    voxcpm_reference_audio_snapshot = (
        bytes(voxcpm_reference_audio) if voxcpm_reference_audio else None
    )
    voxcpm_prompt_audio_snapshot = (
        bytes(voxcpm_prompt_audio) if voxcpm_prompt_audio else None
    )
    voxcpm_prompt_text_snapshot = str(voxcpm_prompt_text or "")
    # Confirmed requests are frozen data objects and are only delivered within the current process. API Key will not enter
    # VideoParams, task status, logs or disk placement history will not be affected by subsequent page reruns.
    loomloom_request_snapshot = loomloom_video_request
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=0,
        video_subject=task_params.video_subject or task_params.video_script or task_id,
    )
    try:
        _task_manager.add_task(
            _run_generation,
            task_id=task_id,
            params=task_params,
            capture_logs=capture_logs,
            voice_preview=voice_preview_snapshot,
            loomloom_video_request=loomloom_request_snapshot,
            voxcpm_reference_audio=voxcpm_reference_audio_snapshot,
            voxcpm_prompt_audio=voxcpm_prompt_audio_snapshot,
            voxcpm_prompt_text=voxcpm_prompt_text_snapshot,
        )
    except Exception as exc:
        # Scheduling failures, like pipeline failures, must become queryable to avoid permanent display in the task manager.
        # "Generating". Preserving exception types makes it easy to quickly locate queue issues from Docker or native logs.
        error = f"{type(exc).__name__}: {exc}"
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=0,
            failed_stage="scheduling",
            error=error,
        )
        logger.exception(
            f"failed to submit WebUI generation task, task_id={task_id}, error={exc}"
        )
        raise
