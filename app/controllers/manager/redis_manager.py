import inspect
import json
from typing import Dict

import redis
from loguru import logger
from pydantic import ValidationError

from app.controllers.manager.base_manager import TaskManager, TaskQueueFullError
from app.models import const
from app.models.schema import AudioRequest, SubtitleRequest, VideoParams
from app.services import state as sm
from app.services import task as tm

FUNC_MAP = {
    "start": tm.start,
    # 'start_test': tm.start_test
}

_ADMIT_QUEUED_TASK_SCRIPT = """
if redis.call("LLEN", KEYS[1]) >= tonumber(ARGV[1]) then
    return 0
end
redis.call("RPUSH", KEYS[1], ARGV[2])
return 1
"""


class RedisTaskManager(TaskManager):
    def __init__(
        self,
        max_concurrent_tasks: int,
        redis_url: str,
        max_queued_tasks: int = 100,
    ):
        self.redis_client = redis.Redis.from_url(redis_url)
        super().__init__(max_concurrent_tasks, max_queued_tasks=max_queued_tasks)

    def create_queue(self):
        return "task_queue"

    def resume_queued_tasks(self):
        """Dispatch persisted queue entries when a new API process starts.

        Queued Redis items survive a restart, but no worker calls check_queue
        until an active task finishes. Fill each available local worker slot so
        an idle process does not leave old requests stuck in processing.
        """
        for _ in range(max(0, self.max_concurrent_tasks - self.current_tasks)):
            self.check_queue()

    def enqueue(self, task: Dict):
        self.redis_client.rpush(self.queue, self._serialize_task(task))

    def enqueue_new_task(self, task: Dict):
        # Manager locks are process-local. Keep capacity validation and admission
        # in one Redis operation so other API workers cannot claim the same slot.
        admitted = self.redis_client.eval(
            _ADMIT_QUEUED_TASK_SCRIPT,
            1,
            self.queue,
            self.max_queued_tasks,
            self._serialize_task(task),
        )
        if not admitted:
            raise TaskQueueFullError("task queue is full, please try again later")

    @staticmethod
    def _serialize_task(task: Dict) -> str:
        task_with_serializable_params = task.copy()
        # task.copy() only copies the outermost dictionary; if you directly rewrite nested kwargs, the caller will
        # Synchronously replace the held VideoParams with dict. Subsequent logs or retries may still read the original task.
        # So the kwargs are copied here separately to ensure there are no unexpected side effects during the serialization process.
        task_kwargs = task.get("kwargs", {})
        task_with_serializable_params["kwargs"] = task_kwargs.copy()

        if "params" in task_kwargs and isinstance(
            task_kwargs["params"], (VideoParams, AudioRequest, SubtitleRequest)
        ):
            task_with_serializable_params["kwargs"]["params"] = task_kwargs[
                "params"
            ].model_dump(warnings=False)

        # Convert the function object to its name
        task_with_serializable_params["func"] = task["func"].__name__
        return json.dumps(task_with_serializable_params)

    def dequeue(self):
        # Loop instead of a single pop-up: a task may meet the verification rules at that time when it is added to the queue, but the verification rules are the same as
        # FUNC_MAP members will change with deployment (for example, VideoParams adds ge=1 constraint, a certain entrance
        # function is removed), so entries written according to the old schema or that cannot be parsed may remain in the queue.
        # lpop is a destructive operation. Once popped, it cannot be put back; this task has been permanently removed from the queue.
        # Removed, can't pretend it's still there. Instead of letting the exception be thrown upward from here (check_queue is called with a lock
        # In this method, the exception will crash the worker thread along the finally path of task_done → run_task; thereafter
        # If there is no task running, no one will call check_queue again, and the subsequent tasks in the queue will be permanently
        # stop at processing), it is better to discard it in place and continue to try the next one to get "an available task"
        # Or the queue is indeed empty." This agreement remains.
        while True:
            task_json = self.redis_client.lpop(self.queue)
            # Only if lpop does not pop up anything, it means the queue is empty. Same for empty string (or empty bytes)
            # is an unavailable entry. There may be available tasks behind it, so you need to discard it as shown below.
            # Path instead of returning directly as "end of queue".
            if task_json is None:
                return None

            task_info = None
            try:
                task_info = json.loads(task_json)
                # Convert function name back to function object. Cannot be used when the name is missing or no longer in FUNC_MAP
                # Index directly, otherwise KeyError will bypass the discarding strategy for params below.
                task_info["func"] = FUNC_MAP[task_info["func"]]
                task_kwargs = task_info["kwargs"]
                if not isinstance(task_kwargs, dict):
                    raise ValueError("queued task has no keyword argument mapping")
                # When args is missing as a whole, the default value of check_queue will be used; write it as null or something other than
                # The shape of the array will cause check_queue to throw a TypeError when expanding `*args`, where
                # Will re-enqueue the entry and allow the exception to escape the worker thread, which must be stopped here first.
                if not isinstance(task_info.get("args", []), list):
                    raise ValueError("queued task positional arguments are not a list")
                # A persisted request can outlive a callable's signature. Fail
                # it before dispatch: Python argument-binding errors happen
                # before start() enters its pipeline failure handler.
                inspect.signature(task_info["func"]).bind(
                    *task_info.get("args", []), **task_kwargs
                )
            except (TypeError, ValueError, KeyError) as e:
                logger.error(f"dropping unusable queued task: {e}")
                # Consistent with the following params verification failure path: as long as the available task_id can be read,
                # This task that has permanently left the queue will fail to converge, otherwise the API/WebUI will always display
                # It's processing. The payload itself cannot be parsed, or task_id is not a string
                # (such as JSON array), there are no records that can be written back and can only be discarded - replace the non-string
                # Directly handing it to patch_task will cause redis to throw DataError, which in turn will interrupt the discard cycle.
                stale_kwargs = (
                    task_info.get("kwargs") if isinstance(task_info, dict) else None
                )
                task_id = (
                    stale_kwargs.get("task_id")
                    if isinstance(stale_kwargs, dict)
                    else None
                )
                if isinstance(task_id, str) and task_id:
                    sm.state.patch_task(
                        task_id,
                        state=const.TASK_STATE_FAILED,
                        failed_stage="dequeue",
                        error=f"discarded stale queued task: {e}",
                    )
                continue

            if "params" in task_kwargs and isinstance(task_kwargs["params"], dict):
                try:
                    params_model = VideoParams
                    # /audio and /subtitle use short-form request models without
                    # video_subject. Preserve those schemas at the queue boundary;
                    # full VideoParams can also stop at either of these stages.
                    if "video_subject" not in task_kwargs["params"]:
                        if task_kwargs.get("stop_at") == "audio":
                            params_model = AudioRequest
                        elif task_kwargs.get("stop_at") == "subtitle":
                            params_model = SubtitleRequest
                    task_kwargs["params"] = params_model(**task_kwargs["params"])
                except ValidationError as e:
                    logger.error(
                        "dropping queued task with params that fail current "
                        f"request model validation (queued under an older, more "
                        f"permissive schema, or corrupted): {e}"
                    )
                    # The task status record is created before joining the queue, and the default is processing; if only
                    # Discard this queue item without touching the status record. The API/WebUI will always display the task status.
                    #Run, never fails. Use patch_task instead of update_task,
                    # In this way, if the user has deleted this task, we will not create it back again.
                    # Same as above when task_id is not a string: there is no record that can be written back, and status update is skipped.
                    task_id = task_kwargs.get("task_id")
                    if isinstance(task_id, str) and task_id:
                        sm.state.patch_task(
                            task_id,
                            state=const.TASK_STATE_FAILED,
                            failed_stage="dequeue",
                            error=f"discarded stale queued task: {e}",
                        )
                    continue

            return task_info

    def is_queue_empty(self):
        return self.redis_client.llen(self.queue) == 0

    def queue_size(self):
        return self.redis_client.llen(self.queue)
