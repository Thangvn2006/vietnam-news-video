import itertools
from concurrent.futures import ThreadPoolExecutor
import io
import math
import os
import random
import gc
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from contextlib import ExitStack, contextmanager, redirect_stdout
from functools import lru_cache
from typing import Callable, List
from loguru import logger
import numpy as np
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    afx,
)
from moviepy.video.tools.subtitles import SubtitlesClip
from PIL import Image, ImageDraw, ImageFont

from uuid import uuid4
from app.config import config
from app.models import const
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoFitMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import bgm as bgm_service
from app.services import video_template
from app.services.utils import video_effects
from app.utils import file_security, logging_utils, utils

class SubClippedVideoClip:
    def __init__(
        self,
        file_path,
        start_time=None,
        end_time=None,
        width=None,
        height=None,
        duration=None,
        source_file_path=None,
    ):
        self.file_path = file_path
        self.start_time = start_time
        self.end_time = end_time
        self.width = width
        self.height = height
        self.source_file_path = source_file_path or file_path
        if duration is None:
            self.duration = end_time - start_time
        else:
            self.duration = duration

    def __str__(self):
        return f"SubClippedVideoClip(file_path={self.file_path}, start_time={self.start_time}, end_time={self.end_time}, duration={self.duration}, width={self.width}, height={self.height})"


audio_codec = "aac"
# The ffmpeg/AAC combination in Docker is more prone to audio quality fluctuations under the default configuration.
# Here, the audio bitrate is explicitly increased to avoid obvious distortion caused by the default value being too low during the production stage.
audio_bitrate = "192k"
fps = 30
# When FFmpeg splices/transcodes at the frame rate, the final duration may be tens of milliseconds shorter than the theoretical duration read by MoviePy.
# Here, a small safety margin is left for the video material to avoid a black screen or black screen at the end of the audio due to frame rounding.
# Stuttering or no picture in the last short paragraph of narration.
_VIDEO_DURATION_SAFETY_MARGIN = 0.1
_MIN_MATERIAL_DIMENSION = 480
# Messaging apps and some encoders will round down the screen size. For example, WhatsApp will round down the 9:16
# The material is compressed to 478x850, which is two pixels less than 480. Directly pressing the 480 hard card will cause all such materials to be
# Discarded and ultimately failed overall with "no valid materials found". Leave a small tolerance here,
# It can not only pass the material that is slightly lower than the threshold due to rounding, but also still block the real low-definition material.
_MIN_DIMENSION_TOLERANCE = 10
# By default, fragments are processed serially to avoid a sudden increase in CPU and memory usage after old users upgrade.
# You can increase the number of concurrencies by yourself when you need to speed up, but each channel will start an independent encoding task.
_CLIP_PROCESSING_CONCURRENCY = 1


def _get_clip_processing_concurrency() -> int:
    try:
        concurrency = int(config.app.get("video_clip_concurrency", _CLIP_PROCESSING_CONCURRENCY))
    except (TypeError, ValueError):
        concurrency = _CLIP_PROCESSING_CONCURRENCY
    return max(1, min(8, concurrency))
_DEFAULT_VIDEO_CODEC = "libx264"
# There is no stage log during ffmpeg concatenation of fragments, `subprocess.run` is blocked until the process exits, and the splicing is time-consuming.
# The log shows "no output". Survival information is recorded here at intervals to facilitate distinguishing between encoding and stuck.
_FFMPEG_CONCAT_HEARTBEAT_SECONDS = 30.0
# MoviePy also does not have any logs when writing the final film. A one-minute vertical video requires several encodings on the CPU.
# minutes. Following the splicing phase, survival information is recorded at intervals.
_STAGE_HEARTBEAT_SECONDS = 30.0
_DEFAULT_FFMPEG_CONCAT_TIMEOUT_SECONDS = 3600
_SUBTITLE_SPRING_DURATION_SECONDS = 0.18
_MIN_SUBTITLE_SPRING_SCALE = 0.05
_MAX_SUBTITLE_SPRING_SCALE = 1.35
_SUPPORTED_VIDEO_CODECS = (
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_mf",
    "h264_videotoolbox",
)
_runtime_disabled_video_codecs = set()


def _get_subtitle_spring_scale(time_seconds: float, duration_seconds: float) -> float:
    """Returns the scaling ratio used by the subtitle bounce animation at the specified time point."""
    if duration_seconds <= 0 or time_seconds >= duration_seconds:
        return 1.0

    progress = max(0.0, min(time_seconds / duration_seconds, 1.0))
    scale = 1.0 - math.exp(-6.0 * progress) * math.cos(2.5 * math.pi * progress)
    return max(
        _MIN_SUBTITLE_SPRING_SCALE,
        min(scale, _MAX_SUBTITLE_SPRING_SCALE),
    )


def _scale_subtitle_frame_on_canvas(frame: np.ndarray, scale: float) -> np.ndarray:
    """
    Scale the title frame or transparency mask around the center while maintaining the same canvas dimensions.

    MoviePy saves subtitle color frames and transparency masks separately. The bounce animation must be used completely for both
    The same scaling and cropping, otherwise the first frame of the animation will treat the transparent area as a black text outline and synthesize it into the video
    On. The two-dimensional array represents the mask with a value of 0 to 1, and the three-dimensional array represents the RGB/RGBA color frame.
    """
    if frame.ndim not in (2, 3):
        raise ValueError("subtitle frame must be a 2D mask or 3D color frame")

    height, width = frame.shape[:2]
    scaled_width = max(1, int(round(width * scale)))
    scaled_height = max(1, int(round(height * scale)))
    offset = ((width - scaled_width) // 2, (height - scaled_height) // 2)

    if frame.ndim == 2:
        # MoviePy masks use floating point numbers from 0 to 1, and Pillow’s L mode uses 0 to 255; after conversion
        # Then restore the original type and scope to ensure that the transparency semantics of CompositeVideoClip remain unchanged.
        mask_image = Image.fromarray(
            np.clip(frame * 255.0, 0, 255).astype(np.uint8)
        )
        resized_mask = mask_image.resize(
            (scaled_width, scaled_height),
            Image.Resampling.BILINEAR,
        )
        mask_canvas = Image.new("L", (width, height), 0)
        mask_canvas.paste(resized_mask, offset)
        return (np.asarray(mask_canvas) / 255.0).astype(frame.dtype, copy=False)

    if frame.shape[2] not in (3, 4):
        raise ValueError("subtitle color frame must use RGB or RGBA channels")
    color_image = Image.fromarray(frame)
    resized_color = color_image.resize(
        (scaled_width, scaled_height),
        Image.Resampling.BILINEAR,
    )
    background = (0, 0, 0, 0) if frame.shape[2] == 4 else (0, 0, 0)
    color_canvas = Image.new(color_image.mode, (width, height), background)
    color_canvas.paste(resized_color, offset)
    return np.asarray(color_canvas).astype(frame.dtype, copy=False)


def _apply_subtitle_spring_animation(clip, subtitle_duration: float):
    """Scale the subtitle color frame and mask at the same time to avoid the black first frame of the bouncing animation."""
    animation_duration = min(
        _SUBTITLE_SPRING_DURATION_SECONDS,
        max(0.0, subtitle_duration),
    )
    if animation_duration <= 0:
        return clip

    def transform_frame(get_frame, time_seconds):
        frame = get_frame(time_seconds)
        scale = _get_subtitle_spring_scale(time_seconds, animation_duration)
        if scale == 1.0:
            return frame
        return _scale_subtitle_frame_on_canvas(frame, scale)

    # apply_to=["mask"] is the key to the fix: MoviePy only handles color frames by default, the old implementation therefore
    # Briefly retain the original size of the mask when each subtitle appears, and display a black text outline.
    return clip.transform(transform_frame, apply_to=["mask"])


def _get_required_video_duration(audio_duration: float) -> float:
    """
    Returns the target duration of video material splicing.

    Usage scenario: When compositing a video, the duration of the material needs to cover the narration audio. Just do it "just equal to"
    When it comes to audio duration, FFmpeg may make the final video slightly shorter due to frame rate rounding, so it adds one
    Lightweight margin. The function is independent, which facilitates testing and subsequent adjustment of the margin size based on actual feedback.
    """
    return max(0.0, float(audio_duration) + _VIDEO_DURATION_SAFETY_MARGIN)


def is_material_resolution_acceptable(width: int, height: int) -> bool:
    """
    Determine whether the material resolution is sufficient for compositing.

    The nominal minimum is 480x480, but `_MIN_DIMENSION_TOLERANCE` pixels below that are allowed,
    Dimensions rounded down by compatible encoders/messaging apps (e.g. 478x850 for WhatsApp).
    """
    min_dimension = _MIN_MATERIAL_DIMENSION - _MIN_DIMENSION_TOLERANCE
    return width >= min_dimension and height >= min_dimension


def _prioritize_unique_source_clips(
    subclipped_items: List[SubClippedVideoClip],
    concat_mode: VideoConcatMode,
    source_usage: dict[str, int] | None = None,
    source_groups: dict[str, str] | None = None,
) -> List[SubClippedVideoClip]:
    """
    Prioritize that each source material appears only once to reduce the probability of the same material appearing repeatedly in the finished film.

    Online materials often encounter the situation of "a long video being cut into multiple short clips". The old logic is
    In random mode, all short clips are directly scrambled, resulting in multiple slices of the same source video.
    Distributed at the beginning and middle, users will perceive the material as repeated. This function only adjusts the fragment order:
    Play the longest clip in each source file first, and use the remaining clips as a backup; when the total duration of the material is insufficient,
    Subsequent segments are still allowed to complete the audio length to avoid damaging the success rate of video generation. Prioritize the longest
    The purpose of clipping is to avoid randomly selecting fragmentary short clips at the end of the video, resulting in premature reuse even though there is enough material.
    """
    if not subclipped_items:
        return []

    concat_mode_value = getattr(concat_mode, "value", concat_mode)
    if concat_mode_value != VideoConcatMode.random.value:
        if source_usage is None:
            return subclipped_items
        if not source_groups:
            return sorted(
                subclipped_items,
                key=lambda item: source_usage.get(item.source_file_path, 0),
            )
        # Keep keyword rounds in order while rotating candidates within each keyword.
        groups = {}
        for item in subclipped_items:
            key = source_groups.get(item.source_file_path, item.source_file_path)
            groups.setdefault(key, []).append(item)
        for items in groups.values():
            items.sort(key=lambda item: source_usage.get(item.source_file_path, 0))
        return [
            item
            for row in itertools.zip_longest(*groups.values())
            for item in row
            if item is not None
        ]

    grouped_items: dict[str, list[SubClippedVideoClip]] = {}
    for item in subclipped_items:
        grouped_items.setdefault(item.source_file_path, []).append(item)

    primary_items = []
    overflow_items = []
    for items in grouped_items.values():
        primary_item = max(items, key=lambda item: item.duration)
        primary_items.append(primary_item)
        overflow_items.extend(item for item in items if item is not primary_item)

    random.shuffle(primary_items)
    random.shuffle(overflow_items)
    if source_usage is not None:
        # Stable sorting retains randomness among equally used sources.
        primary_items.sort(key=lambda item: source_usage.get(item.source_file_path, 0))
        overflow_items.sort(key=lambda item: source_usage.get(item.source_file_path, 0))
    logger.info(
        "prioritized unique video materials, "
        f"sources: {len(grouped_items)}, "
        f"primary clips: {len(primary_items)}, "
        f"fallback clips: {len(overflow_items)}"
    )
    return primary_items + overflow_items


def get_ffmpeg_binary():
    """
    Compatible with callers that historically read FFmpeg paths directly from the video service.

    The real parsing logic has been extracted to `app.utils.utils.get_ffmpeg_binary()`, video, voice
    The same set of priorities should be reused with subsequent new links; thin packaging is retained here to avoid external scripts or
    Old tests raised AttributeError when importing `app.services.video.get_ffmpeg_binary` directly.
    """
    return utils.get_ffmpeg_binary()


def _get_configured_video_codec() -> str:
    """
    Read the user configured video encoder.

    This configuration is for advanced users trying to enable hardware such as NVENC/AMF/QSV/VideoToolbox
    Encoding. Only a fixed whitelist is deliberately allowed here to avoid users filling in errors after opening any FFmpeg parameters.
    Parameters cause the output format to be uncontrollable, and even cause the generation task to fail in subsequent stages.
    """
    configured_codec = str(
        config.app.get("video_codec", _DEFAULT_VIDEO_CODEC) or _DEFAULT_VIDEO_CODEC
    ).strip()
    if configured_codec not in _SUPPORTED_VIDEO_CODECS:
        logger.warning(
            f"unsupported video codec configured: {configured_codec}, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC
    return configured_codec


@lru_cache(maxsize=16)
def _ffmpeg_encoder_exists(ffmpeg_binary: str, codec: str) -> bool:
    """
    Check whether the current FFmpeg declares support for the specified encoder.

    This can only prove that FFmpeg includes this encoder when compiling, but cannot prove the current machine hardware and driver.
    Must be available. Therefore, it will still fall back to libx264 when the actual encoding fails.
    """
    try:
        result = subprocess.run(
            [ffmpeg_binary, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {str(exc)}"
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "failed to inspect ffmpeg encoders, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}: {(result.stderr or result.stdout or '').strip()}"
        )
        return False
    return codec in result.stdout


def _get_effective_video_codec(preferred_codec: str | None = None) -> str:
    """
    Returns the actual video encoder used this time.

    When the user selects a hardware encoder, first perform FFmpeg encoder list detection; if this process already
    If the actual encoding fails, it will be rolled back directly to avoid repeated failures for every segment in a task.
    """
    selected_codec = preferred_codec or _get_configured_video_codec()
    if selected_codec == _DEFAULT_VIDEO_CODEC:
        return _DEFAULT_VIDEO_CODEC

    if selected_codec in _runtime_disabled_video_codecs:
        logger.warning(
            f"video codec {selected_codec} was disabled after a runtime failure, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    ffmpeg_binary = utils.get_ffmpeg_binary()
    if not _ffmpeg_encoder_exists(ffmpeg_binary, selected_codec):
        logger.warning(
            f"ffmpeg encoder {selected_codec} is not available, "
            f"fallback to {_DEFAULT_VIDEO_CODEC}"
        )
        return _DEFAULT_VIDEO_CODEC

    return selected_codec


def _disable_runtime_video_codec(codec: str, reason: str):
    if codec == _DEFAULT_VIDEO_CODEC:
        return
    _runtime_disabled_video_codecs.add(codec)
    logger.warning(
        f"video codec {codec} failed, fallback to {_DEFAULT_VIDEO_CODEC}. "
        f"reason: {reason}"
    )


def _get_temp_audio_dir(output_dir: str) -> str:
    """
    Return the directory to use for MoviePy's temporary audio file.

    On Windows, Windows Defender can lock files written to the task output
    directory while scanning them, causing MoviePy to fail with a
    PermissionError (WinError 32) on the TEMP_MPY_wvf_snd temp file and
    leaving the final MP4 at 0 bytes.  Using the system temp directory
    sidesteps the scan without changing behaviour on other platforms.

    On Linux/macOS/Docker the output directory is returned unchanged so
    existing behaviour is preserved.
    """
    if sys.platform == "win32":
        return tempfile.gettempdir()
    return output_dir


def _fallback_write_videofile(clip, output_file: str, failed_codec: str, reason: str, **kwargs):
    """
    After the hardware encoding fails, try again with libx264. The hardware encoder will be disabled only if the retry is successful.

    The reason why FFmpeg fails on Windows is more complicated: it may be that the graphics card/driver does not support it, or it may be the output
    General IO issues such as file occupation, directory permissions, and anti-virus software interception. When only libx264 can successfully write,
    Only then can we determine that the original failure most likely came from the hardware encoder itself to avoid accidentally damaging subsequent tasks.
    """
    clip.write_videofile(output_file, codec=_DEFAULT_VIDEO_CODEC, **kwargs)
    _disable_runtime_video_codec(failed_codec, reason)
    return _DEFAULT_VIDEO_CODEC


def _write_videofile_with_codec_fallback(
    clip, output_file: str, codec: str, atomic_output: bool = False, **kwargs
):
    """
    Write the video using the specified encoder, and automatically try again with libx264 if it fails.

    Whether a hardware encoder is available depends not only on FFmpeg, but also on the graphics card, driver, and current running environment.
    The generation task cannot fail as a whole because the advanced encoder is unavailable, so the fallback is handled centrally here.
    """
    if atomic_output:
        # Final videos can be downloaded by path while they are being rendered.
        # Keep both failed encodes and in-progress writes away from that path.
        output_dir = os.path.dirname(os.path.abspath(output_file))
        descriptor, temp_output = tempfile.mkstemp(
            prefix=f".{os.path.basename(output_file)}.",
            suffix=os.path.splitext(output_file)[1] or ".mp4",
            dir=output_dir,
        )
        os.close(descriptor)
        os.unlink(temp_output)
        try:
            used_codec = _write_videofile_with_codec_fallback(
                clip, temp_output, codec, **kwargs
            )
            os.replace(temp_output, output_file)
            return used_codec
        finally:
            try:
                os.unlink(temp_output)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    f"failed to remove temporary final video: {temp_output}, "
                    f"error: {exc}"
                )

    effective_codec = _get_effective_video_codec(codec)
    try:
        clip.write_videofile(output_file, codec=effective_codec, **kwargs)
        return effective_codec
    except Exception as exc:
        if effective_codec == _DEFAULT_VIDEO_CODEC:
            raise
        return _fallback_write_videofile(
            clip,
            output_file,
            failed_codec=effective_codec,
            reason=str(exc),
            **kwargs,
        )


def _escape_ffmpeg_concat_path(file_path: str) -> str:
    # concat demuxer uses single quotes to wrap the path, and the single quotes in the path need to be escaped first.
    return file_path.replace("'", "'\\''")


def _format_ffmpeg_concat_path(file_path: str) -> str:
    """
    Generate paths in the concat demuxer file list.

    FFmpeg official documentation requires that special characters and spaces in concat list need to be escaped; Windows
    Backslashes in absolute paths are also easily parsed as escape characters. Here it is uniformly converted into forward slash form.
    Let `C:\\Users\\...` become `C:/Users/...`, and then process single quotes, compatible with macOS/Linux.
    """
    absolute_path = os.path.abspath(file_path)
    return _escape_ffmpeg_concat_path(absolute_path.replace("\\", "/"))


def _describe_concat_output_progress(output_file: str) -> str:
    """Returns a human-readable description of the current size of the output file, used for splicing heartbeat logs."""
    try:
        size = os.path.getsize(output_file)
    except OSError:
        # When the output file has not yet been created, it must also be safely downgraded without affecting the splicing itself.
        return "output size not available"
    return f"output size: {size / (1024 * 1024):.2f} MB"


@contextmanager
def _stage_heartbeat(description: str):
    """
    Keep alive logs recorded at intervals during a time-consuming phase that has no logs of its own.

    The heartbeat thread is bound to the thread entering this stage, and the log will appear in the WebUI panel of the task to which it belongs.
    When the stage ends or an exception is thrown, stop and wait for the heartbeat thread to exit to avoid continuing after the stage has ended.
    Write "still running".
    """
    started_at = time.monotonic()
    stop_event = threading.Event()

    def log_heartbeat() -> None:
        while not stop_event.wait(_STAGE_HEARTBEAT_SECONDS):
            logger.info(
                f"{description} still running: "
                f"elapsed={time.monotonic() - started_at:.0f}s"
            )

    reporter = threading.Thread(
        target=logging_utils.bind_log_scope(log_heartbeat), daemon=True
    )
    reporter.start()
    try:
        yield
    finally:
        stop_event.set()
        reporter.join(timeout=1)


def _report_clip_progress(
    progress_callback: Callable[[float], None] | None,
    covered_duration: float,
    required_duration: float,
) -> None:
    """Convert the covered film duration into a ratio of 0~1 and notify the caller."""
    if progress_callback is None:
        return
    fraction = 1.0
    if required_duration > 0:
        fraction = min(1.0, covered_duration / required_duration)
    try:
        progress_callback(fraction)
    except Exception as exc:
        # Progress is just a display of information. A failed callback (e.g. the status backend is temporarily unavailable) must not allow the already
        # The processed fragments are invalid.
        logger.warning(
            "failed to report clip processing progress: "
            f"error={type(exc).__name__}, detail={exc}"
        )


def _run_concat_with_heartbeat(command: list[str], output_file: str):
    """
    Blocks and waits for ffmpeg to complete, during which survival logs are recorded at intervals.

    There is no stage log when ffmpeg concatenates fragments, and `subprocess.run` buffers the output until the process exits, which makes splicing time-consuming.
    It appears as "no output" in the log. Record the waiting time and output file size to distinguish between still encoding and stuck.
    """
    started_at = time.monotonic()
    stop_event = threading.Event()

    def log_heartbeat() -> None:
        while not stop_event.wait(_FFMPEG_CONCAT_HEARTBEAT_SECONDS):
            logger.info(
                "ffmpeg concat still running: "
                f"elapsed={time.monotonic() - started_at:.0f}s, "
                f"{_describe_concat_output_progress(output_file)}"
            )

    reporter = threading.Thread(
        target=logging_utils.bind_log_scope(log_heartbeat), daemon=True
    )
    reporter.start()
    try:
        configured_timeout = config.app.get(
            "ffmpeg_concat_timeout_seconds", _DEFAULT_FFMPEG_CONCAT_TIMEOUT_SECONDS
        )
        try:
            timeout_seconds = float(configured_timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("ffmpeg_concat_timeout_seconds must be positive") from exc
        if (
            isinstance(configured_timeout, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("ffmpeg_concat_timeout_seconds must be positive")

        try:
            # subprocess.run kills and waits for FFmpeg on timeout, so a stalled
            # encoder cannot leave an orphaned child or a permanently active task.
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"ffmpeg concat exceeded {timeout_seconds:g} seconds"
            ) from exc
    finally:
        stop_event.set()


def concat_video_clips_with_ffmpeg(
    clip_files: List[str],
    output_file: str,
    threads: int,
    output_dir: str,
    max_duration: float | None = None,
):
    # Separate renders may share a directory. Each FFmpeg process must keep its
    # own manifest until all codec attempts finish, without overwriting or
    # deleting another render's list.
    concat_list_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="ffmpeg-concat-", suffix=".txt",
            dir=output_dir, delete=False,
        ) as fp:
            concat_list_file = fp.name
            for clip_file in clip_files:
                fp.write(f"file '{_format_ffmpeg_concat_path(clip_file)}'\n")
    except Exception:
        if concat_list_file:
            delete_files(concat_list_file)
        raise

    staged_output = None

    def build_command(codec: str) -> list[str]:
        command = [
            utils.get_ffmpeg_binary(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list_file,
            "-c:v",
            codec,
            "-threads",
            str(threads or 2),
            "-pix_fmt",
            "yuv420p",
        ]
        if max_duration is not None and max_duration > 0:
            command.extend(["-t", f"{max_duration:.3f}"])
        command.append(staged_output)
        return command

    def run_concat(codec: str):
        command = build_command(codec)
        # Use ffmpeg to concatenate and encode only once to avoid repeated re-encoding when merging MoviePy segment by segment.
        # This reduces the risk of image quality degradation and color shift. During the blocking wait period, the heartbeat log reflects that the task is still running.
        result = _run_concat_with_heartbeat(command, staged_output)
        if result.returncode != 0:
            error_message = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(error_message or "ffmpeg concat failed")
        return codec

    try:
        descriptor, staged_output = tempfile.mkstemp(
            prefix=".ffmpeg-concat-",
            suffix=os.path.splitext(output_file)[1] or ".mp4",
            dir=os.path.dirname(os.path.abspath(output_file)),
        )
        os.close(descriptor)
        effective_codec = _get_effective_video_codec()
        try:
            result_codec = run_concat(effective_codec)
        except TimeoutError:
            # A hung encoder is not evidence that another codec will work. Do
            # not spend a second timeout period retrying the same input.
            raise
        except Exception as exc:
            if effective_codec == _DEFAULT_VIDEO_CODEC:
                raise
            result_codec = run_concat(_DEFAULT_VIDEO_CODEC)
            _disable_runtime_video_codec(effective_codec, str(exc))
        # Failed attempts and in-progress output stay private until FFmpeg has
        # finished. Publication errors must not trigger another codec attempt.
        if os.path.getsize(staged_output) == 0:
            raise RuntimeError("ffmpeg concat produced no output")
        os.replace(staged_output, output_file)
        return result_codec
    finally:
        delete_files([concat_list_file, staged_output])


def _sanitize_image_file(image_path: str) -> str:
    # Although some local images can be opened by Pillow, they will be damaged due to corrupted EXIF/eXIf metadata.
    # ImageClip throws an exception directly during the parsing phase. Here, re-export a "clean image" and strip off the bad metadata.
    image_root, _ = os.path.splitext(image_path)
    sanitized_path = f"{image_root}.sanitized.png"

    with Image.open(image_path) as image:
        image.load()
        # Export to PNG uniformly to avoid the different metadata paths of JPEG/PNG from continuing to bring bad blocks.
        cleaned_image = Image.new(image.mode, image.size)
        cleaned_image.putdata(list(image.getdata()))
        cleaned_image.save(sanitized_path)

    return sanitized_path


def _open_image_clip_with_fallback(image_path: str):
    # Priority is given to opening the original image directly; if it fails due to damaged metadata, try to generate a copy without metadata.
    try:
        return ImageClip(image_path), image_path
    except Exception as exc:
        logger.warning(
            f"failed to open image directly, trying sanitized copy: {image_path}, error: {str(exc)}"
        )
        sanitized_path = _sanitize_image_file(image_path)
        return ImageClip(sanitized_path), sanitized_path


_moviepy_reader_open_lock = threading.Lock()


def _open_video_clip_quietly(video_path: str, audio: bool = False) -> VideoFileClip:
    """
    Open video files silently to prevent MoviePy 2.1.x from printing ffmpeg probe information directly to stdout.

    Background:
    The current dependent version of `FFMPEG_VideoReader` internally contains `print(self.infos)` and
    `print(ffmpeg command)`, will be output when reading the middle video without audio track
    `audio_found: False`. This is just input material metadata, it does not mean that the final film will have no audio.
    But it will mislead the WebUI/end user into thinking that the build failed.

    Implementation:
    1. Redirect stdout only within the short window that opens VideoFileClip;
    2. Default `audio=False`, because the original sound of the material does not need to be preserved in the video material stage of the project.
       The final audio will be mounted uniformly in the `generate_video()` stage;
    3. If the dependent library does output content, downgrade it to debug log to facilitate troubleshooting if necessary.
    """
    captured_stdout = io.StringIO()
    # redirect_stdout changes process-wide state. Overlapping reader opens can
    # restore each other's capture buffers instead of the original stdout.
    # Serialize this short construction window; clip processing stays parallel.
    with _moviepy_reader_open_lock:
        with redirect_stdout(captured_stdout):
            clip = VideoFileClip(video_path, audio=audio)

    moviepy_stdout = captured_stdout.getvalue().strip()
    if moviepy_stdout:
        logger.debug(
            "suppressed MoviePy video reader stdout for "
            f"{video_path}, chars: {len(moviepy_stdout)}"
        )

    return clip


def close_clip(clip):
    if clip is None:
        return
        
    try:
        # close main resources
        if hasattr(clip, 'reader') and clip.reader is not None:
            clip.reader.close()
            
        # close audio resources
        if hasattr(clip, 'audio') and clip.audio is not None:
            if hasattr(clip.audio, 'reader') and clip.audio.reader is not None:
                clip.audio.reader.close()
            del clip.audio
            
        # close mask resources
        if hasattr(clip, 'mask') and clip.mask is not None:
            if hasattr(clip.mask, 'reader') and clip.mask.reader is not None:
                clip.mask.reader.close()
            del clip.mask
            
        # handle child clips in composite clips
        if hasattr(clip, 'clips') and clip.clips:
            for child_clip in clip.clips:
                if child_clip is not clip:  # avoid possible circular references
                    close_clip(child_clip)
            
        # clear clip list
        if hasattr(clip, 'clips'):
            clip.clips = []
            
    except Exception as e:
        logger.error(f"failed to close clip: {str(e)}")
    
    del clip
    gc.collect()

def delete_files(files: List[str] | str):
    if isinstance(files, str):
        files = [files]

    # When looping through video, the same temporary clip path appears multiple times in the FFmpeg splice list.
    # Duplicates must be retained during splicing, but cleaning can only be deleted once; here, the duplicates are removed in the original order, so that all
    # The caller gets idempotent behavior and avoids continuously outputting FileNotFoundError after the first deletion is successful.
    unique_files = dict.fromkeys(file for file in files if file)
    for file in unique_files:
        try:
            os.remove(file)
        except FileNotFoundError:
            # Cleanup actions allow files that no longer exist, such as FFmpeg failed path or concurrent cleanup has
            # Recycle files; this is not a user-related issue and should not pollute the build log.
            continue
        except OSError as e:
            # Permissions, read-only file system or disk exceptions will leave real temporary files and keep warning
            # It is convenient to locate environmental problems based on specific paths and system errors.
            logger.warning(f"failed to delete temporary file {file}: {str(e)}")


def get_bgm_file(bgm_type: str = "random", bgm_file: str = ""):
    if not bgm_type:
        return ""

    if bgm_file:
        try:
            resolved_bgm_file = bgm_service.resolve_bgm_file(bgm_file)
        except ValueError as exc:
            # The bgm_file in the API request comes from user input and is only allowed to be parsed into user BGM or built-in
            # Song directory, preventing MoviePy from reading any server files such as configurations and keys.
            logger.warning(
                f"reject unsafe bgm file: {bgm_file}, error: {str(exc)}"
            )
            return ""
        return resolved_bgm_file

    if bgm_type == "random":
        files = bgm_service.list_bgm_files()
        # When the background music directory is empty, it will directly fall back to "no BGM" to avoid random.choice([]) throwing exceptions.
        if not files:
            logger.warning("no background music files found")
            return ""
        return random.choice(files)

    return ""


def _fit_clip_to_canvas(
    clip,
    *,
    target_width: int,
    target_height: int,
    fit_mode: VideoFitMode | str = VideoFitMode.cover,
):
    """Resize a clip to an exact canvas using cover/crop or contain/letterbox."""
    source_width, source_height = (int(value) for value in clip.size)
    target_width = int(target_width)
    target_height = int(target_height)
    if min(source_width, source_height, target_width, target_height) <= 0:
        raise ValueError(
            "video dimensions must be positive: "
            f"source={source_width}x{source_height}, "
            f"target={target_width}x{target_height}"
        )

    mode = VideoFitMode(fit_mode)
    if (source_width, source_height) == (target_width, target_height):
        return clip

    # Exact aspect-ratio matches do not need either a crop or a background.
    if source_width * target_height == source_height * target_width:
        return clip.resized(new_size=(target_width, target_height))

    width_scale = target_width / source_width
    height_scale = target_height / source_height

    if mode == VideoFitMode.cover:
        # ceil guarantees the resized clip covers the complete canvas despite
        # floating-point rounding. Any excess is removed symmetrically.
        scale_factor = max(width_scale, height_scale)
        resized_width = max(target_width, math.ceil(source_width * scale_factor))
        resized_height = max(target_height, math.ceil(source_height * scale_factor))
        resized_clip = clip.resized(new_size=(resized_width, resized_height))
        crop_x = max(0, (resized_width - target_width) // 2)
        crop_y = max(0, (resized_height - target_height) // 2)
        return resized_clip.cropped(
            x1=crop_x,
            y1=crop_y,
            width=target_width,
            height=target_height,
        )

    # contain preserves the legacy behavior: show the complete source frame,
    # centered over a black canvas when the aspect ratios differ.
    scale_factor = min(width_scale, height_scale)
    resized_width = max(1, min(target_width, int(source_width * scale_factor)))
    resized_height = max(1, min(target_height, int(source_height * scale_factor)))
    background = ColorClip(
        size=(target_width, target_height), color=(0, 0, 0)
    ).with_duration(clip.duration)
    resized_clip = clip.resized(
        new_size=(resized_width, resized_height)
    ).with_position("center")
    return CompositeVideoClip(
        [background, resized_clip], size=(target_width, target_height)
    ).with_duration(clip.duration)


def combine_videos(
    combined_video_path: str,
    video_paths: List[str],
    audio_file: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    video_transition_mode: VideoTransitionMode = None,
    max_clip_duration: int = 5,
    threads: int = 2,
    clip_speed: float = 1.0,
    video_fit_mode: VideoFitMode = VideoFitMode.cover,
    source_usage: dict[str, int] | None = None,
    source_groups: dict[str, str] | None = None,
    used_video_paths: List[str] | None = None,
    progress_callback: Callable[[float], None] | None = None,
) -> str:
    audio_clip = AudioFileClip(audio_file)
    try:
        # Here you only need to read the duration of the narration audio to determine the length of the material video splicing; it will not be used again later.
        # audio_clip. Close immediately after reading is completed to avoid early exit or abnormal path leakage of file handles.
        audio_duration = audio_clip.duration
    finally:
        close_clip(audio_clip)
    logger.info(f"audio duration: {audio_duration} seconds")
    logger.info(f"maximum clip duration: {max_clip_duration} seconds")
    required_video_duration = _get_required_video_duration(audio_duration)
    logger.info(
        f"required video duration: {required_video_duration:.2f} seconds "
        f"(audio duration + {_VIDEO_DURATION_SAFETY_MARGIN:.2f}s safety margin)"
    )

    # Compatible with the situation where the transition mode is not passed when calling the API directly, to avoid crashes when subsequently accessing .value.
    transition_value = getattr(video_transition_mode, "value", video_transition_mode)
    normalized_clip_speed = utils.normalize_clip_speed(clip_speed)
    if normalized_clip_speed != 1.0:
        # Only recording the final effective value once is convenient for locating the problem of normalization of API out-of-bounds parameters.
        # Also avoids repeatedly outputting the same logs in per-fragment hot paths.
        logger.info(f"clip playback speed: {normalized_clip_speed:.2f}x")
    # max_clip_duration restricts the final playback time in the finished film, not the source video reading time.
    # MoviePy plays 1.5 seconds of source footage at 0.5x speed and will get a 3 second clip, played at 2x speed
    # A 6 second source footage will also result in a 3 second clip. Therefore, the source duration must be deduced according to the speed before slicing; if
    # It still reads for 3 seconds before slowing down and cropping, but the next segment starts from the 3rd second of the source video and the middle is skipped.
    # 1.5 seconds of footage. This calculation also ensures that the source timelines at different speeds are continuous and non-overlapping.
    source_clip_duration = max_clip_duration * normalized_clip_speed
    output_dir = os.path.dirname(combined_video_path)

    aspect = VideoAspect(video_aspect)
    fit_mode = VideoFitMode(video_fit_mode)
    video_width, video_height = aspect.to_resolution()

    processed_clips = []
    subclipped_items = []
    video_duration = 0
    for video_path in video_paths:
        clip = None
        try:
            clip = _open_video_clip_quietly(video_path)
            clip_duration = float(clip.duration)
            clip_w, clip_h = clip.size
            if (
                not math.isfinite(clip_duration)
                or clip_duration <= 0
                or not all(
                    math.isfinite(float(dimension)) and float(dimension) > 0
                    for dimension in (clip_w, clip_h)
                )
            ):
                raise ValueError("invalid video duration or dimensions")
        except Exception as exc:
            logger.warning(
                f"skipping unreadable video source: path={video_path}, error={exc}"
            )
            continue
        finally:
            close_clip(clip)
        
        start_time = 0

        while start_time < clip_duration:
            end_time = min(start_time + source_clip_duration, clip_duration)

            # Keep all valid segments.
            # This will not lose the material that "the entire video itself is shorter than max_clip_duration".
            # It won’t swallow up the small piece of tail content left at the end of a long video.
            if end_time > start_time:
                subclipped_items.append(
                    SubClippedVideoClip(
                        file_path=video_path,
                        start_time=start_time,
                        end_time=end_time,
                        width=clip_w,
                        height=clip_h,
                        source_file_path=video_path,
                    )
                )

            start_time = end_time
            if video_concat_mode.value == VideoConcatMode.sequential.value:
                break

    subclipped_items = _prioritize_unique_source_clips(
        subclipped_items=subclipped_items,
        concat_mode=video_concat_mode,
        **({"source_usage": source_usage, "source_groups": source_groups}
           if source_usage is not None else {}),
    )
        
    logger.debug(f"total subclipped items: {len(subclipped_items)}")
    
    # Add downloaded clips over and over until the duration of the audio (max_duration) has been reached
    def process_one_clip(indexed_item):
        """Write out a source clip after trimming, changing speed, and transitioning. Returns None on failure."""
        index, subclipped_item = indexed_item
        source_clip = None
        clip = None
        clip_file = None
        try:
            logger.debug(
                f"processing clip {index + 1}: {subclipped_item.width}x{subclipped_item.height}, "
                f"source: {os.path.basename(subclipped_item.source_file_path)}"
            )
            source_clip = _open_video_clip_quietly(subclipped_item.file_path)
            clip = source_clip.subclipped(
                subclipped_item.start_time, subclipped_item.end_time
            )
            # Playback speed is a property of the material itself and should be applied before transition. This way Fade/Slide waits for one second to transition.
            # It will not follow the material speed to 0.5 seconds or 2 seconds; subsequent maximum duration cropping will continue as
            # A safe margin for floating point errors or abnormal material duration to ensure that the final clip does not exceed the configuration limit.
            if normalized_clip_speed != 1.0:
                clip = clip.with_speed_scaled(normalized_clip_speed)
            # Normalize every source clip before transitions are applied. In cover mode
            # the clip fills the canvas and the excess edges are cropped; contain keeps
            # the complete source frame and uses black bars for the unused area.
            clip_w, clip_h = clip.size
            if clip_w != video_width or clip_h != video_height:
                clip_ratio = clip.w / clip.h
                video_ratio = video_width / video_height
                logger.debug(
                    "resizing clip, "
                    f"source: {clip_w}x{clip_h}, ratio: {clip_ratio:.2f}, "
                    f"target: {video_width}x{video_height}, ratio: {video_ratio:.2f}, "
                    f"fit_mode: {fit_mode.value}"
                )
                clip = _fit_clip_to_canvas(
                    clip,
                    target_width=video_width,
                    target_height=video_height,
                    fit_mode=fit_mode,
                )

            shuffle_side = random.choice(["left", "right", "top", "bottom"])
            if transition_value == VideoTransitionMode.fade_in.value:
                clip = video_effects.fadein_transition(clip, 1)
            elif transition_value == VideoTransitionMode.fade_out.value:
                clip = video_effects.fadeout_transition(clip, 1)
            elif transition_value == VideoTransitionMode.slide_in.value:
                clip = video_effects.slidein_transition(clip, 1, shuffle_side)
            elif transition_value == VideoTransitionMode.slide_out.value:
                clip = video_effects.slideout_transition(clip, 1, shuffle_side)
            elif transition_value == VideoTransitionMode.slide_in_left.value:
                clip = video_effects.slidein_transition(clip, 1, "left")
            elif transition_value == VideoTransitionMode.slide_in_right.value:
                clip = video_effects.slidein_transition(clip, 1, "right")
            elif transition_value == VideoTransitionMode.slide_in_top.value:
                clip = video_effects.slidein_transition(clip, 1, "top")
            elif transition_value == VideoTransitionMode.slide_in_bottom.value:
                clip = video_effects.slidein_transition(clip, 1, "bottom")
            elif transition_value == VideoTransitionMode.pan_left.value:
                clip = video_effects.pan_left_transition(clip, 1)
            elif transition_value == VideoTransitionMode.pan_right.value:
                clip = video_effects.pan_right_transition(clip, 1)
            elif transition_value == VideoTransitionMode.zoom_in.value:
                clip = video_effects.zoomin_transition(clip, 1)
            elif transition_value == VideoTransitionMode.zoom_out.value:
                clip = video_effects.zoomout_transition(clip, 1)
            elif transition_value == VideoTransitionMode.shuffle.value:
                transition_funcs = [
                    lambda c: video_effects.fadein_transition(c, 1),
                    lambda c: video_effects.fadeout_transition(c, 1),
                    lambda c: video_effects.slidein_transition(c, 1, shuffle_side),
                    lambda c: video_effects.slideout_transition(c, 1, shuffle_side),
                    lambda c: video_effects.slidein_transition(c, 1, "left"),
                    lambda c: video_effects.slidein_transition(c, 1, "right"),
                    lambda c: video_effects.pan_left_transition(c, 1),
                    lambda c: video_effects.pan_right_transition(c, 1),
                    lambda c: video_effects.zoomin_transition(c, 1),
                    lambda c: video_effects.zoomout_transition(c, 1),
                ]
                shuffle_transition = random.choice(transition_funcs)
                clip = shuffle_transition(clip)

            if clip.duration > max_clip_duration:
                clip = clip.subclipped(0, max_clip_duration)

            # Write each candidate clip to a unique temporary file. Threads must not
            # share the same output path.
            # Distinct combinations can share a task/output directory. Reserve
            # an owned path so their encoders and cleanup never share a clip.
            with tempfile.NamedTemporaryFile(
                dir=output_dir or ".", prefix="temp-clip-", suffix=".mp4", delete=False
            ) as temporary_clip:
                clip_file = temporary_clip.name
            _write_videofile_with_codec_fallback(
                clip,
                clip_file,
                codec=_get_configured_video_codec(),
                logger=None,
                fps=fps,
            )

            clip_duration_saved = clip.duration
            processed_clip = SubClippedVideoClip(
                file_path=clip_file,
                duration=clip_duration_saved,
                width=clip_w,
                height=clip_h,
                source_file_path=subclipped_item.source_file_path,
            )
            clip_file = None
            return processed_clip
        except Exception as exc:
            logger.error(f"failed to process clip: {str(exc)}")
            return None
        finally:
            # The derived clip shares its FFmpeg reader with the source. If
            # subclipping itself failed, close the original source instead.
            close_clip(clip if clip is not None else source_clip)
            # MoviePy may leave a truncated MP4 even when encoding raises. It
            # was never returned, so concat cleanup cannot see it.
            # Close the reader first so Windows can remove the partial file.
            if clip_file:
                delete_files(clip_file)

    # Fragments are always processed in the thread pool. The fragment-by-fragment log is the only progress information at this stage and is bound to
    # WebUI's task log can only collect synthetic threads after they are launched.
    process_clip_in_task_scope = logging_utils.bind_log_scope(process_one_clip)
    clip_processing_workers = 1
    if len(subclipped_items) >= 2:
        clip_processing_workers = min(_get_clip_processing_concurrency(), len(subclipped_items))
    with ThreadPoolExecutor(
        max_workers=clip_processing_workers,
        thread_name_prefix="clip-process",
    ) as executor:
        next_candidate_index = 0
        while (
            next_candidate_index < len(subclipped_items)
            and video_duration < required_video_duration
        ):
            remaining_duration = required_video_duration - video_duration
            batch = []
            batch_duration = 0.0
            candidate_index = next_candidate_index
            while candidate_index < len(subclipped_items) and batch_duration < remaining_duration:
                subclipped_item = subclipped_items[candidate_index]
                source_duration = subclipped_item.end_time - subclipped_item.start_time
                output_duration = min(
                    max_clip_duration,
                    source_duration / normalized_clip_speed,
                )
                batch.append((candidate_index, subclipped_item))
                batch_duration += output_duration
                candidate_index += 1
            if not batch:
                break
            for processed_clip in executor.map(process_clip_in_task_scope, batch):
                if processed_clip is None:
                    continue
                processed_clips.append(processed_clip)
                video_duration += processed_clip.duration
                # Each piece of 4K material takes more than ten seconds to process, and segment-by-segment reporting covers the duration, logs and progress bar.
                # It can be seen that this stage is still advancing.
                logger.info(
                    f"processed clip {len(processed_clips)}: "
                    f"{video_duration:.1f} of {required_video_duration:.1f}s covered"
                )
                _report_clip_progress(
                    progress_callback, video_duration, required_video_duration
                )
            next_candidate_index = candidate_index
    
    # loop processed clips until the video duration covers the audio duration and the small safety margin.
    if video_duration < required_video_duration:
        logger.warning(
            f"video duration ({video_duration:.2f}s) is shorter than required duration "
            f"({required_video_duration:.2f}s), looping clips to match audio length."
        )
        base_clips = processed_clips.copy()
        for clip in itertools.cycle(base_clips):
            if video_duration >= required_video_duration:
                break
            processed_clips.append(clip)
            video_duration += clip.duration
        logger.info(
            f"video duration: {video_duration:.2f}s, audio duration: {audio_duration:.2f}s, "
            f"required duration: {required_video_duration:.2f}s, "
            f"looped {len(processed_clips)-len(base_clips)} clips"
        )
     
    # merge video clips progressively, avoid loading all videos at once to avoid memory overflow
    logger.info("starting clip merging process")
    if not processed_clips:
        if video_paths:
            raise RuntimeError("no readable video clips available for merging")
        logger.warning("no clips available for merging")
        return combined_video_path
    
    clip_files = [clip.file_path for clip in processed_clips]
    logger.info(f"concatenating {len(clip_files)} clips with ffmpeg")
    try:
        concat_video_clips_with_ffmpeg(
            clip_files=clip_files,
            output_file=combined_video_path,
            threads=threads,
            output_dir=output_dir,
            max_duration=audio_duration,
        )
        if used_video_paths is not None:
            # Exclude safety-margin clips that FFmpeg trims entirely from the output.
            elapsed = 0.0
            for clip in processed_clips:
                if elapsed >= audio_duration:
                    break
                used_video_paths.append(clip.source_file_path)
                elapsed += clip.duration
    finally:
        # FFmpeg failures and timeouts must not strand one encoded MP4 per clip.
        # Repeated clips share a path; delete_files already deduplicates them.
        delete_files(clip_files)
            
    logger.info("video combining completed")
    return combined_video_path


def wrap_text(text, max_width, font="Arial", fontsize=60):
    # Subtitle wrapping must be completed before actually creating the TextClip, otherwise MoviePy will only press the original text
    # Calculate rendering area. Here, PIL is used to measure the width according to the current font and font size, ensuring that each line is as wide as possible
    # Control it within the available width of the video to avoid large font sizes or long Chinese sentences from directly overflowing the screen.
    if "\n" in text:
        # Hard breaks in SRT text are separate layout lines. Measuring a token
        # across a newline makes Pillow count both lines as one wide string,
        # then character wrapping can split an otherwise fitting word.
        wrapped_lines = [
            wrap_text(line, max_width, font=font, fontsize=fontsize)
            for line in text.split("\n")
        ]
        return "\n".join(line for line, _ in wrapped_lines), sum(
            height for _, height in wrapped_lines
        )

    font = ImageFont.truetype(font, fontsize)
    max_width = int(max_width)

    # What getbbox() returns is the "visible ink height of the current glyph", not the font line height. For example, only
    # English characters without descendants such as A, m, n, etc. will lack descent. When there are multiple lines, this error will accumulate line by line.
    # Finally, the last line of TextClip is cut off by the canvas. ascent + descent comes from the font itself,
    # It is not affected by specific language and character combinations, and is consistent with MoviePy's baseline drawing model.
    ascent, descent = font.getmetrics()
    line_height = int(ascent + descent)
    if line_height <= 0:
        # Normal TrueType/OpenType fonts will not enter here; keep diagnostic logs and font size details,
        # Avoid generating zero-height subtitles after corrupted or unconventional fonts return abnormal metrics.
        logger.warning(
            "invalid subtitle font metrics, fallback to font size: "
            f"ascent={ascent}, descent={descent}, fontsize={fontsize}"
        )
        line_height = max(1, int(fontsize))

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        if not inner_text:
            return 0, line_height
        left, top, right, bottom = font.getbbox(inner_text)
        # The bbox is still suitable for measuring the actual width required for line wrapping; the height must always use the stable font line height.
        return right - left, line_height

    width, height = get_text_size(text)
    if width <= max_width:
        # SRT entries allow the author to manually wrap lines. Even if the entire text does not need to be wrapped again in width,
        # The canvas height must still be calculated based on the existing number of rows, otherwise the second and subsequent rows will be cropped.
        return text, (text.count("\n") + 1) * line_height

    def split_long_token(token):
        # When a token itself is too wide (common in long sentences without spaces in Chinese, or long words in English),
        # Degenerates into character-level splitting. The key point is: when a candidate is detected to be too wide, submit the previous one first
        # Current is still legal, and then the current character is put into the next line. Extra-wide characters cannot be stuffed back into the previous line.
        lines = []
        current = ""
        for char in token:
            candidate = f"{current}{char}"
            candidate_width, _ = get_text_size(candidate)
            if candidate_width <= max_width or not current:
                current = candidate
                continue
            lines.append(current)
            current = char
        if current:
            lines.append(current)
        return lines

    lines = []
    current = ""
    words = text.split(" ")
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        word_width, _ = get_text_size(word)
        if word_width <= max_width:
            current = word
        else:
            lines.extend(split_long_token(word))
            current = ""

    if current:
        lines.append(current)

    line_start_punctuation = "，。！？；：、,.!?;:)]}）】》」』”’"
    for index in range(1, len(lines)):
        # When a long Chinese sentence is split by characters, the last period, comma and other closing punctuation may be separated
        # Putting it on the next line causes the subtitle background to be abnormally raised, visually like a small dot falling on the main text.
        # below. Here, without redesigning the newline algorithm, the last word of the previous line is
        # Move it to the front of the punctuation line and let the punctuation follow the text display. It is compatible with common closed punctuation in Chinese and English.
        if not lines[index] or lines[index][0] not in line_start_punctuation:
            continue
        if len(lines[index - 1]) <= 1:
            continue

        candidate = f"{lines[index - 1][-1]}{lines[index]}"
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            lines[index] = candidate
            lines[index - 1] = lines[index - 1][:-1]

    result = "\n".join(line.strip() for line in lines if line.strip()).strip()
    # The height is subject to the final result. Explicit line breaks in the original text may be retained within a token,
    # At this time, the length of the temporary lines list is not equal to the number of lines actually rendered by MoviePy.
    height = (result.count("\n") + 1) * line_height
    return result, height


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    # The subtitle background color comes from API/WebUI parameters and may be empty or in irregular format. Here we only accept
    # #RRGGBB format, illegal values fall back to black to avoid exceptions thrown during the PIL rendering phase and interrupt the task.
    if isinstance(color, str) and color.startswith("#") and len(color) == 7:
        try:
            return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
        except ValueError:
            pass
    return (0, 0, 0)


def _rounded_subtitle_background_clip(
    width: int,
    height: int,
    color: str,
    alpha: int = 140,
    radius: int = 16,
) -> ImageClip:
    # The new subtitle background is only used when the user explicitly turns it on: draw a rounded semi-transparent base plate from an RGBA image,
    # Then hand it over to MoviePy as a transparent ImageClip to participate in the synthesis. In this way, the default path remains completely unchanged.
    # At the same time, you can experiment with softer subtitle visuals at a low cost.
    rgb = _hex_to_rgb(color)
    safe_alpha = max(0, min(255, int(alpha)))
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [0, 0, max(0, width - 1), max(0, height - 1)],
        radius=max(0, int(radius)),
        fill=(rgb[0], rgb[1], rgb[2], safe_alpha),
    )
    return ImageClip(np.array(img), transparent=True)


def _get_visible_center_position(
    text_clip: TextClip,
    container_width: int,
    container_height: int,
) -> tuple[int, int]:
    """
    Place the TextClip in the center of the background container according to the actual visible pixels of the text.

    MoviePy's TextClip creates a transparent canvas based on font line height and baseline. many fonts
    It can be seen that the glyph is not in the geometric center of this canvas, directly `with_position("center")`
    The entire transparent canvas will be centered, causing the subtitles to look higher or lower. Read here TextClip
    The transparent mask only calculates the offset based on the bbox that actually has pixels, so that the user can see the text
    Visually centered within subtitle background.
    """
    x = int(round((container_width - text_clip.w) / 2))
    y = int(round((container_height - text_clip.h) / 2))

    try:
        if text_clip.mask is None:
            return x, y

        mask_frame = text_clip.mask.get_frame(0)
        ys, _ = np.where(mask_frame > 0.01)
        if len(ys) == 0:
            return x, y

        visible_top = int(ys.min())
        visible_bottom = int(ys.max())
        visible_height = visible_bottom - visible_top + 1
        y = int(round((container_height - visible_height) / 2 - visible_top))
    except Exception as exc:
        logger.debug(f"failed to center subtitle text by visible mask: {str(exc)}")

    return x, y


def subtitle_colors_are_indistinguishable(params: VideoParams) -> bool:
    """Determine whether the subtitle text and background are the same color, and remind users that they may not be able to see the subtitles clearly."""
    if not params.subtitle_enabled or not params.text_background_color:
        return False

    def normalize_color(value):
        if isinstance(value, bool):
            return "#000000" if value else ""
        return str(value or "").strip().lower()

    text_color = normalize_color(params.text_fore_color)
    background_color = normalize_color(params.text_background_color)
    return bool(text_color and text_color == background_color)


@lru_cache(maxsize=64)
def _subtitle_font_supports_sample(font_path: str, sample: str) -> bool:
    """Checks whether the font contains the required glyphs for the sample text and caches the duplicate check results."""
    try:
        font = ImageFont.truetype(font_path, 30)
        missing_mask = font.getmask("\U0010ffff")
        missing_signature = (
            missing_mask.size,
            missing_mask.getbbox(),
            bytes(missing_mask),
        )
        for char in sample:
            char_mask = font.getmask(char)
            char_signature = (
                char_mask.size,
                char_mask.getbbox(),
                bytes(char_mask),
            )
            if char_mask.getbbox() is None or char_signature == missing_signature:
                return False
        return True
    except Exception as e:
        # Failure in font detection should not prevent users from building; keep logs for troubleshooting environment compatibility issues.
        logger.warning(f"failed to inspect subtitle font glyphs: {font_path}, {e}")
        return True


def subtitle_font_supports_text(font_path: str, text: str) -> bool:
    """Checks whether the font can draw the letters and numbers in the text, ignoring whitespace and punctuation."""
    sample = "".join(
        dict.fromkeys(
            char
            for char in str(text or "")
            if unicodedata.category(char)[0] in {"L", "N"}
        )
    )[:64]
    if not sample:
        return True
    return _subtitle_font_supports_sample(font_path, sample)


def generate_video(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_file: str,
    params: VideoParams,
    bgm_file_override: str | None = None,
) -> bool:
    """
    Synthesize the final video and return whether the background music processing is successful.

    The return value only describes the BGM processing status: True is returned when BGM is not requested or mixed successfully; requested
    BGM but returns False if loading, effects or mixing fails. Even if BGM fails, it will continue to output only
    The narrated video lets the task orchestration layer decide whether to show the user a degradation warning.
    """
    aspect = VideoAspect(params.video_aspect)
    video_width, video_height = aspect.to_resolution()

    logger.info(f"generating video: {video_width} x {video_height}")
    logger.info(f"  ① video: {video_path}")
    logger.info(f"  ② audio: {audio_path}")
    logger.info(f"  ③ subtitle: {subtitle_path}")
    logger.info(f"  ④ output: {output_file}")

    # https://github.com/harry0703/VietNamNewsVideo/issues/217
    # PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'final-1.mp4.tempTEMP_MPY_wvf_snd.mp3'
    # write into the same directory as the output file
    output_dir = os.path.dirname(output_file)

    font_path = ""
    if params.subtitle_enabled:
        if not params.font_name:
            params.font_name = "STHeitiMedium.ttc"
        # Although the API entrance is pre-checked, WebUI, CLI and internal calls can still directly enter the rendering layer;
        # Always verify fonts with real paths and must stay in resource/fonts, blocking absolute paths,
        # ../ traverses and points to symbolic links outside the directory, and then is opened by PIL/MoviePy.
        font_path = file_security.resolve_path_within_directory(
            utils.font_dir(), params.font_name
        )
        if os.name == "nt":
            font_path = font_path.replace("\\", "/")

        logger.info(f"  ⑤ font: {font_path}")

    def resolve_subtitle_background_color():
        # Compatible with historical parameters: `text_background_color` in the API may be a Boolean value,
        # Might also be an actual color string. Uniformly normalize here to avoid converting True/False
        # Unexpected rendering results occur after passing it directly to TextClip.
        if isinstance(params.text_background_color, bool):
            return "#000000" if params.text_background_color else None
        return params.text_background_color

    def create_text_clip(subtitle_item):
        params.font_size = int(params.font_size)
        params.stroke_width = int(params.stroke_width)
        phrase = subtitle_item[1]
        max_width = video_width * 0.9
        bg_color = resolve_subtitle_background_color()
        rounded_bg_enabled = bool(
            getattr(params, "rounded_subtitle_background", False) and bg_color
        )
        has_subtitle_background = bool(bg_color)
        # The rounded background is generated according to the actual width of the text, and the left and right spaces should be more restrained; the old rectangular background is still retained
        # Larger safety margins to prevent long subtitles from being edged or cropped in historical configurations.
        padding_ratio = 0.4 if rounded_bg_enabled else 0.6
        pad_x = int(params.font_size * padding_ratio) if has_subtitle_background else 0
        # Subtitle backgrounds need to leave clear padding on the left and right sides of the text. First subtract from the available width
        # padding and then wrap the line to avoid long English or large font size that just fills 90% of the video width.
        # The text is pasted to the edge of the background box and looks cropped. Ordinary rectangular background and rounded corner background
        # This logic is followed; subtitles without background maintain the original maximum width.
        text_max_width = max(1, int(max_width) - 2 * pad_x)
        wrapped_txt, txt_height = wrap_text(
            phrase,
            max_width=text_max_width,
            font=font_path,
            fontsize=params.font_size,
        )
        interline = int(params.font_size * 0.25)
        line_count = wrapped_txt.count("\n") + 1
        vertical_padding = int(params.font_size * 0.35)
        # Pillow/MoviePy will expand the stroke to the upper and lower sides of the glyph and include this part in each line
        # travel height. If you only add a stroke of white space outside the entire subtitle block, a thick stroke of multiple lines of text
        # Errors will still accumulate row by row. Here, the actual number of lines is included in the double-sided stroke space. By default, thin strokes only
        # Add a small amount of height, and "small font size + thick stroke + multiple lines" can be fully displayed.
        stroke_padding = int(params.stroke_width * 2 * line_count)
        text_clip_margin_y = max(
            int(params.font_size * 0.3), int(params.stroke_width * 2)
        )
        # MoviePy will automatically shrink the height of the text box under `method=label`. When encountering multi-line subtitles,
        # When using strokes or background colors, it is easy to cut off the lower half of the last line. Explicitly passed in here
        # A more conservative height, taking into account line spacing and extra top and bottom white space, to ensure subtitles
        # Both the background frame and the text itself can be fully rendered.
        clip_h = int(
            txt_height
            + vertical_padding
            + (interline * line_count)
            + stroke_padding
        )

        if rounded_bg_enabled:
            # The rounded background needs to fit the width of the text, rather than taking 90% of the width of the video. Use it here first
            # PIL measures the longest line of text and adds horizontal padding to avoid excessively wide padding for short subtitles.
            try:
                font = ImageFont.truetype(font_path, params.font_size)
                text_w = max(
                    int(font.getbbox(line)[2] - font.getbbox(line)[0])
                    for line in wrapped_txt.split("\n")
                )
            except Exception as exc:
                logger.warning(
                    f"failed to measure subtitle text width, fallback to max width: {str(exc)}"
                )
                text_w = int(max_width)

            box_w = max(1, min(int(max_width), text_w + 2 * pad_x))
            radius = max(8, int(params.font_size * 0.4))
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(box_w, None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            clip_h = max(clip_h, text_clip.h)
            bg_clip = _rounded_subtitle_background_clip(
                width=box_w,
                height=clip_h,
                color=bg_color,
                alpha=140,
                radius=radius,
            )
            text_position = _get_visible_center_position(text_clip, box_w, clip_h)
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=(box_w, clip_h),
            )
        elif bg_color:
            size = (
                int(max_width),
                clip_h,
            )
            text_clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=(int(max_width), None),
                text_align="center",
                margin=(0, text_clip_margin_y),
            )
            size = (size[0], max(size[1], text_clip.h))
            bg_clip = _rounded_subtitle_background_clip(
                width=size[0],
                height=size[1],
                color=bg_color,
                alpha=255,
                radius=0,
            )
            text_position = _get_visible_center_position(text_clip, size[0], size[1])
            _clip = CompositeVideoClip(
                [bg_clip, text_clip.with_position(text_position)],
                size=size,
            )
        else:
            size = (
                int(max_width),
                clip_h,
            )
            _clip = TextClip(
                text=wrapped_txt,
                font=font_path,
                font_size=params.font_size,
                color=params.text_fore_color,
                bg_color=None,
                stroke_color=params.stroke_color,
                stroke_width=params.stroke_width,
                interline=interline,
                size=size,
                text_align="center",
            )
        duration = subtitle_item[0][1] - subtitle_item[0][0]
        _clip = _clip.with_start(subtitle_item[0][0])
        _clip = _clip.with_end(subtitle_item[0][1])
        _clip = _clip.with_duration(duration)

        # The bounce animation is only enabled when the user explicitly selects it; the default is none and the original subtitle rendering path is completely used.
        anim_type = getattr(params, "subtitle_animation", "none")
        if anim_type in ("pop_spring", "spring", "pop"):
            _clip = _apply_subtitle_spring_animation(_clip, duration)

        if params.subtitle_position == "bottom":
            _clip = _clip.with_position(("center", video_height * 0.95 - _clip.h))
        elif params.subtitle_position == "top":
            _clip = _clip.with_position(("center", video_height * 0.05))
        elif params.subtitle_position in ("two_thirds_bottom", "two_thirds", "2/3_bottom"):
            # 2/3 from the bottom = 1/3 from the top: y = (video_height - _clip.h) * (1/3)
            y_two_thirds = (video_height - _clip.h) / 3.0
            _clip = _clip.with_position(("center", y_two_thirds))
        elif params.subtitle_position == "custom":
            # Ensure the subtitle is fully within the screen bounds
            margin = 10  # Additional margin, in pixels
            max_y = video_height - _clip.h - margin
            min_y = margin
            custom_y = (video_height - _clip.h) * (params.custom_position / 100)
            custom_y = max(
                min_y, min(custom_y, max_y)
            )  # Constrain the y value within the valid range
            _clip = _clip.with_position(("center", custom_y))
        else:  # center
            _clip = _clip.with_position(("center", "center"))
        return _clip

    # MoviePy's CompositeAudioClip.close() does not close the child AudioFileClip. Used here
    # ExitStack explicitly holds all raw file readers, ensuring success, subtitle exceptions, remix failures, and
    # Paths such as video writing failure can release the FFmpeg sub-process, especially to prevent Windows files from being occupied.
    with ExitStack() as clip_stack:
        source_video_clip = clip_stack.enter_context(
            _open_video_clip_quietly(video_path)
        )
        voice_source_clip = clip_stack.enter_context(AudioFileClip(audio_path))
        video_clip = source_video_clip
        audio_clip = voice_source_clip.with_effects(
            [afx.MultiplyVolume(params.voice_volume)]
        )

        def make_textclip(text):
            return TextClip(
                text=text,
                font=font_path,
                font_size=params.font_size,
            )

        text_clips = []
        if params.subtitle_enabled and subtitle_path and os.path.exists(subtitle_path):
            sub = clip_stack.enter_context(
                SubtitlesClip(
                    subtitles=subtitle_path,
                    encoding="utf-8",
                    make_textclip=make_textclip,
                )
            )
            for item in sub.subtitles:
                clip = create_text_clip(subtitle_item=item)
                text_clips.append(clip)

        # Overlay clips (Frame template, Brand Logo, Headline banner, and Source badge)
        overlay_clips = []
        if (
            getattr(params, "frame_enabled", True)
            and getattr(params, "frame_template", None)
            and os.path.exists(params.frame_template)
        ):
            try:
                frame_dur = getattr(params, "frame_duration", 0)
                actual_frame_dur = min(frame_dur, video_clip.duration) if frame_dur > 0 else video_clip.duration
                frame_img = video_template.create_frame_overlay_image(
                    frame_path=params.frame_template,
                    width=video_width,
                    height=video_height,
                    pos_x=getattr(params, "frame_x", 0.0),
                    pos_y=getattr(params, "frame_y", 0.0),
                )
                frame_temp_file = os.path.join(output_dir, f"frame_{uuid4().hex[:8]}.png")
                frame_img.save(frame_temp_file, "PNG")
                tmpl_clip = clip_stack.enter_context(
                    ImageClip(frame_temp_file).with_duration(actual_frame_dur)
                )
                overlay_clips.append(tmpl_clip)
            except Exception as exc:
                logger.warning(f"failed to overlay frame template: {exc}")

        # 1. Brand Logo / Custom Image overlay
        if (
            getattr(params, "logo_enabled", False)
            and getattr(params, "logo_file", None)
            and os.path.exists(params.logo_file)
        ):
            try:
                logo_dur = getattr(params, "logo_duration", 0)
                actual_logo_dur = min(logo_dur, video_clip.duration) if logo_dur > 0 else video_clip.duration
                logo_img = video_template.create_logo_overlay_image(
                    logo_path=params.logo_file,
                    width=video_width,
                    height=video_height,
                    position=getattr(params, "logo_position", "top_left"),
                    logo_width=getattr(params, "logo_size", 140),
                    pos_x=getattr(params, "logo_x", None),
                    pos_y=getattr(params, "logo_y", None),
                )
                logo_temp_file = os.path.join(output_dir, f"logo_{uuid4().hex[:8]}.png")
                logo_img.save(logo_temp_file, "PNG")
                logo_clip = clip_stack.enter_context(
                    ImageClip(logo_temp_file).with_duration(actual_logo_dur)
                )
                overlay_clips.append(logo_clip)
            except Exception as exc:
                logger.warning(f"failed to overlay brand logo: {exc}")

        # 2. Headline banner overlay
        if getattr(params, "headline_enabled", True):
            hl_title = (getattr(params, "headline_text", "") or params.video_subject or "").strip()
            if hl_title:
                try:
                    hl_dur = getattr(params, "headline_duration", 0)
                    actual_hl_dur = min(hl_dur, video_clip.duration) if hl_dur > 0 else video_clip.duration
                    hl_img = video_template.create_headline_banner_image(
                        headline_badge="",
                        headline_title=hl_title,
                        width=video_width,
                        height=video_height,
                        position=getattr(params, "headline_position", "top"),
                        pos_x=getattr(params, "headline_x", None),
                        pos_y=getattr(params, "headline_y", None),
                    )
                    hl_temp_file = os.path.join(output_dir, f"headline_{uuid4().hex[:8]}.png")
                    hl_img.save(hl_temp_file, "PNG")
                    hl_clip = clip_stack.enter_context(
                        ImageClip(hl_temp_file).with_duration(actual_hl_dur)
                    )
                    overlay_clips.append(hl_clip)
                except Exception as exc:
                    logger.warning(f"failed to overlay headline banner: {exc}")

        # 3. Source badge overlay
        if (
            getattr(params, "source_badge_enabled", False)
            and getattr(params, "source_badge_text", "")
        ):
            try:
                sb_dur = getattr(params, "source_badge_duration", 0)
                actual_sb_dur = min(sb_dur, video_clip.duration) if sb_dur > 0 else video_clip.duration
                badge_img = video_template.create_source_badge_image(
                    text=params.source_badge_text,
                    width=video_width,
                    height=video_height,
                    position=getattr(params, "source_badge_position", "top_right"),
                    pos_x=getattr(params, "source_badge_x", None),
                    pos_y=getattr(params, "source_badge_y", None),
                )
                badge_temp_file = os.path.join(output_dir, f"badge_{uuid4().hex[:8]}.png")
                badge_img.save(badge_temp_file, "PNG")
                badge_clip = clip_stack.enter_context(
                    ImageClip(badge_temp_file).with_duration(actual_sb_dur)
                )
                overlay_clips.append(badge_clip)
            except Exception as exc:
                logger.warning(f"failed to overlay source badge: {exc}")

        if overlay_clips or text_clips:
            video_clip = CompositeVideoClip([video_clip, *overlay_clips, *text_clips])
            clip_stack.callback(video_clip.close)


        bgm_enabled = bgm_service.should_use_bgm(
            params.bgm_type, params.bgm_volume
        )
        if not bgm_enabled and params.bgm_type:
            # All BGM sources share this short-circuit rule. Cannot parse random or when the volume is not greater than 0
            # Custom files also cannot load files returned by providers to avoid meaningless IO and remixing.
            logger.info(
                f"skipping background music because volume is not positive: "
                f"type={params.bgm_type}, volume={params.bgm_volume}"
            )

        # The provider soundtrack can be directly transferred into the corresponding file from the task orchestration layer. None means to use random/custom
        # BGM parsing, an empty string explicitly disables this BGM; but any source must pass the general volume rules first.
        bgm_file = ""
        if bgm_enabled:
            bgm_file = (
                bgm_file_override
                if bgm_file_override is not None
                else get_bgm_file(
                    bgm_type=params.bgm_type,
                    bgm_file=params.bgm_file,
                )
            )
        bgm_mix_succeeded = True
        if bgm_file:
            try:
                bgm_effects = [
                    afx.MultiplyVolume(params.bgm_volume),
                    afx.AudioFadeOut(3),
                ]
                # The random/customized music parsed in the service may be shorter than the final film and needs to be looped; the task layer
                # The file passed in via override indicates that the provider has completed duration adaptation. Here is the basis
                # The source of the file determines whether to cycle, to avoid modifying the name whitelist every time a provider is added in the future.
                if bgm_file_override is None:
                    bgm_effects.append(afx.AudioLoop(duration=video_clip.duration))
                bgm_source_clip = clip_stack.enter_context(AudioFileClip(bgm_file))
                bgm_clip = bgm_source_clip.with_effects(bgm_effects)
                audio_clip = CompositeAudioClip([audio_clip, bgm_clip])
            except Exception:
                bgm_mix_succeeded = False
                # Record the complete stack and stable context to easily distinguish between file decoding, MoviePy effects and
                # CompositeAudioClip failed; file contents and API Key will not be entered into the log.
                logger.exception(
                    f"failed to mix background music: type={params.bgm_type}, "
                    f"file={bgm_file}"
                )

        final_video_clip = video_clip.with_audio(audio_clip)
        clip_stack.callback(final_video_clip.close)
        # Explicitly use the sampling rate of the input audio; if it cannot be obtained, fall back to MoviePy's default 44100Hz.
        # This can reduce sound quality fluctuations caused by resampling in different environments, especially Docker.
        output_audio_fps = int(getattr(audio_clip, "fps", 0) or 44100)
        with _stage_heartbeat("final video render"):
            _write_videofile_with_codec_fallback(
                final_video_clip,
                output_file=output_file,
                codec=_get_configured_video_codec(),
                atomic_output=True,
                audio_codec=audio_codec,
                audio_fps=output_audio_fps,
                audio_bitrate=audio_bitrate,
                temp_audiofile_path=_get_temp_audio_dir(output_dir),
                threads=params.n_threads or 2,
                logger=None,
                fps=fps,
            )
        return bgm_mix_succeeded


def render_image_zoom_video(
    image_path: str,
    clip_duration: int = 5,
    motion_mode: str = "random",
) -> str:
    """
    Renders a single local image to an mp4 clip with smooth dynamic camera motion (pan/zoom), returning the output file path.
    Supported motion_mode:
      - "random": dynamically alternates between pan left, pan right, zoom in, and zoom out
      - "pan_left": smooth slow pan to the left
      - "pan_right": smooth slow pan to the right
      - "pan_left_zoom": slow pan to the left with subtle zoom
      - "pan_right_zoom": slow pan to the right with subtle zoom
      - "zoom_in": smooth slow zoom in
      - "zoom_out": smooth slow zoom out
    """
    clip = ImageClip(image_path).with_duration(clip_duration).with_position("center")
    temp_path = ""
    try:
        norm_mode = (motion_mode or "random").lower().strip()
        if norm_mode == "random":
            available_modes = [
                "pan_left",
                "pan_right",
                "zoom_in",
                "zoom_out",
                "pan_left_zoom",
                "pan_right_zoom",
            ]
            chosen_mode = available_modes[
                hash(os.path.basename(image_path)) % len(available_modes)
            ]
        else:
            chosen_mode = norm_mode

        zoom_rate = min(0.15, clip_duration * 0.025)

        if chosen_mode == "pan_left" and hasattr(clip, "transform"):
            final_clip = video_effects.pan_left_transition(clip)
        elif chosen_mode == "pan_right" and hasattr(clip, "transform"):
            final_clip = video_effects.pan_right_transition(clip)
        elif chosen_mode == "pan_left_zoom" and hasattr(clip, "transform"):
            final_clip = video_effects.pan_left_zoom_transition(clip)
        elif chosen_mode == "pan_right_zoom" and hasattr(clip, "transform"):
            final_clip = video_effects.pan_right_zoom_transition(clip)
        elif chosen_mode == "zoom_out":
            zoom_clip = clip.resized(
                lambda t: (1.0 + zoom_rate) - zoom_rate * (t / clip.duration)
            )
            final_clip = CompositeVideoClip([zoom_clip])
        elif chosen_mode == "zoom_in":
            zoom_clip = clip.resized(
                lambda t: 1.0 + zoom_rate * (t / clip.duration)
            )
            final_clip = CompositeVideoClip([zoom_clip])
        else:
            is_zoom_out = (hash(os.path.basename(image_path)) % 2 == 1)
            if is_zoom_out:
                zoom_clip = clip.resized(
                    lambda t: (1.0 + zoom_rate) - zoom_rate * (t / clip.duration)
                )
            else:
                zoom_clip = clip.resized(
                    lambda t: 1.0 + zoom_rate * (t / clip.duration)
                )
            final_clip = CompositeVideoClip([zoom_clip])

        try:
            if norm_mode in ("random", "default", "zoom"):
                video_file = f"{image_path}.zoom-{clip_duration}.mp4"
            else:
                video_file = f"{image_path}.{norm_mode}-{clip_duration}.mp4"

            descriptor, temp_path = tempfile.mkstemp(
                prefix=".image-zoom-",
                suffix=".mp4",
                dir=os.path.dirname(os.path.abspath(video_file)),
            )
            os.close(descriptor)
            final_clip.write_videofile(temp_path, fps=30, logger=None)
        finally:
            close_clip(final_clip)
        os.replace(temp_path, video_file)
        temp_path = ""
        return video_file
    finally:
        close_clip(clip)
        if temp_path:
            delete_files(temp_path)


def preprocess_video(
    materials: List[MaterialInfo],
    clip_duration=4,
    image_motion_mode="random",
):
    # WebUI may pass in an empty material list in some secondary generation scenarios. Here, it returns an empty result directly to avoid throwing NoneType exceptions.
    if not materials:
        return []

    # Only materials that pass preprocessing verification are returned to prevent low-resolution images from entering the subsequent video synthesis process.
    valid_materials = []
    local_videos_dir = utils.storage_dir("local_videos", create=True)

    for material in materials:
        if not material.url:
            continue

        try:
            material_source_path = file_security.resolve_path_within_directory(
                local_videos_dir, material.url
            )
        except ValueError as exc:
            # The material path of local video_source comes from API parameters and must be restricted to the dedicated material directory.
            # Users are allowed to pass in file names and are also compatible with absolute paths returned by history, but are not allowed to escape to the system.
            # Other directories to avoid arbitrary file reading or detection of local sensitive files via MoviePy.
            logger.warning(
                f"skip unsafe local material: {material.url}, "
                f"local_videos_dir: {local_videos_dir}, error: {str(exc)}"
            )
            continue

        ext = utils.parse_extension(material_source_path)
        is_image = ext in const.FILE_TYPE_IMAGES
        try:
            # Picture materials are read directly as pictures to avoid misjudgment of VideoFileClip and triggering unstable fallback branches.
            if is_image:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
            else:
                clip = _open_video_clip_quietly(material_source_path)
        except Exception:
            # It will fall back to image mode when there is a non-standard extension or the detection fails, which is compatible with the historical situation of directly uploading the local image path.
            try:
                clip, material_source_path = _open_image_clip_with_fallback(
                    material_source_path
                )
                # The successful decoder determines the material kind, even
                # when the uploaded filename has a video or unknown suffix.
                is_image = True
            except Exception as exc:
                logger.warning(
                    f"skip unreadable local material: {material.url}, error: {str(exc)}"
                )
                continue
        try:
            width = clip.size[0]
            height = clip.size[1]
            if not is_material_resolution_acceptable(width, height):
                logger.warning(
                    f"low resolution material: {width}x{height}, minimum "
                    f"{_MIN_MATERIAL_DIMENSION}x{_MIN_MATERIAL_DIMENSION} required "
                    f"(tolerance {_MIN_DIMENSION_TOLERANCE}px)"
                )
                # Close the resource immediately after detecting low-resolution material and do not return the material to subsequent processes.
                close_clip(clip)
                continue

            if is_image:
                logger.info(f"processing image: {material_source_path}")
                # The material has been opened once when detecting the size. Here, the detection handle is released first and then rendered.
                # Image fragment for export.
                close_clip(clip)
                video_file = render_image_zoom_video(
                    material_source_path,
                    clip_duration,
                    motion_mode=image_motion_mode,
                )
                material.url = video_file
                logger.success(f"image processed: {video_file}")
            else:
                # Ordinary video materials only need to read the size for verification, and release the handle immediately after the verification is completed.
                close_clip(clip)
                # Update url to the resolved absolute path so that downstream
                # stages (combine_videos) can open the file without re-resolving.
                material.url = material_source_path
        except Exception:
            close_clip(clip)
            raise

        valid_materials.append(material)

    return valid_materials
