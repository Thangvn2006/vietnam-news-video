import copy
import errno
import os
import shutil
import socket
import tempfile
import threading
from contextlib import contextmanager

import toml
from loguru import logger

from app import __version__

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
config_file = f"{root_dir}/config.toml"
_CONTAINER_CGROUP_MARKERS = ("docker", "containerd", "kubepods", "libpod", "podman")
_DOCKER_HOST_GATEWAY_NAME = "host.docker.internal"
_config_save_lock = threading.RLock()
_pending_config_lock = threading.RLock()
_pending_config_updates = {}
_pending_config_save_requested = False
_pending_config_flush_scheduled = False
_MISSING = object()
_DELETE = object()
_UTF8_BOM = "\ufeff"


class _SynchronizedConfig(dict):
    """Keep the usage of dict unchanged, and make runtime configuration write operations obey the same lock."""

    def __setitem__(self, key, value):
        # Streamlit will rewrite the current control value back to the configuration every time the entire page is rerun. video task holding
        # When runtime_config_lock, if the value does not change, this writing has no side effects, and
        # The refreshed page should not be stuck in the middle of the form. Writes that actually change the configuration still go into the lower lock,
        # Therefore, you cannot switch providers, keys, or other global settings in the middle of a video being generated.
        current = super().get(key, _MISSING)
        if current is not _MISSING and current == value:
            return
        with _config_save_lock:
            super().__setitem__(key, value)

    def __delitem__(self, key):
        with _config_save_lock:
            super().__delitem__(key)

    def clear(self):
        if not self:
            return
        with _config_save_lock:
            super().clear()

    def pop(self, key, default=_MISSING):
        # ``pop(key, default)`` also does not change the configuration when key does not exist. WebUI usage
        # This way of writing expresses "adopting the default policy", which must be allowed to complete directly when refreshing.
        if key not in self:
            if default is _MISSING:
                raise KeyError(key)
            return default
        with _config_save_lock:
            if default is _MISSING:
                return super().pop(key)
            return super().pop(key, default)

    def setdefault(self, key, default=None):
        # Like __setitem__, setdefault for an existing key is a read-only operation. Return early
        # This allows page refreshes that only read the default configuration to be unaffected by long task configuration locks.
        current = super().get(key, _MISSING)
        if current is not _MISSING:
            return current
        with _config_save_lock:
            return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        changes = dict(*args, **kwargs)
        if all(
            (current := dict.get(self, key, _MISSING)) is not _MISSING
            and current == value
            for key, value in changes.items()
        ):
            return
        with _config_save_lock:
            super().update(changes)


def _pending_update_key(config_section, key):
    """Generate keys to be updated for in-process fixed configuration partitions."""
    return id(config_section), key


def update_config_nonblocking(config_section, key, value):
    """
    Non-blocking updates to the WebUI's runtime configuration.

    Video generation will hold ``runtime_config_lock`` to ensure that the same task will not be switched mid-execution
    Provider, key or voice configuration. The Streamlit control cannot wait for this long task lock when it changes.
    Otherwise the browser will appear to freeze the page. Update immediately when the lock is idle; only retain each configuration item when the lock is busy
    The latest value is applied uniformly when the current task releases the lock.

    Returning True means that the value has taken effect, False means that it has entered the queue to be updated.
    """
    # All updates are put into the same queue before trying to acquire the configuration lock. In this way, multiple pages can modify the same page at the same time.
    # When configuring items, the order of writing to the queue is the final order, and there will be no earlier threads after acquiring the lock.
    # Values already queued by newer threads were mistakenly deleted.
    with _pending_config_lock:
        _pending_config_updates[_pending_update_key(config_section, key)] = (
            config_section,
            key,
            copy.deepcopy(value),
        )

    acquired = _config_save_lock.acquire(blocking=False)
    if not acquired:
        # The caller will usually request a save at the end of this Streamlit rerun, but cannot rely on this step
        # Must be implemented. For example, if the page is abnormal in the middle or the update happens to occur when the task exits the save phase, it still needs to be
        # There is a background refresh thread to ensure that the queued value finally takes effect.
        _schedule_deferred_config_flush()
        return False

    try:
        _apply_pending_config_updates_locked()
        return config_section.get(key, _MISSING) == value
    finally:
        _config_save_lock.release()


def delete_config_nonblocking(config_section, key):
    """
    Non-blocking deletion of WebUI configuration items.

    "Use default" requires actually removing the configuration item, rather than writing an empty string. Video task occupation configuration
    When locked, the deletion intent overwrites previously queued updates to the same configuration item and is executed after the task ends.
    """
    with _pending_config_lock:
        _pending_config_updates[_pending_update_key(config_section, key)] = (
            config_section,
            key,
            _DELETE,
        )

    acquired = _config_save_lock.acquire(blocking=False)
    if not acquired:
        _schedule_deferred_config_flush()
        return False

    try:
        _apply_pending_config_updates_locked()
        return key not in config_section
    finally:
        _config_save_lock.release()


def _apply_pending_config_updates_locked():
    """Apply the latest configuration values staged by the WebUI while holding the configuration write lock."""
    with _pending_config_lock:
        updates = list(_pending_config_updates.values())
        _pending_config_updates.clear()
        # The pending update lock continues to be held while applying the configuration. The thread that reads the "current value + value to be updated" snapshot is thus
        # Only the complete status before or after application can be seen, and only half-updated configuration collections will not be read.
        for config_section, key, value in updates:
            if value is _DELETE:
                config_section.pop(key, None)
            else:
                config_section[key] = value
    return bool(updates)


def snapshot_config_with_pending(config_section):
    """
    Returns a valid snapshot of the configuration partition, merging WebUI updates that have not yet been applied.

    The global configuration cannot be rewritten while the video task is locked, but the user can still prepare the next piece of content. LLM request
    After using this snapshot, the Provider, model and key just selected in the interface will participate in the new request, and at the same time
    Does not change the video task being performed.
    """
    with _pending_config_lock:
        snapshot = dict(config_section)
        section_id = id(config_section)
        for (pending_section_id, key), (_, _, value) in _pending_config_updates.items():
            if pending_section_id != section_id:
                continue
            if value is _DELETE:
                snapshot.pop(key, None)
            else:
                snapshot[key] = copy.deepcopy(value)
    return snapshot


def _flush_pending_config_locked(*, suppress_save_errors):
    """Applies and saves all currently pending configurations while holding a configuration write lock."""
    global _pending_config_save_requested

    updates_applied = _apply_pending_config_updates_locked()
    with _pending_config_lock:
        save_requested = _pending_config_save_requested
        _pending_config_save_requested = False

    if not updates_applied and not save_requested:
        return True

    try:
        save_config()
        return True
    except Exception as exc:
        # The configuration in memory has been successfully applied. If the save fails, only the pending save mark will be retained. Video tasks should not
        # The modification failed because the configuration file is temporarily unwritable; the next page interaction will trigger saving again.
        with _pending_config_lock:
            _pending_config_save_requested = True
        if not suppress_save_errors:
            raise
        logger.exception(f"failed to save deferred runtime config: {exc}")
        return False


def _run_deferred_config_flush():
    """Wait for long tasks to release configuration locks and reliably clear configuration updates accumulated during the period."""
    global _pending_config_flush_scheduled

    while True:
        with _config_save_lock:
            flush_succeeded = _flush_pending_config_locked(
                suppress_save_errors=True
            )

        with _pending_config_lock:
            has_pending_work = bool(
                _pending_config_updates or _pending_config_save_requested
            )
            if not flush_succeeded or not has_pending_work:
                _pending_config_flush_scheduled = False
                return


def _schedule_deferred_config_flush():
    """It is guaranteed that there is at most one background thread waiting to refresh the configuration at the same time."""
    global _pending_config_flush_scheduled

    with _pending_config_lock:
        if _pending_config_flush_scheduled:
            return
        _pending_config_flush_scheduled = True

    try:
        threading.Thread(
            target=_run_deferred_config_flush,
            name="mpt-config-flush",
            daemon=True,
        ).start()
    except Exception:
        # No worker owns this reservation when construction/start fails.
        # Leave queued updates and the save request intact so a later call
        # can schedule another flush rather than silently waiting forever.
        with _pending_config_lock:
            _pending_config_flush_scheduled = False
        raise


def try_save_config():
    """
    The WebUI configuration is saved non-blockingly. When the lock is busy, it will be saved after the current long task is completed.

    Normal API, CLI, and maintenance scripts can still call ``save_config`` to obtain the original blocking write semantics;
    Only Streamlit rerun uses this function to prevent the page from being unresponsive for a long time while waiting for video tasks.
    """
    global _pending_config_save_requested

    with _pending_config_lock:
        _pending_config_save_requested = True

    acquired = _config_save_lock.acquire(blocking=False)
    if not acquired:
        _schedule_deferred_config_flush()
        return False

    try:
        return _flush_pending_config_locked(suppress_save_errors=False)
    finally:
        _config_save_lock.release()


@contextmanager
def runtime_config_lock():
    """
    Prevent other WebUI sessions from overwriting the configuration during a complete operation that relies on global configuration.

    The current project binds the local loopback address by default, and the configuration is still a single-user global configuration. This lightweight lock mainly
    Protect long operations such as generation and listening to prevent another tab from switching providers or keys in the middle of the operation.
    """
    with _config_save_lock:
        # If the background refresh thread has not yet been scheduled when the previous short operation releases the lock, the new task must be read
        # The queue is applied before global configurations such as providers and keys. You cannot continue to use the old configuration to execute the entire pipeline.
        _flush_pending_config_locked(suppress_save_errors=True)
        try:
            yield
        finally:
            _flush_pending_config_locked(suppress_save_errors=True)


@contextmanager
def try_runtime_config_lock():
    """
    Attempts to acquire a runtime configuration lock and returns immediately whether successful.

    WebUI audition is a short operation triggered by the user and should not wait for several minutes while the background video task is locked.
    The caller can prompt the user to try again later when the lock is not acquired; after successfully acquiring the lock, the listening period can still be guaranteed.
    Provider, key and model configuration will not be modified by other sessions.
    """
    acquired = _config_save_lock.acquire(blocking=False)
    try:
        if acquired:
            _flush_pending_config_locked(suppress_save_errors=True)
        yield acquired
    finally:
        if acquired:
            _flush_pending_config_locked(suppress_save_errors=True)
            _config_save_lock.release()


def is_running_in_container(
    dockerenv_path: str = "/.dockerenv",
    containerenv_path: str = "/run/.containerenv",
    cgroup_path: str = "/proc/1/cgroup",
) -> bool:
    """
    Determine whether the current process is running in the container.

    This judgment is mainly used for Ollama default address selection:
    - When running on a normal local machine, `localhost` points to the user's machine itself;
    - In the Docker container, `localhost` points to the container itself and accesses the host Ollama
      Usually you need to use `host.docker.internal`.

    You cannot just determine whether `/proc/1/cgroup` exists, because ordinary Linux will also have this file.
    Here, True is only returned when an explicit container tag is detected to avoid accidentally hurting non-Docker Linux users.
    Parameters are reserved as injectable paths to facilitate unit testing to cover different operating environments.
    """
    if os.path.isfile(dockerenv_path) or os.path.isfile(containerenv_path):
        return True

    try:
        with open(cgroup_path, mode="r", encoding="utf-8") as fp:
            cgroup_content = fp.read().lower()
    except OSError:
        return False

    return any(marker in cgroup_content for marker in _CONTAINER_CGROUP_MARKERS)


def _can_resolve_hostname(hostname: str) -> bool:
    try:
        socket.gethostbyname(hostname)
    except OSError:
        return False
    return True


def _decode_linux_route_gateway(hex_gateway: str) -> str:
    # The Gateway in /proc/net/route is hexadecimal little endian, for example, 010011AC means
    # 172.17.0.1. It is parsed separately here in order to use it when native Linux Docker does not have
    # host.docker.internal DNS record, it can also try to access the host on the container's default gateway.
    if len(hex_gateway) != 8:
        raise ValueError("invalid gateway length")

    octets = [
        str(int(hex_gateway[index : index + 2], 16)) for index in range(6, -1, -2)
    ]
    return ".".join(octets)


def get_container_default_gateway_ip(route_path: str = "/proc/net/route") -> str:
    """
    Read the default gateway IP in the Linux container.

    Docker Desktop usually provides `host.docker.internal`, but native Linux Docker
    This DNS name is not necessarily provided by default. The default gateway can usually be used to access host services.
    Covert address; if the user's Ollama only listens to 127.0.0.1, the user still needs to let
    Ollama listens to the host network card or configure `ollama_base_url` manually.
    """
    try:
        with open(route_path, mode="r", encoding="utf-8") as fp:
            route_lines = fp.readlines()
    except OSError:
        return ""

    for line in route_lines[1:]:
        fields = line.strip().split()
        if len(fields) < 3:
            continue

        destination = fields[1]
        gateway = fields[2]
        if destination != "00000000" or gateway == "00000000":
            continue

        try:
            return _decode_linux_route_gateway(gateway)
        except ValueError:
            logger.warning(f"invalid container gateway route entry: {line.strip()}")
            return ""

    return ""


def get_default_ollama_base_url() -> str:
    """
    Returns Ollama's default OpenAI-compatible base_url.

    Users will not go here when explicitly configuring `ollama_base_url`; this only handles "not configured"
    Best Default". The container points to the host by default, and the normal local machine runs to localhost by default.
    """
    if not is_running_in_container():
        return "http://localhost:11434/v1"

    if _can_resolve_hostname(_DOCKER_HOST_GATEWAY_NAME):
        return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"

    gateway_ip = get_container_default_gateway_ip()
    if gateway_ip:
        logger.info(
            "host.docker.internal is not resolvable, fallback to container "
            f"default gateway for Ollama: {gateway_ip}"
        )
        return f"http://{gateway_ip}:11434/v1"

    logger.warning(
        "failed to resolve host.docker.internal and container default gateway; "
        "fallback to host.docker.internal for Ollama"
    )
    return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"


def _load_toml_config(config_path: str):
    """
    Loads TOML and is compatible with duplicate UTF-8 BOMs that Windows editors may write.

    ``utf-8-sig`` will only remove a BOM at the beginning of the file. Some Windows editors or
    The decompression and saving process may write the BOM again, causing the second invisible character to enter TOML
    The parser reports an error on the first line. Here only a read-only normalization is done after the standard parsing fails,
    Do not write back the original file to avoid accidentally overwriting the API Key already filled in by the user.
    """
    try:
        return toml.load(config_path)
    except (toml.TomlDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "load config failed, retry with UTF-8 BOM compatibility: "
            f"path={config_path}, error={type(exc).__name__}: {exc}"
        )

    try:
        with open(config_path, mode="r", encoding="utf-8-sig") as fp:
            config_content = fp.read()

        normalized_content = config_content.lstrip(_UTF8_BOM)
        removed_bom_count = len(config_content) - len(normalized_content)
        if removed_bom_count:
            logger.warning(
                "removed repeated UTF-8 BOM characters while loading config: "
                f"path={config_path}, count={removed_bom_count}"
            )
        return toml.loads(normalized_content)
    except (toml.TomlDecodeError, UnicodeDecodeError) as exc:
        logger.error(
            "config file is not valid TOML after UTF-8 BOM normalization: "
            f"path={config_path}, error={type(exc).__name__}: {exc}"
        )
        raise



def _initialize_config_from_example(example_file):
    """Publish a complete first-boot config without replacing another writer."""
    fd, temp_path = tempfile.mkstemp(
        prefix=".config-init-", suffix=".toml.tmp",
        dir=os.path.dirname(os.path.abspath(config_file)),
    )
    try:
        with os.fdopen(fd, "wb") as destination:
            with open(example_file, "rb") as source:
                shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            if os.name == "nt":
                # Windows rename refuses an existing destination. POSIX rename
                # overwrites it, so use an exclusive hard-link publication there.
                os.rename(temp_path, config_file)
            else:
                os.link(temp_path, config_file)
        except FileExistsError:
            return False
        except OSError as exc:
            raise OSError(
                exc.errno,
                "cannot safely initialize config.toml; copy config.example.toml "
                "to config.toml before starting the application",
            ) from exc
        return True
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            # A successful Windows rename consumed the temporary path.
            pass


def load_config():
    # Docker may create an empty directory when a missing config.toml is bind-mounted.
    # Only remove that empty placeholder; a nonempty directory may contain user data.
    if os.path.isdir(config_file):
        try:
            os.rmdir(config_file)
        except FileNotFoundError:
            # Another initializer already removed this empty bind-mount stub.
            pass
        except OSError as exc:
            # Another initializer may already have replaced the empty Docker
            # stub with its complete regular file. Accept only that race, not
            # permission failures or a still-present/nonempty directory.
            if not (
                isinstance(exc, NotADirectoryError) and os.path.isfile(config_file)
            ):
                raise IsADirectoryError(
                    f"{config_file} is a directory and cannot be used as the config file; "
                    "move or rename it, then restart"
                ) from exc

    if not os.path.isfile(config_file):
        example_file = f"{root_dir}/config.example.toml"
        if os.path.isfile(example_file):
            if _initialize_config_from_example(example_file):
                logger.info("copy config.example.toml to config.toml")

    logger.info(f"load config from file: {config_file}")

    return _load_toml_config(config_file)


def save_config():
    """
    Atomic saving of runtime configuration.

    Different sessions of Streamlit may trigger configuration saves at similar times. When overwriting config.toml directly,
    Another thread may read the TOML content that was only partially written. In-process reentrant lock serialization is used here
    Save, first write to the temporary file in the same directory, and then atomically replace the target file through os.replace.

    Docker Desktop single file bind mount will use config.toml itself as the mount point.
    The Linux kernel does not allow replacement of mount points via rename/replace, so EBUSY is returned.
    In this scenario, the file can only be overwritten in place within the lock; other exceptions are still thrown to avoid covering permissions, disk
    Or the path is wrong.

    This still retains the project's existing single-user global configuration semantics without introducing an additional complex multi-user configuration system;
    Mainly used to avoid damaging configuration files during multi-tab pages or fast reruns.
    """
    with _config_save_lock:
        config_to_save = dict(_cfg)
        config_to_save["app"] = dict(app)
        config_to_save["azure"] = dict(azure)
        config_to_save["siliconflow"] = dict(siliconflow)
        config_to_save["minimax_tts"] = dict(minimax_tts)
        config_to_save["elevenlabs"] = dict(elevenlabs)
        config_to_save["chatterbox"] = dict(chatterbox)
        config_to_save["kokoro"] = dict(kokoro)
        config_to_save["fish_audio"] = dict(fish_audio)
        config_to_save["voxcpm"] = dict(voxcpm)
        config_to_save["ui"] = dict(ui)
        serialized_config = toml.dumps(config_to_save)

        # Save will be called at the end of a complete rerun of WebUI. Return directly when the content has not changed to avoid each time
        # Clicking on a normal control will cause a disk write and fsync.
        try:
            with open(config_file, mode="r", encoding="utf-8") as f:
                if f.read() == serialized_config:
                    _cfg.clear()
                    _cfg.update(config_to_save)
                    return
        except (OSError, UnicodeError):
            pass

        temp_path = ""
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=".config-",
                suffix=".toml.tmp",
                dir=root_dir,
            )
            with os.fdopen(fd, mode="w", encoding="utf-8") as f:
                f.write(serialized_config)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(temp_path, config_file)
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise

                logger.warning(
                    "atomic config replacement is unavailable for the mounted "
                    f"file, fallback to in-place write: {config_file}"
                )
                with open(config_file, mode="w", encoding="utf-8") as f:
                    f.write(serialized_config)
                    f.flush()
                    os.fsync(f.fileno())
            _cfg.clear()
            _cfg.update(config_to_save)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)


_cfg = load_config()
app = _SynchronizedConfig(_cfg.get("app", {}))
whisper = _cfg.get("whisper", {})
proxy = _cfg.get("proxy", {})
azure = _SynchronizedConfig(_cfg.get("azure", {}))
siliconflow = _SynchronizedConfig(_cfg.get("siliconflow", {}))
minimax_tts = _SynchronizedConfig(_cfg.get("minimax_tts", {}))
elevenlabs = _SynchronizedConfig(_cfg.get("elevenlabs", {}))
chatterbox = _SynchronizedConfig(_cfg.get("chatterbox", {}))
kokoro = _SynchronizedConfig(_cfg.get("kokoro", {}))
fish_audio = _SynchronizedConfig(_cfg.get("fish_audio", {}))
voxcpm = _SynchronizedConfig(_cfg.get("voxcpm", {}))
ui = _SynchronizedConfig(
    _cfg.get(
        "ui",
        {
            "hide_log": False,
        },
    )
)

hostname = socket.gethostname()

log_level = _cfg.get("log_level", "DEBUG")
listen_host = _cfg.get("listen_host", "0.0.0.0")
listen_port = _cfg.get("listen_port", 8080)
project_name = _cfg.get("project_name", "VietNamNewsVideo")
project_description = _cfg.get(
    "project_description",
    "<a href='https://github.com/harry0703/VietNamNewsVideo'>https://github.com/harry0703/VietNamNewsVideo</a>",
)
project_version = _cfg.get("project_version", __version__)
reload_debug = False

app["redis_host"] = os.getenv(
    "MPT_APP_REDIS_HOST",
    os.getenv("REDIS_HOST", app.get("redis_host", "localhost")),
)

ffmpeg_path = app.get("ffmpeg_path", "")
if ffmpeg_path and os.path.isfile(ffmpeg_path):
    os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg_path

logger.info(f"{project_name} v{project_version}")
