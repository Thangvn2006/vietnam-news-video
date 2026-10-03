import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from loguru import logger

from app.utils import file_security, utils


# Streamlit allows larger uploaded files by default, but background music is typically only a few MB in size. Set clearly here
# The upper limit on the server side prevents the API or WebUI from completely writing extremely large files to disk and affecting video tasks in the same process.
MAX_BGM_UPLOAD_BYTES = 30 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_INTERNAL_UPLOAD_PREFIX = ".bgm-upload-"
_WINDOWS_INVALID_FILENAME_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}
    # Win32 also recognizes the Latin-1 superscript numbers ¹, ², ³ as device numbers, so the names are preserved with ordinary numbers.
    # Treat equally to avoid COM¹.mp3 bypass protection. The manifest is consistent with the download file name rules of webui/Main.py.
    | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in ("¹", "²", "³")}
)
# The filename goes into the logs, API responses, and WebUI interface unchanged, so except for path separators and Win32 reserved names,
# Also reject all control characters and characters that change the display form. Previously, only ord < 32 (C0 control character) was intercepted.
# Missing the other half of the same type of questions:
# * C1 control characters U+007F-U+009F: U+0085 will be rendered as a newline by some log viewers, and a file name can
# Fake additional, seemingly independent log lines.
# * Bidirectional text control characters U+200E/U+200F/U+202A-U+202E/U+2066-U+2069: characters after U+202E will be
# Rendered in reverse, "photo\u202egnp.mp3" looks like another extension in Explorer and logs.
# * U+2028/U+2029 (Unicode line/paragraph separators) can also create visual line breaks within a line of logs.
# These characters cannot be typed in the file name input box, and normal upload will not be affected; the judgment is consistent with
# The normalize_task_id of app/controllers/base.py is the same (use isprintable directly there).
_UNSAFE_FILENAME_CHARACTERS = frozenset(
    chr(code)
    for code in (
        *range(0x00, 0x20),
        *range(0x7F, 0xA0),
        0x200E,
        0x200F,
        0x2028,
        0x2029,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    )
)
# MoviePy ultimately decodes background music via FFmpeg, so no artificial limitation to MP3 is required. Only open here
# A mainstream and semantically clear audio extension to avoid mistakenly uploading video containers such as MP4 as background music.
# The tuple also serves as the single data source for the WebUI upload control, so there will be no inconsistency between the front and back ends when adding or deleting formats later.
SUPPORTED_BGM_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
)


class BgmUploadError(ValueError):
    """Indicates that the uploaded file does not meet the security or format requirements for background music."""


class BgmServiceError(RuntimeError):
    """Indicates server-side execution failure such as FFmpeg or file system unavailability."""


def should_use_bgm(bgm_type: str | None, bgm_volume: float | None) -> bool:
    """
    Unifiedly determine whether the current task requires processing any background music.

    This rule has nothing to do with the specific source: when no source is selected, the volume is illegal, or the volume is not greater than 0, random,
    Custom, Sonilo, and future providers must skip file parsing, external generation, and final mixing.
    Placing it in a universal BGM service avoids duplicating a set of 0 volume judgments for each additional provider.
    """
    if not str(bgm_type or "").strip():
        return False
    try:
        normalized_volume = float(bgm_volume or 0)
    except (TypeError, ValueError):
        return False
    return math.isfinite(normalized_volume) and normalized_volume > 0


def uploaded_bgm_dir(create: bool = True) -> str:
    """
    Returns the persistent directory of the user's background music.

    Built-in songs belong to code resources and continue to be placed in resource/songs; user uploaded content belongs to runtime data.
    It must be placed under Docker's mounted storage. It can be retained after the container is rebuilt and will not pollute the Git workspace.
    """
    return utils.storage_dir("bgm", create=create)


def _remove_staged_file(file_path: str) -> None:
    """Do your best to clean upload temporary files without overwriting the original exception being handled by the caller."""
    if not file_path or not os.path.exists(file_path):
        return
    try:
        os.remove(file_path)
    except OSError as exc:
        # Temporary files use reserved prefixes and will not enter the BGM list; failure to clean up should not cause "audio illegal"
        # Wait for the more accurate original exception to be covered, but the path and system errors must be left for operation and maintenance to locate.
        logger.warning(
            f"failed to remove staged background music: path={file_path}, "
            f"error={str(exc)}"
        )


def sanitize_upload_filename(filename: str) -> str:
    """Extract audio file names that can be displayed across platforms, and reject illegal names and unsupported extensions."""
    safe_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if (
        not safe_name
        or safe_name in {".", ".."}
        or len(safe_name) > 255
        or any(character in _UNSAFE_FILENAME_CHARACTERS for character in safe_name)
        or any(character in _WINDOWS_INVALID_FILENAME_CHARS for character in safe_name)
        or safe_name.lower().startswith(_INTERNAL_UPLOAD_PREFIX)
    ):
        raise BgmUploadError("invalid background music filename")

    # Windows will recognize the first paragraph before the extension as the device name, such as CON.mp3 and LPT1.wav.
    # Cannot be created as a normal file. Even if the server ends up using UUIDs, early rejection of such names can
    # Ensure that API input behavior is consistent on different platforms.
    windows_basename = safe_name.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_FILENAMES:
        raise BgmUploadError("invalid background music filename")
    if Path(safe_name).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS:
        supported_formats = ", ".join(
            extension.removeprefix(".").upper()
            for extension in SUPPORTED_BGM_EXTENSIONS
        )
        raise BgmUploadError(
            f"unsupported background music format; supported formats: {supported_formats}"
        )
    return safe_name


def _validate_audio(file_path: str, timeout_seconds: int = 30) -> None:
    """
    Only use FFmpeg currently configured for the project to verify that the file contains a fully decodable audio stream.

    The project allows imageio-ffmpeg to provide portable FFmpeg. This installation method does not guarantee simultaneous existence.
    FFprobe, therefore cannot add independent binary dependencies. `-map 0:a:0` will fail if there is no audio stream,
    `-xerror` will promote decoding errors to failures; complete decoding can also intercept encrypted files or random data accidentally
    Misjudgment of hitting audio frame header. The file can contain additional streams such as album art, but only the first audio stream is verified.
    """
    try:
        decoded = subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                file_path,
                "-map",
                "0:a:0",
                # Normalize timestamps from decoded samples. A header-only
                # WAV can exit successfully without producing any audio.
                "-af",
                "asetpts=N/SR/TB",
                "-progress",
                "pipe:1",
                "-nostats",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BgmServiceError("FFmpeg background music validation timed out") from exc
    except OSError as exc:
        raise BgmServiceError("failed to run FFmpeg for background music validation") from exc
    if decoded.returncode != 0:
        raise BgmUploadError("uploaded file must contain a decodable audio stream")
    decoded_audio = False
    for line in decoded.stdout.splitlines():
        key, separator, value = line.partition(b"=")
        if separator and key == b"out_time_us":
            try:
                decoded_audio = decoded_audio or int(value) > 0
            except ValueError:
                continue
    if not decoded_audio:
        raise BgmUploadError("uploaded file must contain nonempty decodable audio")


def validate_audio_file(file_path: str, timeout_seconds: int = 120) -> None:
    """
    Verify that audio files on disk can be fully decoded by Project FFmpeg.

    Upload preflight typically only takes 30 seconds; Sonilo-generated soundtracks can be up to 6 minutes long, so they are available externally
    Reuse entry with adjustable timeout. The service only relies on FFmpeg and does not require additional installation of FFprobe on the system.
    """
    if not os.path.isfile(file_path) or os.path.getsize(file_path) <= 0:
        raise BgmUploadError("background music file is empty or missing")
    _validate_audio(file_path, timeout_seconds=timeout_seconds)


def _stage_bgm_upload(filename: str, source: BinaryIO) -> tuple[str, str, int]:
    """
    Write the upload stream to a temporary file in the same directory and return the safe file name, temporary path and number of bytes.

    WebUI's upload preflight and final persistence must use exactly the same chunked reads, size limits, and file names
    Rules, otherwise there may be a state split where the interface displays available, but is rejected by the server after clicking to generate.
    Temporary files are deleted or replaced atomically by the caller after completing audio probing.
    """
    safe_name = sanitize_upload_filename(filename)
    try:
        target_dir = uploaded_bgm_dir(create=True)
    except OSError as exc:
        raise BgmServiceError("failed to prepare background music storage") from exc
    temp_path = ""
    total_bytes = 0

    try:
        try:
            source.seek(0)
        except (AttributeError, OSError) as exc:
            raise BgmUploadError("background music upload is not seekable") from exc

        # Keeping the original extension allows FFmpeg to choose the correct one for formats such as AAC without container headers.
        # demuxer; temporary files are still placed in the target directory to ensure that the final os.replace operation is atomic.
        descriptor, temp_path = tempfile.mkstemp(
            prefix=_INTERNAL_UPLOAD_PREFIX,
            suffix=Path(safe_name).suffix.lower(),
            dir=target_dir,
        )
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise BgmUploadError("background music upload must be binary")
                total_bytes += len(chunk)
                if total_bytes > MAX_BGM_UPLOAD_BYTES:
                    raise BgmUploadError("background music file exceeds the 30 MB limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        if total_bytes == 0:
            raise BgmUploadError("background music file is empty")
        return safe_name, temp_path, total_bytes
    except Exception as exc:
        _remove_staged_file(temp_path)
        if isinstance(exc, BgmUploadError):
            raise
        if isinstance(exc, OSError):
            raise BgmServiceError("failed to stage background music upload") from exc
        raise
    finally:
        # Streamlit also needs to use the same UploadedFile for browser listening; restoring the file pointer can
        # Avoid empty content being read by the player or final save after verification.
        try:
            source.seek(0)
        except (AttributeError, OSError):
            pass


def validate_bgm_upload(filename: str, source: BinaryIO) -> str:
    """Completely validates the uploaded audio but does not persist it, used for WebUI preflight before displaying "Ready"."""
    safe_name, temp_path, total_bytes = _stage_bgm_upload(filename, source)
    try:
        _validate_audio(temp_path)
        logger.debug(
            f"background music upload validated: name={safe_name}, "
            f"size={total_bytes} bytes"
        )
        return safe_name
    finally:
        _remove_staged_file(temp_path)


def save_bgm_upload(filename: str, source: BinaryIO) -> str:
    """
    Save user background music in chunked, limited and atomic replacement methods.

    Usage scenarios include FastAPI UploadFile and Streamlit UploadedFile, both of which provide binary
    File interface. First write the temporary file in the same directory and verify it, and then use os.replace to atomically replace it, which can avoid
    Concurrent uploads or process interruptions leave half of the audio file, which will also cause uploads with the same name to obtain different UUID storage keys.
    Queued or running tasks therefore always reference the original immutable file.
    """
    safe_name, temp_path, total_bytes = _stage_bgm_upload(filename, source)
    stored_name = f"{uuid4().hex}{Path(safe_name).suffix.lower()}"
    target_path = os.path.join(os.path.dirname(temp_path), stored_name)

    try:
        _validate_audio(temp_path)
        try:
            os.replace(temp_path, target_path)
        except OSError as exc:
            raise BgmServiceError("failed to persist background music upload") from exc
        temp_path = ""
        logger.info(
            f"background music uploaded: original_name={safe_name}, "
            f"stored_name={stored_name}, size={total_bytes} bytes"
        )
        return stored_name
    finally:
        _remove_staged_file(temp_path)


def _list_bgm_files(directories: tuple[str, ...]) -> list[str]:
    """Enumerate safe and supported background music files by directory priority."""
    files_by_name: dict[str, str] = {}
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory), key=str.lower):
            # Upload preflight and final save will briefly create files in the same directory. Although the temporary file has a legal
            # The audio extension has not yet been verified and cannot be pre-selected by the random BGM list.
            if name.startswith(_INTERNAL_UPLOAD_PREFIX):
                continue
            if Path(name).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS:
                continue
            file_path = os.path.join(directory, name)
            try:
                # The enumeration results also need to be verified by the real path. Otherwise an attacker could place in an allowed directory
                # An audio symbolic link pointing to an external file, and then giving it to MoviePy with a random BGM path.
                resolved_path = file_security.resolve_path_within_directory(
                    directory, file_path
                )
            except ValueError as exc:
                logger.warning(
                    f"skip unsafe background music file: name={name}, error={str(exc)}"
                )
                continue
            files_by_name[name] = resolved_path
    return [files_by_name[name] for name in sorted(files_by_name, key=str.lower)]


def list_builtin_bgm_files() -> list[str]:
    """
    Lists the built-in background music distributed with the project.

    WebUI's "Preset Songs" and set preset import and export only use this list, make sure the saved file name
    Can be restored on another device using the same version; user uploaded files are still managed by Custom Music.
    """
    return _list_bgm_files((utils.song_dir(),))


def list_bgm_files() -> list[str]:
    """Lists the available background music uploaded by users and built-in. If the name is the same, the uploaded file will be used first."""
    return _list_bgm_files((utils.song_dir(), uploaded_bgm_dir(create=True)))


def resolve_builtin_bgm_file(unsafe_path: str) -> str:
    """Parse built-in background music by file name and reject paths, unknown files, and user-uploaded files."""
    if not unsafe_path:
        raise ValueError("background music filename is required")

    filename = str(unsafe_path)
    if filename != os.path.basename(filename):
        raise ValueError("preset background music must use a filename")

    files_by_name = {
        os.path.basename(file_path): file_path for file_path in list_builtin_bgm_files()
    }
    if filename not in files_by_name:
        raise ValueError("preset background music is not available")
    return files_by_name[filename]


def resolve_bgm_file(unsafe_path: str) -> str:
    """
    Parse BGM in the user upload directory and built-in song directory, and reject paths and uploads outside the two whitelists
    Break the temporary files left behind.

    File names hit the user directory first, while retaining `output000.mp3`, absolute whitelist paths and
    `./resource/songs/output000.mp3` and other old usages. Newly uploaded files use UUID. Under normal circumstances
    There will be no duplicate names with built-in songs or historical uploads.
    """
    if (
        not unsafe_path
        or Path(unsafe_path).suffix.lower() not in SUPPORTED_BGM_EXTENSIONS
    ):
        raise ValueError("unsupported background music path")
    if os.path.basename(str(unsafe_path)).lower().startswith(_INTERNAL_UPLOAD_PREFIX):
        # Upload preflight and final save will be temporarily created in the same directory with the ``.bgm-upload-`` prefix.
        # Intermediate files. They have valid audio extensions but have not yet been verified, ``_list_bgm_files``
        # They have been excluded from random BGM; the analysis entry must give the same conclusion, otherwise
        # The name passed in by the API/CLI can hit an intermediate file whose writing is interrupted and whose content is incomplete.
        # Names passed in by the user are compared case-insensitively, consistent with the Windows/macOS file system.
        raise ValueError("background music upload staging files are not selectable")

    candidates = [unsafe_path]
    if not os.path.isabs(unsafe_path):
        candidates.append(os.path.join(utils.root_dir(), unsafe_path))

    last_error = ValueError("background music file does not exist")
    for directory in (uploaded_bgm_dir(create=True), utils.song_dir()):
        for candidate in candidates:
            try:
                return file_security.resolve_path_within_directory(directory, candidate)
            except ValueError as exc:
                last_error = exc
    raise ValueError(str(last_error)) from last_error
