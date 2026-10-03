import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.controllers.manager.redis_manager import RedisTaskManager
from app.models import const
from app.models.schema import VideoParams
from app.services import task as task_service


def _queued_payload(func: str, **kwargs) -> str:
    """Construct a queue entry according to the placement format of RedisTaskManager.enqueue."""
    return json.dumps({"func": func, "args": [], "kwargs": kwargs})


def _video_params() -> dict:
    """Returns a VideoParams serialization result that can pass the current verification."""
    return VideoParams(video_subject="Tea").model_dump(warnings=False)


class TestInMemoryTaskManager(unittest.TestCase):
    def test_queue_operations_preserve_task_payload(self):
        """The memory queue should hold functions, positional arguments, and keyword arguments and should not change task content."""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=2)
        task = {"func": len, "args": ([1, 2],), "kwargs": {}}

        manager.enqueue(task)

        self.assertFalse(manager.is_queue_empty())
        self.assertEqual(manager.queue_size(), 1)
        self.assertEqual(manager.dequeue(), task)
        self.assertTrue(manager.is_queue_empty())

    def test_add_task_rejects_only_after_queue_limit(self):
        """After the concurrent quota is exhausted, queuing is allowed up to the upper limit. A clear error will be returned only when the upper limit is exceeded."""
        manager = InMemoryTaskManager(max_concurrent_tasks=0, max_queued_tasks=1)

        manager.add_task(len, [1])

        with self.assertRaises(TaskQueueFullError):
            manager.add_task(len, [2])

    def test_add_task_reserves_slot_before_background_thread_runs(self):
        """
        The concurrent quota must be reserved before the thread is started; even if the mock thread has not yet entered run_task,
        The second request should also be queued and max_concurrent_tasks cannot be exceeded.
        """
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=1)

        with patch.object(manager, "execute_task") as execute_task:
            manager.add_task(len, [1])
            manager.add_task(len, [2])

        self.assertEqual(manager.current_tasks, 1)
        execute_task.assert_called_once_with(len, [1])
        self.assertEqual(manager.queue_size(), 1)

    def test_add_task_rolls_back_slot_when_thread_cannot_start(self):
        """Thread startup failure cannot permanently occupy the concurrent quota, and exceptions should still be handled by the caller."""
        manager = InMemoryTaskManager(max_concurrent_tasks=1)

        with patch.object(
            manager,
            "execute_task",
            side_effect=RuntimeError("thread unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                manager.add_task(len, [1])

        self.assertEqual(manager.current_tasks, 0)

    def test_task_done_starts_next_queued_task(self):
        """After the current task ends, the concurrent quota should be released and the next task in the queue should be scheduled immediately."""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=2)
        manager.current_tasks = 1
        manager.enqueue({"func": len, "args": ([1, 2],), "kwargs": {}})

        with patch.object(manager, "execute_task") as execute_task:
            manager.task_done()

        self.assertEqual(manager.current_tasks, 1)
        execute_task.assert_called_once_with(len, [1, 2])
        self.assertTrue(manager.is_queue_empty())

    def test_new_request_cannot_take_slot_before_waiting_task(self):
        """A completion must reserve its freed slot for the oldest queued task."""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=2)
        manager.current_tasks = 1

        def queued_task():
            pass

        def incoming_task():
            pass

        manager.enqueue({"func": queued_task, "args": (), "kwargs": {}})
        released = threading.Event()
        resume = threading.Event()

        class PausingLock:
            def __init__(self):
                self.lock = threading.Lock()
                self.worker = None
                self.paused = False

            def __enter__(self):
                self.lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.lock.release()
                if threading.current_thread() is self.worker and not self.paused:
                    self.paused = True
                    released.set()
                    resume.wait(timeout=2)

        manager.lock = PausingLock()
        started = []
        with patch.object(manager, "execute_task", side_effect=lambda func: started.append(func)):
            worker = threading.Thread(target=manager.task_done)
            manager.lock.worker = worker
            worker.start()
            try:
                self.assertTrue(released.wait(timeout=2))
                manager.add_task(incoming_task)
            finally:
                resume.set()
                worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(started, [queued_task])
        self.assertEqual(manager.dequeue()["func"], incoming_task)

    def test_task_done_requeues_task_when_thread_cannot_start(self):
        """If the thread fails to start after dequeuing, the quota should be rolled back and the task returned to the queue to avoid task loss."""
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=1)
        manager.current_tasks = 1
        queued_task = {"func": len, "args": ([1, 2],), "kwargs": {}}
        manager.enqueue(queued_task)

        with patch.object(
            manager,
            "execute_task",
            side_effect=RuntimeError("thread unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                manager.task_done()

        self.assertEqual(manager.current_tasks, 0)
        self.assertEqual(manager.dequeue(), queued_task)

    def test_run_task_releases_slot_after_failure(self):
        """When the task function throws an exception, finally must still release the quota to avoid permanent blocking of the queue."""
        manager = InMemoryTaskManager(max_concurrent_tasks=1)
        manager.current_tasks = 1

        with patch.object(manager, "task_done") as task_done:
            with self.assertRaisesRegex(RuntimeError, "task failed"):
                manager.run_task(MagicMock(side_effect=RuntimeError("task failed")))

        self.assertEqual(manager.current_tasks, 1)
        task_done.assert_called_once_with()

    def test_check_queue_handles_dequeue_returning_none(self):
        """
        dequeue() may return None after internally skipping all queued tasks that no longer satisfy the current validation.
        Even before calling check_queue is_queue_empty() used to be False. check_queue
        It cannot be assumed that dequeue will definitely be able to get available tasks, otherwise it will crash on task_info["func"].
        """
        manager = InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=1)

        with patch.object(manager, "is_queue_empty", return_value=False), patch.object(
            manager, "dequeue", return_value=None
        ), patch.object(manager, "execute_task") as execute_task:
            manager.check_queue()

        execute_task.assert_not_called()
        self.assertEqual(manager.current_tasks, 0)

    def test_execute_task_starts_background_thread(self):
        """The task execution entry must start the thread and pass the complete function parameters to run_task."""
        manager = InMemoryTaskManager(max_concurrent_tasks=1)
        fake_thread = MagicMock()

        with patch(
            "app.controllers.manager.base_manager.threading.Thread",
            return_value=fake_thread,
        ) as thread:
            manager.execute_task(len, [1, 2])

        thread.assert_called_once_with(
            target=manager.run_task,
            args=(len, [1, 2]),
            kwargs={},
        )
        fake_thread.start.assert_called_once_with()

    def test_limits_accept_quoted_toml_integers(self):
        """
        When writing the upper limit as a string in TOML, it must also be compared as an integer; before the repair, the comparison of concurrent quotas will throw
        TypeError, the queue upper limit comparison will not be triggered until the quota is exhausted.
        """
        manager = InMemoryTaskManager(max_concurrent_tasks="1", max_queued_tasks="1")

        with patch.object(manager, "execute_task") as execute_task:
            manager.add_task(len, [1])
            manager.add_task(len, [2])
            with self.assertRaises(TaskQueueFullError):
                manager.add_task(len, [3])

        self.assertEqual(manager.max_concurrent_tasks, 1)
        self.assertEqual(manager.max_queued_tasks, 1)
        execute_task.assert_called_once_with(len, [1])
        self.assertEqual(manager.queue_size(), 1)

    def test_invalid_limits_raise_a_named_error(self):
        """Unresolvable upper bounds must indicate the configuration key name immediately and cannot be postponed to an anonymous exception during the scheduling period."""
        with self.assertRaisesRegex(ValueError, "max_concurrent_tasks"):
            InMemoryTaskManager(max_concurrent_tasks="abc")

        with self.assertRaisesRegex(ValueError, "max_queued_tasks"):
            InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks="abc")

        with self.assertRaisesRegex(ValueError, "max_queued_tasks"):
            InMemoryTaskManager(max_concurrent_tasks=1, max_queued_tasks=True)

    def test_non_integral_limits_are_rejected_by_name(self):
        """Before repair, `0.5` will be truncated to 0 by `int()` (no worker will execute after the task is enqueued), and `inf` will leak OverflowError."""
        cases = (
            (0.5, "an integer"),
            (1.5, "an integer"),
            ("0.5", "an integer"),
            (float("nan"), "a finite"),
            (float("inf"), "a finite"),
            (float("-inf"), "a finite"),
        )
        for field in ("max_concurrent_tasks", "max_queued_tasks"):
            for value, msg in cases:
                with self.subTest(field=field, value=value):
                    limits = {"max_concurrent_tasks": 1, "max_queued_tasks": 1}
                    limits[field] = value
                    pattern = f"{field} must be {msg}"
                    with self.assertRaisesRegex(ValueError, pattern):
                        InMemoryTaskManager(**limits)

        # Integer-valued floating point without risk of truncation is still available (TOML allows `max_queued_tasks = 2.0`).
        integral = {"max_concurrent_tasks": 1, "max_queued_tasks": 2.0}
        self.assertEqual(InMemoryTaskManager(**integral).max_queued_tasks, 2)

    def test_zero_concurrency_keeps_queueing_without_executing(self):
        """0 and negative numbers are still legal values: only queued, not executed, and existing use cases rely on this semantics."""
        manager = InMemoryTaskManager(max_concurrent_tasks=0, max_queued_tasks=1)

        with patch.object(manager, "execute_task") as execute_task:
            manager.add_task(len, [1])

        execute_task.assert_not_called()
        self.assertEqual(manager.queue_size(), 1)


class TestRedisTaskManager(unittest.TestCase):
    def setUp(self):
        self.redis_client = MagicMock()
        patcher = patch(
            "app.controllers.manager.redis_manager.redis.Redis.from_url",
            return_value=self.redis_client,
        )
        self.addCleanup(patcher.stop)
        from_url = patcher.start()
        self.manager = RedisTaskManager(
            max_concurrent_tasks=1,
            redis_url="redis://localhost:6379/0",
            max_queued_tasks=3,
        )
        from_url.assert_called_once_with("redis://localhost:6379/0")

    def test_resume_queued_tasks_fills_available_slots_after_restart(self):
        """Persisted Redis entries should run without waiting for a new API request."""
        self.manager.max_concurrent_tasks = 2
        self.redis_client.llen.return_value = 2
        self.redis_client.lpop.side_effect = [
            _queued_payload("start", task_id="first", params=_video_params()),
            _queued_payload("start", task_id="second", params=_video_params()),
        ]

        with patch.object(self.manager, "execute_task") as execute_task:
            self.manager.resume_queued_tasks()

        self.assertEqual(execute_task.call_count, 2)
        self.assertEqual(self.manager.current_tasks, 2)
        self.assertEqual(self.redis_client.lpop.call_count, 2)

    def test_enqueue_serializes_video_params_without_mutating_task(self):
        """
        Redis can only store JSON; VideoParams should be converted into a dictionary, but the original task still needs to retain the model.
        Avoid serialization side effects affecting logging, retries, or subsequent reads by the caller.
        """
        params = VideoParams(video_subject="Coffee")
        task = {
            "func": task_service.start,
            "args": (),
            "kwargs": {"task_id": "task-1", "params": params},
        }

        self.manager.enqueue(task)

        self.assertIs(task["kwargs"]["params"], params)
        queue_name, payload = self.redis_client.rpush.call_args.args
        decoded = json.loads(payload)
        self.assertEqual(queue_name, "task_queue")
        self.assertEqual(decoded["func"], "start")
        self.assertEqual(decoded["kwargs"]["task_id"], "task-1")
        self.assertEqual(decoded["kwargs"]["params"]["video_subject"], "Coffee")

    def test_dequeue_restores_function_and_video_params(self):
        """Tasks fetched from Redis should restore callable functions and VideoParams models."""
        payload = {
            "func": "start",
            "args": [],
            "kwargs": {
                "task_id": "task-1",
                "params": VideoParams(video_subject="Coffee").model_dump(
                    warnings=False
                ),
            },
        }
        self.redis_client.lpop.return_value = json.dumps(payload)

        task = self.manager.dequeue()

        self.redis_client.lpop.assert_called_once_with("task_queue")
        self.assertIs(task["func"], task_service.start)
        self.assertIsInstance(task["kwargs"]["params"], VideoParams)
        self.assertEqual(task["kwargs"]["params"].video_subject, "Coffee")

    def test_empty_queue_and_size_use_redis_length(self):
        """The queue empty check and length must directly reflect the current list length of Redis."""
        self.redis_client.lpop.return_value = None
        self.redis_client.llen.side_effect = [0, 2]

        self.assertIsNone(self.manager.dequeue())
        self.assertTrue(self.manager.is_queue_empty())
        self.assertEqual(self.manager.queue_size(), 2)

    def test_dequeue_skips_task_that_fails_current_validation(self):
        """
        A task may have been enqueued before the validation rules were tightened (e.g. video_count was allowed to be 0).
        lpop is a destructive operation. When rebuilding VideoParams fails, this task has been deleted from Redis.
        It's permanently removed, you can't pretend it's still there; dequeue should not throw validation exceptions to the caller
        (That will cause the lock-holding caller to crash and lose the task without logging), but should skip it,
        Continue trying the next item in the queue until an available task is obtained or the queue is indeed empty.
        """
        stale_payload = {
            "func": "start",
            "args": [],
            "kwargs": {
                "task_id": "task-stale",
                "params": {**VideoParams(video_subject="Coffee").model_dump(
                    warnings=False
                ), "video_count": 0},
            },
        }
        valid_payload = {
            "func": "start",
            "args": [],
            "kwargs": {
                "task_id": "task-valid",
                "params": VideoParams(video_subject="Tea").model_dump(
                    warnings=False
                ),
            },
        }
        self.redis_client.lpop.side_effect = [
            json.dumps(stale_payload),
            json.dumps(valid_payload),
        ]

        task = self.manager.dequeue()

        self.assertEqual(self.redis_client.lpop.call_count, 2)
        self.assertEqual(task["kwargs"]["task_id"], "task-valid")
        self.assertIsInstance(task["kwargs"]["params"], VideoParams)
        self.assertEqual(task["kwargs"]["params"].video_subject, "Tea")

    def test_dequeue_returns_none_when_every_queued_task_is_stale(self):
        """When all remaining tasks are discarded due to the current validation rules, None should be returned instead of throwing an exception."""
        stale_payload = {
            "func": "start",
            "args": [],
            "kwargs": {
                "task_id": "task-stale",
                "params": {**VideoParams(video_subject="Coffee").model_dump(
                    warnings=False
                ), "video_count": -1},
            },
        }
        self.redis_client.lpop.side_effect = [json.dumps(stale_payload), None]

        self.assertIsNone(self.manager.dequeue())
        self.assertEqual(self.redis_client.lpop.call_count, 2)

    def test_dequeue_marks_stale_task_failed_instead_of_leaving_it_processing(self):
        """
        Task status records are created before being queued, and the default is processing. Just skip in dequeue
        And discarding this queue item without updating the status record will cause this task to be displayed in the API/WebUI forever
        is running. It should be marked as failed using patch_task (not update_task),
        In this way, if the task has been deleted by the user, we will not create its status record back again.
        """
        stale_payload = {
            "func": "start",
            "args": [],
            "kwargs": {
                "task_id": "task-stale",
                "params": {**VideoParams(video_subject="Coffee").model_dump(
                    warnings=False
                ), "video_count": 0},
            },
        }
        self.redis_client.lpop.side_effect = [json.dumps(stale_payload), None]

        with patch("app.controllers.manager.redis_manager.sm.state") as state:
            state.patch_task.return_value = True
            result = self.manager.dequeue()

        self.assertIsNone(result)
        state.patch_task.assert_called_once()
        call_args = state.patch_task.call_args
        self.assertEqual(call_args.args[0], "task-stale")
        self.assertEqual(call_args.kwargs["state"], const.TASK_STATE_FAILED)
        self.assertEqual(call_args.kwargs["failed_stage"], "dequeue")
        self.assertIn("video_count", call_args.kwargs["error"])

    def test_dequeue_does_not_recreate_state_for_already_deleted_task(self):
        """patch_task returns False when the task has been deleted; dequeue should not handle this as an error."""
        stale_payload = {
            "func": "start",
            "args": [],
            "kwargs": {
                "task_id": "task-deleted",
                "params": {**VideoParams(video_subject="Coffee").model_dump(
                    warnings=False
                ), "video_count": 0},
            },
        }
        self.redis_client.lpop.side_effect = [json.dumps(stale_payload), None]

        with patch("app.controllers.manager.redis_manager.sm.state") as state:
            state.patch_task.return_value = False
            result = self.manager.dequeue()

        self.assertIsNone(result)
        state.patch_task.assert_called_once()
        state.update_task.assert_not_called()

    def test_dequeue_skips_task_with_unknown_function_name(self):
        """
        The members of FUNC_MAP will change with deployment (the commented out second entry is retained in this file),
        There may be tasks with old entry functions remaining in the queue. Direct indexing of names not in FUNC_MAP will throw
        KeyError, bypasses the dequeue's own discarding policy, so it must be skipped just like the verification failed path.
        """
        self.redis_client.lpop.side_effect = [
            _queued_payload("start_test", task_id="task-unknown-func"),
            _queued_payload("start", task_id="task-valid", params=_video_params()),
        ]

        with patch("app.controllers.manager.redis_manager.sm.state") as state:
            state.patch_task.return_value = True
            task = self.manager.dequeue()

        self.assertEqual(self.redis_client.lpop.call_count, 2)
        self.assertIs(task["func"], task_service.start)
        self.assertEqual(task["kwargs"]["task_id"], "task-valid")
        state.patch_task.assert_called_once()
        call_args = state.patch_task.call_args
        self.assertEqual(call_args.args[0], "task-unknown-func")
        self.assertEqual(call_args.kwargs["state"], const.TASK_STATE_FAILED)
        self.assertIn("start_test", call_args.kwargs["error"])

    def test_dequeue_skips_every_unusable_payload_shape(self):
        """
        Three types of unavailable entries may remain in the queue: writing truncated JSON, JSON that is not an object, and
        References to tasks whose entry functions have been removed (only comments remain in FUNC_MAP). None of them can handle exceptions
        Throw dequeue, otherwise the available tasks at the back will never be scheduled; the task_id can be read out
        The entries also converge to failed tasks that have permanently left the queue.
        """
        self.redis_client.lpop.side_effect = [
            '{"func": "start", "args": [',
            json.dumps(["not", "a", "mapping"]),
            _queued_payload("retired-entry", task_id="task-retired"),
            _queued_payload("start", task_id="task-valid", params=_video_params()),
        ]

        with patch("app.controllers.manager.redis_manager.sm.state") as state:
            state.patch_task.return_value = True
            task = self.manager.dequeue()

        self.assertEqual(self.redis_client.lpop.call_count, 4)
        self.assertIs(task["func"], task_service.start)
        self.assertEqual(task["kwargs"]["task_id"], "task-valid")
        state.patch_task.assert_called_once()
        self.assertEqual(state.patch_task.call_args.args[0], "task-retired")
        self.assertEqual(
            state.patch_task.call_args.kwargs["state"], const.TASK_STATE_FAILED
        )

    def test_dequeue_returns_none_when_only_unusable_entries_remain(self):
        """When all remaining entries are unavailable, None should be returned instead of throwing an exception."""
        self.redis_client.lpop.side_effect = [
            _queued_payload("retired-entry"),
            None,
        ]

        with patch("app.controllers.manager.redis_manager.sm.state") as state:
            result = self.manager.dequeue()

        self.assertIsNone(result)
        self.assertEqual(self.redis_client.lpop.call_count, 2)
        state.patch_task.assert_not_called()

    def test_task_done_keeps_draining_queue_when_entry_is_unusable(self):
        """
        After the completed task releases the quota, task_done triggers check_queue to schedule the next task. if
        Unavailable entries cause check_queue to throw an exception, and the exception will be processed along the finally path of run_task.
        The thread crashes; no tasks are running since then, so no one will call check_queue again.
        Subsequent tasks will be permanently stopped in processing. Therefore task_done must be able to continue scheduling the next
        available tasks instead of leaving the entire queue in place.
        """
        self.manager.current_tasks = 1
        self.redis_client.lpop.side_effect = [
            _queued_payload("retired-entry"),
            _queued_payload("start", task_id="task-next", params=_video_params()),
        ]

        with patch.object(self.manager, "execute_task") as execute_task:
            self.manager.task_done()

        self.assertEqual(self.manager.current_tasks, 1)
        self.assertEqual(execute_task.call_args.kwargs["task_id"], "task-next")
        self.assertIs(execute_task.call_args.args[0], task_service.start)

    def test_dequeue_discards_payloads_that_cannot_be_dispatched(self):
        """
        Three items that can be parsed but cannot be dispatched: lpop returns a null value, args is written as null / object /
        Number/String. Neither of them can cause dequeue to throw an exception, nor can they be regarded as "the queue is empty"——
        Otherwise, check_queue will re-enter the queue or leave early, and the available tasks queued behind it will no longer be scheduled.
        """
        cases = (
            ("empty bytes", b"", None),
            ("empty string", "", None),
            ("args null", {"args": None}, "t-1"),
            ("args mapping", {"args": {}}, "t-2"),
            ("args number", {"args": 5}, "t-3"),
            ("args string", {"args": "x"}, "t-4"),
        )

        for case, broken, expected_task_id in cases:
            with self.subTest(case=case):
                if isinstance(broken, dict):
                    broken = json.dumps(
                        {
                            "func": "start",
                            "kwargs": {"task_id": expected_task_id},
                            **broken,
                        }
                    )
                self.redis_client.reset_mock()
                self.redis_client.lpop.side_effect = [
                    broken,
                    _queued_payload(
                        "start", task_id="task-valid", params=_video_params()
                    ),
                ]

                with patch("app.controllers.manager.redis_manager.sm.state") as state:
                    state.patch_task.return_value = True
                    task = self.manager.dequeue()

                self.assertEqual(self.redis_client.lpop.call_count, 2)
                self.assertEqual(task["kwargs"]["task_id"], "task-valid")
                self.assertIs(task["func"], task_service.start)
                if expected_task_id is None:
                    state.patch_task.assert_not_called()
                else:
                    self.assertEqual(
                        state.patch_task.call_args.args[0], expected_task_id
                    )
                    self.assertIn(
                        "positional arguments",
                        state.patch_task.call_args.kwargs["error"],
                    )

    def test_dequeue_dispatches_payload_without_args_field(self):
        """The overall absence of the args field is an existing format (check_queue takes the default empty tuple) and cannot be discarded."""
        self.redis_client.lpop.return_value = json.dumps(
            {
                "func": "start",
                "kwargs": {"task_id": "task-no-args", "params": _video_params()},
            }
        )

        with patch("app.controllers.manager.redis_manager.sm.state") as state:
            task = self.manager.dequeue()

        self.assertEqual(task["kwargs"]["task_id"], "task-no-args")
        self.assertIs(task["func"], task_service.start)
        state.patch_task.assert_not_called()

    def test_dequeue_ignores_non_string_task_id_in_every_discard_path(self):
        """
        When task_id is a non-string value such as a JSON array/object, passing it to patch_task will cause redis to
        Throwing DataError interrupts the discard cycle on a bad entry, and the available tasks queued behind it cannot start because of this.
        Both discard paths skip state writeback and only discard the entry itself.
        """
        cases = (
            ("unknown function", _queued_payload("retired-entry", task_id=["task-1"])),
            (
                "stale params",
                _queued_payload(
                    "start",
                    task_id={"nested": "task-2"},
                    params={**_video_params(), "video_count": 0},
                ),
            ),
        )

        for case, payload in cases:
            with self.subTest(case=case):
                self.redis_client.reset_mock()
                self.redis_client.lpop.side_effect = [
                    payload,
                    _queued_payload(
                        "start", task_id="task-valid", params=_video_params()
                    ),
                ]

                with patch("app.controllers.manager.redis_manager.sm.state") as state:
                    task = self.manager.dequeue()

                self.assertEqual(self.redis_client.lpop.call_count, 2)
                self.assertEqual(task["kwargs"]["task_id"], "task-valid")
                state.patch_task.assert_not_called()
                state.update_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
