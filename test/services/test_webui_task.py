import ast
import os
import re
import threading
import time
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from app.models import const
from app.models.schema import VideoParams
from app.services import webui_task
from app.utils import logging_utils


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _attribute_name(node):
    """Restore AST calls of the form ``module.function`` to stable strings."""
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    return ".".join(reversed(names))


def _log_record(file_path, message="generation finished"):
    """Constructs the minimum loguru record required by ``format_log_record``."""
    return {
        "file": SimpleNamespace(name=os.path.basename(file_path), path=file_path),
        "message": message,
    }


def test_generation_controls_submit_background_task_instead_of_blocking_page():
    """
    The WebUI build button cannot re-invoke the synchronization pipeline directly.

    This is the core regression protection for Issue #1120 white screen: whenever the full page script blocks again on
    ``tm.start``, users may still receive a delta pointing to the old render tree when refreshing during a build.
    """
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_render_generation_controls"
    )
    calls = {
        _attribute_name(node.func)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    }

    assert "webui_task.submit_generation" in calls
    assert "tm.start" not in calls


def test_webui_runtime_config_updates_do_not_use_blocking_writes():
    """
    Normal controls during build rerun cannot re-wait on configuration locks held by long-running tasks.

    All WebUI configuration writes must go through the non-blocking helper; LLM connection testing and voice auditioning can
    Use try lock to return quickly, but the page code cannot directly call the blocking lock or blocking save function.
    """
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    calls = {
        _attribute_name(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "config.runtime_config_lock" not in calls
    assert "config.save_config" not in calls
    assert not calls.intersection(
        {
            "config.app.clear",
            "config.app.pop",
            "config.app.setdefault",
            "config.app.update",
            "config.azure.clear",
            "config.azure.pop",
            "config.azure.setdefault",
            "config.azure.update",
            "config.chatterbox.clear",
            "config.chatterbox.pop",
            "config.chatterbox.setdefault",
            "config.chatterbox.update",
            "config.elevenlabs.clear",
            "config.elevenlabs.pop",
            "config.elevenlabs.setdefault",
            "config.elevenlabs.update",
            "config.siliconflow.clear",
            "config.siliconflow.pop",
            "config.siliconflow.setdefault",
            "config.siliconflow.update",
            "config.ui.clear",
            "config.ui.pop",
            "config.ui.setdefault",
            "config.ui.update",
        }
    )

    synchronized_sections = {
        "app",
        "azure",
        "chatterbox",
        "elevenlabs",
        "siliconflow",
        "ui",
    }
    direct_writes = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]

        for target in targets:
            if not isinstance(target, ast.Subscript):
                continue
            section = target.value
            if (
                isinstance(section, ast.Attribute)
                and isinstance(section.value, ast.Name)
                and section.value.id == "config"
                and section.attr in synchronized_sections
            ):
                direct_writes.append(node.lineno)

    assert direct_writes == []


def test_active_task_uses_terminal_state_when_outside_runtime_page(tmp_path):
    """An active session marker must not hide a finished task past page one."""
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_collect_task_summaries"
    )
    namespace = {
        "_scan_history_tasks": lambda limit: [],
        "_active_generation_tasks": lambda: {
            "new-task": {"subject": "Latest video", "mtime": 1000}
        },
        "_task_state_filter_key": lambda task: (
            "failed" if task["state"] == const.TASK_STATE_FAILED else "processing"
        ),
        "sm": SimpleNamespace(
            state=SimpleNamespace(
                get_all_tasks=lambda page, page_size: (
                    [
                        {
                            "task_id": f"old-{index}",
                            "state": const.TASK_STATE_COMPLETE,
                        }
                        for index in range(50)
                    ],
                    51,
                ),
                get_task=MagicMock(
                    return_value={
                        "task_id": "new-task",
                        "state": const.TASK_STATE_FAILED,
                        "progress": 70,
                    }
                ),
            )
        ),
        "utils": SimpleNamespace(task_dir=lambda: str(tmp_path)),
        "os": os,
        "const": const,
        "logger": MagicMock(),
    }
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)

    tasks = namespace["_collect_task_summaries"](limit=20)

    latest = next(task for task in tasks if task["task_id"] == "new-task")
    assert latest["state"] == const.TASK_STATE_FAILED
    assert latest["progress"] == 70
    assert latest["mtime"] == 1000
    namespace["sm"].state.get_task.assert_called_once_with("new-task")


def test_task_summary_tolerates_directory_removed_during_scan():
    """A concurrent deletion between isdir and stat must not crash the panel."""
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_collect_task_summaries"
    )
    getmtime = MagicMock(side_effect=FileNotFoundError("task directory removed"))
    namespace = {
        "_scan_history_tasks": lambda limit: [],
        "_active_generation_tasks": lambda: {},
        "sm": SimpleNamespace(
            state=SimpleNamespace(
                get_all_tasks=lambda page, page_size: (
                    [{"task_id": "removed-task", "state": const.TASK_STATE_COMPLETE}],
                    1,
                )
            )
        ),
        "utils": SimpleNamespace(task_dir=lambda: "/tasks"),
        "os": SimpleNamespace(
            path=SimpleNamespace(
                join=os.path.join,
                isdir=lambda _path: True,
                getmtime=getmtime,
            )
        ),
        "logger": MagicMock(),
    }
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)

    tasks = namespace["_collect_task_summaries"]()

    assert tasks[0]["task_id"] == "removed-task"
    assert tasks[0]["mtime"] == 0
    getmtime.assert_called_once_with(os.path.join("/tasks", "removed-task"))


@pytest.mark.parametrize(
    ("ui_config", "expected_open_count"),
    [
        ({}, 1),
        ({"open_task_folder_on_completion": True}, 1),
        ({"open_task_folder_on_completion": False}, 0),
    ],
)
def test_completed_task_renders_subject_named_video_download(
    tmp_path, ui_config, expected_open_count
):
    """After the task is completed, the movie should be downloaded, and whether to automatically open the directory is determined according to the WebUI configuration."""
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    selected_nodes = []
    target_names = {
        "_DOWNLOAD_FILENAME_INVALID_PATTERN",
        "_WINDOWS_RESERVED_FILENAMES",
        "_build_video_download_name",
        "_normalize_task_state",
        "_render_generation_task_snapshot",
    }
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in target_names
            for target in node.targets
        ):
            selected_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in target_names:
            selected_nodes.append(node)

    class FakeColumn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeStreamlit:
        def __init__(self):
            self.session_state = {}
            self.downloads = []
            self.videos = []
            self.warnings = []

        def columns(self, count):
            return [FakeColumn() for _ in range(count)]

        def video(self, video_path):
            self.videos.append(video_path)

        def download_button(self, label, data, **kwargs):
            self.downloads.append((label, data.read(), kwargs))

        def success(self, _message):
            pass

        def warning(self, message):
            self.warnings.append(message)

        def error(self, _message):
            pass

    video_path = tmp_path / "final-1.mp4"
    video_path.write_bytes(b"video-content")
    fake_st = FakeStreamlit()
    open_task_folder = MagicMock()
    namespace = {
        "Mapping": Mapping,
        "config": SimpleNamespace(ui=ui_config),
        "const": const,
        "logger": MagicMock(),
        "mimetypes": __import__("mimetypes"),
        "open_task_folder": open_task_folder,
        "os": os,
        "re": re,
        "st": fake_st,
        "tr": lambda key: (
            "Video {index} reused {count} source clips."
            if key == "Batch Material Reuse Warning" else key
        ),
        "_render_generation_logs": lambda _task_id: None,
    }
    module = ast.fix_missing_locations(ast.Module(body=selected_nodes, type_ignores=[]))
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)

    namespace["_render_generation_task_snapshot"](
        "download-test",
        {
            "state": const.TASK_STATE_COMPLETE,
            "progress": 100,
            "videos": [str(video_path)],
            "warnings": [
                {"code": "batch_materials_reused", "video_index": 2, "count": 3}
            ],
            "video_subject": "A day: in / Shanghai?",
        },
    )

    assert fake_st.videos == [str(video_path)]
    assert fake_st.warnings == ["Video 2 reused 3 source clips."]
    assert fake_st.downloads == [
        (
            "Download Video",
            b"video-content",
            {
                "file_name": "A day in Shanghai.mp4",
                "mime": "video/mp4",
                "key": "download_generated_video_download-test_0",
                "icon": ":material/download:",
                "on_click": "ignore",
                "use_container_width": True,
            },
        )
    ]
    assert open_task_folder.call_count == expected_open_count
    if expected_open_count:
        open_task_folder.assert_called_once_with("download-test")


def test_submit_generation_returns_while_pipeline_is_still_running():
    """Before the background pipeline ends, the submission function must have returned to allow Streamlit to complete this rendering."""
    task_id = "background-submit-test"
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_start(**_kwargs):
        started.set()
        release.wait(timeout=5)
        finished.set()
        return {"videos": ["/tmp/final-1.mp4"]}

    params = VideoParams(video_subject="异步生成测试")
    try:
        with (
            patch.object(webui_task.tm, "start", side_effect=blocking_start),
            patch.object(
                webui_task.config,
                "runtime_config_lock",
                return_value=nullcontext(),
            ),
        ):
            started_at = time.monotonic()
            webui_task.submit_generation(task_id, params, capture_logs=False)
            elapsed = time.monotonic() - started_at

            assert started.wait(timeout=2)
            assert elapsed < 0.5
            assert not finished.is_set()
            task = webui_task.sm.state.get_task(task_id)
            assert task["state"] == const.TASK_STATE_PROCESSING
    finally:
        release.set()
        assert finished.wait(timeout=2)
        webui_task.sm.state.delete_task(task_id)


def test_submit_generation_copies_params_before_starting_worker():
    """When the page is subsequently rerun or parameters are modified inside the pipeline, the current form object cannot be polluted in reverse."""
    params = VideoParams(video_subject="参数隔离测试")
    with patch.object(webui_task._task_manager, "add_task") as add_task:
        webui_task.submit_generation("copied-params-test", params, capture_logs=False)

    submitted_params = add_task.call_args.kwargs["params"]
    assert submitted_params == params
    assert submitted_params is not params
    webui_task.sm.state.delete_task("copied-params-test")


def test_submit_generation_keeps_voxcpm_reference_audio_out_of_params():
    """The reference audio only belongs to the current request in memory and cannot enter persistent task parameters."""
    params = VideoParams(video_subject="task isolation")
    reference_audio = b"bounded-reference-wav"
    prompt_audio = b"bounded-prompt-wav"
    prompt_text = "delivery transcript"
    with patch.object(webui_task._task_manager, "add_task") as add_task:
        webui_task.submit_generation(
            "reference-audio-isolation",
            params,
            capture_logs=False,
            voxcpm_reference_audio=reference_audio,
            voxcpm_prompt_audio=prompt_audio,
            voxcpm_prompt_text=prompt_text,
        )

    submitted_params = add_task.call_args.kwargs["params"]
    serialized_params = submitted_params.model_dump_json()
    assert "voxcpm_reference_audio" not in serialized_params
    assert "voxcpm_prompt_audio" not in serialized_params
    assert "voxcpm_prompt_text" not in serialized_params
    assert reference_audio.decode("ascii") not in serialized_params
    assert prompt_audio.decode("ascii") not in serialized_params
    assert prompt_text not in serialized_params
    assert add_task.call_args.kwargs["voxcpm_reference_audio"] == reference_audio
    assert add_task.call_args.kwargs["voxcpm_prompt_audio"] == prompt_audio
    assert add_task.call_args.kwargs["voxcpm_prompt_text"] == prompt_text
    webui_task.sm.state.delete_task("reference-audio-isolation")


def test_scheduling_failure_is_saved_as_terminal_task_state():
    """You cannot leave the Task Manager permanently stuck in "Building" when a queue or thread startup fails."""
    task_id = "scheduling-failure-test"
    params = VideoParams(video_subject="调度失败测试")
    with patch.object(
        webui_task._task_manager,
        "add_task",
        side_effect=RuntimeError("worker unavailable"),
    ):
        with pytest.raises(RuntimeError, match="worker unavailable"):
            webui_task.submit_generation(task_id, params, capture_logs=False)

    task = webui_task.sm.state.get_task(task_id)
    assert task["state"] == const.TASK_STATE_FAILED
    assert task["failed_stage"] == "scheduling"
    assert task["error"] == "RuntimeError: worker unavailable"
    webui_task.sm.state.delete_task(task_id)


def test_worker_logs_are_available_without_streamlit_session_state():
    """Background logs are written to a thread-safe cache, and the page can restore live logs simply by polling the snapshot."""
    task_id = "captured-log-test"
    with webui_task._task_logs_lock:
        webui_task._task_logs.pop(task_id, None)

    def logged_start(**_kwargs):
        logger.info("unique background task log")
        return {"videos": ["/tmp/final-1.mp4"]}

    with (
        patch.object(webui_task.tm, "start", side_effect=logged_start),
        patch.object(
            webui_task.config,
            "runtime_config_lock",
            return_value=nullcontext(),
        ),
    ):
        result = webui_task._run_generation(
            task_id,
            VideoParams(video_subject="日志测试"),
            capture_logs=True,
        )

    assert result == {"videos": ["/tmp/final-1.mp4"]}
    records = webui_task.get_task_logs(task_id)
    assert len(records) == 1
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| INFO \| "
        r'"\./test/services/test_webui_task\.py:\d+": logged_start '
        r"- unique background task log",
        records[0],
    )


def test_bound_helper_thread_logs_reach_the_task_log():
    """
    Parallel downloads, fragment encoding, and ffmpeg heartbeats are all logged in a secondary thread started by the task. Just work
    These records will be discarded when filtering by thread ID, and the WebUI will not output any output during the longest phase.
    Auxiliary threads bound by bind_log_scope must be included in the task to which they belong. Unbound threads are still excluded.
    Otherwise, API task logs running at the same time will be mixed in.
    """
    task_id = "helper-thread-log-test"
    with webui_task._task_logs_lock:
        webui_task._task_logs.pop(task_id, None)

    def logged_start(**_kwargs):
        bound = threading.Thread(
            target=logging_utils.bind_log_scope(
                lambda: logger.info("bound helper thread log")
            )
        )
        unrelated = threading.Thread(
            target=lambda: logger.info("unrelated thread log")
        )
        for thread in (bound, unrelated):
            thread.start()
        for thread in (bound, unrelated):
            thread.join()
        return {"videos": []}

    with (
        patch.object(webui_task.tm, "start", side_effect=logged_start),
        patch.object(
            webui_task.config,
            "runtime_config_lock",
            return_value=nullcontext(),
        ),
    ):
        webui_task._run_generation(
            task_id,
            VideoParams(video_subject="辅助线程日志"),
            capture_logs=True,
        )

    messages = "\n".join(webui_task.get_task_logs(task_id))
    assert "bound helper thread log" in messages
    assert "unrelated thread log" not in messages


def test_bind_log_scope_follows_nested_helpers_and_is_released():
    """
    Threads that are restarted by auxiliary threads (such as heartbeats in parallel fragments) must also belong to the original task thread.
    The thread must be unbound after it ends: the thread ID will be reused by the system, and the remaining mapping will be irrelevant later.
    The thread's log incorrectly counts old tasks.
    """
    root_thread_id = threading.get_ident()
    seen = {}

    def inner():
        seen["inner_scope"] = logging_utils.log_scope_thread_id()

    def outer():
        seen["outer_thread_id"] = threading.get_ident()
        seen["outer_scope"] = logging_utils.log_scope_thread_id()
        nested = threading.Thread(target=logging_utils.bind_log_scope(inner))
        nested.start()
        nested.join()

    helper = threading.Thread(target=logging_utils.bind_log_scope(outer))
    helper.start()
    helper.join()

    assert seen["outer_scope"] == root_thread_id
    assert seen["inner_scope"] == root_thread_id
    assert (
        logging_utils.log_scope_thread_id(seen["outer_thread_id"])
        == seen["outer_thread_id"]
    )
    assert logging_utils.log_scope_thread_id() == root_thread_id


def test_bind_log_scope_runs_inline_calls_without_rebinding():
    """When calling the wrapped function directly in the same thread, the return value remains unchanged and the scope is not changed."""
    bound = logging_utils.bind_log_scope(lambda value: value * 2)

    assert bound(21) == 42
    assert logging_utils.log_scope_thread_id() == threading.get_ident()


def test_webui_worker_forwards_reference_audio_to_pipeline():
    reference_audio = b"task-local-reference-wav"
    prompt_audio = b"task-local-prompt-wav"
    prompt_text = "task-local transcript"
    with (
        patch.object(webui_task.tm, "start", return_value={"videos": []}) as start,
        patch.object(
            webui_task.config,
            "runtime_config_lock",
            return_value=nullcontext(),
        ),
    ):
        webui_task._run_generation(
            "reference-audio-forwarding",
            VideoParams(video_subject="reference forwarding"),
            capture_logs=False,
            voxcpm_reference_audio=reference_audio,
            voxcpm_prompt_audio=prompt_audio,
            voxcpm_prompt_text=prompt_text,
        )

    assert start.call_args.kwargs["voxcpm_reference_audio"] == reference_audio
    assert start.call_args.kwargs["voxcpm_prompt_audio"] == prompt_audio
    assert start.call_args.kwargs["voxcpm_prompt_text"] == prompt_text


def test_log_paths_stay_posix_style_on_every_platform():
    """
    The calling location must always appear as ``./app/services/task.py``.

    Windows' ``os.path.relpath`` returns the path separated by backslashes, and direct splicing will output
    ``./app\\services\\task.py``, the format of the same log is inconsistent on different systems, and it cannot
    Align with the background log regression test asserted by forward slash above.
    """
    record = _log_record(
        os.path.join(logging_utils.PROJECT_ROOT, "app", "services", "task.py")
    )

    logging_utils.format_log_record(record)

    assert record["file"].path == "./app/services/task.py"


def test_log_paths_on_another_mount_do_not_discard_the_record():
    """
    The entire log cannot be lost when the mapped disk or ``subst`` disk is started.

    In this deployment, the path in the call stack is still ``X:``, and ``PROJECT_ROOT`` has been replaced by realpath
    Parsing back to ``C:``, ``os.path.relpath`` will throw a ``ValueError``. loguru captured
    Records will be discarded after formatting exceptions, and the terminal and WebUI log panels will become empty at the same time.
    """
    absolute_path = os.path.join(
        logging_utils.PROJECT_ROOT, "app", "services", "task.py"
    )
    record = _log_record(absolute_path)

    with patch.object(
        logging_utils.os.path,
        "relpath",
        side_effect=ValueError("path is on mount 'X:', start on mount 'C:'"),
    ):
        log_format = logging_utils.format_log_record(record)

    assert log_format == logging_utils.LOG_RECORD_FORMAT
    assert record["file"].path == absolute_path


def test_log_paths_outside_the_project_keep_the_absolute_path():
    """Keep absolute paths to files outside the project directory to avoid outputting backtracking paths such as ``./../..``."""
    outside_path = os.path.join(
        os.path.dirname(logging_utils.PROJECT_ROOT), "site-packages", "worker.py"
    )
    record = _log_record(outside_path)

    logging_utils.format_log_record(record)

    assert record["file"].path == outside_path


def test_generation_log_fragment_refreshes_within_half_a_second():
    """The log polling interval cannot fall back to a second-level refresh that significantly lags behind the terminal output."""
    assert webui_task.TASK_LOG_REFRESH_INTERVAL_SECONDS <= 0.5

    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_render_running_generation_task"
    )
    decorator = function.decorator_list[0]
    assert isinstance(decorator, ast.Call)
    assert _attribute_name(decorator.func) == "st.fragment"
    run_every = next(
        keyword.value for keyword in decorator.keywords if keyword.arg == "run_every"
    )
    assert ast.unparse(run_every) == ("webui_task.TASK_LOG_REFRESH_INTERVAL_SECONDS")


def test_generation_submit_skips_duplicate_config_save():
    """
    After submitting the task, you cannot wait for the configuration lock again at the end of the page.

    The background task holds the runtime_config_lock during the full build. The build branch has been requested
    Non-blocking save, no need to repeat requests at the end of the page; normal interactions continue through the same non-blocking helper
    Save and cannot return to config.save_config.
    """
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    controls = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_render_generation_controls"
    )
    application = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_render_application"
    )

    assert isinstance(controls.body[-1], ast.Return)
    assert ast.unparse(controls.body[-1].value) == "start_button"

    submitted_assignment = next(
        node
        for node in application.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "generation_submitted"
            for target in node.targets
        )
    )
    assert isinstance(submitted_assignment.value, ast.Call)
    assert _attribute_name(submitted_assignment.value.func) == (
        "_render_generation_controls"
    )

    guarded_save = next(
        node
        for node in application.body
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "not generation_submitted"
    )
    guarded_calls = {
        _attribute_name(node.func)
        for node in ast.walk(guarded_save)
        if isinstance(node, ast.Call)
    }
    assert guarded_calls == {"_save_runtime_config"}


def test_terminal_logger_reload_preserves_task_log_handler():
    """Hot reload can only replace the terminal handler, but cannot clear the log sink of background tasks."""
    previous_handler_id = logging_utils._terminal_handler_id
    try:
        with (
            patch.object(logging_utils.logger, "remove") as remove,
            patch.object(logging_utils.logger, "add", return_value=456) as add,
        ):
            logging_utils._terminal_handler_id = 123
            handler_id = logging_utils.configure_terminal_logger(
                sink=object(),
                level="DEBUG",
                colorize=True,
            )

        assert handler_id == 456
        remove.assert_called_once_with(123)
        add.assert_called_once()
        assert logging_utils._terminal_handler_id == 456
    finally:
        logging_utils._terminal_handler_id = previous_handler_id


def test_worker_wrapper_failure_is_saved_instead_of_leaving_processing_state():
    """Log or configuration wrapper exceptions must also be converted into queryable failure final states."""
    task_id = "worker-wrapper-failure-test"
    with (
        patch.object(webui_task.tm, "start", side_effect=RuntimeError("lock failed")),
        patch.object(
            webui_task.config,
            "runtime_config_lock",
            return_value=nullcontext(),
        ),
    ):
        result = webui_task._run_generation(
            task_id,
            VideoParams(video_subject="工作线程失败测试"),
            capture_logs=False,
        )

    assert result["state"] == const.TASK_STATE_FAILED
    assert result["failed_stage"] == "webui_worker"
    task = webui_task.sm.state.get_task(task_id)
    assert task["state"] == const.TASK_STATE_FAILED
    assert task["error"] == "RuntimeError: lock failed"
    webui_task.sm.state.delete_task(task_id)
