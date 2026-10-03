import math
import threading
from typing import Any, Callable, Dict

from loguru import logger


class TaskQueueFullError(ValueError):
    pass


def _coerce_task_limit(value: Any, name: str) -> int:
    """
    Parse the concurrency/queuing upper limit in the configuration into an integer.

    The value of TOML may be written as `max_concurrent_tasks = 5` or as `"5"`. string
    will be passed all the way to the comparison operation of `add_task`, becoming a TypeError that does not indicate the configuration key name, and
    If `max_queued_tasks` is written incorrectly, it will only be triggered after the concurrent quota is exhausted. `app/services/webui_task.py`
    The same convergence has been done for the same `max_queued_tasks` key, which is mentioned here as the only construction entry.

    If the integer cannot be parsed, the configuration key name will always be reported: `0.5` will be silently truncated to 0 by `int()` (the task is only queued,
    No worker execution), the `inf` / `-inf` / `nan` allowed by TOML are not valid upper bounds either.
    0 and negative numbers remain unchanged: the former means that the task is not executed for the time being, and the latter allows the request to go directly to the queued branch.
    """
    if isinstance(value, bool):
        # bool is a subclass of int, but `true` is obviously not the upper limit that users want.
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if isinstance(value, float):
        # Non-finite values are not the upper limit, and decimals will degenerate into the semantics of "0: only queued but not executed" after truncation.
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite integer, got {value!r}")
        if not value.is_integer():
            raise ValueError(f"{name} must be an integer, got {value!r}")
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer, got {value!r}") from None


class TaskManager:
    def __init__(self, max_concurrent_tasks: int, max_queued_tasks: int = 100):
        self.max_concurrent_tasks = _coerce_task_limit(
            max_concurrent_tasks, "max_concurrent_tasks"
        )
        self.max_queued_tasks = _coerce_task_limit(max_queued_tasks, "max_queued_tasks")
        self.current_tasks = 0
        self.lock = threading.Lock()
        self.queue = self.create_queue()

    def create_queue(self):
        raise NotImplementedError()

    def add_task(self, func: Callable, *args: Any, **kwargs: Any):
        with self.lock:
            if self.current_tasks < self.max_concurrent_tasks:
                logger.info(
                    f"add task: {func.__name__}, current_tasks: {self.current_tasks}"
                )
                # Reserve the concurrent quota before the thread starts. The original implementation increments within the thread for continuous requests
                # It is possible to see current_tasks=0 before the child thread obtains the lock, thereby breaking through the concurrency
                # upper limit. When the startup fails, the quota is rolled back so that subsequent requests can still be scheduled normally.
                self.current_tasks += 1
                try:
                    self.execute_task(func, *args, **kwargs)
                except Exception:
                    self.current_tasks -= 1
                    raise
            else:
                queue_size = self.queue_size()
                # The queue will be queued only when the number of concurrent requests is full. The queue must be capped, otherwise the anonymous interface can persist
                # Stack up task objects and request parameters, eventually running out of memory or out-of-control third-party API costs.
                if queue_size >= self.max_queued_tasks:
                    logger.warning(
                        f"reject task: {func.__name__}, queue_size: {queue_size}, "
                        f"max_queued_tasks: {self.max_queued_tasks}"
                    )
                    raise TaskQueueFullError("task queue is full, please try again later")

                logger.info(
                    f"enqueue task: {func.__name__}, current_tasks: {self.current_tasks}, "
                    f"queue_size: {queue_size}"
                )
                self.enqueue_new_task({"func": func, "args": args, "kwargs": kwargs})

    def execute_task(self, func: Callable, *args: Any, **kwargs: Any):
        thread = threading.Thread(
            target=self.run_task, args=(func, *args), kwargs=kwargs
        )
        thread.start()

    def run_task(self, func: Callable, *args: Any, **kwargs: Any):
        try:
            func(*args, **kwargs)  # call the function here, passing *args and **kwargs.
        finally:
            self.task_done()

    def check_queue(self):
        with self.lock:
            self._check_queue_locked()

    def _check_queue_locked(self):
        """Dispatch one queued task while the caller holds ``self.lock``."""
        if (
            self.current_tasks < self.max_concurrent_tasks
            and not self.is_queue_empty()
        ):
            task_info = self.dequeue()
            if task_info is None:
                # dequeue() may skip and discard queue entries that no longer
                # pass current validation (see RedisTaskManager.dequeue) and
                # return None once nothing usable is left, even though
                # is_queue_empty() was False a moment earlier.
                return
            func = task_info["func"]
            args = task_info.get("args", ())
            kwargs = task_info.get("kwargs", {})
            # Reserve the freed slot before another add_task can claim it.
            self.current_tasks += 1
            try:
                self.execute_task(func, *args, **kwargs)
            except Exception:
                self.current_tasks -= 1
                self.enqueue(task_info)
                raise

    def task_done(self):
        with self.lock:
            self.current_tasks -= 1
            self._check_queue_locked()

    def enqueue(self, task: Dict):
        raise NotImplementedError()

    def enqueue_new_task(self, task: Dict):
        """Admit new work; shared queues may need their own atomic limit check."""
        self.enqueue(task)

    def dequeue(self):
        raise NotImplementedError()

    def is_queue_empty(self):
        raise NotImplementedError()

    def queue_size(self):
        raise NotImplementedError()
