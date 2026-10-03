import functools
import os
import threading

from loguru import logger


PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
)
LOG_RECORD_FORMAT = (
    "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
    "<level>{level}</> | "
    '"{file.path}:{line}":<blue> {function}</> '
    "- <level>{message}</>\n"
)
# When Loguru starts, the default terminal handler ID is 0. This can only be replaced when WebUI reloads
# Basic terminal output, logger.remove() cannot be called to clear all handlers, otherwise the task is running
# The temporary sink used to collect WebUI logs will also be deleted.
_terminal_handler_id: int | None = 0
_terminal_handler_lock = threading.RLock()
# WebUI filters logs by task worker thread ID. Tasks are started for parallel downloading, segment encoding and heartbeat
# The auxiliary threads have their own thread IDs, and the logs written by them will be discarded entirely, resulting in the longest phase.
# There is no output in WebUI. The mapping of "auxiliary thread -> thread that initiated it" is recorded here,
# Allow the log filter to return the records of the auxiliary thread to the corresponding task.
_log_scope_roots: dict[int, int] = {}
_log_scope_lock = threading.Lock()


def log_scope_thread_id(thread_id: int | None = None) -> int:
    """Returns the root thread ID of the log scope to which the thread belongs; the unbound thread is itself."""
    if thread_id is None:
        thread_id = threading.get_ident()
    with _log_scope_lock:
        return _log_scope_roots.get(thread_id, thread_id)


def bind_log_scope(func):
    """
    Wrap a function that will be executed in another thread so that its log belongs to the thread that called the function.

    Must be called in the thread that submits the task: the scope is determined when packaging, and the auxiliary thread is started again
    It will also return to the original task thread. Unbind after execution, because the thread ID will be reused by the system.
    Residual mapping will count subsequent logs of unrelated threads into completed tasks.
    """
    root_thread_id = log_scope_thread_id()

    @functools.wraps(func)
    def run_in_log_scope(*args, **kwargs):
        thread_id = threading.get_ident()
        if thread_id == root_thread_id:
            return func(*args, **kwargs)

        with _log_scope_lock:
            previous_root = _log_scope_roots.get(thread_id)
            _log_scope_roots[thread_id] = root_thread_id
        try:
            return func(*args, **kwargs)
        finally:
            with _log_scope_lock:
                # The thread pool will reuse threads: restore the ownership before entry instead of deleting them all to avoid
                # Nested packaging clears bindings still in use by the outer layer in advance.
                if previous_root is None:
                    _log_scope_roots.pop(thread_id, None)
                else:
                    _log_scope_roots[thread_id] = previous_root

    return run_in_log_scope


def _project_relative_path(file_path):
    """
    Shorten absolute paths to project-relative paths starting with ``./`` and always using a forward slash.

    On Windows the project may be started from a mapped network disk or a ``subst`` disk. The path in the call stack at this time
    Still ``X:\\VietNamNewsVideo\\...``, and ``PROJECT_ROOT`` via ``realpath``
    If it falls into ``C:\\...`` after parsing, ``os.path.relpath`` will directly throw ``ValueError``.
    If the formatting function throws an error, it will be captured by loguru and the entire record will be discarded. The terminal and WebUI log panel will
    It becomes empty at the same time, so we must return to the original path here. The same goes for files outside the project directory: put
    Spelling ``./`` to ``..`` will only result in a more difficult-to-read result on the backtracking path.
    """
    try:
        relative_path = os.path.relpath(file_path, PROJECT_ROOT)
    except ValueError:
        return file_path
    if relative_path == os.pardir or relative_path.startswith(os.pardir + os.sep):
        return file_path
    # Windows' relpath returns a backslash-separated path, and direct splicing will result in ``./app\\utils``
    # The output of this mixed delimiter is inconsistent with the logs of other platforms.
    return f"./{relative_path.replace(os.sep, '/')}"


def format_log_record(record):
    """
    Unified formatting of terminal and WebUI logs.

    Loguru will deliver the same record to multiple sinks. The first sink may have converted the absolute path
    It is a relative path to the project, so it is compatible with both absolute paths and formatted paths starting with ``./``.
    The WebUI sink turns off color, but the time, level, call location, and message content remain consistent with the terminal.
    """
    file_path = record["file"].path
    if os.path.isabs(file_path):
        record["file"].path = _project_relative_path(file_path)

    # Log messages sometimes contain the absolute path to the task file. Uniformly shorten to project relative path, you can
    # Prevent the WebUI and the terminal from displaying two sets of content due to different initialization entrances.
    record["message"] = record["message"].replace(PROJECT_ROOT, ".")
    return LOG_RECORD_FORMAT


def configure_terminal_logger(sink, level: str, colorize: bool = True) -> int:
    """
    Safely replace process-level terminal log handlers and preserve task-specific handlers.

    Streamlit may re-perform log initialization during code hot reload or cache invalidation. Just click on Recorded here
    The handler ID removes old terminal output exactly, so it doesn't interrupt the WebUI that the background task is writing to
    Log. The lock is used to protect ID updates when multiple browser sessions are initialized simultaneously.
    """
    global _terminal_handler_id

    with _terminal_handler_lock:
        if _terminal_handler_id is not None:
            try:
                logger.remove(_terminal_handler_id)
            except ValueError:
                # A test or external portal may have removed the handler. Go ahead and create a new terminal output,
                # There is no need to affect other log sinks that are still valid.
                pass

        _terminal_handler_id = logger.add(
            sink,
            level=level,
            format=format_log_record,
            colorize=colorize,
        )
        return _terminal_handler_id
