import json
import math
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
import threading
from typing import Any, Iterable
from uuid import uuid4

from loguru import logger

from app.models import const


def get_response(status: int, data: Any = None, message: str = ""):
    obj = {
        "status": status,
    }
    if data:
        obj["data"] = data
    if message:
        obj["message"] = message
    return obj


def to_json(obj):
    try:
        # Define a helper function to handle different types of objects
        def serialize(o):
            # If the object is a serializable type, return it directly
            if isinstance(o, (int, float, bool, str)) or o is None:
                return o
            # If the object is binary data, convert it to a base64-encoded string
            elif isinstance(o, bytes):
                return "*** binary data ***"
            # If the object is a dictionary, recursively process each key-value pair
            elif isinstance(o, dict):
                return {k: serialize(v) for k, v in o.items()}
            # If the object is a list or tuple, recursively process each element
            elif isinstance(o, (list, tuple)):
                return [serialize(item) for item in o]
            # If the object is a custom type, attempt to return its __dict__ attribute
            elif hasattr(o, "__dict__"):
                return serialize(o.__dict__)
            # Return None for other cases (or choose to raise an exception)
            else:
                return None

        # Use the serialize function to process the input object
        serialized_obj = serialize(obj)

        # Serialize the processed object into a JSON string
        return json.dumps(serialized_obj, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"failed to serialize object to json: {str(e)}")
        return None


def get_uuid(remove_hyphen: bool = False):
    u = str(uuid4())
    if remove_hyphen:
        u = u.replace("-", "")
    return u


_CLIP_SPEED_MIN = 0.5
_CLIP_SPEED_MAX = 2.0


def normalize_clip_speed(value, default: float = 1.0) -> float:
    """Normalizes clip playback speed to a safe range supported by WebUI."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default

    # NaN bypasses ordinary size comparisons and propagates when MoviePy calculates duration; infinite values do not
    # is valid user input. Both fall back to default values to ensure that neither API nor internal direct calls will generate
    # Invalid timeline. Zero and negative values ​​also do not represent normal playback speed.
    if not math.isfinite(speed) or speed <= 0:
        return default

    return min(max(speed, _CLIP_SPEED_MIN), _CLIP_SPEED_MAX)


def root_dir():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def storage_dir(sub_dir: str = "", create: bool = False):
    d = os.path.join(root_dir(), "storage")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if create:
        os.makedirs(d, exist_ok=True)

    return d


def resource_dir(sub_dir: str = ""):
    d = os.path.join(root_dir(), "resource")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    return d


def task_dir(sub_dir: str = ""):
    d = os.path.join(storage_dir(), "tasks")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    os.makedirs(d, exist_ok=True)
    return d


def output_videos_dir(sub_dir: str = "", create: bool = True):
    d = os.path.join(storage_dir(), "final_videos")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def font_dir(sub_dir: str = ""):
    d = resource_dir("fonts")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    os.makedirs(d, exist_ok=True)
    return d


def song_dir(sub_dir: str = ""):
    d = resource_dir("songs")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    os.makedirs(d, exist_ok=True)
    return d


def public_dir(sub_dir: str = ""):
    d = resource_dir("public")
    if sub_dir:
        d = os.path.join(d, sub_dir)
    os.makedirs(d, exist_ok=True)
    return d


def get_ffmpeg_binary() -> str:
    """
    Parse the FFmpeg executable that the current process should use.

    Reason for increase:
    1. Video encoding, silent audio generation, and pydub audio transcoding all rely on FFmpeg;
    2. PATH inconsistencies often occur in Windows portable packages, Docker and user-defined installation directories;
    3. Centralized analysis allows all callers to use the same set of priorities, reducing the number of times a certain link can run.
       Another link not found live issue with FFmpeg.

    Priority:
    1. IMAGEIO_FFMPEG_EXE: Explicit configuration of MoviePy/imageio convention;
    2. ffmpeg in system PATH;
    3. imageio-ffmpeg relies on the built-in binary provided;
    4. The string "ffmpeg" tells the story and leaves it to subprocess to expose more specific errors at runtime.
    """
    configured_ffmpeg = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if configured_ffmpeg:
        return configured_ffmpeg

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled_ffmpeg:
            return bundled_ffmpeg
    except Exception as exc:
        logger.warning(f"failed to resolve bundled ffmpeg binary: {str(exc)}")

    return "ffmpeg"


_FFMPEG_INSTALL_HINT = (
    "Install FFmpeg on your system, or set app.ffmpeg_path in config.toml to "
    "the full path of an ffmpeg executable (e.g. downloaded from "
    "https://www.gyan.dev/ffmpeg/builds/)."
)


def check_ffmpeg_ready(timeout: int = 10) -> bool:
    """
    Detect whether FFmpeg is available before actually starting to generate the video.

    Reason for increase:
    Previously, FFmpeg was missing/unavailable only in video synthesis, silent audio track generation, etc.
    ``RuntimeError: No ffmpeg exe could be found`` or subprocess error form
    Appears. Users often have to wait until most of the task is run before seeing this error report for the first time, and the error itself
    Doesn't point to any solution. Here in the shared task pipeline (app/services/task.py
    ``_run_pipeline``) to do a detection in advance and give actionable English tips (related to the project) as soon as possible
    The usage habits of other logger.warning are consistent), API, CLI, and WebUI will all go through
    This pipeline allows the three paths to take effect uniformly.

    Only makes a lightweight ``-version`` call, which will not trigger downloads or change the main process;
    The caller needs to treat the return value as a hard precondition - project-locked imageio-ffmpeg==0.6.0
    It will not automatically download a usable binary when it is actually used, so the detection failure must be
    The stage that requires FFmpeg is terminated directly instead of continuing to the video synthesis before failing.
    """
    ffmpeg_bin = get_ffmpeg_binary()
    try:
        completed = subprocess.run(
            [ffmpeg_bin, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.warning(
            f"no usable ffmpeg executable found (tried: {ffmpeg_bin}). "
            f"{_FFMPEG_INSTALL_HINT}"
        )
        return False
    except Exception as exc:
        logger.warning(
            f"failed to probe ffmpeg ({ffmpeg_bin}): {exc}. {_FFMPEG_INSTALL_HINT}"
        )
        return False

    if completed.returncode != 0:
        logger.warning(
            f"ffmpeg ({ffmpeg_bin}) probe exited with status {completed.returncode}; "
            f"video generation may fail later. {_FFMPEG_INSTALL_HINT}"
        )
        return False

    logger.info(f"ffmpeg check passed, using: {ffmpeg_bin}")
    return True


def run_in_background(func, *args, **kwargs):
    def run():
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"run_in_background error: {e}", exc_info=True)

    thread = threading.Thread(target=run, daemon=False)
    thread.start()
    return thread


def time_convert_seconds_to_hmsm(seconds) -> str:
    hours = int(seconds // 3600)
    seconds = seconds % 3600
    minutes = int(seconds // 60)
    milliseconds = int(seconds * 1000) % 1000
    seconds = int(seconds % 60)
    return "{:02d}:{:02d}:{:02d},{:03d}".format(hours, minutes, seconds, milliseconds)


def text_to_srt(idx: int, msg: str, start_time: float, end_time: float) -> str:
    start_time = time_convert_seconds_to_hmsm(start_time)
    end_time = time_convert_seconds_to_hmsm(end_time)
    srt = """%d
%s --> %s
%s
        """ % (
        idx,
        start_time,
        end_time,
        msg,
    )
    return srt


def str_contains_punctuation(word):
    for p in const.PUNCTUATIONS:
        if p in word:
            return True
    return False


def split_string_by_punctuations(s):
    result = []
    txt = ""

    previous_char = ""
    next_char = ""
    for i in range(len(s)):
        char = s[i]
        if char == "\n":
            result.append(txt.strip())
            txt = ""
            continue

        if i > 0:
            previous_char = s[i - 1]
        if i < len(s) - 1:
            next_char = s[i + 1]

        if char == "." and previous_char.isdigit() and next_char.isdigit():
            # # In the case of "withdraw 10,000, charged at 2.5% fee", the dot in "2.5" should not be treated as a line break marker
            txt += char
            continue

        if char == "," and previous_char.isdigit() and next_char.isdigit():
            # The thousandth comma in English numbers is not a sentence breaker, such as "1,000 years".
            # The word boundary of Edge TTS usually returns this digital whole as continuous content;
            # If this is split into "1" and "000 years", subsequent subtitle aggregation will not be able to match the original text of the script.
            # Then the error falls back to Whisper.
            txt += char
            continue

        if char not in const.PUNCTUATIONS:
            txt += char
        else:
            result.append(txt.strip())
            txt = ""
    result.append(txt.strip())
    # filter empty string
    result = list(filter(None, result))
    return result


PAUSE_TAG_KEYWORDS = (
    r"pause|pausa|silence|silencio|silêncio|silenzio|stille|"
    r"пауза|тишина|停顿|暂停|静音|ポーズ|一時停止|無音|일시중지|정지|stop|mute|silent"
)
# Match all tags (square brackets or parentheses) containing pause keywords, regardless of whether their parameters are legal or not,
# To ensure that illegal tags (such as [pause: -2s], [pause: nope], [pause: 0s]) are completely cleared before synthesis without leaking to TTS
PAUSE_TAG_PATTERN = re.compile(
    rf"[\[\(]\s*(?:{PAUSE_TAG_KEYWORDS})\b(?:\s*[:：]?\s*([^\]\)]*?))?\s*[\]\)]",
    re.IGNORECASE,
)

# Safety threshold for pause duration (unit: seconds):
# The minimum effective pause is 0.1 seconds (100ms). Requests smaller than this value will be verified and corrected to 0.1s;
# Pause tags that are less than or equal to 0 seconds or illegal non-numbers will be judged as invalid tags and removed directly, without being read aloud or generating silence;
# The maximum pause limit is 10.0 seconds, and the excess pause will be safely truncated.
MIN_PAUSE_DURATION_SECONDS = 0.1
MAX_PAUSE_DURATION_SECONDS = 10.0


def has_pause_tags(text: str) -> bool:
    """Check whether the text contains pause tags."""
    if not text:
        return False
    return bool(PAUSE_TAG_PATTERN.search(text))


def remove_pause_tags(text: str) -> str:
    """
    Remove all pause/pause tags (both valid and invalid) from the script text.

    Such non-pronounced tags must be cleared when subtitle segmentation, LLM keyword extraction, or when passed as phonetic text to TTS.
    Avoid illegal or unprocessed tags from being read aloud or used as visual search terms.
    """
    if not text:
        return ""
    cleaned = PAUSE_TAG_PATTERN.sub(" ", text)
    # Merge consecutive horizontal spaces, retain line breaks
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned.strip()


def parse_script_with_pauses(text: str) -> list[tuple[str, Any]]:
    """
    Parse text and pause tags in scripts.

    Continuous pause labels will be automatically merged into one pause segment;
    Illegal tags (such as non-numeric parameters, duration less than or equal to 0) will be directly removed and ignored, and will never be passed to TTS as lines;
    Pauses that are smaller than MIN_PAUSE_DURATION_SECONDS (0.1s/100ms) will be checked and raised to 0.1s;
    Pauses that exceed MAX_PAUSE_DURATION_SECONDS (10.0s) will be capped within a safe limit.

    Returns:
        An ordered list of tuples in the form [("speech", "copywriting"), ("pause", 2.0), ...]
    """
    if not text:
        return []

    segments: list[tuple[str, Any]] = []
    last_idx = 0

    for match in PAUSE_TAG_PATTERN.finditer(text):
        start, end = match.span()
        if start > last_idx:
            speech_text = text[last_idx:start].strip()
            if speech_text:
                segments.append(("speech", speech_text))

        raw_arg = match.group(1)
        if raw_arg is None or not raw_arg.strip():
            # When no parameters are specified, the default pause is 1.0 seconds.
            duration = 1.0
        else:
            raw_arg_str = raw_arg.strip()
            num_match = re.match(
                r"^([+-]?\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|ms|msec|msecs|秒|毫秒)?$",
                raw_arg_str,
                re.IGNORECASE,
            )
            if not num_match:
                logger.warning(
                    f"invalid pause tag value '{raw_arg_str}', tag removed and ignored"
                )
                last_idx = end
                continue

            val = float(num_match.group(1))
            unit = (num_match.group(2) or "s").lower()
            duration = val / 1000.0 if ("ms" in unit or "毫秒" in unit) else val

        if duration <= 0:
            logger.warning(
                f"invalid non-positive pause duration {duration}s, tag removed and ignored"
            )
            last_idx = end
            continue

        if duration < MIN_PAUSE_DURATION_SECONDS:
            logger.warning(
                f"pause duration {duration:.3f}s is below minimum limit of {MIN_PAUSE_DURATION_SECONDS}s (100ms), "
                f"clamped to {MIN_PAUSE_DURATION_SECONDS}s"
            )
            duration = MIN_PAUSE_DURATION_SECONDS

        if duration > MAX_PAUSE_DURATION_SECONDS:
            logger.warning(
                f"pause duration {duration}s exceeds maximum limit of {MAX_PAUSE_DURATION_SECONDS}s, "
                f"clamped to {MAX_PAUSE_DURATION_SECONDS}s"
            )
            duration = MAX_PAUSE_DURATION_SECONDS

        # Continuous pause tags are merged into one pause segment to avoid generating fragmented silent files.
        if segments and segments[-1][0] == "pause":
            merged_duration = min(
                segments[-1][1] + duration, MAX_PAUSE_DURATION_SECONDS
            )
            segments[-1] = ("pause", merged_duration)
        else:
            segments.append(("pause", duration))

        last_idx = end

    if last_idx < len(text):
        speech_text = text[last_idx:].strip()
        if speech_text:
            segments.append(("speech", speech_text))

    return segments


def normalize_script_for_subtitle_matching(video_script: str) -> str:
    """
    Clean script text before subtitle matching.

    Users may manually enter Markdown delimiters, title emphasis, or formatting symbols such as `_`.
    These characters usually do not appear in the recognition results of TTS/Whisper; if you continue to participate
    Subtitles are matched line by line. The number of script lines will be greater than the number of real subtitle lines, and may eventually be filled in.
    `00:00:00,000 --> 00:00:00,000`, causing the editing software to be unable to import SRT.
    """
    video_script = remove_pause_tags(video_script or "")
    underscore_count = video_script.count("_")
    video_script = video_script.replace("_", "")
    cleaned_lines = []
    removed_separator_lines = 0
    for line in video_script.splitlines():
        line = line.strip()
        # Markdown delimiters or emphasis marks will not be read by TTS when placed on a separate line, and must be read from
        # Removed from the script line to prevent subtitle aggregation from getting stuck on such "unvoiceable" target lines.
        if re.fullmatch(r"[-*_]{3,}", line):
            removed_separator_lines += 1
            continue
        cleaned_lines.append(line)

    normalized_script = "\n".join(cleaned_lines).strip()
    if underscore_count or removed_separator_lines:
        logger.debug(
            "normalized script for subtitle matching, "
            f"removed underscores: {underscore_count}, "
            f"removed markdown separator lines: {removed_separator_lines}"
        )
    return normalized_script


def md5(text):
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def resolve_ui_language(
    saved_language: str | None,
    browser_locale: str | None,
    supported_languages: Iterable[str],
    default_language: str = "vi",
) -> str:
    """
    Select the interface language according to the priority of "Saved settings, browser language, default language".

    Browsers usually return locale with locale, such as ``zh-CN``, ``pt-BR``. Language file usage
    Basic codes such as ``zh`` and ``pt``, so try to complete the match first, and then fall back to the language before the hyphen.
    code. The function maintains pure logic and avoids coupling the browser context and configuration writing to the tool layer, which facilitates testing.
    """
    supported = [str(language).strip() for language in supported_languages]
    supported_by_lower = {
        language.lower(): language for language in supported if language
    }

    def match_language(value: str | None) -> str | None:
        normalized = str(value or "").strip().replace("_", "-").lower()
        if not normalized:
            return None
        if normalized in supported_by_lower:
            return supported_by_lower[normalized]
        base_language = normalized.split("-", 1)[0]
        return supported_by_lower.get(base_language)

    saved_match = match_language(saved_language)
    if saved_match:
        return saved_match

    browser_match = match_language(browser_locale)
    if browser_match:
        return browser_match

    default_match = match_language(default_language)
    if default_match:
        return default_match

    en_match = match_language("en")
    if en_match:
        return en_match

    # Normal projects always contain English; keep empty language collections to avoid corrupted language directories leaving pages
    # An exception is thrown directly during initialization, and subsequent translation functions will continue to display the original key for diagnosis.
    return supported[0] if supported else default_language


@lru_cache(maxsize=8)
def load_locales(i18n_dir):
    # Each interaction with WebUI will trigger Streamlit to re-execute the script, and the language file will not change during the runtime.
    # Therefore the parsing results are cached to avoid repeatedly reading and parsing all i18n JSON files.
    _locales = {}
    for root, dirs, files in os.walk(i18n_dir):
        for file in files:
            if file.endswith(".json"):
                lang = file.split(".")[0]
                with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                    _locales[lang] = json.loads(f.read())
    return _locales


def parse_extension(filename):
    return Path(filename).suffix.lower().lstrip('.')
