"""Video material cache statistics, preview and cleaning services."""

from __future__ import annotations

import os
import re
import stat
import time
from dataclasses import dataclass
from typing import Iterator

from loguru import logger

from app.utils import utils


# Online material uses the MD5 of the URL as a stable filename. Cache management only accepts this naming format to avoid
# Videos, description files or other business files that users mistakenly put in the directory will be deleted as cache.
_VIDEO_CACHE_FILE_PATTERN = re.compile(r"^vid-[0-9a-f]{32}\.mp4$")
# save_video writes to this same-directory name before atomically publishing a
# validated clip. A forced process exit can leave one behind, so include only
# aged temporary files in cache cleanup and never touch an active download.
_VIDEO_CACHE_TEMP_FILE_PATTERN = re.compile(
    r"^\.vid-[0-9a-f]{32}-[a-z0-9_]+\.mp4$"
)
_SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class VideoCacheStats:
    """Lightweight statistical results for cache directories, containing only file system metadata."""

    file_count: int = 0
    total_size: int = 0
    oldest_mtime: float | None = None
    newest_mtime: float | None = None


@dataclass(frozen=True)
class VideoCacheCleanupResult:
    """The execution result of a cleanup allows partial file deletion to fail."""

    deleted_count: int = 0
    deleted_size: int = 0
    failed_count: int = 0


@dataclass(frozen=True)
class _VideoCacheEntry:
    """The smallest file information saved during the scanning phase to avoid opening or parsing the video during cleaning."""

    path: str
    name: str
    size: int
    mtime: float
    device: int
    inode: int
    mtime_ns: int


def video_cache_dir() -> str:
    """Returns the default video cache directory for project management."""

    return os.path.realpath(utils.storage_dir("cache_videos"))


def _iter_video_cache_entries(include_temp: bool = False) -> Iterator[_VideoCacheEntry]:
    """
    Sequentially scan the first level of the default cache directory.

    The purpose of using ``os.scandir`` is to reuse the metadata returned by directory traversal when the cache reaches tens of thousands of files.
    Avoid querying file types again after ``Path.iterdir``. There is no recursion, no video opening, and no call
    FFmpeg, so the time consumption is mainly linearly related to the number of files, not to the total video capacity.
    """

    cache_dir = video_cache_dir()
    try:
        entries = os.scandir(cache_dir)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(
            f"failed to scan video cache directory: path={cache_dir}, error={exc}"
        )
        return

    with entries:
        for entry in entries:
            if not _VIDEO_CACHE_FILE_PATTERN.fullmatch(entry.name) and not (
                include_temp and _VIDEO_CACHE_TEMP_FILE_PATTERN.fullmatch(entry.name)
            ):
                continue

            try:
                # Symbolic links are not followed to ensure cleanup logic does not cross default cache directory boundaries.
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat_result = entry.stat(follow_symlinks=False)
            except OSError as exc:
                logger.warning(
                    f"failed to inspect video cache file: file={entry.name}, error={exc}"
                )
                continue

            yield _VideoCacheEntry(
                path=entry.path,
                name=entry.name,
                size=stat_result.st_size,
                mtime=stat_result.st_mtime,
                device=stat_result.st_dev,
                inode=stat_result.st_ino,
                mtime_ns=stat_result.st_mtime_ns,
            )


def _is_cleanup_candidate(
    entry: _VideoCacheEntry,
    max_age_days: int | None,
    now: float,
) -> bool:
    if (
        _VIDEO_CACHE_TEMP_FILE_PATTERN.fullmatch(entry.name)
        and entry.mtime >= now - _SECONDS_PER_DAY
    ):
        return False
    if max_age_days is None:
        return True
    return entry.mtime < now - max_age_days * _SECONDS_PER_DAY


def _validate_max_age_days(max_age_days: int | None) -> None:
    """Invalid cleanup parameters should be reliably rejected even if the cache directory is empty."""
    if max_age_days is None:
        return
    if (
        isinstance(max_age_days, bool)
        or not isinstance(max_age_days, int)
        or max_age_days <= 0
    ):
        raise ValueError("max_age_days must be a positive integer or None")


def get_video_cache_stats(max_age_days: int | None = None) -> VideoCacheStats:
    """
    Count all caches, or preview purgeable caches whose modification time is older than a specified number of days.

    ``max_age_days=None`` means caching all. The statistical process only reads the size and modification time of the directory entry.
    Video content is not read, so even if the total cache capacity is large it does not incur I/O proportional to the capacity.
    """

    _validate_max_age_days(max_age_days)
    now = time.time()
    file_count = 0
    total_size = 0
    oldest_mtime = None
    newest_mtime = None

    for entry in _iter_video_cache_entries(include_temp=True):
        if not _is_cleanup_candidate(entry, max_age_days, now):
            continue
        file_count += 1
        total_size += entry.size
        oldest_mtime = (
            entry.mtime if oldest_mtime is None else min(oldest_mtime, entry.mtime)
        )
        newest_mtime = (
            entry.mtime if newest_mtime is None else max(newest_mtime, entry.mtime)
        )

    return VideoCacheStats(
        file_count=file_count,
        total_size=total_size,
        oldest_mtime=oldest_mtime,
        newest_mtime=newest_mtime,
    )


def clean_video_cache(max_age_days: int | None = None) -> VideoCacheCleanupResult:
    """
    Cleans the default video cache and returns aggregated results that can be displayed to the user.

    There may be a long interval between the page preview and the actual click to clean, so you must rescan and judge when executing.
    Old candidate lists cannot be reused. Deletion adopts file-by-file fault tolerance: records when a single file is occupied or has insufficient permissions
    Warn and continue to avoid one abnormal file among hundreds of files causing the entire cleanup to fail.
    """

    _validate_max_age_days(max_age_days)
    now = time.time()
    logger.info(
        f"start cleaning video cache: max_age_days={max_age_days}"
    )

    candidate_count = 0
    candidate_size = 0
    deleted_count = 0
    deleted_size = 0
    failed_count = 0
    cache_dir = video_cache_dir()

    # Delete while scanning without keeping the complete candidate list in memory. Even if the directory grows to hundreds of thousands of files,
    # The additional memory during the cleanup process remains constant; use unified now during execution to avoid long cleanup processes.
    # Cutoff times keep moving creating an unpredictable range of candidates.
    for entry in _iter_video_cache_entries(include_temp=True):
        if not _is_cleanup_candidate(entry, max_age_days, now):
            continue
        candidate_count += 1
        candidate_size += entry.size
        try:
            # entry.path comes from the first level scandir of the default directory; verify the parent directory and the sum again before deleting
            # File name to prevent accidentally expanding the deletable range when modifying the scanning logic in the future.
            if (
                os.path.realpath(os.path.dirname(entry.path)) != cache_dir
                or not (
                    _VIDEO_CACHE_FILE_PATTERN.fullmatch(entry.name)
                    or _VIDEO_CACHE_TEMP_FILE_PATTERN.fullmatch(entry.name)
                )
                or os.path.islink(entry.path)
            ):
                raise ValueError("cache file is outside the managed directory")
            try:
                current = os.stat(entry.path, follow_symlinks=False)
            except FileNotFoundError:
                # Another cleanup already removed the scanned candidate.
                continue
            if not stat.S_ISREG(current.st_mode):
                continue
            if entry.device and current.st_dev != entry.device:
                continue
            if entry.inode and current.st_ino != entry.inode:
                continue
            if (current.st_mtime_ns, current.st_size) != (entry.mtime_ns, entry.size):
                # A downloader may atomically publish a new file at this name
                # after the scan. Do not delete the fresh replacement.
                continue
            os.unlink(entry.path)
            deleted_count += 1
            deleted_size += entry.size
        except (OSError, ValueError) as exc:
            failed_count += 1
            logger.warning(
                f"failed to delete video cache file: file={entry.name}, error={exc}"
            )

    logger.info(
        "finished cleaning video cache: "
        f"candidates={candidate_count}, candidate_bytes={candidate_size}, "
        f"deleted={deleted_count}, deleted_bytes={deleted_size}, failed={failed_count}"
    )
    return VideoCacheCleanupResult(
        deleted_count=deleted_count,
        deleted_size=deleted_size,
        failed_count=failed_count,
    )
