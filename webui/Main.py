from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID, uuid4

import requests
import streamlit as st
import streamlit.components.v1 as st_components_v1
import streamlit.components.v2 as st_components_v2
from loguru import logger

try:
    from streamlit_tour import Tour
except Exception:
    Tour = None

# When WebUI is run as an independent portal, the project root directory needs to take precedence over third-party dependencies.
# Prevent the app package with the same name in the dependency from obscuring VietNamNewsVideo's own app package.
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

from app.config import config
from app.models import const
from app.models.llm_provider import (
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDER_REGISTRY,
    get_llm_provider,
    normalize_provider_override,
)
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoFitMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import (
    article_scraper,
    cache_manager,
    llm,
    loomloom,
    material,
    metaso_minimax,
    muapi,
    ofox,
    subtitle,
    system_updater,
    version_checker,
    video,
    video_template,
    voice,
    volcengine_seedance,
    webui_task,
)
from app.services import bgm as bgm_service
from app.services import elevenlabs_music as elevenlabs_music_service
from app.services import material_upload as material_upload_service
from app.services import sonilo as sonilo_service
from app.services import state as sm
from app.services import task as tm
from app.utils import utils
from app.utils.logging_utils import configure_terminal_logger

_DRAGGABLE_CANVAS_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "components", "draggable_canvas")
_draggable_canvas = st_components_v1.declare_component("draggable_canvas", path=_DRAGGABLE_CANVAS_PATH)

st.set_page_config(
    page_title="VietNamNewsVideo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/Thangvn2006/vietnam-news-video/issues",
        "About": "# VietNamNewsVideo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/Thangvn2006/vietnam-news-video",
    },
)


# Streamlit 1.59 will display platform entrances such as Deploy and skills nudge by default in the upper right corner of the page.
# VietNamNewsVideo is a native tool for end users, these entries will create a large white space at the top,
# It can also confuse new users into thinking they need to install additional components. The Streamlit platform toolbar is uniformly hidden here.
# And compress the top space of the main container to leave only the project's own title, language selection and business settings area.
style_file = Path(__file__).with_name("styles.css")
streamlit_style = f"<style>{style_file.read_text(encoding='utf-8')}</style>"
st.markdown(streamlit_style, unsafe_allow_html=True)
# Define resource directory
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
# The language list must be available before session state is initialized so that the browser locale can be mapped to
# Languages truly supported by the project; the automatic recognition results only enter the current session and do not modify the global configuration.
locales = utils.load_locales(i18n_dir)
DEFAULT_CHATTERBOX_BASE_URL = "http://127.0.0.1:4123/v1"
DEFAULT_CHATTERBOX_MODEL = "chatterbox"
DEFAULT_CHATTERBOX_VOICES = ["default-Female"]
DEFAULT_KOKORO_BASE_URL = "http://127.0.0.1:8880/v1"
DEFAULT_KOKORO_MODEL = "kokoro"
# empty = ask the server for its voice list (GET {base_url}/audio/voices)
DEFAULT_KOKORO_VOICES: list[str] = []
DEFAULT_VOXCPM_BASE_URL = voice.VOXCPM_DEFAULT_BASE_URL
DEFAULT_VOXCPM_VOICE = voice.VOXCPM_DEFAULT_VOICE
VOXCPM_REFERENCE_AUDIO_SESSION_KEY = "voxcpm_reference_audio"
VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY = "voxcpm_reference_audio_error"
VOXCPM_PROMPT_AUDIO_SESSION_KEY = "voxcpm_prompt_audio"
VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY = "voxcpm_prompt_audio_error"
VOXCPM_PROMPT_TEXT_SESSION_KEY = "voxcpm_prompt_text_input"
VOXCPM_HIGH_FIDELITY_SESSION_KEY = "voxcpm_high_fidelity_enabled"
VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY = "voxcpm_separate_prompt_audio_enabled"
VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY = "voxcpm_prompt_example_mode"
ONBOARDING_TOUR_KEY = "mpt-onboarding-v1"
CUSTOM_LLM_ENDPOINT_ID = "custom"
VOICE_MODE_TTS = "tts"
VOICE_MODE_UPLOAD = "upload"
VOICE_MODE_NONE = "none"
LOOMLOOM_MAX_POLL_FAILURES = 5
# WebUI displays video sources grouped by material capabilities, but the original video_source value is still retained at the bottom layer.
# The AI video group and the settings page share the same business order: priority is given to cooperative service providers, and according to Secret Tower, OFox,
# The odds cloud and volcano engine are arranged; the remaining services will be displayed later. In this way, the order of the two entrances is consistent, and at the same time
# Field semantics in config.toml, historical tasks, and API requests are not changed, and legacy users do not need to migrate their configurations.
VIDEO_SOURCE_GROUPS = {
    "stock_video": ("pexels", "pixabay", "coverr"),
    "ai_video": (
        "metaso_minimax",
        "ofox",
        "loomloom",
        "volcengine_seedance",
        "wavespeed",
        "muapi",
    ),
    "ai_image": ("openai_image",),
    "local": ("local",),
}
# Upload-Post's API Key and publishing user are managed on two pages respectively, and the publishing user name is also managed.
# It is not the same as the login email. Centralized maintenance of portals can avoid deviations in multi-language copywriting after hard-coding URLs.
# It also facilitates users to complete first-time configuration and subsequent account maintenance directly from the WebUI.
UPLOAD_POST_API_KEYS_URL = "https://app.upload-post.com/api-keys"
UPLOAD_POST_MANAGE_USERS_URL = "https://app.upload-post.com/manage-users"
# The material settings and video source description share the promotion entrance to avoid inconsistency in the link parameters of the two locations.
OFOX_REFERRAL_URL = (
    "https://ofox.ai/?utm_source=github"
    "&utm_medium=sponsorship&utm_content=vietnamnewsvideo"
)
# "Default" is a WebUI-specific sentinel that will not be written to config.toml or passed to FFmpeg.
# The backend continues to use stable libx264 when video_codec is not configured; leaving this sentinel alone can differentiate
# "Follow project default policy" and "User explicitly fix libx264" to facilitate future security adjustments to the default policy.
DEFAULT_VIDEO_CODEC_OPTION = "__default__"
# LoomLoom's capability interface only returns the model ID and display name, but does not provide a price. Only maintenance users confirmed here
# The reference price is used to help select models; the final cost is settled based on the actual model call. Alias coverage at the same time
# The current display name and common model ID. New models that are not included will naturally return an empty price, which does not affect the selection or quotation.
LOOMLOOM_VIDEO_MODEL_PRICES = (
    (("veo31fast", "googleveo31fastpreview"), "￥0.700/秒", "￥0.700/秒"),
    (
        (
            "通义万相22图生视频fastlora",
            "通义万相22文生视频fastlora",
            "tongyiwanxiang22i2vfastlora",
            "tongyiwanxiang22t2vfastlora",
            "wanx22i2vfastlora",
            "wanx22t2vfastlora",
        ),
        "￥0.350–0.770/条",
        "￥0.350/条（480P）；￥0.770/条（720P）",
    ),
    (("即梦30文生视频720p", "jimeng30t2v720p"), "￥0.230/秒", "￥0.230/秒"),
    (("即梦30pro视频", "jimeng30pro视频", "jimeng30provideo"), "￥1.000/秒", "￥1.000/秒"),
    (("veo3", "googleveo3"), "￥1.400/秒", "￥1.400/秒"),
    (("veo31", "googleveo31"), "￥1.400/秒", "￥1.400/秒"),
    (
        ("klingv2", "可灵v2"),
        "￥10.00–20.00/条",
        "￥10.00/条（5 秒）；￥20.00/条（10 秒）",
    ),
    (
        ("klingv21master", "可灵v21master"),
        "￥10.00–20.00/条",
        "￥10.00/条（5 秒）；￥20.00/条（10 秒）",
    ),
    (
        ("viduq3pro",),
        "￥0.440–1.000/秒",
        "￥0.440/秒（540P）；￥0.940/秒（720P）；￥1.000/秒（1080P）",
    ),
)
DEFAULT_SUBTITLE_SETTINGS = {
    "subtitle_enabled": True,
    "font_name": "MicrosoftYaHeiBold.ttc",
    "subtitle_position": "bottom",
    "subtitle_display_mode": "sentence",
    "subtitle_animation": "none",
    "custom_position": 70.0,
    "text_fore_color": "#FFFFFF",
    "font_size": 60,
    "stroke_color": "#000000",
    "stroke_width": 1.5,
    "subtitle_background_enabled": False,
    "subtitle_background_color": "#000000",
    "rounded_subtitle_background": False,
}
LOCAL_MATERIAL_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".flv",
    ".mkv",
    ".jpg",
    ".jpeg",
    ".png",
}
CUSTOM_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_FINAL_VIDEO_PATTERN = re.compile(
    r"^final-(?P<index>\d+)\.(?P<extension>mp4|mov|mkv|webm)$",
    re.IGNORECASE,
)
_DOWNLOAD_FILENAME_INVALID_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_FILENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }
    # Win32 also recognizes Latin-1 superscript numbers ¹, ², ³ as device numbers. Although this type of subject
    # It's rare, but can still cause Windows downloads to fail, so it's treated the same as normal numeric reserved names.
    | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in ("¹", "²", "³")
    }
)
_RUNTIME_CONFIG_SECTIONS = {
    "app": config.app,
    "azure": config.azure,
    "chatterbox": config.chatterbox,
    "kokoro": config.kokoro,
    "elevenlabs": config.elevenlabs,
    "minimax_tts": config.minimax_tts,
    "siliconflow": config.siliconflow,
    "fish_audio": config.fish_audio,
    "voxcpm": config.voxcpm,
    "ui": config.ui,
}
# Setup presets and key backups use separate file identifiers. When importing, first verify the schema and version.
# Avoid mistaking task records, config.toml, or other JSON for cost function export files.
SETTINGS_PRESET_SCHEMA = "vietnamnewsvideo.settings-preset"
SETTINGS_PRESET_VERSION = 1
SETTINGS_PRESET_FILE_NAME = "vietnamnewsvideo-settings.json"
KEY_BACKUP_SCHEMA = "vietnamnewsvideo.key-backup"
KEY_BACKUP_VERSION = 1
KEY_BACKUP_FILE_NAME = "vietnamnewsvideo-keys.json"
# Export files contain only settings or credentials, not media. Reject oversized
# uploads before decoding and parsing them in the Streamlit process.
MAX_SETTINGS_TRANSFER_BYTES = 2 * 1024 * 1024
# Presets only describe build parameters. Materials, dubbing and soundtracks are all local file paths, and presets usually need to be on another computer.
# Importing into the machine or another container, bringing these paths will only point to files that do not exist.
PRESET_EXCLUDED_PARAM_KEYS = frozenset(
    {
        "video_materials",
        "custom_audio_file",
        "bgm_file",
    }
)
# Keys are identified by the configuration item name suffix. As long as the new Provider continues to be named, it will be automatically entered.
# Backup, no need to maintain a second key list.
CREDENTIAL_KEY_SUFFIXES = (
    "api_key",
    "api_keys",
    "api_token",
    "access_key",
    "secret_key",
    "speech_key",
)
# When you restore only the key without restoring the accompanying configuration items, the credentials are still unavailable. These companion items are backed up along with the key.
CREDENTIAL_COMPANION_KEYS = {
    # Azure Speech must also be region aware.
    "azure": ("speech_region",),
    # Provider's additional fields are declared by the Registry, such as Cloudflare AI Gateway's
    # Account ID and Gateway ID. When only restoring the API Key and losing these fields, switch to another
    # The Provider still cannot be called after the machine is installed. Reading from the Registry allows future additions
    # The Provider automatically goes into backup and there is no need to maintain a second field list here.
    "app": tuple(
        provider.config_key(field.config_suffix)
        for provider in LLM_PROVIDER_REGISTRY
        for field in provider.extra_fields
    ),
}

NON_LLM_COMPANION_KEYS = {
    "app": ("upload_post_username",)
}
# The same key may use respective control keys in different panels: the audio panel directly edits Gemini and
# MiMo's LLM key. Each alias must be cleared when restoring the backup, otherwise the old value will be left behind
# The newly restored key will be overwritten in the next rerun.
CREDENTIAL_WIDGET_STATE_ALIASES = {
    ("app", "gemini_api_key"): ("gemini_tts_api_key_input",),
    ("app", "mimo_api_key"): ("mimo_tts_api_key_input",),
}
# The ui partition only saves interface preferences, does not contain any credentials, and is skipped entirely during backup.
KEY_BACKUP_EXCLUDED_SECTIONS = frozenset({"ui"})


# -----------------------------------------------------------------------------
# Launch configuration, session state and localization
# -----------------------------------------------------------------------------


def _set_runtime_config(section_name, key, value):
    """
    Updates the WebUI configuration but does not wait for the background task that is generating the video.

    Before the background task ends, the configuration layer only retains the latest value of the same configuration item; when the task releases the configuration lock, it will automatically
    Apply and save. Page control values are still maintained by Streamlit session_state, so the
    rerun will not reset what the user just entered to the old configuration.
    """
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    updated = config.update_config_nonblocking(config_section, key, value)
    if not updated:
        logger.debug(f"deferred WebUI config update: section={section_name}, key={key}")
    return updated


def _delete_runtime_config(section_name, key):
    """Delete WebUI configuration items; background tasks will be executed after the configured time delay."""
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    deleted = config.delete_config_nonblocking(config_section, key)
    if not deleted:
        logger.debug(f"deferred WebUI config delete: section={section_name}, key={key}")
    return deleted


def _save_runtime_config():
    """Requests to save the WebUI configuration; returns immediately when the background task takes up the configuration."""
    saved = config.try_save_config()
    if not saved:
        logger.debug("deferred WebUI config save until active task completes")
    return saved


def _saved_ui_choice(key, options, default, section=None):
    """Reads a persistent selection and downgrades old configuration or manually edited illegal values to default values."""
    options = list(options)
    section = config.ui if section is None else section
    saved = section.get(key, default)
    numeric_default = isinstance(default, (int, float)) and not isinstance(
        default, bool
    )
    # bool is a subclass of int, ``True == 1``. Manually write numerical options as TOML
    # Boolean values must be rejected and cannot be disguised as the first numerical option.
    if numeric_default and isinstance(saved, bool):
        return default
    for option in options:
        if saved == option:
            # Return the real value in options, and by the way, the TOML 1.0 equivalent is normalized to
            # Integer option 1 to avoid downstream parameter types from drifting with configuration writing.
            return option

    # Values in TOML usually retain their original types; they are still compatible with users manually writing them into strings.
    if numeric_default and isinstance(saved, str):
        try:
            converted = type(default)(saved)
        except (TypeError, ValueError):
            converted = None
        for option in options:
            if converted == option:
                return option
    return default


def _saved_ui_number(key, default, minimum, maximum, number_type=float):
    """Read and limit persistent values to prevent illegal configuration from damaging the Streamlit slider."""
    try:
        saved = config.ui.get(key, default)
        if isinstance(saved, bool):
            raise ValueError("boolean is not a numeric setting")
        value = number_type(saved)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite value")
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


def _saved_ui_bool(key, default):
    """Compatible with TOML booleans and common handcrafted strings, rejecting old values with unclear meanings."""
    value = config.ui.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _saved_ui_color(key, default):
    """Only standard six-digit hexadecimal colors are passed to the Streamlit color picker."""
    value = str(config.ui.get(key, default) or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return default


def _saved_ui_text(key, default="", max_length=None):
    """Reads persistent text and respects the maximum length limit of the corresponding WebUI control."""
    value = str(config.ui.get(key, default) or default)
    if max_length is not None:
        value = value[:max_length]
    return value


def _run_llm_read_operation(operation_name, operation):
    """
    Use a stable current LLM configuration to perform read-only requests and avoid waiting for video generation tasks.

    When the configuration lock can be obtained immediately, the original mutual exclusion protection will continue to be used; when the lock is already held by the background video task,
    The global configuration will not change until the end of the task, so the current configuration can be safely copied and the page overlaid
    Providers, models and keys that have not yet been shipped. This way the new copywriter uses the latest options in the interface, and at the same time
    Does not change the video task being generated.
    """
    with config.try_runtime_config_lock() as lock_acquired:
        # The configuration layer holds the queue lock during copying of global values and overlaying of values to be updated, so the snapshot can only see
        # The complete state before or after the update, without mixing the two sets of Provider parameters.
        app_config_snapshot = config.snapshot_config_with_pending(config.app)
        if lock_acquired:
            return operation(app_config_snapshot)

    logger.info(
        f"run read-only LLM operation with active task configuration: "
        f"operation={operation_name}"
    )
    return operation(app_config_snapshot)


def _parse_chatterbox_voices(voices):
    # Chatterbox is a self-hosted service, and patch lists are entered manually by the user in the WebUI.
    # This is uniformly compatible with TOML arrays and comma-separated strings in input boxes to avoid drop-down boxes,
    # The audition button and subsequent generation process use different formats resulting in inconsistent status.
    if isinstance(voices, str):
        return [v.strip() for v in voices.split(",") if v.strip()]
    return [str(v).strip() for v in voices or [] if str(v).strip()]


def _sync_chatterbox_config_from_session_state():
    # Streamlit's button will trigger a full page rerun, and the Chatterbox configuration input box is located
    # After the "Listen to Speech Synthesis" button. If you only read config.chatterbox during the audition, you may not be able to get it.
    # The base_url/model/voices that the user just filled in the input box. First synchronize once from session_state,
    # It can be ensured that the button logic and input box display logic use the same latest configuration.
    _set_runtime_config(
        "chatterbox",
        "base_url",
        (
            st.session_state.get(
                "chatterbox_base_url_input",
                config.chatterbox.get("base_url") or DEFAULT_CHATTERBOX_BASE_URL,
            )
            or ""
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "api_key",
        st.session_state.get(
            "chatterbox_api_key_input", config.chatterbox.get("api_key", "")
        ),
    )
    _set_runtime_config(
        "chatterbox",
        "model_id",
        (
            st.session_state.get(
                "chatterbox_model_input",
                config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
            )
            or DEFAULT_CHATTERBOX_MODEL
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "voices",
        _parse_chatterbox_voices(
            st.session_state.get(
                "chatterbox_voices_input",
                config.chatterbox.get("voices") or DEFAULT_CHATTERBOX_VOICES,
            )
        ),
    )


def _sync_kokoro_config_from_session_state():
    # The sound catalog is rendered before setting the input box, and the browser status is synchronized first to ensure that it is used in this rerun.
    # New endpoints and manual tone configuration eliminate the need to fiddle with controls again.
    _set_runtime_config(
        "kokoro",
        "base_url",
        (
            st.session_state.get(
                "kokoro_base_url_input",
                config.kokoro.get("base_url") or DEFAULT_KOKORO_BASE_URL,
            )
            or ""
        ).strip(),
    )
    _set_runtime_config(
        "kokoro",
        "api_key",
        st.session_state.get(
            "kokoro_api_key_input", config.kokoro.get("api_key", "")
        ),
    )
    _set_runtime_config(
        "kokoro",
        "model_id",
        (
            st.session_state.get(
                "kokoro_model_input",
                config.kokoro.get("model_id") or DEFAULT_KOKORO_MODEL,
            )
            or DEFAULT_KOKORO_MODEL
        ).strip(),
    )
    _set_runtime_config(
        "kokoro",
        "voices",
        _parse_chatterbox_voices(
            st.session_state.get(
                "kokoro_voices_input",
                config.kokoro.get("voices") or DEFAULT_KOKORO_VOICES,
            )
        ),
    )


def _get_kokoro_voice_options(saved_voice_name: str) -> list[str]:
    """The remote directory is cached within the session, and the last selection is retained when disconnected, and the failure is not regarded as the user changing the tone."""
    if config.kokoro.get("voices"):
        return voice.get_kokoro_voices()

    # Only a cache of the current service is retained. Recheck immediately after changing the endpoint/credential, and the cache will not save the clear text Key;
    # Other UI operations within 30 seconds do not repeatedly block for 5 seconds waiting for a service that is known to be offline.
    signature = (
        (config.kokoro.get("base_url") or "").strip().rstrip("/"),
        _credential_signature(config.kokoro.get("api_key", "")),
    )
    catalog = st.session_state.get("kokoro_voice_catalog", {})
    if catalog.get("signature") != signature:
        catalog = {"signature": signature, "voices": [], "checked_at": None}
    now = time.monotonic()
    if catalog["checked_at"] is None or now - catalog["checked_at"] >= 30:
        fetched = voice.get_kokoro_voices(fallback=False)
        catalog.update(checked_at=now, available=bool(fetched))
        if fetched:
            catalog["voices"] = fetched
        st.session_state["kokoro_voice_catalog"] = catalog

    options = list(catalog["voices"])
    if not catalog["available"]:
        st.warning(tr("Kokoro Voices Unavailable"))
        # May not be cached when first opened, still retaining the real selection in the profile; after resuming the connection
        # Only the successfully returned new directory can determine that an old sound has indeed been deleted by the server.
        if voice.is_kokoro_voice(saved_voice_name) and saved_voice_name not in options:
            options.insert(0, saved_voice_name)
    return options or [f"kokoro:{voice.KOKORO_DEFAULT_VOICE}"]


def _detect_audio_mime(audio_file: str, audio_bytes: bytes) -> str:
    # Some OpenAI-compatible TTS services, such as travisvn/chatterbox-tts-api,
    # Even if response_format=mp3 is requested, WAV content will be returned. WebUI audition if fixed
    # With audio/mp3, the browser may not be able to play it, so here the real format is identified by the file header.
    header = audio_bytes[:12]
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"ID3") or header[:2] in (
        b"\xff\xfb",
        b"\xff\xf3",
        b"\xff\xf2",
    ):
        return "audio/mp3"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    ext = os.path.splitext(audio_file)[1].lower()
    return {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(ext, "audio/mp3")


def _build_uploaded_file_path(uploaded_file, target_dir, allowed_extensions, prefix):
    """Generate controlled server-side save paths for browser-uploaded files."""
    original_name = os.path.basename(str(uploaded_file.name or ""))
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in allowed_extensions:
        logger.warning(
            f"reject unsupported uploaded file extension: {original_name or '<empty>'}"
        )
        raise ValueError("unsupported uploaded file type")

    normalized_target_dir = os.path.realpath(target_dir)
    os.makedirs(normalized_target_dir, exist_ok=True)
    # Do not reuse the file name passed in by the browser and avoid overwriting path separators, control characters or the same name. UUID is only used for
    # The server-side download does not change the original name seen by the user in the upload control.
    file_path = os.path.realpath(
        os.path.join(normalized_target_dir, f"{prefix}-{uuid4().hex}{extension}")
    )
    if os.path.commonpath([normalized_target_dir, file_path]) != normalized_target_dir:
        logger.warning(f"invalid uploaded file path: {file_path}")
        raise ValueError("invalid uploaded file path")
    return file_path


def _save_uploaded_local_materials(uploaded_files):
    """Validate a WebUI material batch and undo earlier files on failure."""
    local_videos_dir = utils.storage_dir("local_videos", create=True)
    materials = []
    persisted = []
    saved_paths = []
    try:
        for uploaded_file in uploaded_files:
            stored_name = material_upload_service.save_material_upload(
                uploaded_file.name, uploaded_file
            )
            file_path = os.path.join(local_videos_dir, stored_name)
            saved_paths.append(file_path)
            material_info = MaterialInfo()
            material_info.provider = "local"
            material_info.url = file_path
            materials.append(material_info)
            persisted.append(
                {
                    "provider": material_info.provider,
                    "url": material_info.url,
                    "duration": material_info.duration,
                }
            )
    except Exception:
        for file_path in saved_paths:
            try:
                os.remove(file_path)
            except OSError as exc:
                logger.warning(
                    f"failed to remove local material after batch error: "
                    f"path={file_path}, error={exc}"
                )
        raise
    return materials, persisted


def _initialize_session_state():
    """Centrally initialize page state that is preserved across reruns."""
    if not st.session_state.get("cross_post_recovery_checked"):
        # WebUI can run independently without FastAPI, so it also needs to be processed during the first session initialization
        # Publishing status left behind by process restart. When recovery fails, no mark is written, and subsequent reruns will try again.
        recovered = tm.recover_interrupted_cross_posts()
        if recovered is not None:
            st.session_state["cross_post_recovery_checked"] = True

    saved_ui_language = config.ui.get("language", "")
    browser_locale = st.context.locale
    initial_ui_language = utils.resolve_ui_language(
        saved_language=saved_ui_language,
        browser_locale=browser_locale,
        supported_languages=locales.keys(),
    )

    defaults = {
        "video_subject": "",
        "video_script": "",
        "video_terms": "",
        "news_article_url_input": "",
        "scraped_article_data": None,
        "scraped_article_gemini_prompt": "",
        "video_creation_mode": "non_ai",
        "preview_image_index": 0,
        "paragraph_number_input": _saved_ui_number(
            "paragraph_number",
            1,
            llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
            llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
            int,
        ),
        "video_script_prompt": _saved_ui_text(
            "video_script_prompt",
            max_length=llm.MAX_SCRIPT_PROMPT_LENGTH,
        ),
        "custom_system_prompt": _saved_ui_text(
            "custom_system_prompt",
            llm.DEFAULT_SCRIPT_SYSTEM_PROMPT,
            llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
        ),
        "match_materials_to_script": bool(
            config.app.get("match_materials_to_script", False)
        ),
        "custom_bgm_file_input": _saved_ui_text("custom_bgm_file"),
        "sonilo_bgm_prompt_input": _saved_ui_text(
            "sonilo_bgm_prompt",
            max_length=sonilo_service.MAX_PROMPT_LENGTH,
        ),
        "elevenlabs_music_prompt_input": _saved_ui_text(
            "elevenlabs_music_prompt",
            max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
        ),
        "subtitle_enabled_checkbox": _saved_ui_bool("subtitle_enabled", True),
        "stroke_color_picker": _saved_ui_color("stroke_color", "#000000"),
        "stroke_width_slider": _saved_ui_number(
            "stroke_width", 1.5, 0.0, 10.0
        ),
        "loomloom_candidate_count": _saved_ui_number(
            "loomloom_candidate_count",
            3,
            1,
            loomloom.MAX_SCRIPT_CANDIDATES,
            int,
        ),
        "loomloom_script_duration_seconds": _saved_ui_number(
            "loomloom_script_duration_seconds", 60, 10, 600, int
        ),
        "ui_language": initial_ui_language,
        # Local materials that have been placed on disk allow users to continue to reuse them after modifying only the copy.
        "local_video_materials": [],
        # To generate a button callback, register the task first so that the top entry can immediately display the running quantity.
        "active_generation_tasks": {},
        # The most recent task submitted from the current page. After the generation is changed to background execution, the page fragment
        # Query status by this ID; refresh no longer relies on the old page script being executed.
        "current_generation_task_id": "",
        # LoomLoom queries and executions must retain exactly the same input and
        # clientRequestId, to avoid repeated payment tasks caused by network retries.
        "loomloom_script_batch": None,
        "loomloom_script_quote": None,
        "loomloom_script_input_signature": "",
        "loomloom_client_request_id": "",
        "loomloom_run_id": "",
        "loomloom_run_status": "",
        "loomloom_run_error": "",
        "loomloom_poll_failure_count": 0,
        "loomloom_poll_retry_after": 0.0,
        "loomloom_poll_paused": False,
        "loomloom_script_candidates": (),
        "loomloom_candidate_errors": (),
        "loomloom_selected_candidate": 0,
        "loomloom_video_batch": None,
        "loomloom_video_quote": None,
        "loomloom_video_input_signature": "",
        "loomloom_video_client_request_id": "",
        "loomloom_video_confirm_charge": False,
        "loomloom_video_quote_error_signature": "",
        "loomloom_video_quote_error": "",
        "loomloom_video_capability": None,
        "loomloom_video_capability_fingerprint": "",
        "loomloom_video_capability_load_attempt": "",
        "loomloom_video_capability_error": "",
        "loomloom_video_model_id": "",
        # When copywriting or complete dubbing is first generated, consume this summary and automatically
        # Fill in the number of recommended materials; it will be cleared after consumption to avoid overwriting the user's subsequent manual adjustments.
        "loomloom_video_scene_autofill_digest": "",
        "wavespeed_confirm_charge": False,
        "volcengine_seedance_confirm_charge": False,
        "ofox_confirm_charge": False,
        "metaso_minimax_confirm_charge": False,
        "muapi_confirm_charge": False,
        # AI videos are billed by material segment. By default, only one segment is generated. The user can actively increase the quantity after confirming the effect.
        "loomloom_video_scene_count": _saved_ui_number(
            "loomloom_video_scene_count",
            1,
            1,
            loomloom.MAX_VIDEO_SCENES,
            int,
        ),
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


_initialize_session_state()


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key)
    if value is not None:
        return value
    # New features will be maintained in Chinese and English first. When other languages lack individual translations, they fall back to English to avoid multiple translations.
    # After copying the same English in the locale, it loses synchronization for a long time; the original key is displayed only when the key does not exist in English.
    return locales.get("en", {}).get("Translation", {}).get(key, key)


def _t(key: str) -> str:
    loc = locales.get(st.session_state.get("ui_language", "vi"), {})
    trans = loc.get("Translation", {})
    if key in trans:
        return trans[key]
    return locales.get("en", {}).get("Translation", {}).get(key, key)


# -----------------------------------------------------------------------------
# Task management: historical scan, running status, parameter recovery and list interaction
# -----------------------------------------------------------------------------


def _format_task_time(timestamp):
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _format_task_subject(subject, max_length=30):
    subject = str(subject or "").replace("\n", " ").strip()
    if len(subject) <= max_length:
        return subject or "-"
    return f"{subject[:max_length]}..."


def _safe_load_task_script(task_path):
    script_file = os.path.join(task_path, "script.json")
    if not os.path.isfile(script_file):
        return {}

    try:
        with open(script_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            logger.warning(f"task script data is not an object: {script_file}")
            return {}
        return payload
    except Exception as e:
        logger.warning(f"failed to read task script data: {script_file}, {e}")
        return {}


def _find_final_task_video(task_path: str) -> str:
    """
    Return the final film with the smallest sequence number in the task directory.

    The compositing process also produces combined, temp-clip, and MoviePy temporary files, which cannot
    Indicates that the task has been completed successfully, so only ``final-<serial number>.<extension>`` is accepted here.
    """
    try:
        files = os.listdir(task_path)
    except OSError:
        return ""

    candidates = []
    for file_name in files:
        match = _FINAL_VIDEO_PATTERN.fullmatch(file_name)
        if match:
            candidates.append((int(match.group("index")), file_name))

    if not candidates:
        return ""

    _, file_name = min(candidates, key=lambda item: item[0])
    return os.path.join(task_path, file_name)


def _build_restore_upload_requirements(params: Mapping) -> dict:
    """
    Record uploaded file dependencies in historical tasks that cannot be automatically restored by Streamlit.

    The browser does not allow the program to repopulate file_uploader, so a separate local log is required when restoring the task
    Material and custom audio dependencies, and check whether they have been actively supplemented or replaced before user regeneration.
    """
    return {
        "local_materials": params.get("video_source") == "local",
        "custom_audio": bool(params.get("custom_audio_file")),
        "original_voice_name": params.get("voice_name") or "",
    }


def _get_unmet_restore_upload_requirements(
    requirements: Mapping | None,
    *,
    video_source: str,
    voice_name: str,
    has_local_materials: bool,
    has_custom_audio: bool,
    voice_mode: str | None = None,
) -> set[str]:
    """Returns historical uploaded file dependencies that are still unsatisfied by the current form."""
    requirements = requirements or {}
    unmet = set()

    if (
        requirements.get("local_materials")
        and video_source == "local"
        and not has_local_materials
    ):
        unmet.add("local_materials")

    if requirements.get("custom_audio") and not has_custom_audio:
        if voice_mode is not None:
            # The new version of WebUI uses explicit voiceover. The user switches to automatic dubbing or no dubbing, indicating
            # Historically uploaded audio has been actively replaced; re-uploading is only required if the upload mode continues to be selected.
            if voice_mode == VOICE_MODE_UPLOAD:
                unmet.add("custom_audio")
        elif voice_name == requirements.get("original_voice_name", ""):
            # Keep the old caller's compatibility behavior based on timbre to avoid affecting the API and existing testing tools.
            unmet.add("custom_audio")

    return unmet


def _queue_task_restore(task_id):
    # The task list runs in a fragment and cannot directly modify the state of the created main form control.
    # Here only candidate tasks are recorded and a full page rerun is triggered. Confirmation and parameter recovery are handled uniformly by the main page.
    st.session_state["task_restore_candidate_id"] = task_id
    st.session_state["task_manager_popover_nonce"] = (
        st.session_state.get("task_manager_popover_nonce", 0) + 1
    )
    st.rerun(scope="app")


def _normalize_task_state(state):
    if state in (
        const.TASK_STATE_COMPLETE,
        const.TASK_STATE_FAILED,
        const.TASK_STATE_PROCESSING,
    ):
        return state
    try:
        return int(state)
    except (TypeError, ValueError):
        return state


def _active_generation_tasks():
    tasks = st.session_state.setdefault("active_generation_tasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
        st.session_state["active_generation_tasks"] = tasks
    return tasks


def _add_active_generation_task(task_id, subject=None):
    tasks = _active_generation_tasks()
    task = tasks.setdefault(task_id, {})
    task["subject"] = subject or task.get("subject") or task_id
    task["mtime"] = task.get("mtime") or datetime.now().timestamp()


def _remove_active_generation_task(task_id):
    tasks = _active_generation_tasks()
    if task_id in tasks:
        del tasks[task_id]
    if st.session_state.get("pending_generation_task_id") == task_id:
        del st.session_state["pending_generation_task_id"]


def _prepare_generation_task():
    # st.button's on_click will be triggered before the page script is re-executed. Generate the task ID in advance here,
    # The top task management entry can display the number of "generating" in the same rerun.
    task_id = str(uuid4())
    st.session_state["pending_generation_task_id"] = task_id
    subject = st.session_state.get("video_subject") or st.session_state.get(
        "video_script"
    )
    _add_active_generation_task(task_id, subject=subject)


def _task_state_label(state, has_video):
    normalized_state = _normalize_task_state(state)
    if normalized_state == const.TASK_STATE_COMPLETE:
        return tr("Task Status Complete")
    if normalized_state == const.TASK_STATE_FAILED:
        return tr("Task Status Failed")
    if normalized_state == const.TASK_STATE_PROCESSING:
        return tr("Task Status Processing")
    if has_video:
        return tr("Task Status Complete")
    return tr("Task Status History")


def _task_state_filter_key(task):
    normalized_state = _normalize_task_state(task.get("state"))
    if normalized_state == const.TASK_STATE_PROCESSING:
        return "processing"
    if normalized_state == const.TASK_STATE_FAILED:
        return "failed"
    if normalized_state == const.TASK_STATE_COMPLETE or task["video_file"]:
        return "complete"
    return "history"


def _scan_history_tasks(limit=30):
    tasks_root = utils.task_dir()
    if not os.path.isdir(tasks_root):
        return []

    # The task management fragment is refreshed every two seconds. First read only low-cost directory metadata and intercept the most recent
    # task, and then parse the script.json and video list to avoid repeatedly scanning the entire content when there are many historical tasks.
    task_entries = []
    try:
        with os.scandir(tasks_root) as entries:
            for entry in entries:
                try:
                    if entry.name.startswith(".") or not entry.is_dir(
                        follow_symlinks=False
                    ):
                        continue
                    task_entries.append(
                        (
                            entry.stat(follow_symlinks=False).st_mtime,
                            entry.name,
                            entry.path,
                        )
                    )
                except OSError as e:
                    # Individual task directories may be being deleted, and this should not render the entire task panel useless.
                    logger.debug(f"skip unavailable task directory: {entry.path}, {e}")
    except OSError as e:
        logger.warning(f"failed to scan task directory: {tasks_root}, {e}")
        return []

    task_entries.sort(key=lambda item: item[0], reverse=True)
    tasks = []
    for mtime, name, task_path in task_entries[:limit]:
        script_data = _safe_load_task_script(task_path)
        params_data = script_data.get("params", {})
        if not isinstance(params_data, dict):
            params_data = {}
        script_text = script_data.get("script", "")
        if not isinstance(script_text, str):
            script_text = ""
        video_file = _find_final_task_video(task_path)
        subject = (
            params_data.get("video_subject")
            or script_text[:40]
            or name
        )
        tasks.append(
            {
                "task_id": name,
                "subject": subject,
                "state": const.TASK_STATE_COMPLETE if video_file else None,
                "progress": 100 if video_file else 0,
                "mtime": mtime,
                "task_path": task_path,
                "video_file": video_file,
                "source": "history",
            }
        )

    return tasks


def _collect_task_summaries(limit=20):
    history_tasks = {task["task_id"]: task for task in _scan_history_tasks(limit=50)}
    active_tasks = _active_generation_tasks()

    try:
        runtime_tasks, _ = sm.state.get_all_tasks(1, 50)
    except Exception as e:
        logger.warning(f"failed to load runtime tasks: {e}")
        runtime_tasks = []

    # The paginated state view can omit this session's newer tasks after 50
    # older records. Read those active IDs directly so a completed or failed
    # task cannot remain labelled as processing forever.
    runtime_ids = {task.get("task_id") for task in runtime_tasks}
    for task_id in active_tasks:
        if task_id in runtime_ids:
            continue
        try:
            task = sm.state.get_task(task_id)
        except Exception as e:
            logger.warning(f"failed to load active task {task_id}: {e}")
            continue
        if task:
            runtime_tasks.append(task)

    for task in runtime_tasks:
        task_id = task.get("task_id", "")
        if not task_id:
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_task = history_tasks.get(task_id, {})
        video_files = task.get("videos") or []
        video_file = (
            video_files[0] if video_files else history_task.get("video_file", "")
        )
        subject = (
            task.get("video_subject")
            or history_task.get("subject")
            or (task.get("script", "")[:40] if task.get("script") else "")
            or task_id
        )
        task_mtime = active_tasks.get(task_id, {}).get("mtime") or history_task.get(
            "mtime", 0
        )
        if os.path.isdir(task_path):
            try:
                task_mtime = os.path.getmtime(task_path)
            except OSError:
                # Another session can delete this directory between isdir and
                # getmtime. Keep rendering the persisted task state.
                pass

        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": subject,
            "state": task.get("state"),
            "cross_post_state": task.get("cross_post_state"),
            "progress": int(task.get("progress", 0) or 0),
            "mtime": task_mtime,
            "task_path": task_path,
            "video_file": video_file,
            "source": "runtime",
        }

    for task_id, active_task in active_tasks.items():
        history_task = history_tasks.get(task_id, {})
        if history_task and _task_state_filter_key(history_task) in {
            "complete",
            "failed",
        }:
            # The active tag in the session is only responsible for covering the very short window just before the task is submitted to the state store.
            # After the background task ends, the real final state must prevail, and failed tasks cannot be redisplayed as being generated.
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": active_task.get("subject")
            or history_task.get("subject")
            or task_id,
            "state": const.TASK_STATE_PROCESSING,
            "progress": history_task.get("progress", 0),
            "mtime": active_task.get("mtime")
            or history_task.get("mtime", datetime.now().timestamp()),
            "task_path": task_path,
            "video_file": history_task.get("video_file", ""),
            "source": "active",
        }

    tasks = list(history_tasks.values())
    return sorted(tasks, key=lambda item: item["mtime"], reverse=True)[:limit]


def _is_headless_server():
    # In Docker or desktop-less server deployment, the WebUI process does not have access to the user's desktop environment:
    # xdg-open/webbrowser will only fail silently within the container. This should be changed to in-browser preview
    # Video, use path prompt instead of opening directory. macOS/Windows desktop deployments are not affected.
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _open_task_path(task_path):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path: {normalized_path}")
        return
    if not os.path.isdir(normalized_path):
        return
    if _is_headless_server():
        # The storage directory is usually mapped back to the host as a volume mount, and files can be located by prompting for relative paths.
        rel_path = os.path.relpath(normalized_path, os.path.dirname(tasks_root))
        st.toast(f"{tr('Open Task Folder')}: ./storage/{rel_path}", icon="📂")
        return
    webbrowser.open(f"file://{normalized_path}")


def _open_task_video(video_file):
    tasks_root = os.path.abspath(utils.task_dir())
    normalized_file = os.path.abspath(video_file)

    # Video paths come from task directory scans or runtime status. There is still a restriction that only the task directory can be opened.
    # files within the UI to prevent UI operations from being expanded by abnormal paths into arbitrary local file opening capabilities.
    if not normalized_file.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task video path: {normalized_file}")
        return
    if not os.path.isfile(normalized_file):
        logger.warning(f"task video does not exist: {normalized_file}")
        return

    if _is_headless_server():
        # When there is no desktop environment, the player preview is embedded in the task panel instead of calling the system player.
        st.session_state["task_preview_video_file"] = normalized_file
        return

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", normalized_file])
        elif sys.platform.startswith("win"):
            os.startfile(normalized_file)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", normalized_file])
    except Exception as e:
        logger.error(f"failed to open task video: {normalized_file}, {e}")


def _delete_task(task_id, task_path, task_state=None):
    # The status of page display may lag behind background tasks. Also check the incoming status and current session before deleting
    # Active tasks and latest status to avoid accidental deletion when a task has just started or an intermediate video has been produced.
    current_task = None
    try:
        current_task = sm.state.get_task(task_id)
    except Exception as e:
        logger.exception(f"failed to verify task state before deletion: {task_id}, {e}")
        return False

    task_snapshot = dict(current_task or {})
    task_snapshot.setdefault("state", task_state)
    if task_id in _active_generation_tasks():
        task_snapshot["state"] = const.TASK_STATE_PROCESSING

    if tm.is_task_busy(task_snapshot):
        logger.warning(f"refused to delete running task: {task_id}")
        return False

    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)

    # Deleting a task removes the task status and local build files. This must be limited to storage/tasks
    # to avoid accidental deletion of other local directories caused by abnormal task_path.
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path for deletion: {normalized_path}")
        return False

    try:
        if hasattr(sm.state, "delete_task"):
            sm.state.delete_task(task_id)
        if os.path.isdir(normalized_path):
            shutil.rmtree(normalized_path)
        logger.info(f"deleted task: {task_id}")
        return True
    except Exception as e:
        logger.exception(f"failed to delete task: {task_id}, {e}")
        return False


def _count_processing_tasks(tasks):
    # The top task management portal only needs to display the number of "generating" tasks.
    # The internal state key judgment is reused here to avoid relying on multi-language display copywriting to cause statistical inconsistency in different languages.
    processing_task_ids = {
        task["task_id"]
        for task in tasks
        if _task_state_filter_key(task) == "processing"
    }
    return len(processing_task_ids)


def _task_manager_label(processing_count):
    label = tr("Task Manager")
    if processing_count <= 0:
        return label
    return f"{label} · {processing_count}"


def _build_video_download_name(subject, index, total):
    """Generate cross-platform secure download file names based on video themes."""
    safe_subject = _DOWNLOAD_FILENAME_INVALID_PATTERN.sub(" ", str(subject or ""))
    safe_subject = re.sub(r"\s+", " ", safe_subject).strip(" .")[:80].rstrip(" .")
    if not safe_subject:
        safe_subject = "video"
    # Win32 ignores trailing spaces and periods before the extension when recognizing device names. Upload with background music
    # Consistent with existing rules to avoid ``CON .topic`` bypassing reserved name protection.
    windows_basename = safe_subject.split(".", 1)[0].rstrip(" .").upper()
    if windows_basename in _WINDOWS_RESERVED_FILENAMES:
        safe_subject = f"_{safe_subject}"

    suffix = f"-{index}" if total > 1 else ""
    return f"{safe_subject}{suffix}.mp4"


def _render_task_table(filtered_tasks, key_prefix):
    with st.container(key=f"task_table_header_{key_prefix}"):
        header_cols = st.columns([1.1, 1.7, 3.0, 0.8, 1.6], vertical_alignment="center")
        header_cols[0].caption(tr("Task Status"))
        header_cols[1].caption(tr("Task Updated At"))
        header_cols[2].caption(tr("Task Subject"))
        header_cols[3].caption(tr("Task Progress"))
        header_cols[4].caption(tr("Task Actions"))

    if not filtered_tasks:
        st.info(tr("No Tasks Match Filter"))
        return

    visible_tasks = filtered_tasks[:12]
    list_height = min(390, max(96, len(visible_tasks) * 58))
    with st.container(height=list_height, border=False):
        for task in visible_tasks:
            task_id = task["task_id"]
            has_video = bool(task["video_file"] and os.path.isfile(task["video_file"]))
            is_processing = _task_state_filter_key(task) == "processing"
            is_busy = is_processing or tm.is_task_busy(task)
            has_restore_data = os.path.isfile(
                os.path.join(task["task_path"], "script.json")
            )
            safe_task_key = "".join(ch if ch.isalnum() else "_" for ch in task_id)[:40]

            # Use Streamlit native bordered container + columns to preserve per-row operations.
            # Compared with custom HTML/CSS tables, this method is more stable against Streamlit version changes;
            # Compared with dataframe, it can retain inline actions such as playing, opening directories, and deleting.
            with st.container(
                key=f"task_row_{key_prefix}_{safe_task_key}", border=True
            ):
                row_cols = st.columns(
                    [1.1, 1.7, 3.0, 0.8, 1.6],
                    vertical_alignment="center",
                )
                row_cols[0].write(_task_state_label(task["state"], has_video))
                row_cols[1].write(_format_task_time(task["mtime"]))
                row_cols[2].write(_format_task_subject(task["subject"]))
                row_cols[3].write(f"{task['progress']}%")

                action_cols = row_cols[4].columns(
                    4,
                    vertical_alignment="center",
                    gap="small",
                )
                with action_cols[0]:
                    play_label = tr("Play")
                    if st.button(
                        play_label,
                        key=f"play_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/play_arrow:",
                        help=play_label,
                        disabled=not has_video,
                    ):
                        _open_task_video(task["video_file"])

                with action_cols[1]:
                    open_label = tr("Open Task Folder")
                    if st.button(
                        open_label,
                        key=f"open_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/folder_open:",
                        help=open_label,
                    ):
                        _open_task_path(task["task_path"])

                with action_cols[2]:
                    restore_label = tr("Regenerate Task")
                    if st.button(
                        restore_label,
                        key=f"restore_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/replay:",
                        help=restore_label,
                        disabled=is_processing or not has_restore_data,
                    ):
                        _queue_task_restore(task_id)

                with action_cols[3]:
                    delete_label = tr("Delete Task")
                    delete_help = (
                        f"{delete_label} ({tr('Task Status Processing')})"
                        if is_busy
                        else delete_label
                    )
                    if st.button(
                        delete_label,
                        key=f"delete_task_{key_prefix}_{task_id}",
                        use_container_width=True,
                        icon=":material/delete:",
                        help=delete_help,
                        disabled=is_busy,
                    ):
                        if _delete_task(task_id, task["task_path"], task["state"]):
                            st.toast(tr("Task Deleted"))
                            st.rerun()
                        else:
                            st.error(tr("Task Delete Failed"))


def _render_task_manager_panel(tasks=None):
    tasks = tasks if tasks is not None else _collect_task_summaries()
    if not tasks:
        st.info(tr("No Tasks Yet"))
        return

    # Streamlit 1.59 supports lazy rendering of stateful Tabs. Only the current list is rebuilt when switching,
    # Avoid scheduled fragments to repeatedly create four sets of task rows and action buttons every two seconds.
    status_tabs = [
        ("all", tr("All Tasks")),
        ("processing", tr("Task Status Processing")),
        ("complete", tr("Task Status Complete")),
        ("failed", tr("Task Status Failed")),
    ]
    tabs = st.tabs(
        [label for _, label in status_tabs],
        key="task_manager_status_tabs",
        on_change="rerun",
    )
    for (status_key, _), tab in zip(status_tabs, tabs):
        if not tab.open:
            continue
        with tab:
            filtered_tasks = [
                task
                for task in tasks
                if status_key == "all" or _task_state_filter_key(task) == status_key
            ]
            _render_task_table(filtered_tasks, status_key)

    _render_task_video_preview()


def _render_task_video_preview():
    # In-browser fallback without "Play" button in desktop deployment: Render player at bottom of task panel.
    preview_file = st.session_state.get("task_preview_video_file")
    if not preview_file:
        return

    tasks_root = os.path.abspath(utils.task_dir())
    if not (
        preview_file.startswith(tasks_root + os.sep) and os.path.isfile(preview_file)
    ):
        st.session_state.pop("task_preview_video_file", None)
        return

    st.divider()
    preview_cols = st.columns([5, 1], vertical_alignment="center")
    task_name = os.path.basename(os.path.dirname(preview_file))
    preview_cols[0].caption(f"{os.path.basename(preview_file)} · {task_name}")
    closed = preview_cols[1].button(
        "✕",
        key="close_task_video_preview",
        use_container_width=True,
        help=tr("Close"),
    )
    if closed:
        st.session_state.pop("task_preview_video_file", None)
        return
    st.video(preview_file)


@st.fragment(run_every="2s")
def _render_task_manager_entry():
    # Tasks may be triggered by the current page or other pages. The entrance is refreshed regularly using fragment alone.
    # Only the task number and popover content are updated, without interrupting the main page form input.
    task_summaries = _collect_task_summaries()
    processing_task_count = _count_processing_tasks(task_summaries)
    with st.container(key="task_manager_entry", width="content"), st.popover(
        _task_manager_label(processing_task_count),
        width="content",
        key=(
            "task_manager_popover_"
            f"{st.session_state.get('task_manager_popover_nonce', 0)}"
        ),
    ):
        _render_task_manager_panel(task_summaries)


def _load_task_restore_payload(task_id):
    tasks_root = os.path.realpath(utils.task_dir())
    task_path = os.path.realpath(os.path.join(tasks_root, str(task_id)))
    try:
        if os.path.commonpath([tasks_root, task_path]) != tasks_root:
            raise ValueError("task path is outside the task directory")
    except ValueError as e:
        logger.warning(f"invalid task restore path: {task_id}, {e}")
        return None

    script_data = _safe_load_task_script(task_path)
    raw_params = script_data.get("params")
    if not isinstance(raw_params, dict):
        logger.warning(f"task has no restorable parameters: {task_id}")
        return None

    params_input = dict(raw_params)
    if script_data.get("script"):
        params_input["video_script"] = script_data["script"]
    if script_data.get("search_terms"):
        params_input["video_terms"] = script_data["search_terms"]

    try:
        params = VideoParams.model_validate(params_input).model_dump(mode="json")
    except Exception as e:
        logger.warning(f"failed to validate task restore parameters: {task_id}, {e}")
        return None

    return {
        "task_id": str(task_id),
        "subject": params.get("video_subject") or script_data.get("script") or task_id,
        "params": params,
    }


def _infer_tts_server_from_voice(voice_name):
    if voice.is_no_voice(voice_name):
        return voice.NO_VOICE_NAME
    if voice.is_siliconflow_voice(voice_name):
        return "siliconflow"
    if voice.is_gemini_voice(voice_name):
        return "gemini-tts"
    if voice.is_mimo_voice(voice_name):
        return "mimo-tts"
    if voice.is_minimax_voice(voice_name):
        return "minimax-tts"
    if voice.is_elevenlabs_voice(voice_name):
        return "elevenlabs"
    if voice.is_chatterbox_voice(voice_name):
        return "chatterbox"
    if voice.is_kokoro_voice(voice_name):
        return "kokoro"
    if voice.is_fish_audio_voice(voice_name):
        return "fish_audio"
    if voice.is_voxcpm_voice(voice_name):
        return "voxcpm"
    if voice.is_azure_v2_voice(voice_name):
        return "azure-tts-v2"
    return "azure-tts-v1"


def _set_stable_widget_value(key, value):
    if value is not None:
        st.session_state[localized_widget_key(key)] = value


def _apply_pending_task_restore():
    payload = st.session_state.pop("task_restore_payload", None)
    if not payload:
        return False

    _apply_restored_params(payload["params"])
    st.session_state["task_restore_succeeded"] = True
    logger.info(f"restored task configuration: {payload['task_id']}")
    return True


def _apply_restored_params(params):
    """
    Write a complete copy of the generated parameters back to the page control state.

    Historical task recovery and setting preset import use the same parameter model, so they share the same implementation to avoid
    When adding a new field, only one of the paths is updated. The caller must execute before rendering any controls, otherwise
    Streamlit will refuse to modify the state of an already instantiated control.
    """
    video_terms = params.get("video_terms") or ""
    if isinstance(video_terms, list):
        video_terms = ", ".join(str(term) for term in video_terms)

    # Copywriting and advanced script settings.
    st.session_state["video_subject"] = params.get("video_subject") or ""
    st.session_state["video_script"] = params.get("video_script") or ""
    st.session_state["video_terms"] = str(video_terms)
    _set_stable_widget_value(
        "script_language_select", params.get("video_language") or ""
    )
    st.session_state["paragraph_number_input"] = params.get("paragraph_number", 1)
    st.session_state["video_script_prompt"] = params.get("video_script_prompt") or ""
    st.session_state["custom_system_prompt"] = (
        params.get("custom_system_prompt") or llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
    )

    # Video settings. The material upload control cannot be written by the server, so local materials need to be re-selected by the user.
    video_source = params.get("video_source") or "pexels"
    _set_stable_widget_value("video_source_select", video_source)
    _set_stable_widget_value(
        "video_concat_mode_select", params.get("video_concat_mode") or "random"
    )
    _set_stable_widget_value(
        "video_transition_mode_select",
        params.get("video_transition_mode") or VideoTransitionMode.none.value,
    )
    _set_stable_widget_value(
        "image_motion_mode_select",
        params.get("image_motion_mode") or "random",
    )
    _set_stable_widget_value(
        f"video_aspect_for_{video_source}",
        params.get("video_aspect") or VideoAspect.portrait.value,
    )
    _set_stable_widget_value(
        "video_fit_mode_select",
        params.get("video_fit_mode") or VideoFitMode.cover.value,
    )
    _set_stable_widget_value(
        "video_clip_duration_select", params.get("video_clip_duration", 3)
    )
    _set_stable_widget_value(
        "video_clip_speed_slider",
        # The API can be written faster than the WebUI can handle, and the task generation phase is safely normalized, but
        # History may still retain its original value. Normalize again before resuming the task to avoid giving Streamlit
        # Slider injection of out-of-bounds values, NaN or infinite values causes abnormal control status.
        utils.normalize_clip_speed(params.get("video_clip_speed", 1.0)),
    )
    _set_stable_widget_value("video_count_select", params.get("video_count", 1))
    st.session_state["match_materials_to_script"] = bool(
        params.get("match_materials_to_script", False)
    )

    # Audio settings. TTS server does not write old tasks, inferred based on historical voice_name.
    voice_name = params.get("voice_name") or voice.NO_VOICE_NAME
    tts_server = _infer_tts_server_from_voice(voice_name)
    if params.get("custom_audio_file"):
        voice_mode = VOICE_MODE_UPLOAD
    elif voice.is_no_voice(voice_name):
        voice_mode = VOICE_MODE_NONE
    else:
        voice_mode = VOICE_MODE_TTS
    _set_stable_widget_value("voice_mode_control", voice_mode)
    if tts_server != voice.NO_VOICE_NAME:
        _set_stable_widget_value("tts_server_select", tts_server)
        _set_stable_widget_value(f"speech_synthesis_select_{tts_server}", voice_name)
    _set_stable_widget_value("voice_volume_select", params.get("voice_volume", 1.0))
    _set_stable_widget_value("voice_rate_select", params.get("voice_rate", 1.0))
    bgm_type = params.get("bgm_type") or ""
    _set_stable_widget_value("bgm_type_select", bgm_type)
    _set_stable_widget_value("bgm_volume_select", params.get("bgm_volume", 0.2))
    if bgm_type == "preset" and params.get("bgm_file"):
        # The preset song control uses the filename as a stable business value. Historical tasks may save absolute paths or
        # Relative path, after uniformly taking basename, it can match the currently safely enumerated song list.
        _set_stable_widget_value(
            "preset_song_select", os.path.basename(str(params["bgm_file"]))
        )
    st.session_state["custom_bgm_file_input"] = params.get("bgm_file") or ""
    st.session_state["sonilo_bgm_prompt_input"] = (
        params.get("video_music_prompt") or params.get("sonilo_bgm_prompt") or ""
    )
    st.session_state["elevenlabs_music_prompt_input"] = (
        params.get("video_music_prompt") or ""
    )

    # Subtitle settings. Minimize the out-of-bounds values ​​in old tasks to prevent Slider from failing to initialize.
    st.session_state["subtitle_enabled_checkbox"] = bool(
        params.get("subtitle_enabled", True)
    )
    _set_stable_widget_value("font_name_select", params.get("font_name") or "")
    _set_stable_widget_value(
        "subtitle_position_select", params.get("subtitle_position") or "bottom"
    )
    _set_stable_widget_value(
        "subtitle_display_mode_select", params.get("subtitle_display_mode") or "sentence"
    )
    _set_stable_widget_value(
        "subtitle_animation_select", params.get("subtitle_animation") or "none"
    )
    custom_position = min(100.0, max(0.0, float(params.get("custom_position", 70.0))))
    st.session_state["custom_position_input"] = str(custom_position)
    st.session_state["font_color_picker"] = params.get("text_fore_color") or "#FFFFFF"
    st.session_state["font_size_slider"] = min(
        100, max(30, int(params.get("font_size", 60)))
    )
    st.session_state["stroke_color_picker"] = params.get("stroke_color") or "#000000"
    st.session_state["stroke_width_slider"] = min(
        10.0, max(0.0, float(params.get("stroke_width", 1.5)))
    )
    background_color = params.get("text_background_color")
    background_enabled = bool(background_color)
    st.session_state["subtitle_background_enabled_checkbox"] = background_enabled
    if isinstance(background_color, str):
        st.session_state["subtitle_background_color_picker"] = background_color
    st.session_state["rounded_subtitle_background_checkbox"] = bool(
        params.get("rounded_subtitle_background", False) and background_enabled
    )

    st.session_state.pop("local_video_materials_uploader", None)
    # Historical tasks only save the material paths, and there is no guarantee that these files will still exist in the current environment.
    # At the same time, clear the cached uploaded materials on the current page to avoid misuse of files from another task after recovery.
    st.session_state["local_video_materials"] = []
    st.session_state.pop("custom_audio_file_uploader", None)
    st.session_state.pop("voxcpm_reference_audio_uploader", None)
    st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY, None)
    st.session_state.pop("voxcpm_prompt_audio_uploader", None)
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_TEXT_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_HIGH_FIDELITY_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY, None)
    st.session_state.pop("custom_bgm_uploader", None)
    st.session_state.pop("custom_bgm_validation", None)
    st.session_state["task_restore_upload_requirements"] = (
        _build_restore_upload_requirements(params)
    )

    return True


def _dismiss_task_restore_dialog():
    st.session_state.pop("task_restore_candidate_id", None)


@st.dialog(
    tr("Regenerate Task"),
    width="small",
    on_dismiss=_dismiss_task_restore_dialog,
)
def _render_task_restore_dialog(task_id):
    payload = _load_task_restore_payload(task_id)
    if payload is None:
        st.error(tr("Task Restore Failed"))
        if st.button(tr("Cancel"), key="cancel_invalid_task_restore"):
            st.session_state.pop("task_restore_candidate_id", None)
            st.rerun(scope="app")
        return

    st.write(tr("Regenerate Task Confirmation"))
    st.caption(_format_task_subject(payload["subject"], max_length=80))
    cancel_col, load_col = st.columns(2)
    if cancel_col.button(
        tr("Cancel"),
        key="cancel_task_restore",
        use_container_width=True,
    ):
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")
    if load_col.button(
        tr("Load Task Configuration"),
        key="confirm_task_restore",
        type="primary",
        use_container_width=True,
    ):
        st.session_state["task_restore_payload"] = payload
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")


def _dismiss_settings_dialog():
    """Close the settings popup and ensure that the next full page rerun does not open it automatically again."""
    st.session_state["settings_dialog_open"] = False


def _open_settings_dialog(target_tab=None):
    """Open the settings pop-up window and navigate directly to the specified business tab."""
    st.session_state["settings_dialog_open"] = True
    if target_tab:
        # Only the stable business ID is saved here, and the translated text is not saved; before actually creating tabs, the
        # The current interface language parses the label to prevent the old copy from becoming an illegal option after the user switches languages.
        st.session_state["settings_dialog_target_tab"] = target_tab


def _open_material_settings_dialog():
    """For video source component callback use: directly open the material service settings."""
    _open_settings_dialog("material")


def _render_brand(available_update: str | None = None):
    """Render project name, current version and optional update entry."""
    update_link = ""
    if available_update:
        update_label = html.escape(
            tr("Update Available").format(version=available_update)
        )
        # Streamlit will continue to parse the incoming HTML using Markdown. Keep the link as a single line here,
        # Prevent the indentation of multi-line strings from being recognized as code blocks, causing the page to directly display the HTML source code.
        update_link = (
            '<a class="mpt-brand__update" '
            f'href="{version_checker.LATEST_RELEASE_PAGE_URL}" '
            'target="_blank" rel="noopener noreferrer" '
            f'aria-label="{update_label}" title="{update_label}">'
            f"{update_label}</a>"
        )
    st.markdown(
        f"""
        <h1 class="mpt-brand">
            <span class="mpt-brand__name">VietNamNewsVideo</span>
            <a class="mpt-brand__version"
               href="https://github.com/Thangvn2006/vietnam-news-video"
               target="_blank"
               rel="noopener noreferrer"
               aria-label="Open VietNamNewsVideo on GitHub"
               title="Open project on GitHub">v{html.escape(str(config.project_version))}</a>
            {update_link}
        </h1>
        """,
        unsafe_allow_html=True,
    )


@st.fragment(run_every="1s")
def _render_pending_version_check():
    """Only refresh the branding area while the check is pending, avoiding blocking or repeatedly re-executing the full-page form."""
    snapshot = version_checker.poll_available_update(config.project_version)
    if snapshot.complete:
        # After the check is completed, refresh the entire page, change the top bar to static rendering and stop fragment polling.
        # This refresh occurs after the background request is completed and does not delay other content of the initial page.
        st.rerun(scope="app")
    _render_brand()


def _render_top_bar():
    """Render the top bar of the page consisting of branding, task management, settings and language switching."""
    # The top bar is divided into two independent areas: brand area and operation area. Narrow screen by Streamlit
    # Wrap the two areas as a whole, and then automatically wrap the inside of the operation area according to the remaining width.
    with st.container(key="top_bar"):
        brand_col, actions_col = st.columns(
            [3.5, 2.0],
            vertical_alignment="center",
            gap="small",
        )

    with brand_col:
        update_snapshot = version_checker.poll_available_update(config.project_version)
        if update_snapshot.complete:
            _render_brand(update_snapshot.available_version)
        else:
            _render_pending_version_check()

    with actions_col:
        with st.container(
            key="top_bar_actions",
            horizontal=True,
            horizontal_alignment="right",
            vertical_alignment="center",
            gap="small",
            width="stretch",
        ):
            _render_task_manager_entry()

            st.button(
                tr("Settings"),
                key="open_settings_dialog_button",
                type="secondary",
                icon=":material/settings:",
                width="content",
                on_click=_open_settings_dialog,
            )

            FLAG_MAP = {
                "vi": "🇻🇳 Tiếng Việt",
                "en": "🇺🇸 English",
                "zh": "🇨🇳 简体中文",
                "fr": "🇫🇷 Français",
                "de": "🇩🇪 Deutsch",
                "es": "🇪🇸 Español",
                "it": "🇮🇹 Italiano",
                "ru": "🇷🇺 Русский",
                "ko": "🇰🇷 한국어",
                "pt": "🇵🇹 Português",
                "id": "🇮🇩 Bahasa Indonesia",
                "tr": "🇹🇷 Türkçe",
                "ca": "Català",
                "az": "🇦🇿 Azərbaycan",
            }
            priority_order = ["vi", "en", "zh"]
            language_codes = sorted(
                locales.keys(),
                key=lambda c: (priority_order.index(c) if c in priority_order else 99, c),
            )
            current_lang = st.session_state.get("ui_language", "vi")
            selected_index = (
                language_codes.index(current_lang) if current_lang in language_codes else 0
            )

            selected_language_code = st.selectbox(
                _t("Switch Language"),
                options=language_codes,
                index=selected_index,
                format_func=lambda code: str(FLAG_MAP.get(code) or (locales.get(code, {}).get("Language") if isinstance(locales.get(code), dict) else None) or code),
                key="top_language_code_selector",
                label_visibility="collapsed",
                help=_t("Switch Language"),
            )
            if selected_language_code:
                previous_language = st.session_state.get("ui_language", "")
                if selected_language_code != previous_language:
                    logger.info(
                        "UI language changed by user: "
                        f"previous_language={previous_language or '<empty>'}, "
                        f"selected_language={selected_language_code}"
                    )
                    st.session_state["ui_language"] = selected_language_code
                    # Browser automatic recognition only affects the current session; only when the user actively switches the drop-down box
                    # Write to config.toml and subsequent new sessions will take precedence over this explicit selection.
                    _set_runtime_config("ui", "language", selected_language_code)
                    _save_runtime_config()
                    # Switching the language will rerun the top bar first, and the rerun will be triggered before the text control is rendered.
                    # Explicitly retain the content of this creation to prevent Streamlit from cleaning up the old control state.
                    # The new language page resets the entered topics, copywriting and keywords to empty.
                    for content_key in ("video_subject", "video_script", "video_terms"):
                        if content_key in st.session_state:
                            st.session_state[content_key] = st.session_state[content_key]
                    # Force refresh after switching languages to prevent the selectbox from continuing to display the old language copy.
                    st.rerun()


support_locales = [
    "ca-ES",
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "es-ES",
    "fr-FR",
    "it-IT",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


# -----------------------------------------------------------------------------
# Common UI components, resource caching and logging
# -----------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner=False)
def get_all_fonts():
    # The font directory rarely changes, but Streamlit reruns the page every time the control is interacted with. short term cache
    # It can avoid continuous repetition of os.walk and ensure that the newly added font can be discovered in up to 30 seconds.
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


@st.cache_data(ttl=30, show_spinner=False)
def get_all_songs():
    # Background music and fonts use the same short-cycle strategy, without permanent caching, taking into account rerun performance and
    # Scenario where the user manually adds music files during runtime.
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        # task_id should always be a server-generated UUID. Here we do format verification first to avoid outliers.
        # Access locations outside the task directory through path splicing, and avoid triggering when the directory is subsequently opened.
        # The platform shell's interpretation of special characters.
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(os.path.join(root_dir, "storage", "tasks"))
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        # Even if the UUID verification passes, confirm again that the final path is still within the task root directory to avoid
        # The risk of path traversal will be introduced when the caller adjusts the source of task_id in the future.
        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.exception(f"failed to open task folder: task_id={task_id}, error={e}")


@st.cache_resource
def init_log():
    # The basic log Handler is a process-level resource, not a page session state. Streamlit per component
    # Interaction will rerun the page script, and code hot reloading may also invalidate the cache. Log initialization can only
    # Exactly replace the terminal Handler and cannot clear the WebUI temporary Handler used by the task being generated.
    _lvl = "DEBUG"

    return configure_terminal_logger(
        sys.stdout,
        level=_lvl,
        colorize=True,
    )


init_log()


def tr_optional(key, fallback_language=""):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key, "")
    if not value and fallback_language:
        fallback_loc = locales.get(fallback_language, {})
        value = fallback_loc.get("Translation", {}).get(key, "")
    return value if value else ""


def render_onboarding_tour():
    """Disabled startup guide/tour per user request."""
    return


def _render_generation_logs(task_id):
    """Renders background task log snapshots without accessing Streamlit session state from worker threads."""
    if config.ui.get("hide_log", False):
        return

    log_records = webui_task.get_task_logs(task_id)
    if not log_records:
        return

    st.code("\n".join(log_records))


def _render_generation_task_snapshot(task_id, task):
    """Render progress, failure reason, or final film based on snapshots in the state store."""
    if not task:
        st.info(tr("Generating Video"))
        _render_generation_logs(task_id)
        return

    state = _normalize_task_state(task.get("state"))
    progress = max(0, min(100, int(task.get("progress", 0) or 0)))
    if state == const.TASK_STATE_PROCESSING:
        st.info(tr("Generating Video"))
        st.progress(
            progress,
            text=f"{tr('Task Progress')}: {progress}%",
        )
        _render_generation_logs(task_id)
        return

    if state == const.TASK_STATE_FAILED:
        error = str(task.get("error") or "").strip()
        message = tr("Video Generation Failed")
        st.error(f"{message}: {error}" if error else message)
        _render_generation_logs(task_id)
        return

    video_files = task.get("videos") or []
    if state != const.TASK_STATE_COMPLETE or not video_files:
        st.error(tr("Video Generation Failed"))
        _render_generation_logs(task_id)
        return

    st.success(tr("Video Generation Completed"))
    for warning in task.get("warnings") or []:
        if isinstance(warning, Mapping) and warning.get("code") == "batch_materials_reused":
            st.warning(
                tr("Batch Material Reuse Warning").format(
                    index=warning.get("video_index", ""), count=warning.get("count", 0)
                )
            )
        elif isinstance(warning, Mapping) and warning.get("code") == "sonilo_bgm_failed":
            st.warning(
                tr("Sonilo BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        elif (
            isinstance(warning, Mapping)
            and warning.get("code") == "elevenlabs_bgm_failed"
        ):
            st.warning(
                tr("ElevenLabs BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        else:
            st.warning(str(warning))

    available_videos = [
        (index, url)
        for index, url in enumerate(video_files)
        if os.path.isfile(url)
    ]
    for index, url in enumerate(video_files):
        if os.path.isfile(url):
            continue
        logger.warning(
            f"generated video is unavailable: "
            f"task_id={task_id}, video_file={url}"
        )

    try:
        if not available_videos:
            st.warning(tr("Generated Video Files Unavailable"))
        else:
            # Render compact centered video player so vertical videos do not display overly large
            for video_index, url in available_videos:
                c_space_l, c_video, c_space_r = st.columns([1.5, 1.2, 1.5])
                with c_video, st.container(border=True):
                    st.video(url)
                    download_label = tr("Download Video")
                    if len(video_files) > 1:
                        download_label = f"{download_label} {video_index + 1}"
                    download_name = _build_video_download_name(
                        task.get("video_subject"),
                        video_index + 1,
                        len(video_files),
                    )
                    with open(url, "rb") as video_file:
                        st.download_button(
                            download_label,
                            data=video_file,
                            file_name=download_name,
                            mime=mimetypes.guess_type(url)[0] or "video/mp4",
                            key=f"download_completed_vid_{task_id}_{video_index}",
                            icon=":material/download:",
                            on_click="ignore",
                            use_container_width=True,
                        )
    except Exception as exc:
        logger.exception(
            f"failed to render generated video preview: task_id={task_id}, "
            f"video_files={video_files}, error={exc}"
        )

    _render_generation_logs(task_id)
    if st.session_state.get("handled_generation_task_id") != task_id:
        # Fragments may render the same completion task repeatedly. Regardless of whether automatic directory opening is enabled or not,
        # Each task only handles the completion event once to avoid repeatedly popping up the resource manager or repeatedly writing to the log.
        st.session_state["handled_generation_task_id"] = task_id
        if config.ui.get("open_task_folder_on_completion", True):
            open_task_folder(task_id)
        logger.info(f"{tr('Video Generation Completed')}: task_id={task_id}")


@st.fragment(run_every=webui_task.TASK_LOG_REFRESH_INTERVAL_SECONDS)
def _render_running_generation_task(task_id):
    """Only poll during the running of the task; switch back to static results after the end to stop unnecessary scheduled refresh."""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query WebUI generation task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        # Full page scripts now have no time-consuming generation logic and can be safely rerun and change the results to static
        # render. In this way, the browser will not permanently retain a two-second polling Fragment after the task is completed.
        st.rerun(scope="app")

    _render_generation_task_snapshot(task_id, task)


def _render_current_generation_task():
    """Restore the queryable UI of the most recently submitted tasks for the current page below the generate button."""
    task_id = st.session_state.get("current_generation_task_id", "")
    if not task_id:
        return

    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query current WebUI task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        _render_generation_task_snapshot(task_id, task)
        return

    _render_running_generation_task(task_id)


def get_llm_provider_tips(provider_id, **kwargs):
    # LLM provider description copy uniformly uses the `llm_provider_tips.<provider_id>` rule.
    # In this way, when adding a provider, you only need to fill in the copy in the locale; if there is no copy, the prompt block will not be displayed.
    # Avoid stacking a large number of Chinese and English hard-coded instructions in Main.py.
    provider = get_llm_provider(provider_id)
    if provider is None:
        return ""

    # Provider configuration instructions currently maintain two sets of standard templates in Chinese and English; other interface languages
    # Use English uniformly to avoid long-term desynchronization after copying English in the locale. A certain language will be completed later.
    # After it is fully translated, it will be added to the independent maintenance scope here.
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    tips = (
        locales.get(tips_language, {}).get("Translation", {}).get(provider.tips_key, "")
    )
    if not tips:
        return tips

    service_endpoint = provider.preferred_service_endpoint(
        prefer_international=tips_language == "en"
    )
    api_key_url = (
        service_endpoint.api_key_url
        if service_endpoint
        else provider.effective_api_key_url()
    )
    format_context = {
        "api_key_url": api_key_url,
        "default_model": provider.default_model,
        "default_base_url": (
            service_endpoint.base_url
            if service_endpoint
            else provider.effective_default_base_url
        ),
        "model_docs_url": (
            service_endpoint.model_docs_url
            if service_endpoint and service_endpoint.model_docs_url
            else provider.effective_model_docs_url(
                prefer_international=tips_language == "en"
            )
        ),
        **{
            f"default_{field.config_suffix}": field.default_value
            for field in provider.extra_fields
        },
        **kwargs,
    }
    try:
        return tips.format(**format_context)
    except Exception as e:
        logger.warning(f"format llm provider tips failed: {provider_id}, {e}")
        return tips


def format_llm_connection_error(provider_id, base_url, error):
    """Supplement configuration checking recommendations for unambiguously localized authentication errors while preserving original responses."""
    error_text = str(error or "").strip()
    normalized_error = error_text.lower()
    authentication_markers = (
        "401",
        "authentication",
        "invalid api key",
        "invalid_api_key",
        "unauthorized",
    )
    provider = get_llm_provider(provider_id)
    if provider is None or not provider.service_endpoints or not any(
        marker in normalized_error for marker in authentication_markers
    ):
        return error_text

    message = tr_optional(
        provider.authentication_error_key,
        fallback_language="en",
    )
    if not message:
        return error_text
    return message.format(base_url=base_url or "-", error=error_text)


def get_llm_provider_label(provider):
    return tr_optional(provider.label_key) or provider.default_label


def get_tts_provider_tips(provider_id):
    # TTS configuration instructions adopt the same maintenance strategy as LLM Provider: only Chinese and English are maintained.
    # Other interface languages fall back to English to avoid long-term desynchronization after copying.
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    return (
        locales.get(tips_language, {})
        .get("Translation", {})
        .get(f"tts_provider_tips.{provider_id}", "")
    )


def localized_widget_key(name, *parts):
    # Some Streamlit selectboxes use stable keys to remember the selection state, but display text from the locale.
    # When switching languages, put the language into the key to force the control to be rebuilt to prevent the selected item from still displaying the old language.
    language = st.session_state.get("ui_language", config.ui.get("language", ""))
    suffix_parts = [name, language, *[str(part) for part in parts if part]]
    return "_".join(suffix_parts)


def stable_selectbox(label, options, default_value, key, format_func=None, **kwargs):
    # Streamlit 1.59 is more sensitive to selectbox state reuse: if the control does not have a fixed key,
    # Or the real options are just a set of temporary subscripts, which are easily overwritten by the recalculated index after the page is rerun.
    # The performance is that the user's first selection does not take effect and needs to be selected again. This helper uses stable business values ​​uniformly
    # As a real option, and save the value in session_state; display copy only through format_func
    # Transform to avoid translation copy, option order, or upstream configuration changes from affecting selection status.
    options = list(options)
    if not options:
        raise ValueError(f"selectbox options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    selected_value = st.session_state.get(widget_key)
    accepts_custom_value = bool(kwargs.get("accept_new_options"))
    has_valid_custom_value = (
        accepts_custom_value
        and isinstance(selected_value, str)
        and bool(selected_value.strip())
    )
    if selected_value not in options and not has_valid_custom_value:
        # If the upstream options change (for example, the sound list changes after switching TTS provider),
        # The old value is no longer valid. Initialize session_state directly before the control is created, and then only let the key
        # Management status is no longer passed to index at the same time. This avoids Streamlit when rerun
        # The value just selected by the user is overwritten with the recalculated index, causing the first selection to not take effect.
        st.session_state[widget_key] = default_value

    if format_func is None:
        format_func = str

    return st.selectbox(
        label,
        options=options,
        format_func=format_func,
        key=widget_key,
        **kwargs,
    )


# Streamlit's native selectbox does not currently support HTML optgroup. Here we use the one that comes with 1.59
# Components v2 encapsulates native <select>/<optgroup> without introducing front-end dependencies while retaining the browser
# Native keyboard navigation, accessible semantics, and mobile selection experience. The component only passes fixed business values and translated text,
# Do not receive any HTML and prevent configuration content from entering innerHTML at the boundary.
_GROUPED_SELECT_COMPONENT = st_components_v2.component(
    "mpt_grouped_select",
    html="""
        <div class="mpt-grouped-select">
            <div class="mpt-grouped-select__label-row">
                <label class="mpt-grouped-select__label"></label>
                <button class="mpt-grouped-select__settings" type="button"></button>
            </div>
            <div class="mpt-grouped-select__control">
                <select></select>
            </div>
        </div>
    """,
    css="""
        .mpt-grouped-select {
            width: 100%;
            color: var(--st-text-color);
            font-family: var(--st-font);
        }

        .mpt-grouped-select__label-row {
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 0.45rem;
            margin-bottom: 0.35rem;
        }

        .mpt-grouped-select__label {
            font-size: 0.875rem;
            line-height: 1.25rem;
        }

        .mpt-grouped-select__settings {
            padding: 0;
            border: 0;
            background: transparent;
            color: var(--st-link-color);
            font: inherit;
            font-size: 0.8rem;
            line-height: 1.25rem;
            cursor: pointer;
        }

        .mpt-grouped-select__settings:hover {
            text-decoration: underline;
            text-underline-offset: 0.15rem;
        }

        .mpt-grouped-select__settings:focus-visible {
            border-radius: 0.2rem;
            outline: 2px solid var(--st-primary-color);
            outline-offset: 2px;
        }

        .mpt-grouped-select__control {
            position: relative;
        }

        .mpt-grouped-select__control::after {
            position: absolute;
            top: 50%;
            right: 1rem;
            width: 0.55rem;
            height: 0.55rem;
            border-right: 2px solid currentColor;
            border-bottom: 2px solid currentColor;
            content: "";
            pointer-events: none;
            transform: translateY(-70%) rotate(45deg);
        }

        .mpt-grouped-select select {
            width: 100%;
            min-height: 2.5rem;
            padding: 0.45rem 2.75rem 0.45rem 0.75rem;
            border: 1px solid color-mix(in srgb, currentColor 20%, transparent);
            border-radius: 0.5rem;
            outline: none;
            appearance: none;
            background: var(--st-secondary-background-color);
            color: inherit;
            font: inherit;
            cursor: pointer;
        }

        .mpt-grouped-select select:hover {
            border-color: color-mix(in srgb, currentColor 36%, transparent);
        }

        .mpt-grouped-select select:focus-visible {
            border-color: var(--st-primary-color);
            box-shadow: 0 0 0 1px var(--st-primary-color);
        }
    """,
    js="""
        export default function(component) {
            const { data, parentElement, setTriggerValue } = component;
            const label = parentElement.querySelector("label");
            const settings = parentElement.querySelector(".mpt-grouped-select__settings");
            const select = parentElement.querySelector("select");

            label.textContent = data.label;
            settings.textContent = data.settingsLabel;
            settings.hidden = !data.settingsLabel;
            select.id = data.controlId;
            label.htmlFor = data.controlId;
            select.setAttribute("aria-label", data.label);
            select.replaceChildren();

            for (const groupData of data.groups) {
                const group = document.createElement("optgroup");
                group.label = groupData.label;
                for (const optionData of groupData.options) {
                    const option = document.createElement("option");
                    option.value = optionData.value;
                    option.textContent = optionData.label;
                    group.appendChild(option);
                }
                select.appendChild(group);
            }

            select.value = data.value;
            const handleChange = () => {
                setTriggerValue("selected", select.value);
            };
            const handleSettings = () => {
                setTriggerValue("settings", true);
            };
            select.addEventListener("change", handleChange);
            settings.addEventListener("click", handleSettings);

            return () => {
                select.removeEventListener("change", handleChange);
                settings.removeEventListener("click", handleSettings);
            };
        }
    """,
)


def grouped_selectbox(
    label,
    groups,
    default_value,
    key,
    format_func=None,
    settings_label="",
    on_settings=None,
):
    """Render a single dropdown with non-selectable group headers and return a stable business value."""
    if format_func is None:
        format_func = str

    normalized_groups = []
    valid_values = []
    for group_label, options in groups:
        normalized_options = []
        for option in options:
            valid_values.append(option)
            normalized_options.append(
                {"value": option, "label": str(format_func(option))}
            )
        if normalized_options:
            normalized_groups.append(
                {"label": str(group_label), "options": normalized_options}
            )

    if not valid_values:
        raise ValueError(f"grouped selectbox options cannot be empty: {key}")
    if len(set(valid_values)) != len(valid_values):
        raise ValueError(f"grouped selectbox options must be unique: {key}")
    if default_value not in valid_values:
        default_value = valid_values[0]

    # Business selections are saved in the same session key as the old selectbox, setting preset recovery and
    # Language switching logic does not need to be forked; the component itself uses an independent key to avoid conflict with business status.
    widget_key = localized_widget_key(key)
    if widget_key not in st.session_state:
        st.session_state[widget_key] = default_value
    selected_value = st.session_state[widget_key]
    if selected_value not in valid_values:
        selected_value = default_value
        st.session_state[widget_key] = selected_value

    result = _GROUPED_SELECT_COMPONENT(
        key=f"{widget_key}_component",
        data={
            "label": label,
            "settingsLabel": settings_label,
            # Explicitly associate visible labels with native selects. The component key consists of a fixed business name and
            # It consists of language codes and is unique within the page. It is convenient for mouse clicks to focus on label controls.
            # It also does not introduce random IDs that cause the front-end state to be rebuilt every rerun.
            "controlId": f"{widget_key}_control",
            "value": selected_value,
            "groups": normalized_groups,
        },
        on_selected_change=lambda: None,
        on_settings_change=on_settings or (lambda: None),
    )
    changed_value = getattr(result, "selected", None)
    if changed_value in valid_values and changed_value != selected_value:
        st.session_state[widget_key] = changed_value
        # Components v2 When the current script round returns an event, the data passed to the front end in this round is still
        # The old value before the event occurred. Automatically rerun immediately, allowing components and controls that depend on video_source
        # The new value is received at the same time; otherwise the drop-down box will be briefly overwritten by the old data, and the user can only select it once.
        st.rerun()

    return selected_value


def sync_script_order_concat_mode():
    """Fixed use of sequential splicing when copy sequence matching is turned on, and restores the original selection when turned off."""
    widget_key = localized_widget_key("video_concat_mode_select")
    previous_key = "video_concat_mode_before_script_order_match"
    match_script_order = bool(st.session_state.get("match_materials_to_script", False))

    if match_script_order:
        current_mode = st.session_state.get(widget_key, VideoConcatMode.random.value)
        if current_mode != VideoConcatMode.sequential.value:
            st.session_state[previous_key] = current_mode
        st.session_state[widget_key] = VideoConcatMode.sequential.value
        return

    previous_mode = st.session_state.pop(previous_key, None)
    if previous_mode in {
        VideoConcatMode.sequential.value,
        VideoConcatMode.random.value,
    }:
        st.session_state[widget_key] = previous_mode


def reset_script_system_prompt():
    """Restore the system prompt words in the advanced script settings to the default content of the current version."""
    st.session_state["custom_system_prompt"] = llm.DEFAULT_SCRIPT_SYSTEM_PROMPT


def reset_subtitle_settings():
    """Restore default values in WebUI subtitle controls and persistence configuration."""
    defaults = DEFAULT_SUBTITLE_SETTINGS
    st.session_state["subtitle_enabled_checkbox"] = defaults["subtitle_enabled"]
    _set_stable_widget_value("font_name_select", defaults["font_name"])
    _set_stable_widget_value("subtitle_position_select", defaults["subtitle_position"])
    _set_stable_widget_value(
        "subtitle_display_mode_select", defaults["subtitle_display_mode"]
    )
    _set_stable_widget_value(
        "subtitle_animation_select", defaults["subtitle_animation"]
    )
    st.session_state["custom_position_input"] = str(defaults["custom_position"])
    st.session_state["font_color_picker"] = defaults["text_fore_color"]
    st.session_state["font_size_slider"] = defaults["font_size"]
    st.session_state["stroke_color_picker"] = defaults["stroke_color"]
    st.session_state["stroke_width_slider"] = defaults["stroke_width"]
    st.session_state["subtitle_background_enabled_checkbox"] = defaults[
        "subtitle_background_enabled"
    ]
    st.session_state["subtitle_background_color_picker"] = defaults[
        "subtitle_background_color"
    ]
    st.session_state["rounded_subtitle_background_checkbox"] = defaults[
        "rounded_subtitle_background"
    ]

    # Synchronizing persistent UI options ensures that the default settings remain when refreshing the page after recovery.
    for key in (
        "subtitle_enabled",
        "font_name",
        "subtitle_position",
        "subtitle_display_mode",
        "subtitle_animation",
        "custom_position",
        "text_fore_color",
        "font_size",
        "stroke_color",
        "stroke_width",
        "subtitle_background_enabled",
        "subtitle_background_color",
        "rounded_subtitle_background",
    ):
        if key in defaults:
            _set_runtime_config("ui", key, defaults[key])


@st.dialog(tr("Final Prompt Preview"), width="large")
def render_script_prompt_preview(prompt):
    """Displays the complete script generation prompt word that will be sent to the large model."""
    st.code(prompt, language="markdown", wrap_lines=True)


def stable_segmented_control(
    label, options, default_value, key, format_func=None, **kwargs
):
    """Use stable business values to create radio-select segmented controls to prevent the status from being overwritten by display copy after language switching."""
    options = list(options)
    if not options:
        raise ValueError(f"segmented control options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    if st.session_state.get(widget_key) not in options:
        st.session_state[widget_key] = default_value

    return st.segmented_control(
        label,
        options=options,
        selection_mode="single",
        required=True,
        format_func=format_func or str,
        key=widget_key,
        **kwargs,
    )


@st.cache_data(ttl=300, show_spinner=False)
def get_groq_model_ids(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        return []

    normalized_base_url = (
        (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    )
    models_url = f"{normalized_base_url}/models"

    try:
        response = requests.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        model_ids = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())

        return sorted(set(model_ids))
    except Exception as e:
        logger.warning(f"failed to fetch groq models: {e}")
        return []


def _get_material_api_keys(config_key):
    """Convert the material API Key in the configuration into a WebUI editable string."""
    api_keys = config.app.get(config_key, [])
    if isinstance(api_keys, str):
        api_keys = [api_keys]
    return ", ".join(api_keys)


def _save_material_api_keys(config_key, value):
    """Save comma-separated material API Keys and allow the user to explicitly clear the old configuration."""
    normalized_value = value.replace(" ", "")
    _set_runtime_config(
        "app",
        config_key,
        normalized_value.split(",") if normalized_value else [],
    )


def _format_file_size(size_bytes):
    """Format the byte count into compact text suitable for display on the settings page."""
    size = float(max(0, size_bytes))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


@st.cache_data(ttl=30, show_spinner=False)
def _get_video_cache_stats_data(max_age_days=None):
    """
    Cache directory statistics in a short period to avoid repeatedly scanning a large number of files by interacting with common controls in the pop-up window.

    Cache key contains cleanup days, so switching ranges will only scan once per range; proactively refresh or clean
    It will be explicitly cleared when completed, and the cache for up to 30 seconds will not affect the secondary scan during actual deletion.

    Only pure dicts are cached here, not VideoCacheStats instances: st.cache_data requires pickle
    Serialize the return value, and when pickle saves the custom class, it is referenced by "module name + class name" and verifies the parsing.
    The class that comes out is the same object as the class of the instance. Streamlit source code monitor changes local source code files
    sys.modules is cleared when
    The monitored module is re-imported; the settings pop-up window is still open at this time (Dialog inherits fragment behavior,
    Internal interaction will not rebuild the Main.py top-level reference) still holds the classes in the old module, and pickle will throw
    PicklingError: it's not the same object as ... and is wrapped by Streamlit as
    UnserializableReturnValueError (corresponds to the known defect of Streamlit official issue #14593).
    Pure dict is serialized by value, contains no class references, and therefore is not affected by module re-imports.
    """
    stats = cache_manager.get_video_cache_stats(max_age_days=max_age_days)
    # The field name is exactly the same as VideoCacheStats and can be directly expanded by _get_video_cache_stats below.
    # Restore; if fields are added or deleted in VideoCacheStats in the future, the field mapping here needs to be maintained simultaneously.
    return {
        "file_count": stats.file_count,
        "total_size": stats.total_size,
        "oldest_mtime": stats.oldest_mtime,
        "newest_mtime": stats.newest_mtime,
    }


def _get_video_cache_stats(max_age_days=None) -> cache_manager.VideoCacheStats:
    """Restore pure data to VideoCacheStats outside the cache boundary, and the caller attribute access method remains unchanged."""

    # Use the currently effective cache_manager module when restoring; even if the module is re-imported, it will only be rebuilt
    # A lightweight dataclass that will no longer trigger pickle's "class reference identity" check.
    return cache_manager.VideoCacheStats(
        **_get_video_cache_stats_data(max_age_days=max_age_days)
    )


def _render_cache_management_settings(panel):
    """Render statistics, preview and security cleanup operations for the default online video material cache."""
    with panel:
        cleanup_message = st.session_state.pop("video_cache_cleanup_message", None)
        if cleanup_message:
            message_type, message = cleanup_message
            if message_type == "success":
                st.success(message)
            else:
                st.warning(message)

        st.caption(tr("Video Cache Directory"))
        st.code(cache_manager.video_cache_dir(), language="text")

        total_stats = _get_video_cache_stats()
        metric_count, metric_size, metric_oldest = st.columns(3)
        metric_count.metric(tr("Cache File Count"), total_stats.file_count)
        metric_size.metric(
            tr("Cache Total Size"), _format_file_size(total_stats.total_size)
        )
        oldest_text = (
            datetime.fromtimestamp(total_stats.oldest_mtime).strftime("%Y-%m-%d")
            if total_stats.oldest_mtime is not None
            else "-"
        )
        metric_oldest.metric(tr("Oldest Cache Date"), oldest_text)

        st.caption(tr("Video Cache Management Help"))
        cleanup_options = (30, 7, 90, None)
        cleanup_labels = {
            30: tr("Cache Older Than 30 Days"),
            7: tr("Cache Older Than 7 Days"),
            90: tr("Cache Older Than 90 Days"),
            None: tr("All Video Cache"),
        }
        max_age_days = st.selectbox(
            tr("Cache Cleanup Range"),
            options=cleanup_options,
            format_func=lambda value: cleanup_labels[value],
            key="video_cache_cleanup_range",
        )
        cleanup_preview = _get_video_cache_stats(max_age_days=max_age_days)
        st.info(
            tr("Cache Cleanup Preview").format(
                count=cleanup_preview.file_count,
                size=_format_file_size(cleanup_preview.total_size),
            )
        )

        confirm_nonce = st.session_state.get("video_cache_cleanup_confirm_nonce", 0)
        confirmed = st.checkbox(
            tr("Confirm Cache Cleanup"),
            key=f"video_cache_cleanup_confirm_{confirm_nonce}",
        )
        refresh_col, open_col, cleanup_col = st.columns(3)
        if refresh_col.button(
            tr("Refresh Cache Stats"),
            key="refresh_video_cache_stats",
            use_container_width=True,
            icon=":material/refresh:",
        ):
            _get_video_cache_stats_data.clear()
            st.rerun(scope="fragment")

        if open_col.button(
            tr("Open Cache Directory"),
            key="open_video_cache_directory",
            use_container_width=True,
            icon=":material/folder_open:",
        ):
            webbrowser.open(Path(cache_manager.video_cache_dir()).as_uri())

        cleanup_disabled = not confirmed or cleanup_preview.file_count == 0
        if cleanup_col.button(
            tr("Clean Cache Now"),
            key="clean_video_cache_now",
            type="primary",
            disabled=cleanup_disabled,
            use_container_width=True,
            icon=":material/delete_sweep:",
        ):
            result = cache_manager.clean_video_cache(max_age_days=max_age_days)
            message_key = (
                "Cache Cleanup Completed With Failures"
                if result.failed_count
                else "Cache Cleanup Completed"
            )
            st.session_state["video_cache_cleanup_message"] = (
                "warning" if result.failed_count else "success",
                tr(message_key).format(
                    count=result.deleted_count,
                    size=_format_file_size(result.deleted_size),
                    failed=result.failed_count,
                ),
            )
            # Streamlit does not allow session_state with the same name to be modified after the control is instantiated. by incrementing
            # nonce allows the next fragment rerun to create unchecked new controls to avoid cleaning up after completion
            # The danger confirmation status is retained.
            st.session_state["video_cache_cleanup_confirm_nonce"] = confirm_nonce + 1
            _get_video_cache_stats_data.clear()
            st.rerun(scope="fragment")


# -----------------------------------------------------------------------------
# Set up default export, import and key backup
# -----------------------------------------------------------------------------


def _is_credential_config_key(key):
    """Determines whether a configuration item name represents credentials."""
    return str(key).endswith(CREDENTIAL_KEY_SUFFIXES)


def _is_backup_config_key(section_name, key):
    """The credential itself and its supporting configuration items are part of the key backup scope."""
    if _is_credential_config_key(key):
        return True
    if key in CREDENTIAL_COMPANION_KEYS.get(section_name, ()):
        return True
    return key in NON_LLM_COMPANION_KEYS.get(section_name, ())


def _credential_widget_state_keys(section_name, key):
    """
    Returns all Streamlit control keys corresponding to a certain credential configuration item.

    Password input boxes all have keys, and the value of session_state in Streamlit takes precedence over the value of the control.
    parameters. These residual control states must be cleared after restoring the backup, otherwise the page will continue to display the old keys.
    And rewrite the old values back to the configuration during the next rerun, making the recovery seem ineffective. multiple panels
    When sharing the same key, they will each hold the control state, so the default key and all aliases are returned.
    """
    if section_name == "app":
        default_widget_key = f"{key}_input"
    else:
        default_widget_key = f"{section_name}_{key}_input"
    return (
        default_widget_key,
        *CREDENTIAL_WIDGET_STATE_ALIASES.get((section_name, key), ()),
    )


def _normalize_backup_value(value):
    """Normalize backup values and discard empty strings and empty lists to avoid overwriting empty configurations during recovery."""
    if isinstance(value, list):
        items = [
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        return items or None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _collect_key_backup(config_sections):
    """Collect all populated keys and their accompanying configuration items from the runtime configuration partition."""
    backup = {}
    for section_name, section in config_sections.items():
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        entries = {}
        for key, value in section.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                entries[key] = normalized_value
        if entries:
            backup[section_name] = entries
    return backup


def _count_backup_keys(backup):
    """Count the number of configuration items in the backup, which is used for interface prompts and disabling empty exports."""
    return sum(len(entries) for entries in backup.values())


def _build_key_backup_payload(config_sections, app_version):
    """Construct key backup file contents."""
    return {
        "schema": KEY_BACKUP_SCHEMA,
        "version": KEY_BACKUP_VERSION,
        "app_version": str(app_version),
        "keys": _collect_key_backup(config_sections),
    }


def _load_transfer_payload(raw_bytes, schema, version):
    """
    Parse the export file and verify that it is indeed from the same version of this feature.

    Users may upload arbitrary JSON. Only files that declare the correct schema and version are accepted here, so errors
    The prompt stays at the import entry instead of writing unrecognizable content into the configuration or control state.
    Windows editors may save JSON with BOM and therefore decode as utf-8-sig.
    """
    if len(raw_bytes) > MAX_SETTINGS_TRANSFER_BYTES:
        raise ValueError("settings import exceeds the 2 MB limit")
    payload = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("exported file must contain a JSON object")
    if payload.get("schema") != schema:
        raise ValueError(f"unexpected schema: {payload.get('schema')!r}")
    if payload.get("version") != version:
        raise ValueError(f"unsupported version: {payload.get('version')!r}")
    return payload


def _parse_key_backup(raw_bytes, config_sections):
    """
    Parse the key backup file and retain only the partitions and configuration items recognized by the current version.

    The backup file can be manually edited or may be from a newer version. Unknown partitions or non-key configuration items are always
    Ignore to avoid overwriting non-credential-related configuration via the import function.
    """
    payload = _load_transfer_payload(raw_bytes, KEY_BACKUP_SCHEMA, KEY_BACKUP_VERSION)
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("key backup file has no keys object")

    restored = {}
    for section_name, entries in keys.items():
        if section_name not in config_sections:
            continue
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        if not isinstance(entries, dict):
            continue
        section_entries = {}
        for key, value in entries.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                section_entries[key] = normalized_value
        if section_entries:
            restored[section_name] = section_entries

    if not restored:
        raise ValueError("key backup file contains no restorable keys")
    return restored


def _build_settings_preset_payload(params, app_version):
    """Construct the content of the build parameter default file."""
    preset_params = {
        key: value
        for key, value in params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    if params.get("bgm_type") == "preset" and params.get("bgm_file"):
        try:
            builtin_bgm_path = bgm_service.resolve_builtin_bgm_file(
                str(params["bgm_file"])
            )
        except ValueError:
            # Customization files are native resources and cannot be entered into portable settings presets. Maintain in abnormal situations
            # Exclusion behavior exists to avoid exporting files containing absolute paths or UUIDs that do not exist on another device.
            pass
        else:
            preset_params["bgm_file"] = Path(builtin_bgm_path).name
    return {
        "schema": SETTINGS_PRESET_SCHEMA,
        "version": SETTINGS_PRESET_VERSION,
        "app_version": str(app_version),
        "params": preset_params,
    }


def _parse_settings_preset(raw_bytes):
    """
    Parse the preset file and submit it to VideoParams for verification.

    Presets can be generated on other machines or edited manually. Unified model verification can reuse existing
    The value range constraint, illegal presets are rejected when imported, instead of failing when the task is generated.
    """
    payload = _load_transfer_payload(
        raw_bytes, SETTINGS_PRESET_SCHEMA, SETTINGS_PRESET_VERSION
    )
    preset_params = payload.get("params")
    if not isinstance(preset_params, dict):
        raise ValueError("settings preset file has no params object")

    params_input = {
        key: value
        for key, value in preset_params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    if preset_params.get("bgm_type") == "preset" and preset_params.get("bgm_file"):
        # Setting a preset can only restore the built-in songs that actually exist in the current version. Service layer also rejects directory separator
        # Upload files with users to prevent imported files from reading arbitrary local paths through the listening function.
        builtin_bgm_path = bgm_service.resolve_builtin_bgm_file(
            str(preset_params["bgm_file"])
        )
        params_input["bgm_file"] = Path(builtin_bgm_path).name
    # video_subject is a required field of VideoParams, but the preset allows only style settings to be saved.
    params_input.setdefault("video_subject", "")
    return VideoParams.model_validate(params_input).model_dump(mode="json")


def _apply_key_backup(restored_keys):
    """Write the parsed key back to the runtime configuration and clear the residual state of the corresponding control."""
    restored_count = 0
    for section_name, entries in restored_keys.items():
        for key, value in entries.items():
            _set_runtime_config(section_name, key, value)
            for widget_key in _credential_widget_state_keys(section_name, key):
                st.session_state.pop(widget_key, None)
            restored_count += 1
    # ElevenLabs sound lists are cached by key and must be pulled again after changing to another backup.
    for cache_key in list(st.session_state.keys()):
        if str(cache_key).startswith("elevenlabs_voices_"):
            del st.session_state[cache_key]
    return restored_count


def _apply_pending_settings_preset():
    """Apply imported presets before rendering any controls."""
    preset_params = st.session_state.pop("settings_preset_payload", None)
    if not preset_params:
        return False

    _apply_restored_params(preset_params)
    logger.info("applied imported settings preset")
    return True


def _render_settings_transfer(params):
    """Export and import portal for rendering and generating parameter presets."""
    with st.expander(tr("Settings Preset"), expanded=False):
        st.caption(tr("Settings Preset Help"))
        preset_payload = _build_settings_preset_payload(
            params.model_dump(mode="json"), config.project_version
        )
        st.download_button(
            tr("Export Settings"),
            data=json.dumps(preset_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=SETTINGS_PRESET_FILE_NAME,
            mime="application/json",
            use_container_width=True,
            key="export_settings_preset_button",
            icon=":material/download:",
        )
        uploaded_preset = st.file_uploader(
            tr("Import Settings"),
            type=["json"],
            key="settings_preset_uploader",
        )
        if uploaded_preset is None:
            return
        # The uploaded file will reappear every time it is rerun. Record processed file identification,
        # This prevents users from being repeatedly overwritten by the same preset after changing the controls.
        if st.session_state.get("settings_preset_file_id") == uploaded_preset.file_id:
            return

        st.session_state["settings_preset_file_id"] = uploaded_preset.file_id
        try:
            preset_params = _parse_settings_preset(uploaded_preset.getvalue())
        except Exception as e:
            logger.warning(f"failed to import settings preset: {e}")
            st.error(tr("Settings Preset Import Failed"))
            return

        st.session_state["settings_preset_payload"] = preset_params
        st.rerun()


def _render_key_backup_settings(panel):
    """Export and restore portal for rendering key backup."""
    with panel:
        backup_message = st.session_state.pop("key_backup_message", None)
        if backup_message:
            message_type, message = backup_message
            if message_type == "success":
                st.success(message)
            else:
                st.error(message)

        st.caption(tr("Key Backup Help"))
        st.warning(tr("Key Backup Warning"))

        backup_payload = _build_key_backup_payload(
            _RUNTIME_CONFIG_SECTIONS, config.project_version
        )
        backup_key_count = _count_backup_keys(backup_payload["keys"])
        st.caption(tr("Key Backup Summary").format(count=backup_key_count))
        st.download_button(
            tr("Export Keys"),
            data=json.dumps(backup_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=KEY_BACKUP_FILE_NAME,
            mime="application/json",
            disabled=backup_key_count == 0,
            use_container_width=True,
            key="export_key_backup_button",
            icon=":material/download:",
        )

        uploaded_backup = st.file_uploader(
            tr("Import Keys"),
            type=["json"],
            key="key_backup_uploader",
        )
        if uploaded_backup is None:
            return
        if st.session_state.get("key_backup_file_id") == uploaded_backup.file_id:
            return

        st.session_state["key_backup_file_id"] = uploaded_backup.file_id
        try:
            restored_keys = _parse_key_backup(
                uploaded_backup.getvalue(), _RUNTIME_CONFIG_SECTIONS
            )
        except Exception as e:
            logger.warning(f"failed to import key backup: {e}")
            st.session_state["key_backup_message"] = (
                "error",
                tr("Key Restore Failed"),
            )
        else:
            restored_count = _apply_key_backup(restored_keys)
            _save_runtime_config()
            logger.info(f"restored keys from backup file: count={restored_count}")
            st.session_state["key_backup_message"] = (
                "success",
                tr("Keys Restored").format(count=restored_count),
            )
        # The TTS key input box on the main page also needs to read the restored configuration, so the entire page is refreshed.
        # The open state of the set pop-up window is saved in session_state and will be re-expanded after refreshing.
        st.rerun(scope="app")


# -----------------------------------------------------------------------------
# Settings and prompt word pop-up window
# -----------------------------------------------------------------------------


# The setting is a low-frequency operation. Use a medium-sized Dialog to avoid occupying the vertical space of the main page for a long time.
# At the same time, control the reading line width to prevent the pop-up window from appearing too loose on wide-screen devices.
# Dialog inherits fragment behavior, and internal control interaction only redraws the pop-up window; the configuration is saved separately at the end of the function.
# Trigger full page synchronization through callback when closing to ensure that the generation process reads the latest Provider and interface settings.
def _render_settings_content(in_dialog=False):
    with st.container():
        # History hide_config is only used to hide the old basic settings panel. After changing to a fixed setting entry, the value
        # It no longer has user-visible meaning and is uniformly migrated to false to prevent the old configuration from affecting subsequent versions.
        _set_runtime_config("app", "hide_config", False)
        settings_tab_labels = [
            tr("LLM Settings Tab"),
            tr("Material API Tab"),
            tr("Auto-Publish Settings"),
            tr("Interface Settings Tab"),
            tr("Key Backup Tab"),
            tr("Cache Management Tab"),
        ]
        settings_tab_targets = {
            "llm": tr("LLM Settings Tab"),
            "material": tr("Material API Tab"),
        }
        tab_prefix = "settings_dialog_tabs" if in_dialog else "settings_page_tabs"
        settings_tabs_key = localized_widget_key(tab_prefix)
        target_tab = st.session_state.pop("settings_dialog_target_tab", None)
        if target_tab in settings_tab_targets:
            # st.tabs uses the display label as the status value. The entrance button only saves the stable business ID.
            # Go here and write the label of the current language, which can accurately locate and be compatible with language switching.
            st.session_state[settings_tabs_key] = settings_tab_targets[target_tab]

        (
            middle_config_panel,
            right_config_panel,
            publish_config_panel,
            left_config_panel,
            key_backup_panel,
            cache_config_panel,
        ) = st.tabs(
            settings_tab_labels,
            key=settings_tabs_key,
            on_change="rerun",
        )

        with publish_config_panel:
            st.write(tr("Automatically publish generated videos to social media using upload-post.com"))
            st.info(
                tr("Upload-Post Setup Guide").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL,
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL,
                )
            )

            is_enabled = config.app.get("upload_post_enabled", False)
            is_auto = config.app.get("upload_post_auto_upload", False)

            # The two keys are independent: enabled allows external processes to call Upload-Post,
            # auto_upload determines whether to automatically publish after rendering is completed. Combined into one checkbox will be in
            # In a configuration where the two keys are inconsistent, just open the settings dialog box and rewrite enabled to False.
            upload_post_enabled = st.checkbox(
                tr("Enable Upload-Post Integration"),
                value=is_enabled,
                key="upload_post_enabled_checkbox"
            )
            if upload_post_enabled != is_enabled:
                _set_runtime_config("app", "upload_post_enabled", upload_post_enabled)

            upload_post_auto_upload = st.checkbox(
                tr("Enable Auto-Publish"),
                value=is_auto,
                key="upload_post_auto_upload_checkbox"
            )
            if upload_post_auto_upload != is_auto:
                _set_runtime_config("app", "upload_post_auto_upload", upload_post_auto_upload)

            upload_post_api_key = st.text_input(
                tr("Upload-Post API Key"),
                value=config.app.get("upload_post_api_key", ""),
                type="password",
                help=tr("Upload-Post API Key Help").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL
                ),
                key="upload_post_api_key_input"
            )
            if upload_post_api_key != config.app.get("upload_post_api_key", ""):
                _set_runtime_config("app", "upload_post_api_key", upload_post_api_key)

            upload_post_username = st.text_input(
                tr("Upload-Post Profile Username"),
                value=config.app.get("upload_post_username", ""),
                help=tr("Upload-Post Profile Username Help").format(
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL
                ),
                key="upload_post_username_input"
            )
            if upload_post_username != config.app.get("upload_post_username", ""):
                _set_runtime_config("app", "upload_post_username", upload_post_username)

            upload_post_platforms = st.multiselect(
                tr("Platforms"),
                options=["tiktok", "instagram", "youtube"],
                default=config.app.get("upload_post_platforms", ["tiktok", "instagram"]),
                help="Select platforms to publish to",
                key="upload_post_platforms_multiselect"
            )
            if upload_post_platforms != config.app.get("upload_post_platforms", ["tiktok", "instagram"]):
                _set_runtime_config("app", "upload_post_platforms", upload_post_platforms)

            if "youtube" in upload_post_platforms:
                yt_status_options = ["public", "private", "unlisted"]
                yt_saved = config.app.get("upload_post_youtube_privacy_status", "public")
                if yt_saved not in yt_status_options:
                    yt_saved = "public"
                upload_post_youtube_privacy_status = st.selectbox(
                    tr("YouTube Privacy Status"),
                    options=yt_status_options,
                    index=yt_status_options.index(yt_saved),
                    key="upload_post_youtube_privacy_status_selectbox"
                )
                if upload_post_youtube_privacy_status != config.app.get("upload_post_youtube_privacy_status", "public"):
                    _set_runtime_config("app", "upload_post_youtube_privacy_status", upload_post_youtube_privacy_status)

                # Audience declarations only affect YouTube publishing and do not change requests for generated content or other platforms.
                # Use true boolean options and avoid treating display text or strings as API parameters.
                saved_audience = config.app.get("upload_post_youtube_made_for_kids", False)
                audience_labels = {False: tr("Not Made for Kids"), True: tr("Made for Kids")}
                made_for_kids = st.selectbox(
                    tr("YouTube Audience"),
                    options=[False, True],
                    # The illegal configuration remains unselected and is not changed to a non-child declaration without authorization when opening the settings.
                    index=int(saved_audience) if isinstance(saved_audience, bool) else None,
                    format_func=lambda x: str(audience_labels.get(bool(x), "")),
                    help=tr("YouTube Audience Help"),
                    key="upload_post_youtube_made_for_kids_selectbox",
                )
                if isinstance(made_for_kids, bool):
                    _set_runtime_config("app", "upload_post_youtube_made_for_kids", made_for_kids)

        # Left panel - Log settings
        with left_config_panel:
            hide_log = st.checkbox(
                tr("Hide Log"),
                value=config.ui.get("hide_log", False),
                key="hide_log_checkbox",
            )
            _set_runtime_config("ui", "hide_log", hide_log)

        _render_cache_management_settings(cache_config_panel)
        # Key recovery writes back the configuration and clears the password control state and must be performed before rendering these controls below.
        _render_key_backup_settings(key_backup_panel)

        # Middle Panel - LLM Setup

        with middle_config_panel:
            # Drop-down order, default label and stable provider id all come from Registry; locale
            # Only the display copy is covered, and Main.py no longer maintains a second Provider list.
            llm_provider_ids = [
                provider.provider_id for provider in LLM_PROVIDER_REGISTRY
            ]
            llm_provider_labels = {
                provider.provider_id: get_llm_provider_label(provider)
                for provider in LLM_PROVIDER_REGISTRY
            }
            saved_llm_provider = config.app.get(
                "llm_provider", DEFAULT_LLM_PROVIDER_ID
            ).lower()
            if saved_llm_provider not in llm_provider_ids:
                saved_llm_provider = DEFAULT_LLM_PROVIDER_ID

            llm_provider = stable_selectbox(
                tr("LLM Provider"),
                options=llm_provider_ids,
                default_value=saved_llm_provider,
                key="llm_provider_select",
                format_func=lambda provider_id: llm_provider_labels[provider_id],
            )
            # Display the configuration form and Provider description side by side, reducing line breaks in long descriptions in narrow columns.
            # At the same time, make full use of the horizontal space of the basic settings panel.
            llm_form_panel, llm_help_panel = st.columns(
                [0.9, 1.1],
                gap="large",
                vertical_alignment="top",
            )
            llm_helper = llm_help_panel.container()
            _set_runtime_config("app", "llm_provider", llm_provider)
            llm_provider_spec = get_llm_provider(llm_provider)
            if llm_provider_spec is None:
                # Under normal circumstances, the drop-down options all come from the Registry and will not enter this branch; reserved
                # Explicit errors are used to diagnose corrupted session state or missed subsequent access.
                raise RuntimeError(f"unsupported llm provider: {llm_provider}")

            llm_api_key = config.app.get(llm_provider_spec.config_key("api_key"), "")
            configured_llm_base_url = config.app.get(
                llm_provider_spec.config_key("base_url"), ""
            )
            llm_default_base_url = llm_provider_spec.effective_default_base_url
            llm_base_url = configured_llm_base_url or llm_default_base_url
            llm_model_name = llm_provider_spec.resolve_model_name(
                config.app.get(llm_provider_spec.config_key("model_name"), "")
            )

            provider_tip_context = {}
            selected_service_endpoint = None
            if llm_provider_spec.service_endpoints:
                # Providers such as Kimi use different account systems for their Chinese and international sites. Only allow users
                # Select the service area, and then use the Registry synchronization API to apply for the entrance and Base URL.
                # Avoid manual assembly errors. If there is an empty Base URL configuration, the Chinese site will continue to be used. Only
                # For new configurations that have not yet filled in the Key, the corresponding entry will be recommended based on the interface language.
                selected_service_endpoint = (
                    llm_provider_spec.select_service_endpoint(
                        configured_llm_base_url,
                        has_api_key=bool(str(llm_api_key).strip()),
                        prefer_international=(
                            st.session_state.get("ui_language", "en") != "zh"
                        ),
                    )
                )
                endpoint_options = [
                    endpoint.endpoint_id
                    for endpoint in llm_provider_spec.service_endpoints
                ] + [CUSTOM_LLM_ENDPOINT_ID]
                default_endpoint_id = (
                    selected_service_endpoint.endpoint_id
                    if selected_service_endpoint
                    else CUSTOM_LLM_ENDPOINT_ID
                )
                endpoint_labels = {
                    endpoint.endpoint_id: (
                        tr_optional(
                            llm_provider_spec.endpoint_label_key(endpoint.endpoint_id),
                            fallback_language="en",
                        )
                        or endpoint.default_label
                    )
                    for endpoint in llm_provider_spec.service_endpoints
                }
                endpoint_labels[CUSTOM_LLM_ENDPOINT_ID] = (
                    tr_optional("Custom API Endpoint", fallback_language="en")
                    or "Custom API Endpoint"
                )
                with llm_form_panel:
                    selected_endpoint_id = stable_selectbox(
                        tr_optional(
                            llm_provider_spec.endpoint_selector_label_key,
                            fallback_language="en",
                        )
                        or tr("API Platform"),
                        options=endpoint_options,
                        default_value=default_endpoint_id,
                        key=f"{llm_provider}_service_endpoint_select",
                        format_func=lambda endpoint_id: endpoint_labels[endpoint_id],
                        help=(
                            tr_optional(
                                llm_provider_spec.endpoint_selector_help_key,
                                fallback_language="en",
                            )
                            or None
                        ),
                    )
                selected_service_endpoint = next(
                    (
                        endpoint
                        for endpoint in llm_provider_spec.service_endpoints
                        if endpoint.endpoint_id == selected_endpoint_id
                    ),
                    None,
                )
                if selected_service_endpoint:
                    llm_base_url = selected_service_endpoint.base_url
                    provider_tip_context.update(
                        {
                            "api_key_url": selected_service_endpoint.api_key_url,
                            "default_base_url": selected_service_endpoint.base_url,
                            "model_docs_url": selected_service_endpoint.model_docs_url,
                        }
                    )
                else:
                    # Custom mode only retains addresses explicitly saved by the user and does not disguise a standard area
                    # into a custom value. When the input is empty, the configuration will not be persisted and will return to the compatible default next time.
                    llm_base_url = str(configured_llm_base_url or "").strip()

            if llm_provider == "ollama":
                llm_default_base_url = config.get_default_ollama_base_url()
                if not llm_base_url:
                    llm_base_url = llm_default_base_url
                docker_hint = ""
                if config.is_running_in_container():
                    docker_hint = tr_optional(
                        "llm_provider_tips.ollama.docker_hint",
                        fallback_language="en",
                    )
                provider_tip_context["docker_hint"] = docker_hint

            tips = get_llm_provider_tips(llm_provider, **provider_tip_context)
            if tips:
                with llm_helper:
                    st.info(tips)

            st_llm_api_key = llm_api_key
            if llm_provider_spec.show_api_key:
                st_llm_api_key = llm_form_panel.text_input(
                    tr("API Key"),
                    value=llm_api_key,
                    type="password",
                    key=f"{llm_provider}_api_key_input",
                )

            st_llm_base_url = llm_base_url
            if llm_provider_spec.show_base_url:
                st_llm_base_url = llm_form_panel.text_input(
                    tr("Base Url"),
                    value=llm_base_url,
                    key=(
                        f"{llm_provider}_base_url_"
                        f"{selected_service_endpoint.endpoint_id}_input"
                        if selected_service_endpoint
                        else f"{llm_provider}_base_url_custom_input"
                    ),
                    disabled=selected_service_endpoint is not None,
                )
            st_llm_model_name = ""
            if llm_provider == "groq":
                effective_api_key = st_llm_api_key or llm_api_key
                effective_base_url = st_llm_base_url or llm_base_url
                groq_models = get_groq_model_ids(
                    api_key=effective_api_key,
                    base_url=effective_base_url,
                )

                if groq_models:
                    selected_index = 0
                    if llm_model_name in groq_models:
                        selected_index = groq_models.index(llm_model_name)

                    st_llm_model_name = llm_form_panel.selectbox(
                        tr("Model Name"),
                        options=groq_models,
                        index=selected_index,
                        key="groq_model_name_select",
                    )
                else:
                    st_llm_model_name = llm_form_panel.text_input(
                        tr("Model Name"),
                        value=llm_model_name,
                        key="groq_model_name_input",
                    )
                    if effective_api_key:
                        llm_form_panel.caption(tr("Groq Model List Load Failed"))
                    else:
                        llm_form_panel.caption(
                            tr("Groq API Key Required for Model List")
                        )
            else:
                st_llm_model_name = llm_form_panel.text_input(
                    tr("Model Name"),
                    value=llm_model_name,
                    key=f"{llm_provider}_model_name_input",
                )
            # The input box displays the Registry default value, but the configuration only saves the actual user override value.
            # In this way, after the default model and Base URL are updated, uncustomized users can automatically follow them.
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("api_key"),
                st_llm_api_key,
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("base_url"),
                normalize_provider_override(
                    st_llm_base_url,
                    llm_default_base_url,
                ),
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("model_name"),
                normalize_provider_override(
                    st_llm_model_name,
                    llm_provider_spec.default_model,
                ),
            )

            # Provider-specific fields are also declared by the Registry. For example Cloudflare AI Gateway
            # Account ID is required; there is no need to add judgment in Main.py when adding similar fields in the future.
            for field in llm_provider_spec.extra_fields:
                field_config_key = llm_provider_spec.config_key(field.config_suffix)
                field_value = llm_form_panel.text_input(
                    tr(field.label_key),
                    value=(config.app.get(field_config_key, "") or field.default_value),
                    type="password" if field.secret else "default",
                    key=f"{llm_provider}_{field.config_suffix}_input",
                )
                _set_runtime_config(
                    "app",
                    field_config_key,
                    normalize_provider_override(
                        field_value,
                        field.default_value,
                    ),
                )

            if llm_form_panel.button(
                tr("Test LLM Connection"),
                key="test_llm_connection_button",
                use_container_width=True,
                type="secondary",
                icon=":material/network_check:",
            ):
                connection_ok = False
                connection_error = ""
                connection_elapsed = 0.0
                with config.try_runtime_config_lock() as lock_acquired:
                    if not lock_acquired:
                        llm_form_panel.warning(tr("Runtime Configuration Busy"))
                    else:
                        with llm_form_panel.spinner(tr("Testing LLM Connection")):
                            connection_ok, connection_error, connection_elapsed = (
                                llm.test_connection()
                            )

                if not lock_acquired:
                    connection_ok = None
                elif connection_ok:
                    llm_form_panel.success(
                        tr("LLM Connection Test Succeeded").format(
                            provider=llm_provider_labels[llm_provider],
                            model=st_llm_model_name or "-",
                            elapsed=f"{connection_elapsed:.2f}",
                        )
                    )
                else:
                    connection_error = format_llm_connection_error(
                        llm_provider,
                        st_llm_base_url,
                        connection_error,
                    )
                    llm_form_panel.error(
                        tr("LLM Connection Test Failed").format(error=connection_error)
                    )

        # Right panel - API key settings
        with right_config_panel:
            # Material Provider Click "Search stock materials/AI generated videos/AI generated pictures"
            # Grouping to avoid all fields being mixed in a long list as the number of Providers increases.
            # Grouping only adjusts the display level and does not change existing configuration keys. After upgrading, old users
            # The original config.toml value will continue to be read.
            with st.container(border=True):
                st.markdown(f"#### {tr('Stock Video APIs')}")
                st.caption(tr("Stock Video APIs Help"))

                pexels_api_key = _get_material_api_keys("pexels_api_keys")
                pixabay_api_key = _get_material_api_keys("pixabay_api_keys")
                coverr_api_key = _get_material_api_keys("coverr_api_keys")
                pexels_api_key = st.text_input(
                    tr("Pexels API Key"),
                    value=pexels_api_key,
                    type="password",
                    key="pexels_api_keys_input",
                )
                _save_material_api_keys("pexels_api_keys", pexels_api_key)

                pixabay_api_key = st.text_input(
                    tr("Pixabay API Key"),
                    value=pixabay_api_key,
                    type="password",
                    key="pixabay_api_keys_input",
                )
                _save_material_api_keys("pixabay_api_keys", pixabay_api_key)

                coverr_api_key = st.text_input(
                    tr("Coverr API Key"),
                    value=coverr_api_key,
                    type="password",
                    key="coverr_api_keys_input",
                )
                _save_material_api_keys("coverr_api_keys", coverr_api_key)

            with st.container(border=True):
                st.markdown(f"#### {tr('AI Video Generation APIs')}")
                st.caption(tr("AI Video Generation APIs Help"))

                # Video generation provider displays first by sponsor, in order within the sponsor
                # Consistent with VIDEO_SOURCE_GROUPS: Secret Tower, OFox, Odds Cloud, Volcano Engine.
                st.markdown(f"**{tr('Metaso MiniMax H3')}**")
                metaso_api_key = st.text_input(
                    tr("Metaso MiniMax API Key"),
                    value=str(
                        config.app.get("metaso_minimax_api_key", "") or ""
                    ).strip(),
                    type="password",
                    help=tr("Metaso MiniMax API Key Help"),
                    key="metaso_minimax_api_key_input",
                )
                _set_runtime_config(
                    "app", "metaso_minimax_api_key", metaso_api_key.strip()
                )
                configured_metaso_base_url = str(
                    config.app.get(
                        "metaso_minimax_base_url",
                        metaso_minimax.DEFAULT_BASE_URL,
                    )
                    or metaso_minimax.DEFAULT_BASE_URL
                ).strip()
                metaso_base_url = st.text_input(
                    tr("Metaso MiniMax Base URL"),
                    value=(
                        ""
                        if configured_metaso_base_url == metaso_minimax.DEFAULT_BASE_URL
                        else configured_metaso_base_url
                    ),
                    placeholder=metaso_minimax.DEFAULT_BASE_URL,
                    key="metaso_minimax_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "metaso_minimax_base_url",
                    metaso_base_url.strip() or metaso_minimax.DEFAULT_BASE_URL,
                )
                configured_metaso_resolution = (
                    str(
                        config.app.get(
                            "metaso_minimax_resolution",
                            metaso_minimax.DEFAULT_RESOLUTION,
                        )
                    )
                    .strip()
                    .upper()
                )
                metaso_resolution_options = sorted(
                    metaso_minimax.SUPPORTED_RESOLUTIONS,
                    key=lambda value: value != metaso_minimax.DEFAULT_RESOLUTION,
                )
                resolution_is_valid = (
                    configured_metaso_resolution
                    in metaso_minimax.SUPPORTED_RESOLUTIONS
                )
                if not resolution_is_valid:
                    # Resolution directly affects billing. In case of manual configuration error, retain the original value and ask the user
                    # It is an active choice and cannot be silently changed to the more expensive 2K when the settings pop-up window is opened.
                    st.error(
                        tr("Metaso MiniMax Invalid Resolution").format(
                            value=configured_metaso_resolution,
                            supported=", ".join(metaso_resolution_options),
                        )
                    )
                metaso_resolution = st.selectbox(
                    tr("Metaso MiniMax Resolution"),
                    options=metaso_resolution_options,
                    index=(
                        metaso_resolution_options.index(configured_metaso_resolution)
                        if resolution_is_valid
                        else None
                    ),
                    key="metaso_minimax_resolution_input",
                    help=tr("Metaso MiniMax Resolution Help"),
                    placeholder=tr("Select Metaso MiniMax Resolution"),
                )
                if metaso_resolution is not None:
                    _set_runtime_config(
                        "app", "metaso_minimax_resolution", metaso_resolution
                    )

                st.divider()
                st.markdown("**OfoxAI**")
                st.caption(f"[OfoxAI]({OFOX_REFERRAL_URL}) · {tr('OFox AI Video Help')}")
                ofox_api_key = st.text_input(
                    tr("OFox API Key"),
                    value=str(config.app.get("ofox_api_key", "") or ""),
                    type="password",
                    key="ofox_api_key_input",
                )
                _set_runtime_config("app", "ofox_api_key", ofox_api_key.strip())
                ofox_model = st.text_input(
                    tr("OFox Text-to-Video Model"),
                    value=str(
                        config.app.get(
                            "ofox_text_to_video_model",
                            ofox.DEFAULT_MODEL_ID,
                        )
                        or ofox.DEFAULT_MODEL_ID
                    ),
                    key="ofox_text_to_video_model_input",
                )
                _set_runtime_config(
                    "app", "ofox_text_to_video_model", ofox_model.strip()
                )
                configured_ofox_base_url = str(
                    config.app.get("ofox_base_url", ofox.DEFAULT_BASE_URL)
                    or ofox.DEFAULT_BASE_URL
                ).strip()
                ofox_base_url = st.text_input(
                    tr("OFox Base URL"),
                    value=(
                        ""
                        if configured_ofox_base_url == ofox.DEFAULT_BASE_URL
                        else configured_ofox_base_url
                    ),
                    placeholder=ofox.DEFAULT_BASE_URL,
                    key="ofox_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "ofox_base_url",
                    ofox_base_url.strip() or ofox.DEFAULT_BASE_URL,
                )
                ofox_vendor_options = [
                    (tr("OFox Vendor BytePlus"), "byteplus"),
                    (tr("OFox Vendor Volcengine"), "volcengine"),
                    (tr("OFox Vendor Auto"), ""),
                ]
                configured_ofox_vendor = str(
                    config.app.get("ofox_provider", ofox.DEFAULT_PROVIDER_TYPE)
                    or ""
                ).strip()
                if configured_ofox_vendor not in {
                    value for _, value in ofox_vendor_options
                }:
                    # Keep this selection when the user manually pinned other vendor names in config.toml.
                    # Avoid being overwritten back to the default value by the drop-down box when opening the settings page.
                    ofox_vendor_options.append(
                        (configured_ofox_vendor, configured_ofox_vendor)
                    )
                selected_ofox_vendor = stable_selectbox(
                    tr("OFox Upstream Vendor"),
                    options=[value for _, value in ofox_vendor_options],
                    default_value=configured_ofox_vendor,
                    key="ofox_provider_select",
                    format_func=lambda value: dict(
                        (v, label) for label, v in ofox_vendor_options
                    )[value],
                    help=tr("OFox Upstream Vendor Help"),
                )
                _set_runtime_config("app", "ofox_provider", selected_ofox_vendor)

                st.divider()
                st.markdown(f"**{tr('Shengsuan Cloud AI Video')}**")
                app_config_snapshot = config.snapshot_config_with_pending(config.app)
                if (
                    str(app_config_snapshot.get("llm_provider", "") or "").lower()
                    == "shengsuanyun"
                ):
                    # When the large model Provider has been selected to win the cloud, the video generation is directly reused.
                    # For the same key, an independent input box that is prone to ambiguity is no longer displayed.
                    st.caption(tr("Shengsuan Cloud API Key Reused"))
                else:
                    configured_loomloom_token = str(
                        app_config_snapshot.get("loomloom_api_token", "") or ""
                    ).strip()
                    loomloom_api_token = st.text_input(
                        tr("Shengsuan Cloud API Key"),
                        value=configured_loomloom_token,
                        type="password",
                        key="loomloom_api_token_input",
                        help=tr("Shengsuan Cloud API Key Help"),
                        placeholder=tr("Shengsuan Cloud API Key Placeholder"),
                    ).strip()
                    _set_runtime_config(
                        "app", "loomloom_api_token", loomloom_api_token
                    )

                st.divider()
                seedance_api_key_value = str(
                    config.app.get("volcengine_seedance_api_key", "") or ""
                ).strip()
                shared_ark_api_key = str(
                    config.app.get("volcengine_api_key", "") or ""
                ).strip()
                environment_ark_api_key = os.getenv(
                    "VOLCENGINE_ARK_API_KEY", ""
                ).strip()
                seedance_reuses_llm_key = bool(
                    not seedance_api_key_value
                    and not environment_ark_api_key
                    and shared_ark_api_key
                )
                seedance_title = f"**{tr('Volcano Engine Seedance')}**"
                if seedance_reuses_llm_key:
                    # Only the reused large model key cannot be directly seen from the current input box, so keep this prompt.
                    # This can prevent users from mistakenly thinking that they must fill in the information repeatedly; the general configuration status will not be described again.
                    seedance_title += f" :blue[{tr('Reusing LLM API Key')}]"
                st.markdown(seedance_title)
                seedance_api_key = st.text_input(
                    tr("Volcano Engine Ark API Key"),
                    value=seedance_api_key_value,
                    type="password",
                    help=tr("Volcano Engine Ark API Key Help"),
                    key="volcengine_seedance_api_key_input",
                )
                _set_runtime_config(
                    "app", "volcengine_seedance_api_key", seedance_api_key.strip()
                )
                configured_seedance_model = str(
                    config.app.get(
                        "volcengine_seedance_model",
                        volcengine_seedance.DEFAULT_MODEL_ID,
                    )
                    or volcengine_seedance.DEFAULT_MODEL_ID
                ).strip()
                seedance_model = st.text_input(
                    tr("Volcano Engine Seedance Model"),
                    # Built-in default values are displayed through placeholders, user-defined
                    # Model or access point IDs are still displayed and saved as real values.
                    value=(
                        ""
                        if configured_seedance_model
                        == volcengine_seedance.DEFAULT_MODEL_ID
                        else configured_seedance_model
                    ),
                    placeholder=volcengine_seedance.DEFAULT_MODEL_ID,
                    key="volcengine_seedance_model_input",
                )
                _set_runtime_config(
                    "app",
                    "volcengine_seedance_model",
                    seedance_model.strip() or volcengine_seedance.DEFAULT_MODEL_ID,
                )
                configured_seedance_base_url = str(
                    config.app.get(
                        "volcengine_seedance_base_url",
                        volcengine_seedance.DEFAULT_BASE_URL,
                    )
                    or volcengine_seedance.DEFAULT_BASE_URL
                ).strip()
                seedance_base_url = st.text_input(
                    tr("Volcano Engine Ark Base URL"),
                    value=(
                        ""
                        if configured_seedance_base_url
                        == volcengine_seedance.DEFAULT_BASE_URL
                        else configured_seedance_base_url
                    ),
                    placeholder=volcengine_seedance.DEFAULT_BASE_URL,
                    key="volcengine_seedance_base_url_input",
                )
                _set_runtime_config(
                    "app",
                    "volcengine_seedance_base_url",
                    seedance_base_url.strip() or volcengine_seedance.DEFAULT_BASE_URL,
                )

                st.divider()
                wavespeed_api_key = _get_material_api_keys("wavespeed_api_keys")
                st.markdown("**WaveSpeed**")
                wavespeed_api_key = st.text_input(
                    tr("WaveSpeed API Key"),
                    value=wavespeed_api_key,
                    type="password",
                    key="wavespeed_api_keys_input",
                )
                _save_material_api_keys("wavespeed_api_keys", wavespeed_api_key)

                st.divider()
                st.markdown(f"**{tr('MuAPI AI Video')}**")
                st.caption(tr("MuAPI AI Video Help"))
                muapi_api_key = st.text_input(
                    tr("MuAPI API Key"),
                    value=str(config.app.get("muapi_api_key", "") or ""),
                    type="password",
                    help=tr("MuAPI API Key Help"),
                    key="muapi_api_key_input",
                )
                _set_runtime_config("app", "muapi_api_key", muapi_api_key.strip())
                configured_muapi_base_url = str(
                    config.app.get("muapi_base_url", muapi.DEFAULT_BASE_URL)
                    or muapi.DEFAULT_BASE_URL
                ).strip()
                muapi_base_url = st.text_input(
                    tr("MuAPI Base URL"),
                    value=(
                        ""
                        if configured_muapi_base_url == muapi.DEFAULT_BASE_URL
                        else configured_muapi_base_url
                    ),
                    placeholder=muapi.DEFAULT_BASE_URL,
                    key="muapi_base_url_input",
                    help=tr("MuAPI Base URL Help"),
                )
                _set_runtime_config(
                    "app",
                    "muapi_base_url",
                    muapi_base_url.strip() or muapi.DEFAULT_BASE_URL,
                )
                configured_muapi_endpoint = str(
                    config.app.get("muapi_video_endpoint", muapi.DEFAULT_ENDPOINT)
                    or muapi.DEFAULT_ENDPOINT
                ).strip()
                muapi_endpoint = st.text_input(
                    tr("MuAPI Video Endpoint"),
                    value=(
                        ""
                        if configured_muapi_endpoint == muapi.DEFAULT_ENDPOINT
                        else configured_muapi_endpoint
                    ),
                    placeholder=muapi.DEFAULT_ENDPOINT,
                    key="muapi_video_endpoint_input",
                    help=tr("MuAPI Video Endpoint Help"),
                )
                _set_runtime_config(
                    "app",
                    "muapi_video_endpoint",
                    muapi_endpoint.strip() or muapi.DEFAULT_ENDPOINT,
                )
                configured_muapi_resolution = str(
                    config.app.get("muapi_resolution", muapi.DEFAULT_RESOLUTION)
                    or muapi.DEFAULT_RESOLUTION
                ).strip()
                muapi_resolution = st.text_input(
                    tr("MuAPI Resolution"),
                    value=(
                        ""
                        if configured_muapi_resolution == muapi.DEFAULT_RESOLUTION
                        else configured_muapi_resolution
                    ),
                    placeholder=muapi.DEFAULT_RESOLUTION,
                    key="muapi_resolution_input",
                    help=tr("MuAPI Resolution Help"),
                )
                _set_runtime_config(
                    "app",
                    "muapi_resolution",
                    muapi_resolution.strip() or muapi.DEFAULT_RESOLUTION,
                )


            with st.container(border=True):
                st.markdown(f"#### {tr('AI Image Generation APIs')}")
                st.caption(tr("AI Image Generation APIs Help"))
                st.markdown(f"**{tr('OpenAI Compatible Text-to-Image')}**")

                openai_image_base_url = st.text_input(
                    tr("OpenAI Image Base URL"),
                    value=str(config.app.get("openai_image_base_url", "") or ""),
                    placeholder="https://api.openai.com/v1",
                    key="openai_image_base_url_input",
                )
                _set_runtime_config(
                    "app", "openai_image_base_url", openai_image_base_url.strip()
                )

                openai_image_api_key = _get_material_api_keys(
                    "openai_image_api_keys"
                )
                openai_image_api_key = st.text_input(
                    tr("OpenAI Image API Key"),
                    value=openai_image_api_key,
                    type="password",
                    help=tr("OpenAI Image API Key Help"),
                    key="openai_image_api_keys_input",
                )
                _save_material_api_keys(
                    "openai_image_api_keys", openai_image_api_key
                )

                openai_image_model = st.text_input(
                    tr("OpenAI Image Model"),
                    value=str(config.app.get("openai_image_model", "") or ""),
                    placeholder="gpt-image-2",
                    key="openai_image_model_input",
                )
                _set_runtime_config(
                    "app", "openai_image_model", openai_image_model.strip()
                )
                # Only reference values are shown and OpenAI official endpoints are not written as the default configuration.
                # There is no uniform value for the Base URL and model ID of compatible services; leaving blank will not
                # If a user mistakenly connects to the official payment interface without knowing it, the old configuration will not be overwritten.
                st.caption(tr("OpenAI Image Configuration Example"))

                with st.expander(
                    tr("OpenAI Image Advanced Settings"), expanded=False
                ):
                    openai_image_size = st.text_input(
                        tr("OpenAI Image Size"),
                        value=str(config.app.get("openai_image_size", "") or ""),
                        placeholder="1024x1536",
                        help=tr("OpenAI Image Size Help"),
                        key="openai_image_size_input",
                    )
                    _set_runtime_config(
                        "app", "openai_image_size", openai_image_size.strip()
                    )

                    openai_image_prompt_template = st.text_input(
                        tr("OpenAI Image Prompt Template"),
                        value=str(
                            config.app.get("openai_image_prompt_template", "") or ""
                        ),
                        placeholder="cinematic photo of {term}, photorealistic",
                        help=tr("OpenAI Image Prompt Template Help"),
                        key="openai_image_prompt_template_input",
                    )
                    _set_runtime_config(
                        "app",
                        "openai_image_prompt_template",
                        openai_image_prompt_template.strip(),
                    )

    _save_runtime_config()


@st.dialog(
    tr("Settings"),
    width="medium",
    on_dismiss=_dismiss_settings_dialog,
)
def _render_settings_dialog():
    _render_settings_content(in_dialog=True)


# -----------------------------------------------------------------------------
# Main generation form: copywriting, video, audio and subtitle panels
# -----------------------------------------------------------------------------


def _create_loomloom_script_backend():
    """Create a bulk copywriting client from the current WebUI/config.toml configuration."""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.LoomLoomSettings.from_mapping(app_config_snapshot)
    return loomloom.LoomLoomScriptBackend(settings)


def _create_loomloom_video_backend():
    """Create a video client using the project default SkillBot and currently valid credentials."""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.video_settings_from_mapping(app_config_snapshot)
    return loomloom.LoomLoomVideoBackend(settings)


def _effective_loomloom_api_token():
    """Read the Winning Cloud API Key that has not yet been placed in the WebUI or in config.toml."""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    return loomloom.resolve_api_token(app_config_snapshot)


def _effective_script_generation_backend():
    """Read the copywriting generation method that contains the changes to be saved in WebUI."""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    backend = str(
        app_config_snapshot.get("script_generation_backend", "local") or "local"
    ).strip()
    return backend if backend in {"local", "loomloom"} else "local"




def _script_generation_method_help(selected_backend):
    """Let the question mark content of "Copywriting Generation Method" strictly follow the current selection."""
    if selected_backend != "loomloom":
        return tr("Script Generation Method Help")

    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    guidance = [tr("LoomLoom Batch Script Generation Help")]
    if (
        str(app_config_snapshot.get("llm_provider", "") or "").strip().lower()
        == "shengsuanyun"
    ):
        guidance.append(tr("Shengsuan Cloud API Key Reused"))
    guidance.append(tr("Shengsuan Cloud API Key Link"))
    return "\n\n".join(guidance)


def _loomloom_video_scene_prompts(video_terms, subject, scene_count):
    """A limited number of scene descriptions are generated based on material keywords for the video model to generate materials segment by segment."""
    if isinstance(video_terms, str):
        terms = [
            term.strip() for term in re.split(r"[,，\n]", video_terms) if term.strip()
        ]
    elif isinstance(video_terms, list):
        terms = [
            str(term or "").strip() for term in video_terms if str(term or "").strip()
        ]
    else:
        terms = []
    fallback = str(subject or "").strip()
    if not terms and fallback:
        terms = [fallback]
    if not terms:
        return ()
    return tuple(
        (
            terms[index % len(terms)]
            if index < len(terms)
            else f"{terms[index % len(terms)]}; alternative camera angle {index + 1}"
        )
        for index in range(int(scene_count))
    )


def _loomloom_video_signature(batch, credential_fingerprint):
    """Incorporate all billing inputs and voucher summaries into the signature, and force requotes after parameter changes."""
    payload = {
        "inputRows": [dict(row) for row in batch.input_rows],
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _loomloom_video_account_signature(token):
    """The service address and credentials jointly isolate the model directory and quotation, and the confirmed status cannot be reused across endpoints."""
    values = config.snapshot_config_with_pending(config.app)
    base_url = str(values.get("loomloom_base_url") or loomloom.DEFAULT_BASE_URL).strip().rstrip("/")
    payload = json.dumps([base_url, str(token or "").strip()])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_loomloom_video_capability(token, *, force=False):
    """Cache Profile by current credentials; retain the most recent successful result for the same credential when refresh fails."""
    normalized_token = str(token or "").strip()
    if not normalized_token:
        return None

    fingerprint = _loomloom_video_account_signature(normalized_token)
    if st.session_state.get("loomloom_video_capability_fingerprint") != fingerprint:
        st.session_state["loomloom_video_capability"] = None
        st.session_state["loomloom_video_capability_fingerprint"] = fingerprint
        st.session_state["loomloom_video_capability_load_attempt"] = ""
        st.session_state["loomloom_video_capability_error"] = ""

    should_load = force or (
        st.session_state.get("loomloom_video_capability_load_attempt") != fingerprint
    )
    if should_load:
        st.session_state["loomloom_video_capability_load_attempt"] = fingerprint
        try:
            capability = _create_loomloom_video_backend().resolve_video_capability()
        except (loomloom.LoomLoomError, ValueError) as exc:
            logger.warning(
                f"failed to load LoomLoom video capability: error={type(exc).__name__}"
            )
            st.session_state["loomloom_video_capability_error"] = str(exc)
        else:
            st.session_state["loomloom_video_capability"] = capability
            st.session_state["loomloom_video_capability_error"] = ""

    capability = st.session_state.get("loomloom_video_capability")
    return (
        capability if isinstance(capability, loomloom.LoomLoomVideoCapability) else None
    )


def _normalize_loomloom_model_identifier(value):
    """Unify the delimiters and case of display names and model IDs for safe matching in local price lists."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value or "").lower())


def _loomloom_video_model_price(model):
    """Returns the known model (short price in the drop-down box, complete reference price after selection); returns null value for the unknown model."""
    identifiers = {
        _normalize_loomloom_model_identifier(model.model_id),
        _normalize_loomloom_model_identifier(model.display_name),
    }
    for aliases, compact_price, detailed_price in LOOMLOOM_VIDEO_MODEL_PRICES:
        if identifiers.intersection(aliases):
            return compact_price, detailed_price
    return "", ""


def _format_loomloom_video_model_option(model):
    """Display a short price to the right of the model name to prevent multiple resolution prices from making the drop-down box too wide."""
    compact_price, _ = _loomloom_video_model_price(model)
    return f"{model.display_name} · {compact_price}" if compact_price else model.display_name


def _effective_voice_rate_before_audio_panel():
    """The video panel is located before the audio panel and needs to read the current speech rate from the existing control state or configuration."""
    raw_rate = st.session_state.get(
        localized_widget_key("voice_rate_select"),
        config.ui.get("voice_rate", 1.0),
    )
    try:
        rate = float(raw_rate)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    return rate if math.isfinite(rate) and rate > 0 else 1.0


def _matching_full_voice_preview_duration(script, voice_rate):
    """The actual duration of the complete audition will only be used when the copy, provider, timbre, and speaking speed have not changed."""
    cached = st.session_state.get("voice_preview_audio")
    if not isinstance(cached, dict) or cached.get("preview_type") != "full":
        return None
    script_digest = hashlib.sha256(str(script or "").encode("utf-8")).hexdigest()
    if cached.get("content_digest") != script_digest:
        return None

    current_tts_server = st.session_state.get(
        localized_widget_key("tts_server_select"),
        config.ui.get("tts_server", "azure-tts-v1"),
    )
    current_voice_name = st.session_state.get(
        localized_widget_key(f"speech_synthesis_select_{current_tts_server}"),
        config.ui.get("voice_name", ""),
    )
    try:
        cached_voice_rate = float(cached.get("voice_rate", 0))
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        cached.get("tts_server") != current_tts_server
        or cached.get("voice_name") != current_voice_name
        or not math.isfinite(cached_voice_rate)
        or not math.isclose(cached_voice_rate, voice_rate)
    ):
        return None

    duration = cached.get("duration")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return None
    return float(duration)


def _loomloom_video_coverage_plan(params):
    """The number of recommended materials is based on the actual or estimated narration duration; the missing parts are still filled in by the original loop logic."""
    script = str(params.video_script or "").strip()
    if not script:
        return None

    voice_rate = _effective_voice_rate_before_audio_panel()
    actual_duration = _matching_full_voice_preview_duration(script, voice_rate)
    if actual_duration is not None:
        duration_min = duration_max = actual_duration
        basis_key = "AI Video Duration Basis Actual"
    else:
        estimated = _estimate_voiceover_duration_range(script, voice_rate)
        if not estimated:
            return None
        duration_min, duration_max = estimated
        basis_key = "AI Video Duration Basis Estimated"

    clip_duration = max(float(params.video_clip_duration or 1), 1.0)
    needed_min = max(math.ceil(duration_min / clip_duration), 1)
    needed_max = max(math.ceil(duration_max / clip_duration), needed_min)
    return {
        "script_digest": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "basis_key": basis_key,
        "duration_min": float(duration_min),
        "duration_max": float(duration_max),
        "clip_duration": clip_duration,
        "needed_min": needed_min,
        "needed_max": needed_max,
        # The recommended value first covers the conservative upper bound, but will never exceed the upper limit of paid tasks allowed by the server.
        "recommended_count": min(needed_max, loomloom.MAX_VIDEO_SCENES),
    }


def _format_numeric_range(minimum, maximum, digits=1):
    if math.isclose(float(minimum), float(maximum)):
        return f"{float(maximum):.{digits}f}"
    return f"{float(minimum):.{digits}f}–{float(maximum):.{digits}f}"


def _selected_loomloom_video_model(capability):
    """Return the user selection that is still among the current Profile candidates without silent rollback."""
    selected_model_id = str(
        st.session_state.get("loomloom_video_model_id", "") or ""
    ).strip()
    eligible_model_ids = {model.model_id for model in capability.models}
    return selected_model_id if selected_model_id in eligible_model_ids else ""


def _current_loomloom_video_quote_context(params):
    """Builds the default SkillBot's batch of video quotes based on the current page parameters."""
    token = _effective_loomloom_api_token()
    fingerprint = _loomloom_video_account_signature(token) if token else ""
    capability = st.session_state.get("loomloom_video_capability")
    if (
        not isinstance(capability, loomloom.LoomLoomVideoCapability)
        or st.session_state.get("loomloom_video_capability_fingerprint") != fingerprint
    ):
        return None, ""
    model_id = _selected_loomloom_video_model(capability)
    scene_count = int(st.session_state.get("loomloom_video_scene_count", 1) or 1)
    prompts = _loomloom_video_scene_prompts(
        params.video_terms,
        params.video_subject or params.video_script,
        scene_count,
    )
    aspect_ratio = str(
        params.video_aspect.value
        if isinstance(params.video_aspect, VideoAspect)
        else params.video_aspect
    )
    if (
        not token
        or not model_id
        or not prompts
        or aspect_ratio not in capability.aspect_ratios
    ):
        return None, ""
    try:
        batch = _create_loomloom_video_backend().prepare_video_batch(
            subject=params.video_subject or params.video_script,
            scene_prompts=prompts,
            model_id=model_id,
            aspect_ratio=aspect_ratio,
        )
    except (loomloom.LoomLoomError, ValueError):
        return None, ""
    return batch, _loomloom_video_signature(batch, fingerprint)


def _retry_loomloom_video_quote():
    """The failure lock is released when the user actively retries; the previous payment confirmation is not used."""
    st.session_state["loomloom_video_quote_error_signature"] = ""
    st.session_state["loomloom_video_quote_error"] = ""
    st.session_state["loomloom_video_confirm_charge"] = False


def _render_loomloom_video_settings(params):
    """Render default video SkillBot's quote, quote invalidation, and payment confirmation processes."""
    st.caption(tr("Shengsuan Cloud AI Video Help"))
    if (
        str(
            config.snapshot_config_with_pending(config.app).get("llm_provider", "")
            or ""
        ).lower()
        == "shengsuanyun"
    ):
        st.caption(tr("Shengsuan Cloud API Key Reused"))

    token = _effective_loomloom_api_token()

    refresh_models = st.button(
        tr("Refresh AI Video Models"),
        key="loomloom_refresh_video_models",
        use_container_width=True,
        disabled=not token,
    )
    capability = _load_loomloom_video_capability(token, force=refresh_models)
    capability_error = str(
        st.session_state.get("loomloom_video_capability_error", "") or ""
    ).strip()
    if capability_error:
        st.warning(tr("AI Video Model List Load Failed").format(error=capability_error))

    if capability is not None:
        models_by_id = {model.model_id: model for model in capability.models}
        selected_model_id = str(
            st.session_state.get("loomloom_video_model_id", "") or ""
        ).strip()
        if not selected_model_id:
            selected_model_id = capability.default_model_id
            st.session_state["loomloom_video_model_id"] = selected_model_id

        model_options = list(models_by_id)
        if selected_model_id not in models_by_id:
            # Keep the original selection that has expired, allowing users to clearly see the status and proactively reselect. Directly put the control
            # Changing to the new default model will make the old quotes inconsistent with user perceptions.
            model_options.insert(0, selected_model_id)

        selected_model_id = stable_selectbox(
            tr("AI Video Model"),
            options=model_options,
            default_value=selected_model_id,
            key="loomloom_video_model_select",
            format_func=lambda model_id: (
                _format_loomloom_video_model_option(models_by_id[model_id])
                if model_id in models_by_id
                else tr("Unavailable AI Video Model").format(model=model_id)
            ),
        )
        st.session_state["loomloom_video_model_id"] = selected_model_id
        if selected_model_id not in models_by_id:
            st.error(tr("Selected AI Video Model Unavailable"))
        else:
            _, detailed_price = _loomloom_video_model_price(
                models_by_id[selected_model_id]
            )
            if detailed_price:
                st.caption(
                    tr("AI Video Model Reference Price").format(price=detailed_price)
                )

        current_aspect_ratio = str(
            params.video_aspect.value
            if isinstance(params.video_aspect, VideoAspect)
            else params.video_aspect
        )
        if current_aspect_ratio not in capability.aspect_ratios:
            st.error(tr("Selected AI Video Ratio Unavailable"))

    coverage_plan = _loomloom_video_coverage_plan(params)
    pending_autofill_digest = str(
        st.session_state.get("loomloom_video_scene_autofill_digest", "") or ""
    )
    if (
        coverage_plan is not None
        and pending_autofill_digest == coverage_plan["script_digest"]
    ):
        # It is only recommended once when "the copy has just been generated" or "the full trial duration has just been obtained".
        # After consuming the mark, it will no longer be overwritten, and the number of segments manually adjusted by the user will be completely retained.
        st.session_state["loomloom_video_scene_count"] = coverage_plan[
            "recommended_count"
        ]
        st.session_state["loomloom_video_scene_autofill_digest"] = ""

    scene_count = st.number_input(
        tr("AI Video Scene Count"),
        min_value=1,
        max_value=loomloom.MAX_VIDEO_SCENES,
        step=1,
        key="loomloom_video_scene_count",
    )
    _set_runtime_config("ui", "loomloom_video_scene_count", int(scene_count))
    if coverage_plan is not None:
        coverage_seconds = int(scene_count) * coverage_plan["clip_duration"]
        shortfall_min = max(
            coverage_plan["duration_min"] - coverage_seconds, 0.0
        )
        shortfall_max = max(
            coverage_plan["duration_max"] - coverage_seconds, 0.0
        )
        duration_basis = tr(coverage_plan["basis_key"]).format(
            duration=_format_numeric_range(
                coverage_plan["duration_min"], coverage_plan["duration_max"]
            )
        )
        coverage_message = tr("AI Video Material Coverage").format(
            basis=duration_basis,
            clip=_format_numeric_range(
                coverage_plan["clip_duration"], coverage_plan["clip_duration"]
            ),
            needed=_format_numeric_range(
                coverage_plan["needed_min"], coverage_plan["needed_max"], digits=0
            ),
            count=int(scene_count),
            coverage=_format_numeric_range(coverage_seconds, coverage_seconds),
            shortfall=_format_numeric_range(shortfall_min, shortfall_max),
        )
        if shortfall_max > 0:
            st.warning(coverage_message)
        else:
            st.caption(coverage_message)

    batch, input_signature = _current_loomloom_video_quote_context(params)
    if not token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    quote_result = st.session_state.get("loomloom_video_quote")
    quoted_batch = st.session_state.get("loomloom_video_batch")
    quote_is_current = bool(
        quote_result is not None
        and quoted_batch is not None
        and st.session_state.get("loomloom_video_input_signature") == input_signature
    )
    # Automatic requests are paused after the same set of parameters fails to avoid repeated waiting for service timeout for ordinary page interactions.
    # The signature includes the account number, endpoint and all billing inputs; the price will be inquired after parameter changes or the user actively retries.
    if st.session_state.get("loomloom_video_quote_error_signature") != input_signature:
        st.session_state["loomloom_video_quote_error_signature"] = ""
        st.session_state["loomloom_video_quote_error"] = ""
    quote_failed = bool(st.session_state.get("loomloom_video_quote_error"))
    # Quote does not create paid tasks, and the actual execution still requires the user to explicitly check and confirm.
    if token and batch is not None and not quote_is_current and not quote_failed:
        st.session_state["loomloom_video_confirm_charge"] = False
        try:
            quote_result = _create_loomloom_video_backend().quote(batch)
        except (loomloom.LoomLoomError, ValueError) as exc:
            logger.warning(f"failed to quote LoomLoom videos: error={exc}")
            st.session_state["loomloom_video_quote_error_signature"] = input_signature
            st.session_state["loomloom_video_quote_error"] = str(exc) or type(exc).__name__
        else:
            st.session_state["loomloom_video_batch"] = batch
            st.session_state["loomloom_video_quote"] = quote_result
            st.session_state["loomloom_video_input_signature"] = input_signature
            st.session_state["loomloom_video_client_request_id"] = (
                f"mpt-video-{uuid4()}"
            )
            st.session_state["loomloom_video_confirm_charge"] = False
            logger.info(
                "LoomLoom video quote ready: "
                f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
            )

    if st.session_state.get("loomloom_video_quote_error"):
        st.error(st.session_state["loomloom_video_quote_error"])
        st.button(
            tr("Retry AI Video Quote"),
            key="loomloom_retry_video_quote",
            on_click=_retry_loomloom_video_quote,
        )

    quote_result = st.session_state.get("loomloom_video_quote")
    quoted_batch = st.session_state.get("loomloom_video_batch")
    if quote_result is not None and quoted_batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        if quote_result.estimated_buyer_payable_t == 0:
            st.warning(tr("AI Video Quote Estimate Incomplete"))
        else:
            st.success(
                tr(
                    "AI Video Quote Summary Singular"
                    if quote_result.task_count == 1
                    else "AI Video Quote Summary"
                ).format(
                    tasks=quote_result.task_count,
                    amount=display_amount,
                    currency=quote_result.currency,
                )
            )
        quote_is_current = (
            st.session_state.get("loomloom_video_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        st.checkbox(
            tr("Confirm AI Video Charge"),
            key="loomloom_video_confirm_charge",
            help=tr("Confirm AI Video Charge Help"),
            disabled=not quote_is_current,
        )


def _loomloom_script_signature(
    *,
    subject,
    language,
    candidate_count,
    duration_seconds,
    style,
    credential_fingerprint,
):
    payload = {
        "subject": str(subject or "").strip(),
        "language": str(language or "auto").strip() or "auto",
        "candidateCount": int(candidate_count),
        "durationSeconds": int(duration_seconds),
        "style": str(style or "").strip(),
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _render_local_script_generation(params):
    """Keep the original local LLM script generation path of VietNamNewsVideo."""
    if not st.button(
        tr("Generate Video Script and Keywords"),
        key="auto_generate_script",
        use_container_width=True,
        type="secondary",
        icon=":material/auto_awesome:",
    ):
        return

    if not params.video_subject:
        st.toast(tr("Please Enter the Video Subject First"))
        st.warning(tr("Please Enter the Video Subject First"))
        return

    with st.spinner(tr("Generating Video Script and Keywords")):

        def generate_script_and_terms(app_config_snapshot):
            script = llm.generate_script(
                video_subject=params.video_subject,
                language=params.video_language,
                paragraph_number=params.paragraph_number,
                video_script_prompt=params.video_script_prompt,
                custom_system_prompt=params.custom_system_prompt,
                app_config=app_config_snapshot,
            )
            terms = llm.generate_terms(
                params.video_subject,
                script,
                amount=8 if params.match_materials_to_script else 5,
                match_script_order=params.match_materials_to_script,
                app_config=app_config_snapshot,
            )
            return script, terms

        script, terms = _run_llm_read_operation(
            "generate_script_and_terms",
            generate_script_and_terms,
        )
        if "Error: " in script:
            st.error(tr(script))
        elif "Error: " in terms:
            st.error(tr(terms))
        else:
            st.session_state["video_script"] = script
            st.session_state["video_terms"] = ", ".join(terms)
            st.session_state["loomloom_video_scene_autofill_digest"] = (
                hashlib.sha256(script.strip().encode("utf-8")).hexdigest()
            )


def _render_loomloom_candidates():
    candidates = tuple(st.session_state.get("loomloom_script_candidates") or ())
    raw_errors: Iterable[Any] = st.session_state.get("loomloom_candidate_errors") or ()
    errors = list(raw_errors)
    if errors:
        st.warning(
            tr("LoomLoom Candidate Errors").format(
                count=len(errors),
                details="; ".join(
                    f"#{getattr(error, 'row_index', 0) + 1}: {getattr(error, 'message', str(error))}" for error in errors
                ),
            )
        )
    if not candidates:
        return

    selected_index = st.radio(
        tr("Choose Script Candidate"),
        options=list(range(len(candidates))),
        key="loomloom_selected_candidate",
        format_func=lambda index: (
            f"#{candidates[index].row_index + 1} {candidates[index].script[:80]}"
        ),
    )
    selected = candidates[selected_index]
    st.code(selected.script, language=None, wrap_lines=True)
    st.caption(", ".join(selected.video_terms))
    if st.button(
        tr("Use Selected Candidate"),
        key="loomloom_apply_candidate",
        type="primary",
        use_container_width=True,
    ):
        st.session_state["video_script"] = selected.script
        st.session_state["video_terms"] = ", ".join(selected.video_terms)
        # Consistent with ordinary large model copywriting generation: the number of materials is only recommended once after applying a new candidate.
        st.session_state["loomloom_video_scene_autofill_digest"] = (
            hashlib.sha256(selected.script.strip().encode("utf-8")).hexdigest()
        )
        st.toast(tr("LoomLoom Candidate Applied"))


def _handle_loomloom_poll_error(run_id, exc):
    """Perform limited backoff for script task polling errors, and stop polling immediately for deterministic errors."""
    logger.warning(f"failed to poll LoomLoom run: run_id={run_id}, error={exc}")
    failure_count = int(st.session_state.get("loomloom_poll_failure_count", 0) or 0) + 1
    retryable = isinstance(exc, loomloom.LoomLoomAPIError) and exc.retryable
    if not retryable or failure_count >= LOOMLOOM_MAX_POLL_FAILURES:
        st.session_state["loomloom_run_error"] = str(exc)
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        # The failure of the query does not mean the failure of the remote payment task. Keep run_id and pause automatic polling to let users
        # You can continue to query the same task; if you discard the ID and resubmit it, you may be charged twice.
        st.session_state["loomloom_poll_paused"] = True
        st.rerun(scope="app")
        return

    retry_delay = min(2**failure_count, 30)
    st.session_state["loomloom_poll_failure_count"] = failure_count
    st.session_state["loomloom_poll_retry_after"] = time.monotonic() + retry_delay
    st.warning(
        tr("LoomLoom Poll Retry Warning").format(
            attempt=failure_count,
            max_attempts=LOOMLOOM_MAX_POLL_FAILURES,
        )
    )


@st.fragment(run_every="2s")
def _render_loomloom_run_progress():
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if not run_id or st.session_state.get("loomloom_poll_paused", False):
        return
    retry_after = float(st.session_state.get("loomloom_poll_retry_after", 0.0) or 0.0)
    retry_wait_seconds = max(0, int(math.ceil(retry_after - time.monotonic())))
    if retry_wait_seconds > 0:
        st.info(
            tr("LoomLoom Poll Retry Pending").format(
                seconds=retry_wait_seconds,
            )
        )
        return
    try:
        backend = _create_loomloom_script_backend()
        run = backend.get_run(run_id)
    except loomloom.LoomLoomError as exc:
        _handle_loomloom_poll_error(run_id, exc)
        return

    st.session_state["loomloom_run_status"] = run.status
    if run.status == "completed":
        try:
            result = backend.get_script_results(run_id)
        except loomloom.LoomLoomError as exc:
            _handle_loomloom_poll_error(run_id, exc)
            return
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        st.session_state["loomloom_poll_paused"] = False
        st.session_state["loomloom_script_candidates"] = result.candidates
        st.session_state["loomloom_candidate_errors"] = result.errors
        st.session_state["loomloom_selected_candidate"] = 0
        st.session_state["loomloom_run_id"] = ""
        st.rerun(scope="app")
        return
    if run.status in {"failed", "cancelled", "canceled"}:
        st.session_state["loomloom_run_error"] = run.first_error_message or run.status
        st.session_state["loomloom_run_id"] = ""
        st.session_state["loomloom_poll_paused"] = False
        st.rerun(scope="app")
        return

    st.session_state["loomloom_poll_failure_count"] = 0
    st.session_state["loomloom_poll_retry_after"] = 0.0
    st.info(
        tr("LoomLoom Run Progress").format(
            completed=run.completed_tasks,
            total=run.total_tasks,
        )
    )


def _render_loomloom_script_generation(params):
    st.caption(tr("LoomLoom Batch Script Generation Help"))
    effective_token = _effective_loomloom_api_token()
    if not effective_token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    candidate_col, duration_col = st.columns(2)
    candidate_count = candidate_col.number_input(
        tr("Script Candidate Count"),
        min_value=1,
        max_value=loomloom.MAX_SCRIPT_CANDIDATES,
        step=1,
        key="loomloom_candidate_count",
    )
    duration_seconds = duration_col.number_input(
        tr("Target Script Duration Seconds"),
        min_value=10,
        max_value=600,
        step=10,
        key="loomloom_script_duration_seconds",
    )
    _set_runtime_config("ui", "loomloom_candidate_count", int(candidate_count))
    _set_runtime_config(
        "ui", "loomloom_script_duration_seconds", int(duration_seconds)
    )
    input_signature = _loomloom_script_signature(
        subject=params.video_subject,
        language=params.video_language,
        candidate_count=candidate_count,
        duration_seconds=duration_seconds,
        style=params.video_script_prompt,
        credential_fingerprint=(
            hashlib.sha256(effective_token.encode("utf-8")).hexdigest()
            if effective_token
            else ""
        ),
    )

    if st.button(
        tr("Get LoomLoom Quote"),
        key="loomloom_quote_scripts",
        use_container_width=True,
        type="secondary",
        icon=":material/request_quote:",
        disabled=not effective_token or bool(st.session_state.get("loomloom_run_id")),
    ):
        if not params.video_subject:
            st.toast(tr("Please Enter the Video Subject First"))
            st.warning(tr("Please Enter the Video Subject First"))
        else:
            try:
                backend = _create_loomloom_script_backend()
                batch = backend.prepare_script_batch(
                    subject=params.video_subject,
                    candidate_count=int(candidate_count),
                    language=params.video_language,
                    duration_seconds=int(duration_seconds),
                    style=params.video_script_prompt,
                )
                quote_result = backend.quote(batch)
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to quote LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_script_batch"] = batch
                st.session_state["loomloom_script_quote"] = quote_result
                st.session_state["loomloom_script_input_signature"] = input_signature
                st.session_state["loomloom_client_request_id"] = f"mpt-{uuid4()}"
                st.session_state["loomloom_run_id"] = ""
                st.session_state["loomloom_run_status"] = "quoted"
                st.session_state["loomloom_run_error"] = ""
                st.session_state["loomloom_poll_failure_count"] = 0
                st.session_state["loomloom_poll_retry_after"] = 0.0
                st.session_state["loomloom_poll_paused"] = False
                st.session_state["loomloom_script_candidates"] = ()
                st.session_state["loomloom_candidate_errors"] = ()
                st.session_state["loomloom_confirm_charge"] = False
                logger.info(
                    "LoomLoom script quote ready: "
                    f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                    f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
                )

    quote_result = st.session_state.get("loomloom_script_quote")
    batch = st.session_state.get("loomloom_script_batch")
    if quote_result is not None and batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        st.success(
            tr(
                "LoomLoom Quote Summary Singular"
                if quote_result.task_count == 1
                else "LoomLoom Quote Summary"
            ).format(
                tasks=quote_result.task_count,
                amount=display_amount,
                currency=quote_result.currency,
            )
        )
        quote_is_current = (
            st.session_state.get("loomloom_script_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        confirm_charge = st.checkbox(
            tr("Confirm LoomLoom Charge"),
            key="loomloom_confirm_charge",
            disabled=not quote_is_current,
        )
        run_in_progress = bool(st.session_state.get("loomloom_run_id"))
        if st.button(
            tr("Run LoomLoom Batch"),
            key="loomloom_execute_scripts",
            use_container_width=True,
            type="primary",
            disabled=(not quote_is_current or not confirm_charge or run_in_progress),
        ):
            try:
                execution = _create_loomloom_script_backend().execute(
                    batch,
                    client_request_id=st.session_state["loomloom_client_request_id"],
                    listing_version_id=quote_result.listing_version_id,
                    confirm=True,
                )
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to execute LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_run_id"] = execution.run_id
                st.session_state["loomloom_run_status"] = "running"
                st.session_state["loomloom_poll_paused"] = False
                # Only one paid batch is allowed to be initiated per quote. The background status only depends on run_id, submission
                # After that, the quotation and idempotent request ID can be discarded; after failure, the user needs to re-quote and try again.
                st.session_state["loomloom_script_batch"] = None
                st.session_state["loomloom_script_quote"] = None
                st.session_state["loomloom_script_input_signature"] = ""
                st.session_state["loomloom_client_request_id"] = ""
                logger.info(
                    f"LoomLoom script run submitted: run_id={execution.run_id}, "
                    f"tasks={len(batch.input_rows)}"
                )
                st.toast(tr("LoomLoom Run Submitted"))

    run_error = str(st.session_state.get("loomloom_run_error", "") or "").strip()
    if run_error:
        st.error(tr("LoomLoom Run Failed").format(error=run_error))
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if run_id and st.session_state.get("loomloom_poll_paused", False):
        retry_col, stop_col = st.columns(2)
        if retry_col.button(
            tr("Resume LoomLoom Status Check"),
            key="loomloom_resume_status_check",
            use_container_width=True,
            type="secondary",
        ):
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_failure_count"] = 0
            st.session_state["loomloom_poll_retry_after"] = 0.0
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
        if stop_col.button(
            tr("Stop Tracking LoomLoom Run"),
            key="loomloom_stop_tracking_run",
            use_container_width=True,
            type="secondary",
            help=tr("Stop Tracking LoomLoom Run Help"),
        ):
            # This only stops the local status query and does not claim to cancel the remote execution. After the user confirms to give up tracking
            # Only after clearing the run_id, the next paid run still needs to be re-quoted and confirmed.
            st.session_state["loomloom_run_id"] = ""
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
    # Only the batches that are actually running will start the two-second polling, and the quotation phase and result display phase will not be created.
    # Timing fragments avoid meaningless network requests and reruns when users stay on the page.
    if run_id and not st.session_state.get("loomloom_poll_paused", False):
        _render_loomloom_run_progress()
    _render_loomloom_candidates()


def _render_news_article_scraper(params):
    """Render news article scraper expander to extract text/images and generate Gemini video prompts."""
    with st.expander("📰 " + _t("Scrape News Article for Video"), expanded=False):
        st.caption(
            "Cào tiêu đề, tóm tắt và hình ảnh từ link báo để tạo kịch bản video và tư liệu hình ảnh."
        )
        article_url_input = st.text_input(
            _t("Article URL"),
            placeholder=_t("Article URL Placeholder"),
            key="news_article_url_input",
        ).strip()

        col_scrape, col_clear = st.columns([3, 1])
        scrape_clicked = col_scrape.button(
            _t("Scrape Article"),
            icon=":material/travel_explore:",
            type="primary",
            use_container_width=True,
            key="btn_scrape_news_article",
        )
        if col_clear.button(
            "Xóa",
            icon=":material/clear_all:",
            type="tertiary",
            use_container_width=True,
            key="btn_clear_scraped_news",
        ):
            st.session_state["scraped_article_data"] = None
            st.session_state["scraped_article_gemini_prompt"] = ""
            st.rerun(scope="app")

        if scrape_clicked:
            if not article_url_input:
                st.warning("Vui lòng nhập URL bài báo!")
            elif not article_scraper.is_safe_url(article_url_input):
                st.error("URL không hợp lệ hoặc địa chỉ IP thuộc mạng nội bộ!")
            else:
                with st.spinner(_t("Scraping Article")):
                    try:
                        scraped = article_scraper.scrape_article(article_url_input)
                        st.session_state["scraped_article_data"] = scraped
                        src_name = article_scraper.get_news_source_name(scraped.url)
                        if src_name:
                            src_badge = f"Nguồn: {src_name}"
                            st.session_state["source_badge_text"] = src_badge
                            st.session_state["source_badge_text_input"] = src_badge
                            st.session_state["preview_source_badge_text_input"] = src_badge
                            st.session_state["source_badge_enabled"] = True
                            st.session_state["source_badge_enabled_checkbox"] = True
                            st.session_state["preview_source_badge_enabled_toggle"] = True
                            params.source_badge_text = src_badge
                            params.source_badge_enabled = True
                        st.session_state["scraped_article_gemini_prompt"] = (
                            article_scraper.generate_gemini_news_prompt(
                                scraped,
                                target_duration=60,
                                language=params.video_language or "vi",
                            )
                        )
                        st.toast(_t("Article Scraped Successfully"), icon="✅")
                    except Exception as exc:
                        logger.error(f"Failed to scrape article: {exc}")
                        st.error(f"Không thể cào dữ liệu: {exc}")

        scraped_article = st.session_state.get("scraped_article_data")
        if scraped_article:
            st.markdown("---")
            st.markdown(f"**📰 {scraped_article.title}**")
            meta_parts = []
            if scraped_article.domain:
                meta_parts.append(f"🌐 Nguồn: `{scraped_article.domain}`")
            if scraped_article.authors:
                meta_parts.append(f"✍ Tác giả: {', '.join(scraped_article.authors)}")
            if scraped_article.publish_date:
                meta_parts.append(f"📅 {scraped_article.publish_date}")
            if meta_parts:
                st.caption(" | ".join(meta_parts))

            if scraped_article.summary:
                with st.expander("📄 " + _t("Article Summary"), expanded=False):
                    st.write(scraped_article.summary)

            st.write(
                f"🖼 {_t('Images Found')}: **{len(scraped_article.images)}**"
            )

            # --- DIRECT ARTICLE READING & VIDEO CREATION (1-CLICK) ---
            st.markdown("---")
            st.markdown("#### 🎬 " + _t("Read Article & Use Images"))
            st.caption(
                "Tự động tạo lời đọc trực tiếp từ bài báo (TTS), tải ảnh và cấu hình chuyển động ảnh + chuyển cảnh."
            )

            reading_mode_options = [
                ("concise", _t("Concise Summary (Shorts/TikTok)")),
                ("full", _t("Full Article Reading")),
            ]
            selected_reading_mode = st.radio(
                _t("Reading Mode"),
                options=[opt[0] for opt in reading_mode_options],
                format_func=lambda x: dict(reading_mode_options)[x],
                horizontal=True,
                key="news_reading_mode_radio",
            )

            current_reading_script = article_scraper.build_article_reading_script(
                scraped_article,
                max_words=220 if selected_reading_mode == "concise" else 2000,
                mode=selected_reading_mode,
            )

            with st.expander("📝 " + _t("Article Reading Script"), expanded=True):
                edited_reading_script = st.text_area(
                    _t("Article Reading Script"),
                    value=current_reading_script,
                    height=130,
                    key="news_reading_script_editor",
                    label_visibility="collapsed",
                )

            # Settings for Article Video Motion & Transitions
            with st.expander("🎞 " + _t("Article Video Motion & Transition Settings"), expanded=True):
                art_motion_col1, art_motion_col2, art_motion_col3 = st.columns(3)
                with art_motion_col1:
                    art_transition_options = [
                        ("🎲 " + tr("Shuffle"), VideoTransitionMode.shuffle.value),
                        ("⬅ " + _t("Slide In Left"), VideoTransitionMode.slide_in_left.value),
                        ("➡ " + _t("Slide In Right"), VideoTransitionMode.slide_in_right.value),
                        ("🔄 " + _t("Pan Left (Quét sang trái)"), VideoTransitionMode.pan_left.value),
                        ("🔄 " + _t("Pan Right (Quét sang phải)"), VideoTransitionMode.pan_right.value),
                        ("🔍 " + tr("ZoomIn"), VideoTransitionMode.zoom_in.value),
                        ("🔍 " + tr("ZoomOut"), VideoTransitionMode.zoom_out.value),
                        ("🌫 " + tr("FadeIn"), VideoTransitionMode.fade_in.value),
                        ("🌫 " + tr("FadeOut"), VideoTransitionMode.fade_out.value),
                        (tr("None"), VideoTransitionMode.none.value),
                    ]
                    selected_art_transition = stable_selectbox(
                        _t("Transition Effect"),
                        options=[v for _, v in art_transition_options],
                        default_value=_saved_ui_choice(
                            "news_article_transition",
                            [v for _, v in art_transition_options],
                            VideoTransitionMode.shuffle.value,
                        ),
                        key="news_article_transition_select",
                        format_func=lambda val: dict((v, lbl) for lbl, v in art_transition_options).get(val, str(val)),
                    )

                with art_motion_col2:
                    art_motion_options = [
                        ("🎲 " + _t("Random Pan & Zoom (Dynamic)"), "random"),
                        ("⬅ " + _t("Slow Pan Left (Quét sang trái chậm)"), "pan_left"),
                        ("➡ " + _t("Slow Pan Right (Quét sang phải chậm)"), "pan_right"),
                        ("↔ " + _t("Slow Pan Left + Zoom In"), "pan_left_zoom"),
                        ("↔ " + _t("Slow Pan Right + Zoom In"), "pan_right_zoom"),
                        ("🔍 " + _t("Slow Zoom In (Phóng to chậm)"), "zoom_in"),
                        ("🔍 " + _t("Slow Zoom Out (Thu nhỏ chậm)"), "zoom_out"),
                    ]
                    selected_art_motion = stable_selectbox(
                        _t("Image Pan / Motion"),
                        options=[v for _, v in art_motion_options],
                        default_value=_saved_ui_choice(
                            "news_article_motion",
                            [v for _, v in art_motion_options],
                            "random",
                        ),
                        key="news_article_motion_select",
                        format_func=lambda val: dict((v, lbl) for lbl, v in art_motion_options).get(val, str(val)),
                    )

                with art_motion_col3:
                    selected_art_duration = st.slider(
                        _t("Duration per Image (s)"),
                        min_value=3,
                        max_value=10,
                        value=int(config.ui.get("video_clip_duration", 4) or 4),
                        step=1,
                        key="news_article_clip_duration_slider",
                    )

            if st.button(
                "🚀 " + _t("Read Article & Use Images"),
                type="primary",
                use_container_width=True,
                key="btn_apply_read_aloud_news_video",
            ):
                with st.spinner(_t("Downloading Images")):
                    st.session_state["video_subject"] = scraped_article.title
                    clean_script = edited_reading_script.strip()
                    st.session_state["video_script"] = clean_script
                    params.video_subject = scraped_article.title
                    params.video_script = clean_script
                    src_name = article_scraper.get_news_source_name(scraped_article.url)
                    if src_name:
                        src_badge = f"Nguồn: {src_name}"
                        st.session_state["source_badge_text"] = src_badge
                        st.session_state["source_badge_text_input"] = src_badge
                        st.session_state["preview_source_badge_text_input"] = src_badge
                        st.session_state["source_badge_enabled"] = True
                        st.session_state["source_badge_enabled_checkbox"] = True
                        st.session_state["preview_source_badge_enabled_toggle"] = True
                        params.source_badge_text = src_badge
                        params.source_badge_enabled = True

                    local_dir = utils.storage_dir("local_videos", create=True)
                    downloaded = article_scraper.download_article_images(
                        scraped_article.images,
                        output_dir=local_dir,
                        max_images=15,
                    )

                    if downloaded:
                        persisted = [
                            {"provider": "local", "url": path, "duration": 0}
                            for path in downloaded
                        ]
                        st.session_state["local_video_materials"] = persisted
                        _set_runtime_config("app", "video_source", "local")
                        _set_runtime_config("ui", "video_source", "local")

                    _set_runtime_config(
                        "ui",
                        "video_transition_mode",
                        selected_art_transition,
                    )
                    st.session_state["video_transition_mode_select"] = (
                        selected_art_transition
                    )
                    params.video_transition_mode = VideoTransitionMode(selected_art_transition)

                    _set_runtime_config(
                        "ui",
                        "image_motion_mode",
                        selected_art_motion,
                    )
                    st.session_state["image_motion_mode_select"] = (
                        selected_art_motion
                    )
                    params.image_motion_mode = selected_art_motion

                    _set_runtime_config(
                        "ui",
                        "video_clip_duration",
                        selected_art_duration,
                    )
                    st.session_state["video_clip_duration"] = selected_art_duration
                    params.video_clip_duration = selected_art_duration

                    _set_runtime_config(
                        "ui",
                        "video_concat_mode",
                        VideoConcatMode.sequential.value,
                    )
                    st.session_state["video_concat_mode_select"] = (
                        VideoConcatMode.sequential.value
                    )

                    st.toast(
                        _t("Auto-configured Video from Article").format(
                            count=len(downloaded)
                        ),
                        icon="🎉",
                    )
                    st.rerun(scope="app")

            st.markdown("---")
            st.markdown("##### ⚙ " + tr("Advanced Script Settings"))
            action_col1, action_col2 = st.columns(2)
            if action_col1.button(
                "⚡ " + _t("Apply to Video Subject & Prompt"),
                use_container_width=True,
                type="secondary",
                key="btn_apply_scraped_to_subject",
            ):
                st.session_state["video_subject"] = scraped_article.title
                summary_text = (
                    f"Tóm tắt: {scraped_article.summary}\n\n"
                    if scraped_article.summary
                    else ""
                )
                st.session_state["video_script_prompt"] = (
                    f"{summary_text}Nội dung chính:\n{scraped_article.content[:800]}..."
                )
                params.video_subject = scraped_article.title
                params.video_script_prompt = st.session_state[
                    "video_script_prompt"
                ]
                st.toast("Đã áp dụng chủ đề và yêu cầu kịch bản!", icon="✨")
                st.rerun(scope="app")

            if action_col2.button(
                "📥 " + _t("Download Images as Materials"),
                use_container_width=True,
                type="secondary",
                key="btn_download_article_images",
                disabled=len(scraped_article.images) == 0,
            ):
                with st.spinner(_t("Downloading Images")):
                    local_dir = utils.storage_dir("local_videos", create=True)
                    downloaded = article_scraper.download_article_images(
                        scraped_article.images,
                        output_dir=local_dir,
                        max_images=10,
                    )
                    if downloaded:
                        persisted = [
                            {"provider": "local", "url": path, "duration": 0}
                            for path in downloaded
                        ]
                        st.session_state["local_video_materials"] = persisted
                        _set_runtime_config("app", "video_source", "local")
                        _set_runtime_config("ui", "video_source", "local")
                        st.toast(
                            _t("Images Downloaded Successfully").format(
                                count=len(downloaded)
                            ),
                            icon="🎉",
                        )
                        st.rerun(scope="app")
                    else:
                        st.warning("Không thể tải hình ảnh nào từ bài báo.")

            # Gemini Prompt Section
            st.markdown("#### 🤖 " + _t("Gemini Prompt for News Video"))
            st.caption(_t("Copy or Send to Gemini"))
            gemini_prompt = st.session_state.get(
                "scraped_article_gemini_prompt"
            ) or article_scraper.generate_gemini_news_prompt(
                scraped_article,
                target_duration=60,
                language=params.video_language or "vi",
            )
            st.code(gemini_prompt, language="markdown")

            # Direct generation with Gemini if available
            app_llm_provider = config.app.get("llm_provider", "")
            has_gemini_key = bool(config.app.get("gemini_api_key"))
            if has_gemini_key or app_llm_provider == "gemini":
                if st.button(
                    "🚀 " + _t("Generate with Gemini Now"),
                    type="primary",
                    use_container_width=True,
                    key="btn_generate_script_gemini_direct",
                ):
                    with st.spinner(_t("Generating Script with Gemini")):
                        try:
                            res = _run_llm_read_operation(
                                "gemini_news_script",
                                lambda app_cfg: llm._generate_response(
                                    gemini_prompt,
                                    app_config=dict(app_cfg or {}, llm_provider="gemini"),
                                ),
                            )
                            st.session_state["video_subject"] = scraped_article.title
                            st.session_state["video_script"] = res
                            params.video_subject = scraped_article.title
                            params.video_script = res
                            st.toast(
                                _t("Gemini Generated Script Successfully"),
                                icon="🎯",
                            )
                            st.rerun(scope="app")
                        except Exception as exc:
                            logger.error(f"Gemini generation failed: {exc}")
                            st.error(f"Lỗi khi gọi Gemini: {exc}")


def _render_script_settings(panel, params):
    """Render copy settings and update generation parameters."""
    with panel:
        with st.container(border=True):
            st.write(tr("Video Script Settings"))
            _render_news_article_scraper(params)
            # The label row needs to accommodate the "Configure Large Model" entry, so text_area cannot be used anymore
            # Built-in tags. After collecting the label and input box into the same field container, the internal spacing can be covered.
            # Also keep the field in a consistent external rhythm with other form controls on the page.
            with st.container(key="video_subject_field"):
                with st.container(
                    key="video_subject_label_row",
                    horizontal=True,
                    vertical_alignment="center",
                    gap="small",
                ):
                    st.markdown(
                        tr("Video Subject"),
                        help=tr("Video Subject Help"),
                        width="content",
                    )
                    st.button(
                        tr("Configure LLM"),
                        key="open_llm_settings_from_subject",
                        type="tertiary",
                        on_click=_open_settings_dialog,
                        args=("llm",),
                    )
                params.video_subject = st.text_area(
                    tr("Video Subject"),
                    placeholder=tr("Video Subject Placeholder"),
                    height=96,
                    key="video_subject",
                    label_visibility="collapsed",
                ).strip()

            video_languages = [
                (tr("Auto Detect"), ""),
            ]
            for code in support_locales:
                video_languages.append((code, code))

            selected_language_code = stable_selectbox(
                tr("Script Language"),
                options=[value for _, value in video_languages],
                default_value=_saved_ui_choice(
                    "video_language",
                    [value for _, value in video_languages],
                    "",
                ),
                key="script_language_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_languages
                )[value],
            )
            params.video_language = selected_language_code
            _set_runtime_config("ui", "video_language", params.video_language)

            # Use the local container with key to limit the folding entry style and maintain the native interaction of the expander.
            # At the same time, avoid styles accidentally damaging other folding areas such as "Basic Settings" at the top of the page.
            with st.container(key="advanced_settings_script"):
                with st.expander(tr("Advanced Script Settings"), expanded=False):
                    script_backend_options = ["local", "loomloom"]
                    script_backend_labels = {
                        "local": tr("Local LLM Script Generation"),
                        "loomloom": tr("Shengsuan Cloud Batch Script Generation"),
                    }
                    script_backend_widget_key = localized_widget_key(
                        "script_generation_backend_select"
                    )
                    current_script_backend = st.session_state.get(
                        script_backend_widget_key,
                        _effective_script_generation_backend(),
                    )
                    script_generation_backend = stable_selectbox(
                        tr("Script Generation Method"),
                        options=script_backend_options,
                        default_value=_effective_script_generation_backend(),
                        key="script_generation_backend_select",
                        format_func=lambda value: script_backend_labels[value],
                        help=_script_generation_method_help(current_script_backend),
                    )
                    _set_runtime_config(
                        "app", "script_generation_backend", script_generation_backend
                    )

                    params.paragraph_number = st.slider(
                        tr("Script Paragraph Number"),
                        min_value=llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
                        max_value=llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
                        key="paragraph_number_input",
                    )
                    _set_runtime_config(
                        "ui", "paragraph_number", params.paragraph_number
                    )
                    params.video_script_prompt = st.text_area(
                        tr("Custom Script Requirements"),
                        height=100,
                        max_chars=llm.MAX_SCRIPT_PROMPT_LENGTH,
                        placeholder=tr("Custom Script Requirements Placeholder"),
                        key="video_script_prompt",
                    ).strip()
                    _set_runtime_config(
                        "ui", "video_script_prompt", params.video_script_prompt
                    )

                    system_prompt = st.text_area(
                        tr("Custom System Prompt"),
                        height=240,
                        max_chars=llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
                        key="custom_system_prompt",
                    ).strip()
                    # The default content is maintained uniformly by the service layer. Although the interface directly displays the default prompt words, it only
                    # Only the actual modifications made by the user are transferred with the task to avoid the old version of default rules being solidified in historical tasks.
                    params.custom_system_prompt = (
                        ""
                        if system_prompt == llm.DEFAULT_SCRIPT_SYSTEM_PROMPT.strip()
                        else system_prompt
                    )
                    _set_runtime_config(
                        "ui", "custom_system_prompt", params.custom_system_prompt
                    )

                    restore_prompt_col, preview_prompt_col = st.columns(2)
                    if restore_prompt_col.button(
                        tr("Restore Default System Prompt"),
                        key="restore_default_system_prompt",
                        icon=":material/restart_alt:",
                        on_click=reset_script_system_prompt,
                        use_container_width=True,
                    ):
                        st.toast(tr("Default System Prompt Restored"))
                    if preview_prompt_col.button(
                        tr("Preview Final Prompt"),
                        key="preview_final_script_prompt",
                        icon=":material/preview:",
                        use_container_width=True,
                    ):
                        render_script_prompt_preview(
                            llm.build_script_prompt(
                                video_subject=params.video_subject,
                                language=params.video_language,
                                paragraph_number=params.paragraph_number,
                                video_script_prompt=params.video_script_prompt,
                                custom_system_prompt=params.custom_system_prompt,
                            )
                        )

            # Model discovery only enhances the video material and does not change the copy provider explicitly selected by the user.
            if _effective_script_generation_backend() == "loomloom":
                _render_loomloom_script_generation(params)
            else:
                _render_local_script_generation(params)
            params.video_script = st.text_area(
                tr("Video Script"),
                help=tr("Video Script Help"),
                height=180,
                key="video_script",
            )
            if _effective_script_generation_backend() == "loomloom":
                st.caption(tr("LoomLoom Video Terms Reuse Help"))
            elif st.button(
                tr("Generate Video Keywords"),
                key="auto_generate_terms",
                use_container_width=True,
                type="secondary",
                icon=":material/auto_awesome:",
            ):
                if not params.video_script:
                    # Video keywords need to be extracted based on the copy. If the copy is empty, you will be prompted in advance and the model call will be skipped.
                    st.toast(tr("Please Enter the Video Subject"))
                    st.warning(tr("Please Enter the Video Subject"))
                else:
                    with st.spinner(tr("Generating Video Keywords")):
                        terms = _run_llm_read_operation(
                            "generate_terms",
                            lambda app_config_snapshot: llm.generate_terms(
                                params.video_subject,
                                params.video_script,
                                amount=8 if params.match_materials_to_script else 5,
                                match_script_order=params.match_materials_to_script,
                                app_config=app_config_snapshot,
                            ),
                        )
                        if "Error: " in terms:
                            st.error(tr(terms))
                        else:
                            st.session_state["video_terms"] = ", ".join(terms)

            params.video_terms = st.text_area(
                tr("Video Keywords"),
                help=tr("Video Keywords Help"),
                key="video_terms",
            )


def _sync_overlay_session_state(params):
    """Ensure all overlay states (Headline, Source badge, Logo, Frame) are initialized and synchronized."""
    # 1. Headline banner
    st.session_state.setdefault("headline_enabled", getattr(params, "headline_enabled", True))
    if not st.session_state.get("headline_text"):
        st.session_state["headline_text"] = getattr(params, "headline_text", "") or params.video_subject or ""
    st.session_state.setdefault("headline_x", float(getattr(params, "headline_x", 50.0)))
    st.session_state.setdefault("headline_y", float(getattr(params, "headline_y", 8.0)))
    st.session_state.setdefault("headline_duration", int(getattr(params, "headline_duration", 0)))

    # 2. Source badge
    st.session_state.setdefault("source_badge_enabled", getattr(params, "source_badge_enabled", True))
    if not st.session_state.get("source_badge_text"):
        st.session_state["source_badge_text"] = getattr(params, "source_badge_text", "") or "Nguồn: VnExpress"
    st.session_state.setdefault("source_badge_x", float(getattr(params, "source_badge_x", 75.0)))
    st.session_state.setdefault("source_badge_y", float(getattr(params, "source_badge_y", 12.0)))
    st.session_state.setdefault("source_badge_duration", int(getattr(params, "source_badge_duration", 0)))

    # 3. Brand Logo / Photo
    st.session_state.setdefault("logo_enabled", getattr(params, "logo_enabled", False))
    st.session_state.setdefault("logo_file", getattr(params, "logo_file", ""))
    st.session_state.setdefault("logo_size", int(getattr(params, "logo_size", 140)))
    st.session_state.setdefault("logo_x", float(getattr(params, "logo_x", 8.0)))
    st.session_state.setdefault("logo_y", float(getattr(params, "logo_y", 6.0)))
    st.session_state.setdefault("logo_duration", int(getattr(params, "logo_duration", 0)))

    # 4. Custom Frame
    st.session_state.setdefault("frame_enabled", getattr(params, "frame_enabled", True))
    st.session_state.setdefault("frame_template_id", getattr(params, "frame_template_id", "none") if hasattr(params, "frame_template_id") else "none")
    st.session_state.setdefault("frame_x", float(getattr(params, "frame_x", 0.0)))
    st.session_state.setdefault("frame_y", float(getattr(params, "frame_y", 0.0)))
    st.session_state.setdefault("frame_duration", int(getattr(params, "frame_duration", 0)))

    # Sync onto params object
    params.headline_enabled = bool(st.session_state["headline_enabled"])
    params.headline_text = str(st.session_state["headline_text"] or "")
    params.headline_x = float(st.session_state.get("headline_x", 50.0))
    params.headline_y = float(st.session_state.get("headline_y", 8.0))
    params.headline_duration = int(st.session_state.get("headline_duration", 0))

    params.source_badge_enabled = bool(st.session_state["source_badge_enabled"])
    params.source_badge_text = str(st.session_state["source_badge_text"] or "")
    params.source_badge_x = float(st.session_state.get("source_badge_x", 75.0))
    params.source_badge_y = float(st.session_state.get("source_badge_y", 12.0))
    params.source_badge_duration = int(st.session_state.get("source_badge_duration", 0))

    params.logo_enabled = bool(st.session_state["logo_enabled"])
    params.logo_file = str(st.session_state["logo_file"] or "")
    params.logo_size = int(st.session_state.get("logo_size", 140))
    params.logo_x = float(st.session_state.get("logo_x", 8.0))
    params.logo_y = float(st.session_state.get("logo_y", 6.0))
    params.logo_duration = int(st.session_state.get("logo_duration", 0))

    params.frame_enabled = bool(st.session_state.get("frame_enabled", True))
    params.frame_x = float(st.session_state.get("frame_x", 0.0))
    params.frame_y = float(st.session_state.get("frame_y", 0.0))
    params.frame_duration = int(st.session_state.get("frame_duration", 0))


DURATION_CHOICE_TUPLES = [
    (0, "♾️ Vĩnh viễn (Suốt video)"),
    (3, "⏱️ 3 giây"),
    (5, "⏱️ 5 giây"),
    (10, "⏱️ 10 giây"),
    (15, "⏱️ 15 giây"),
    (30, "⏱️ 30 giây"),
    (60, "⏱️ 1 phút"),
    (120, "⏱️ 2 phút"),
]


def _render_frame_and_source_settings(params):
    """Render frame overlay template selector, brand logo, headline banner, and news source badge."""
    _sync_overlay_session_state(params)

    with st.expander("🖼 " + _i18n("Giao diện, Tiêu đề, Logo & Nguồn", "Overlay, Headline, Logo & Source Settings"), expanded=True):
        st.info("💡 " + _i18n(
            "Tất cả các thành phần (Tiêu đề, Nhãn nguồn, Logo, Khung) có thể kéo thả trực tiếp trên khung Video Preview bên dưới, hoặc tinh chỉnh tọa độ và thời hạn hiển thị riêng biệt tại đây.",
            "All overlay elements (Headline, Source, Logo, Frame) can be dragged directly on the Live Video Preview below, or fine-tuned with coordinates and individual display durations here."
        ))

        # 1. Headline banner settings
        st.markdown(f"**📢 {_i18n('Tiêu đề video (Headline)', 'Video Headline Banner')}**")
        hl_en = st.checkbox(
            _i18n("Hiển thị Tiêu đề video", "Show Video Headline"),
            value=st.session_state["headline_enabled"],
            key="settings_headline_enabled_checkbox",
        )
        st.session_state["headline_enabled"] = hl_en
        params.headline_enabled = hl_en

        if hl_en:
            c_hl_t, c_hl_d = st.columns([2, 1.2])
            with c_hl_t:
                hl_val = st.text_input(
                    _i18n("Nội dung tiêu đề", "Headline Text"),
                    value=st.session_state["headline_text"],
                    placeholder="VD: Thủ tướng yêu cầu...",
                    key="settings_headline_text_input",
                )
                st.session_state["headline_text"] = hl_val
                params.headline_text = hl_val

            with c_hl_d:
                cur_hl_d = int(st.session_state.get("headline_duration", 0))
                idx_hl_d = next((i for i, (d, _) in enumerate(DURATION_CHOICE_TUPLES) if d == cur_hl_d), 0)
                sel_hl_d = st.selectbox(
                    _i18n("Thời gian hiển thị", "Display Duration"),
                    options=[d[0] for d in DURATION_CHOICE_TUPLES],
                    format_func=lambda x: dict(DURATION_CHOICE_TUPLES).get(x, f"{x}s"),
                    index=idx_hl_d,
                    key="settings_headline_duration_select",
                )
                st.session_state["headline_duration"] = sel_hl_d
                params.headline_duration = sel_hl_d

            c_hl_x, c_hl_y, c_hl_rst = st.columns([1.2, 1.2, 0.8])
            with c_hl_x:
                hl_x_val = st.slider(
                    _i18n("Tọa độ X (%)", "Position X (%)"),
                    min_value=0.0,
                    max_value=100.0,
                    value=float(st.session_state.get("headline_x", 50.0)),
                    step=0.5,
                    key="settings_headline_x_slider",
                )
                st.session_state["headline_x"] = hl_x_val
                params.headline_x = hl_x_val
            with c_hl_y:
                hl_y_val = st.slider(
                    _i18n("Tọa độ Y (%)", "Position Y (%)"),
                    min_value=0.0,
                    max_value=100.0,
                    value=float(st.session_state.get("headline_y", 8.0)),
                    step=0.5,
                    key="settings_headline_y_slider",
                )
                st.session_state["headline_y"] = hl_y_val
                params.headline_y = hl_y_val
            with c_hl_rst:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("↺ " + _i18n("Mặc định (50%, 8%)", "Default"), key="btn_rst_hl_settings", use_container_width=True):
                    st.session_state["headline_x"] = 50.0
                    st.session_state["headline_y"] = 8.0
                    params.headline_x = 50.0
                    params.headline_y = 8.0
                    st.rerun(scope="app")

        st.markdown("---")

        # 2. News Source Badge
        st.markdown(f"**📌 {_i18n('Nhãn nguồn tin tức', 'News Source Badge')}**")
        src_en = st.checkbox(
            _t("News Source Badge"),
            value=st.session_state["source_badge_enabled"],
            key="source_badge_enabled_checkbox",
        )
        st.session_state["source_badge_enabled"] = src_en
        params.source_badge_enabled = src_en

        if src_en:
            col_text, col_dur = st.columns([2, 1.2])
            with col_text:
                entered_src = st.text_input(
                    _t("Source Text"),
                    value=st.session_state["source_badge_text"],
                    placeholder="VD: Nguồn: VnExpress",
                    key="source_badge_text_input",
                )
                st.session_state["source_badge_text"] = (entered_src or "").strip()
                params.source_badge_text = st.session_state["source_badge_text"]

            with col_dur:
                cur_sb_d = int(st.session_state.get("source_badge_duration", 0))
                idx_sb_d = next((i for i, (d, _) in enumerate(DURATION_CHOICE_TUPLES) if d == cur_sb_d), 0)
                sel_sb_d = st.selectbox(
                    _i18n("Thời gian hiển thị", "Display Duration"),
                    options=[d[0] for d in DURATION_CHOICE_TUPLES],
                    format_func=lambda x: dict(DURATION_CHOICE_TUPLES).get(x, f"{x}s"),
                    index=idx_sb_d,
                    key="settings_source_badge_duration_select",
                )
                st.session_state["source_badge_duration"] = sel_sb_d
                params.source_badge_duration = sel_sb_d

            c_src_x, c_src_y, c_src_rst = st.columns([1.2, 1.2, 0.8])
            with c_src_x:
                sb_x_val = st.slider(
                    _i18n("Tọa độ X (%)", "Position X (%)"),
                    min_value=0.0,
                    max_value=100.0,
                    value=float(st.session_state.get("source_badge_x", 75.0)),
                    step=0.5,
                    key="settings_source_x_slider",
                )
                st.session_state["source_badge_x"] = sb_x_val
                params.source_badge_x = sb_x_val
            with c_src_y:
                sb_y_val = st.slider(
                    _i18n("Tọa độ Y (%)", "Position Y (%)"),
                    min_value=0.0,
                    max_value=100.0,
                    value=float(st.session_state.get("source_badge_y", 12.0)),
                    step=0.5,
                    key="settings_source_y_slider",
                )
                st.session_state["source_badge_y"] = sb_y_val
                params.source_badge_y = sb_y_val
            with c_src_rst:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("↺ " + _i18n("Mặc định (75%, 12%)", "Default"), key="btn_rst_sb_settings", use_container_width=True):
                    st.session_state["source_badge_x"] = 75.0
                    st.session_state["source_badge_y"] = 12.0
                    params.source_badge_x = 75.0
                    params.source_badge_y = 12.0
                    st.rerun(scope="app")

        st.markdown("---")

        # 3. Brand Logo / Custom Photo
        st.markdown(f"**🛡️ {_i18n('Logo / Ảnh thương hiệu', 'Brand Logo / Custom Photo')}**")
        logo_en = st.checkbox(
            _i18n("Chèn Logo / Ảnh lên Video", "Overlay Brand Logo / Photo onto Video"),
            value=st.session_state["logo_enabled"],
            key="settings_logo_enabled_checkbox",
        )
        st.session_state["logo_enabled"] = logo_en
        params.logo_enabled = logo_en

        if logo_en:
            uploaded_logo_file = st.file_uploader(
                _i18n("Tải ảnh Logo / Sticker (PNG trong suốt, JPG, WebP)", "Upload Logo / Sticker Image (Transparent PNG, JPG, WebP)"),
                type=["png", "jpg", "jpeg", "webp"],
                key="settings_logo_uploader",
            )
            if uploaded_logo_file is not None:
                saved_logo = video_template.save_uploaded_logo(uploaded_logo_file.getvalue(), uploaded_logo_file.name)
                st.session_state["logo_file"] = saved_logo
                params.logo_file = saved_logo
                st.toast(_i18n("Đã tải logo thành công!", "Logo uploaded successfully!"), icon="🛡️")

            c_ls, c_ld = st.columns([1.2, 1.2])
            with c_ls:
                sel_ls = st.slider(
                    _i18n("Kích thước logo (px)", "Logo Size (px)"),
                    min_value=40,
                    max_value=240,
                    value=int(st.session_state.get("logo_size", 140)),
                    step=10,
                    key="settings_logo_size_slider",
                )
                st.session_state["logo_size"] = sel_ls
                params.logo_size = sel_ls

            with c_ld:
                cur_ld = int(st.session_state.get("logo_duration", 0))
                idx_ld = next((i for i, (d, _) in enumerate(DURATION_CHOICE_TUPLES) if d == cur_ld), 0)
                sel_ld = st.selectbox(
                    _i18n("Thời gian hiển thị", "Display Duration"),
                    options=[d[0] for d in DURATION_CHOICE_TUPLES],
                    format_func=lambda x: dict(DURATION_CHOICE_TUPLES).get(x, f"{x}s"),
                    index=idx_ld,
                    key="settings_logo_duration_select",
                )
                st.session_state["logo_duration"] = sel_ld
                params.logo_duration = sel_ld

            c_lg_x, c_lg_y, c_lg_rst = st.columns([1.2, 1.2, 0.8])
            with c_lg_x:
                lg_x_val = st.slider(
                    _i18n("Tọa độ X (%)", "Position X (%)"),
                    min_value=0.0,
                    max_value=100.0,
                    value=float(st.session_state.get("logo_x", 8.0)),
                    step=0.5,
                    key="settings_logo_x_slider",
                )
                st.session_state["logo_x"] = lg_x_val
                params.logo_x = lg_x_val
            with c_lg_y:
                lg_y_val = st.slider(
                    _i18n("Tọa độ Y (%)", "Position Y (%)"),
                    min_value=0.0,
                    max_value=100.0,
                    value=float(st.session_state.get("logo_y", 6.0)),
                    step=0.5,
                    key="settings_logo_y_slider",
                )
                st.session_state["logo_y"] = lg_y_val
                params.logo_y = lg_y_val
            with c_lg_rst:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("↺ " + _i18n("Mặc định (8%, 6%)", "Default"), key="btn_rst_lg_settings", use_container_width=True):
                    st.session_state["logo_x"] = 8.0
                    st.session_state["logo_y"] = 6.0
                    params.logo_x = 8.0
                    params.logo_y = 6.0
                    st.rerun(scope="app")

            if st.session_state.get("logo_file") and os.path.exists(st.session_state["logo_file"]):
                col_prev_logo, col_del_logo = st.columns([2, 1])
                with col_prev_logo:
                    st.caption("🖼 " + _i18n("Đang dùng logo:", "Current logo:") + f" `{os.path.basename(st.session_state['logo_file'])}`")
                with col_del_logo:
                    if st.button("❌ " + _i18n("Gỡ logo", "Remove Logo"), key="btn_remove_logo_settings", type="tertiary"):
                        st.session_state["logo_file"] = ""
                        st.session_state["logo_enabled"] = False
                        st.rerun(scope="app")

        st.markdown("---")

        # 4. Frame Overlay Template & Custom Frame
        st.markdown(f"**🖼️ {_i18n('Khung viền / Khung trang trí', 'Frame Overlay / Decorative Frame')}**")
        fr_en = st.checkbox(
            _i18n("Hiển thị Khung viền", "Show Frame Overlay"),
            value=st.session_state.get("frame_enabled", True),
            key="settings_frame_enabled_checkbox",
        )
        st.session_state["frame_enabled"] = fr_en
        params.frame_enabled = fr_en

        if fr_en:
            aspect_str = getattr(params.video_aspect, "value", str(params.video_aspect or "9:16"))
            available_templates = video_template.get_available_templates(aspect=aspect_str)
            template_choices = [t["id"] for t in available_templates]
            template_labels = {t["id"]: t["name"] for t in available_templates}
            template_paths = {t["id"]: t["path"] for t in available_templates}

            saved_tmpl_id = st.session_state.get("frame_template_id", "none")
            choices = template_choices if template_choices else ["none"]
            tmpl_idx = choices.index(saved_tmpl_id) if saved_tmpl_id in choices else 0

            c_fr_tmpl, c_fr_dur = st.columns([1.5, 1.2])
            with c_fr_tmpl:
                selected_tmpl_id = st.selectbox(
                    _t("Frame Template"),
                    options=choices,
                    format_func=lambda x: str(template_labels.get(x, x)),
                    index=tmpl_idx,
                    help=_t("Frame Template Help"),
                    key="frame_template_select",
                )
                st.session_state["frame_template_id"] = selected_tmpl_id
                params.frame_template = template_paths.get(selected_tmpl_id, "")

            with c_fr_dur:
                cur_fr_d = int(st.session_state.get("frame_duration", 0))
                idx_fr_d = next((i for i, (d, _) in enumerate(DURATION_CHOICE_TUPLES) if d == cur_fr_d), 0)
                sel_fr_d = st.selectbox(
                    _i18n("Thời gian hiển thị", "Display Duration"),
                    options=[d[0] for d in DURATION_CHOICE_TUPLES],
                    format_func=lambda x: dict(DURATION_CHOICE_TUPLES).get(x, f"{x}s"),
                    index=idx_fr_d,
                    key="settings_frame_duration_select",
                )
                st.session_state["frame_duration"] = sel_fr_d
                params.frame_duration = sel_fr_d

            # Upload Custom Frame Template
            uploaded_template = st.file_uploader(
                _i18n("Tải khung viền / khung ảnh tùy chỉnh (PNG trong suốt)", "Upload Custom Frame (Transparent PNG)"),
                type=["png", "webp"],
                help=_t("Upload Custom Frame Help"),
                key="custom_frame_template_uploader",
            )
            if uploaded_template is not None:
                saved_path = video_template.save_uploaded_template(
                    uploaded_template.getvalue(), uploaded_template.name
                )
                st.session_state["frame_template_id"] = os.path.basename(saved_path)
                params.frame_template = saved_path
                st.toast(_t("Template Uploaded Successfully"), icon="🎨")
                st.rerun(scope="app")

            c_fr_x, c_fr_y, c_fr_rst = st.columns([1.2, 1.2, 0.8])
            with c_fr_x:
                fr_x_val = st.slider(
                    _i18n("Tọa độ Khung X (%)", "Frame Position X (%)"),
                    min_value=0.0,
                    max_value=100.0,
                    value=float(st.session_state.get("frame_x", 0.0)),
                    step=0.5,
                    key="settings_frame_x_slider",
                )
                st.session_state["frame_x"] = fr_x_val
                params.frame_x = fr_x_val
            with c_fr_y:
                fr_y_val = st.slider(
                    _i18n("Tọa độ Khung Y (%)", "Frame Position Y (%)"),
                    min_value=0.0,
                    max_value=100.0,
                    value=float(st.session_state.get("frame_y", 0.0)),
                    step=0.5,
                    key="settings_frame_y_slider",
                )
                st.session_state["frame_y"] = fr_y_val
                params.frame_y = fr_y_val
            with c_fr_rst:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("🔲 " + _i18n("Toàn màn hình (0,0)", "Full Screen"), key="btn_rst_fr_settings", use_container_width=True):
                    st.session_state["frame_x"] = 0.0
                    st.session_state["frame_y"] = 0.0
                    params.frame_x = 0.0
                    params.frame_y = 0.0
                    st.rerun(scope="app")

            # Download Template Buttons
            st.caption("📥 **Tải mẫu về máy để chỉnh sửa (Photoshop / Canva / Figma):**")
            templates_dir = video_template.get_templates_dir()
            is_portrait = "9:16" in aspect_str or "portrait" in str(aspect_str).lower()
            caro_file = (
                "template_9_16_checkerboard.png"
                if is_portrait
                else "template_16_9_checkerboard.png"
            )
            trans_file = (
                "template_9_16_transparent.png"
                if is_portrait
                else "template_16_9_transparent.png"
            )

            col_dl1, col_dl2 = st.columns(2)
            caro_path = os.path.join(templates_dir, caro_file)
            if os.path.exists(caro_path):
                with open(caro_path, "rb") as f:
                    col_dl1.download_button(
                        _t("Download Checkerboard Template"),
                        data=f.read(),
                        file_name=caro_file,
                        mime="image/png",
                        use_container_width=True,
                        key="btn_download_caro_template",
                    )

            trans_path = os.path.join(templates_dir, trans_file)
            if os.path.exists(trans_path):
                with open(trans_path, "rb") as f:
                    col_dl2.download_button(
                        _t("Download Transparent Template"),
                        data=f.read(),
                        file_name=trans_file,
                        mime="image/png",
                        use_container_width=True,
                        key="btn_download_trans_template",
                    )


def _render_video_settings(panel, params):
    """Render video settings and return the local material selected this time."""
    uploaded_files = []
    with panel:
        with st.container(border=True):

            st.write(tr("Video Settings"))
            video_concat_modes = [
                (tr("Sequential"), "sequential"),
                (tr("Random"), "random"),
            ]
            video_source_labels = {
                "pexels": tr("Pexels"),
                "pixabay": tr("Pixabay"),
                "coverr": tr("Coverr"),
                "wavespeed": tr("WaveSpeed AI Video"),
                "volcengine_seedance": tr("Volcano Engine Seedance"),
                "ofox": tr("OFox AI Video"),
                "metaso_minimax": tr("Metaso MiniMax H3"),
                "muapi": tr("MuAPI AI Video"),
                "loomloom": tr("Shengsuan Cloud AI Video"),
                "openai_image": tr("OpenAI Compatible Text-to-Image"),
                "local": tr("Local file"),
            }
            saved_video_source_name = str(
                config.app.get("video_source", "pexels") or "pexels"
            )
            params.video_source = grouped_selectbox(
                tr("Video Source"),
                groups=(
                    (tr("Stock Video"), VIDEO_SOURCE_GROUPS["stock_video"]),
                    (tr("AI Video"), VIDEO_SOURCE_GROUPS["ai_video"]),
                    (tr("AI Image"), VIDEO_SOURCE_GROUPS["ai_image"]),
                    (tr("Local Material"), VIDEO_SOURCE_GROUPS["local"]),
                ),
                default_value=saved_video_source_name,
                key="video_source_select",
                format_func=video_source_labels.get,
                settings_label=tr("Configure Material Sources"),
                on_settings=_open_material_settings_dialog,
            )
            _set_runtime_config("app", "video_source", params.video_source)

            loomloom_video_capability = None
            if params.video_source == "loomloom":
                # Read the cache as early as possible so that the lower aspect ratio control is directly constrained by the current Profile.
                # After entering the Key for the first time, Streamlit will rerun and load it here.
                loomloom_video_capability = _load_loomloom_video_capability(
                    _effective_loomloom_api_token()
                )

            if params.video_source == "wavespeed":
                st.caption(tr("WaveSpeed AI Video Help"))
            if params.video_source == "volcengine_seedance":
                st.caption(tr("Volcano Engine Seedance Help"))
            if params.video_source == "ofox":
                st.caption(f"[OfoxAI]({OFOX_REFERRAL_URL}) · {tr('OFox AI Video Help')}")
            if params.video_source == "metaso_minimax":
                st.caption(tr("Metaso MiniMax H3 Help"))
            if params.video_source == "muapi":
                st.caption(tr("MuAPI AI Video Help"))
            if params.video_source == "local":
                # Streamlit's file type verification is sensitive to the case of the extension, and both upper and lower case forms are allowed here.
                local_file_types = sorted(
                    extension.removeprefix(".")
                    for extension in LOCAL_MATERIAL_EXTENSIONS
                )
                uploaded_files = st.file_uploader(
                    tr("Upload Local Files"),
                    type=local_file_types
                    + [file_type.upper() for file_type in local_file_types],
                    accept_multiple_files=True,
                    key="local_video_materials_uploader",
                )

            # Copy sequence matching will maintain the narrative order from keyword generation to final synthesis, so when it is turned on
            # Sequential splicing is the only option that fits the actual execution logic. Synchronizing control values prevents the interface from still being displayed
            # "Random splicing", while retaining the user's original selection, and automatically restores after closing.
            sync_script_order_concat_mode()
            selected_concat_mode = stable_selectbox(
                tr("Video Concat Mode"),
                options=[value for _, value in video_concat_modes],
                default_value=_saved_ui_choice(
                    "video_concat_mode",
                    [value for _, value in video_concat_modes],
                    VideoConcatMode.random.value,
                ),
                key="video_concat_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_concat_modes
                )[value],
                disabled=bool(st.session_state.get("match_materials_to_script", False)),
            )
            params.video_concat_mode = VideoConcatMode(selected_concat_mode)

            params.match_materials_to_script = st.checkbox(
                tr("Match Materials to Script Order"),
                help=tr("Match Materials to Script Order Help"),
                key="match_materials_to_script",
                on_change=sync_script_order_concat_mode,
            )
            _set_runtime_config(
                "app",
                "match_materials_to_script",
                params.match_materials_to_script,
            )
            # When sequential matching is turned on, sequential is a derived mandatory value and should not override the user's
            # This function is the selected splicing preference; after turning it off, the previous random/sequential can still be restored.
            if not params.match_materials_to_script:
                _set_runtime_config(
                    "ui", "video_concat_mode", params.video_concat_mode.value
                )

            # Video transition mode
            video_transition_modes = [
                (tr("None"), VideoTransitionMode.none.value),
                (tr("Shuffle"), VideoTransitionMode.shuffle.value),
                (tr("FadeIn"), VideoTransitionMode.fade_in.value),
                (tr("FadeOut"), VideoTransitionMode.fade_out.value),
                (tr("SlideIn"), VideoTransitionMode.slide_in.value),
                ("⬅ " + _t("Slide In Left"), VideoTransitionMode.slide_in_left.value),
                ("➡ " + _t("Slide In Right"), VideoTransitionMode.slide_in_right.value),
                ("⬆ " + _t("Slide In Top"), VideoTransitionMode.slide_in_top.value),
                ("⬇ " + _t("Slide In Bottom"), VideoTransitionMode.slide_in_bottom.value),
                ("🔄 " + _t("Pan Left (Quét sang trái)"), VideoTransitionMode.pan_left.value),
                ("🔄 " + _t("Pan Right (Quét sang phải)"), VideoTransitionMode.pan_right.value),
                (tr("SlideOut"), VideoTransitionMode.slide_out.value),
                (tr("ZoomIn"), VideoTransitionMode.zoom_in.value),
                (tr("ZoomOut"), VideoTransitionMode.zoom_out.value),
            ]
            selected_transition_mode = stable_selectbox(
                tr("Video Transition Mode"),
                options=[value for _, value in video_transition_modes],
                default_value=_saved_ui_choice(
                    "video_transition_mode",
                    [value for _, value in video_transition_modes],
                    VideoTransitionMode.none.value,
                ),
                key="video_transition_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_transition_modes
                )[value],
            )
            params.video_transition_mode = VideoTransitionMode(selected_transition_mode)
            _set_runtime_config(
                "ui",
                "video_transition_mode",
                params.video_transition_mode.value,
            )

            # Image motion mode (Ken Burns motion for images)
            image_motion_modes = [
                ("🎲 " + _t("Random Pan & Zoom (Dynamic)"), "random"),
                ("⬅ " + _t("Slow Pan Left (Quét sang trái chậm)"), "pan_left"),
                ("➡ " + _t("Slow Pan Right (Quét sang phải chậm)"), "pan_right"),
                ("↔ " + _t("Slow Pan Left + Zoom In"), "pan_left_zoom"),
                ("↔ " + _t("Slow Pan Right + Zoom In"), "pan_right_zoom"),
                ("🔍 " + _t("Slow Zoom In (Phóng to chậm)"), "zoom_in"),
                ("🔍 " + _t("Slow Zoom Out (Thu nhỏ chậm)"), "zoom_out"),
            ]
            selected_motion_mode = stable_selectbox(
                _t("Image Motion Mode (Ken Burns)"),
                options=[value for _, value in image_motion_modes],
                default_value=_saved_ui_choice(
                    "image_motion_mode",
                    [value for _, value in image_motion_modes],
                    "random",
                ),
                key="image_motion_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in image_motion_modes
                )[value],
            )
            params.image_motion_mode = selected_motion_mode
            _set_runtime_config(
                "ui",
                "image_motion_mode",
                selected_motion_mode,
            )

            video_aspect_ratios = [
                (tr("Portrait"), VideoAspect.portrait.value),
                (tr("Landscape"), VideoAspect.landscape.value),
            ]
            if loomloom_video_capability is not None:
                ratio_labels = {value: label for label, value in video_aspect_ratios}
                video_aspect_ratios = [
                    (ratio_labels[value], value)
                    for value in loomloom_video_capability.aspect_ratios
                ]
            # 99% of the Coverr library is 16:9 horizontal screen. The default vertical screen will make the screen surrounded by a lot of black borders.
            # Use a source-specific widget key to have each source remember its aspect selection:
            # - Switch to coverr for the first time → default Landscape(index=1)
            # - Other sources follow Portrait(index=0)
            # - If the user manually changes the aspect under a certain source, the session_state will be remembered.
            # The user's choice will be respected the next time he returns to the same source and will not be forcibly overwritten again.
            default_aspect_index = 1 if params.video_source == "coverr" else 0
            video_aspect_values = [value for _, value in video_aspect_ratios]
            video_aspect_config_key = f"video_aspect_{params.video_source}"
            selected_aspect_ratio = stable_selectbox(
                tr("Video Ratio"),
                options=video_aspect_values,
                default_value=_saved_ui_choice(
                    video_aspect_config_key,
                    video_aspect_values,
                    video_aspect_ratios[default_aspect_index][1],
                ),
                key=f"video_aspect_for_{params.video_source}",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_aspect_ratios
                )[value],
            )
            params.video_aspect = VideoAspect(selected_aspect_ratio)
            _set_runtime_config(
                "ui", video_aspect_config_key, params.video_aspect.value
            )

            video_fit_modes = [
                (tr("Fill and Crop"), VideoFitMode.cover.value),
                (tr("Fit with Black Bars"), VideoFitMode.contain.value),
            ]
            selected_fit_mode = stable_selectbox(
                tr("Video Fit Mode"),
                options=[value for _, value in video_fit_modes],
                default_value=_saved_ui_choice(
                    "video_fit_mode",
                    [value for _, value in video_fit_modes],
                    VideoFitMode.cover.value,
                ),
                key="video_fit_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_fit_modes
                )[value],
                help=tr("Video Fit Mode Help"),
            )
            params.video_fit_mode = VideoFitMode(selected_fit_mode)
            _set_runtime_config(
                "ui", "video_fit_mode", params.video_fit_mode.value
            )

            # The remote duration range of MiniMax H3 is 4 to 15 seconds. Use full ability when selecting Secret Tower
            # The range not only prevents 2/3 seconds from being billed as 4 seconds, but also makes the WebUI consistent with the CLI and service layer.
            if params.video_source == "metaso_minimax":
                video_clip_durations = list(
                    range(
                        metaso_minimax.DEFAULT_MIN_DURATION_SECONDS,
                        metaso_minimax.DEFAULT_MAX_DURATION_SECONDS + 1,
                    )
                )
            elif params.video_source == "muapi":
                video_clip_durations = list(
                    range(
                        muapi.DEFAULT_MIN_DURATION_SECONDS,
                        muapi.DEFAULT_MAX_DURATION_SECONDS + 1,
                    )
                )
            else:
                video_clip_durations = [2, 3, 4, 5, 6, 7, 8, 9, 10]
            params.video_clip_duration = stable_selectbox(
                tr("Clip Duration"),
                options=video_clip_durations,
                default_value=_saved_ui_choice(
                    "video_clip_duration",
                    video_clip_durations,
                    5
                    if params.video_source in {"metaso_minimax", "muapi"}
                    else 3,
                ),
                key="video_clip_duration_select",
                help=tr("Clip Duration Help"),
            )
            _set_runtime_config(
                "ui", "video_clip_duration", params.video_clip_duration
            )
            clip_speed_key = localized_widget_key("video_clip_speed_slider")
            # session_state may come from a legacy task, API parameter, or legacy page state. Before the control is created
            # Unified normalization not only retains legal choices, but also ensures that the slider always receives 0.5~2.0
            # A finite floating point number within the range.
            st.session_state[clip_speed_key] = utils.normalize_clip_speed(
                st.session_state.get(
                    clip_speed_key,
                    _saved_ui_number("video_clip_speed", 1.0, 0.5, 2.0),
                )
            )
            params.video_clip_speed = st.slider(
                tr("Clip Speed"),
                min_value=0.5,
                max_value=2.0,
                step=0.05,
                format="%.2fx",
                key=clip_speed_key,
                help=tr("Clip Speed Help"),
            )
            _set_runtime_config("ui", "video_clip_speed", params.video_clip_speed)
            video_count_options = [1, 2, 3, 4, 5]
            params.video_count = stable_selectbox(
                tr("Number of Videos Generated Simultaneously"),
                options=video_count_options,
                default_value=_saved_ui_choice(
                    "video_count", video_count_options, 1
                ),
                key="video_count_select",
            )
            _set_runtime_config("ui", "video_count", params.video_count)

            video_codec_options = [
                (tr("Default Video Encoder"), DEFAULT_VIDEO_CODEC_OPTION),
                ("libx264 (CPU)", "libx264"),
                ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
                ("AMD AMF (h264_amf)", "h264_amf"),
                ("Intel QSV (h264_qsv)", "h264_qsv"),
                ("Windows MediaFoundation (h264_mf)", "h264_mf"),
                ("macOS VideoToolbox (h264_videotoolbox)", "h264_videotoolbox"),
            ]
            saved_video_codec = config.app.get(
                "video_codec", DEFAULT_VIDEO_CODEC_OPTION
            )
            saved_video_codec_values = [item[1] for item in video_codec_options]
            if saved_video_codec not in saved_video_codec_values:
                # Older versions or manual configuration may leave invalid values. UI returns to "default" instead of replacing the user
                # Fixed a certain encoder and the backend will still resolve to libx264 according to the stable policy.
                saved_video_codec = DEFAULT_VIDEO_CODEC_OPTION
            selected_video_codec = stable_selectbox(
                tr("Video Encoder"),
                options=saved_video_codec_values,
                default_value=saved_video_codec,
                key="video_encoder_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_codec_options
                )[value],
                help=tr("Video Encoder Help"),
            )
            if selected_video_codec == DEFAULT_VIDEO_CODEC_OPTION:
                # The default mode does not persist specific encoders, letting the configuration express "follow the project defaults".
                _delete_runtime_config("app", "video_codec")
            else:
                _set_runtime_config("app", "video_codec", selected_video_codec)

            concurrency_options = [1, 2, 4, 6, 8]
            if params.video_source in {"pexels", "pixabay", "coverr"}:
                selected_material_concurrency = stable_selectbox(
                    tr("Material Concurrency"),
                    options=concurrency_options,
                    default_value=_saved_ui_choice(
                        "material_concurrency", concurrency_options, 1, config.app
                    ),
                    key="material_concurrency_select",
                    help=tr("Material Concurrency Help"),
                )
                _set_runtime_config(
                    "app", "material_concurrency", selected_material_concurrency
                )

            selected_clip_concurrency = stable_selectbox(
                tr("Clip Rendering Concurrency"),
                options=concurrency_options,
                default_value=_saved_ui_choice(
                    "video_clip_concurrency", concurrency_options, 1, config.app
                ),
                key="clip_rendering_concurrency_select",
                help=tr("Clip Rendering Concurrency Help"),
            )
            _set_runtime_config(
                "app", "video_clip_concurrency", selected_clip_concurrency
            )

            if params.video_source == "loomloom":
                _render_loomloom_video_settings(params)

            if params.video_source == "wavespeed":
                _render_wavespeed_video_settings(params)
            if params.video_source == "volcengine_seedance":
                _render_seedance_video_settings(params)
            if params.video_source == "ofox":
                _render_ofox_video_settings(params)
            if params.video_source == "metaso_minimax":
                _render_metaso_minimax_video_settings(params)
            if params.video_source == "muapi":
                _render_muapi_video_settings(params)

            _render_frame_and_source_settings(params)
    return uploaded_files



def _render_wavespeed_video_settings(params):
    """
    Render WaveSpeed to generate quantity estimates and billing confirmations.

    When generating billing per item, the user must be able to see the approximate number of segments that will be generated before submission. Estimates are entirely local
    Completion: Divide the dubbing duration estimate interval by the clip duration to get the number of clips that need to be covered. Material flow
    It is generated piece by piece on demand and stops when the required time is reached. Therefore, the actual number of generations is subject to runtime.
    The estimation is only used for magnitude prompts and does not participate in task execution.
    """
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""),
        params.voice_rate,
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("WaveSpeed Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("WaveSpeed Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm WaveSpeed Charge"),
        key="wavespeed_confirm_charge",
        help=tr("Confirm WaveSpeed Charge Help"),
    )


def _render_seedance_video_settings(params):
    """Display the expected number of paid tasks and ask users to clearly confirm the Ark generation fee."""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("Volcano Engine Seedance Billing Notice").format(
                min=min_clips, max=max_clips
            )
        )
    else:
        st.warning(tr("Volcano Engine Seedance Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm Volcano Engine Seedance Charge"),
        key="volcengine_seedance_confirm_charge",
        help=tr("Confirm Volcano Engine Seedance Charge Help"),
    )


def _render_ofox_video_settings(params):
    """Display the expected number of paid tasks and ask users to explicitly confirm OFox generates fees."""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("OFox Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("OFox Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm OFox Charge"),
        key="ofox_confirm_charge",
        help=tr("Confirm OFox Charge Help"),
    )


def _render_metaso_minimax_video_settings(params):
    """Display the estimated number of paid tasks and ask users to confirm the Secret Tower MiniMax generation fee."""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    voice_mode = st.session_state.get(
        localized_widget_key("voice_mode_control"),
        config.ui.get("voice_mode"),
    )
    if voice_mode == VOICE_MODE_UPLOAD:
        # The video settings are rendered before the audio settings. At this time, the actual value of the newly uploaded files in this round cannot be reliably read.
        # duration. The upload mode no longer displays numbers calculated based on script text to prevent users from mistakenly thinking that a
        # 5-second audio will also create multiple paid tasks based on longer copywriting; the actual duration of the file will still prevail during runtime.
        st.warning(
            tr("Metaso MiniMax Billing Notice Uploaded Audio").format(
                resolution=str(
                    config.app.get(
                        "metaso_minimax_resolution",
                        metaso_minimax.DEFAULT_RESOLUTION,
                    )
                    or metaso_minimax.DEFAULT_RESOLUTION
                ),
                duration=clip_duration,
                count=video_count,
            )
        )
    elif estimated_range := _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    ):
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("Metaso MiniMax Billing Notice").format(
                min=min_clips,
                max=max_clips,
                resolution=str(
                    config.app.get(
                        "metaso_minimax_resolution",
                        metaso_minimax.DEFAULT_RESOLUTION,
                    )
                    or metaso_minimax.DEFAULT_RESOLUTION
                ),
            )
        )
    else:
        st.warning(tr("Metaso MiniMax Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm Metaso MiniMax Charge"),
        key="metaso_minimax_confirm_charge",
        help=tr("Confirm Metaso MiniMax Charge Help"),
    )


def _render_muapi_video_settings(params):
    """Show an estimated MuAPI task count and require explicit billing consent."""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    if estimated_range := _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    ):
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("MuAPI Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("MuAPI Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm MuAPI Charge"),
        key="muapi_confirm_charge",
        help=tr("Confirm MuAPI Charge Help"),
    )


def _estimate_voiceover_duration_range(
    text: str, voice_rate: float
) -> tuple[float, float] | None:
    """
    Locally estimates the complete dubbing duration, returning conservative upper and lower bounds in seconds.

    This estimate is only used to help users judge the magnitude of copywriting before calling paid TTS and does not participate in task execution.
    Chinese, Japanese and Korean are estimated based on character speed, and other languages that use space word segmentation are estimated based on word speed.
    Common punctuation pauses are also included. Different providers, timbres and tones will cause actual deviations, so the interface
    An interval must be presented rather than a pseudo-exact single result.
    """
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized_text:
        return None

    script_chars = re.findall(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        normalized_text,
    )
    remaining_text = re.sub(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        " ",
        normalized_text,
    )
    words = re.findall(r"\b[\w]+(?:[-'’][\w]+)*\b", remaining_text, re.UNICODE)
    punctuation_count = len(re.findall(r"[,，.。!?！？;；:：]", normalized_text))

    # 4.2 words/second and 2.6 words/second are close to the daily commentary speed; press 0.12 seconds for punctuation to add a slight pause.
    # voice_rate is only used as an estimate modifier. Partially generated TTS does not strictly enforce magnification, so in the end
    # The ±15% interval is still retained to prevent users from mistakenly thinking that this value is equivalent to the real result on the server side.
    base_seconds = len(script_chars) / 4.2 + len(words) / 2.6 + punctuation_count * 0.12
    if base_seconds <= 0:
        return None

    normalized_rate = max(float(voice_rate or 1.0), 0.1)
    estimated_seconds = base_seconds / normalized_rate
    return (
        round(max(estimated_seconds * 0.85, 1.0), 1),
        round(max(estimated_seconds * 1.15, 1.0), 1),
    )


def _get_voice_preview_sample(voice_name: str) -> str:
    """Returns a short audition copy suitable for the current timbre, without using the user's full video copy."""
    # ElevenLabs sounds are selected based on Vietnamese characters in the display name when they lack an explicit language field
    # Listen to the copy and avoid using language that clearly does not match to judge the timbre effect.
    if voice.is_elevenlabs_voice(voice_name):
        parts = voice_name.split(":", 2)
        display = parts[2] if len(parts) >= 3 else ""
        vietnamese_chars = set("àáâãèéêìíòóôõùúýăđơưÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐƠƯ")
        if any(char in vietnamese_chars for char in display):
            return "Xin chào, đây là đoạn âm thanh thử nghiệm giọng nói."
    return tr("Voice Example")


def _voice_preview_fingerprint(
    *,
    preview_type: str,
    content: str,
    tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
    provider_signature: dict,
) -> str:
    """Generate audition cache fingerprints, and automatically invalidate old audition results after any dubbing parameter changes."""
    payload = {
        "preview_type": preview_type,
        "content": content,
        "tts_server": tts_server,
        "voice_name": voice_name,
        "voice_rate": voice_rate,
        "voice_volume": voice_volume,
        "provider_signature": provider_signature,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _credential_signature(value: str) -> str:
    """
    Generate a credential digest that is only used for cache invalidation determination.

    The summary is not written to the configuration, log, or task files. After the user modifies the API Key, the summary will change, thus
    Forces a recall of the current dubbing service to avoid old audition caches making invalid new credentials appear available.
    """
    normalized_value = str(value or "")
    if not normalized_value:
        return ""
    return hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()


def _get_voxcpm_reference_audio() -> bytes | None:
    """Return the current session's normalized reference audio, if any."""
    payload = st.session_state.get(VOXCPM_REFERENCE_AUDIO_SESSION_KEY)
    if not isinstance(payload, dict):
        return None
    audio_bytes = payload.get("audio_bytes")
    return bytes(audio_bytes) if isinstance(audio_bytes, bytes) else None


def _get_voxcpm_reference_audio_digest() -> str:
    payload = st.session_state.get(VOXCPM_REFERENCE_AUDIO_SESSION_KEY)
    if not isinstance(payload, dict):
        return ""
    digest = payload.get("audio_digest")
    return str(digest) if isinstance(digest, str) else ""


def _get_voxcpm_prompt_audio() -> bytes | None:
    payload = st.session_state.get(VOXCPM_PROMPT_AUDIO_SESSION_KEY)
    if not isinstance(payload, dict):
        return None
    audio_bytes = payload.get("audio_bytes")
    return bytes(audio_bytes) if isinstance(audio_bytes, bytes) else None


def _get_voxcpm_prompt_audio_digest() -> str:
    payload = st.session_state.get(VOXCPM_PROMPT_AUDIO_SESSION_KEY)
    if not isinstance(payload, dict):
        return ""
    digest = payload.get("audio_digest")
    return str(digest) if isinstance(digest, str) else ""


def _get_voxcpm_prompt_text() -> str:
    return str(st.session_state.get(VOXCPM_PROMPT_TEXT_SESSION_KEY, "") or "").strip()


def _clear_voxcpm_separate_prompt_audio() -> None:
    st.session_state.pop("voxcpm_prompt_audio_uploader", None)
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)


def _clear_voxcpm_prompt_state() -> None:
    _clear_voxcpm_separate_prompt_audio()
    _clear_voxcpm_prompt_transcript()
    st.session_state.pop(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, None)
    st.session_state.pop(VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY, None)


def _clear_voxcpm_prompt_transcript() -> None:
    st.session_state.pop(VOXCPM_PROMPT_TEXT_SESSION_KEY, None)


def _sync_voxcpm_prompt_example_mode(use_separate_prompt_audio: bool) -> None:
    """Invalidate the transcript whenever its effective example changes mode."""
    previous_mode = st.session_state.get(VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY)
    current_mode = bool(use_separate_prompt_audio)
    if previous_mode is not None and bool(previous_mode) != current_mode:
        _clear_voxcpm_prompt_transcript()
    st.session_state[VOXCPM_PROMPT_EXAMPLE_MODE_SESSION_KEY] = current_mode


def _get_voxcpm_effective_prompt_audio() -> bytes | None:
    if not st.session_state.get(VOXCPM_HIGH_FIDELITY_SESSION_KEY, False):
        return None
    if st.session_state.get(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, False):
        return _get_voxcpm_prompt_audio()
    return _get_voxcpm_reference_audio()


def _get_voxcpm_effective_prompt_audio_digest() -> str:
    if not st.session_state.get(VOXCPM_HIGH_FIDELITY_SESSION_KEY, False):
        return ""
    if st.session_state.get(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, False):
        return _get_voxcpm_prompt_audio_digest()
    return _get_voxcpm_reference_audio_digest()


def _get_voxcpm_prompt_validation_error() -> str:
    if not st.session_state.get(VOXCPM_HIGH_FIDELITY_SESSION_KEY, False):
        return ""
    upload_error = st.session_state.get(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY)
    if upload_error:
        return str(upload_error)
    prompt_text = _get_voxcpm_prompt_text()
    if not prompt_text:
        return tr("VoxCPM Prompt Text Required")
    if (
        st.session_state.get(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, False)
        and not _get_voxcpm_prompt_audio()
    ):
        return tr("VoxCPM Prompt Pair Required")
    return ""


def _get_voxcpm_preview_validation_error(
    selected_tts_server: str,
    voice_name: str,
) -> str:
    if selected_tts_server != "voxcpm" and not voice.is_voxcpm_voice(voice_name):
        return ""
    reference_error = st.session_state.get(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY)
    if reference_error:
        return str(reference_error)
    return _get_voxcpm_prompt_validation_error()


def _sync_voxcpm_reference_audio(uploaded_file) -> bytes | None:
    """Normalize a new upload once and keep only bounded WAV bytes in session."""
    if uploaded_file is None:
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY, None)
        _clear_voxcpm_prompt_state()
        st.session_state.pop(VOXCPM_HIGH_FIDELITY_SESSION_KEY, None)
        return None

    upload_size = int(getattr(uploaded_file, "size", 0) or 0)
    if upload_size <= 0:
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
        _clear_voxcpm_prompt_state()
        st.session_state[VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY] = tr(
            "VoxCPM Reference Audio Empty"
        )
        return None
    if upload_size > voice.VOXCPM_REFERENCE_AUDIO_MAX_UPLOAD_BYTES:
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
        _clear_voxcpm_prompt_state()
        st.session_state[VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY] = tr(
            "VoxCPM Reference Audio Upload Too Large"
        ).format(limit=voice.VOXCPM_REFERENCE_AUDIO_MAX_UPLOAD_BYTES // (1024 * 1024))
        return None

    previous_digest = _get_voxcpm_reference_audio_digest()
    raw_audio = uploaded_file.getvalue()
    upload_digest = hashlib.sha256(raw_audio).hexdigest()
    cached = st.session_state.get(VOXCPM_REFERENCE_AUDIO_SESSION_KEY)
    if isinstance(cached, dict) and cached.get("upload_digest") == upload_digest:
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY, None)
        return _get_voxcpm_reference_audio()

    try:
        with st.spinner(tr("Validating VoxCPM Reference Audio")):
            wav_audio = voice.prepare_voxcpm_reference_audio(
                raw_audio,
                Path(str(getattr(uploaded_file, "name", ""))).suffix,
            )
    except ValueError as exc:
        # Do not retain a prior clip after the user selects a replacement that
        # fails validation. Otherwise preview and generation could silently use
        # a different person's voice than the one currently shown in the UI.
        st.session_state.pop(VOXCPM_REFERENCE_AUDIO_SESSION_KEY, None)
        _clear_voxcpm_prompt_state()
        st.session_state[VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY] = str(exc)
        return None

    st.session_state[VOXCPM_REFERENCE_AUDIO_SESSION_KEY] = {
        "upload_digest": upload_digest,
        "audio_digest": hashlib.sha256(wav_audio).hexdigest(),
        "audio_bytes": wav_audio,
    }
    if (
        previous_digest != hashlib.sha256(wav_audio).hexdigest()
        and not st.session_state.get(VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY, False)
    ):
        _clear_voxcpm_prompt_transcript()
    st.session_state.pop(VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY, None)
    return bytes(wav_audio)


def _sync_voxcpm_prompt_audio(uploaded_file) -> bytes | None:
    """Normalize the optional delivery example without persisting its content."""
    if uploaded_file is None:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)
        return None

    upload_size = int(getattr(uploaded_file, "size", 0) or 0)
    if upload_size <= 0:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
        st.session_state[VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY] = tr(
            "VoxCPM Prompt Audio Empty"
        )
        return None
    if upload_size > voice.VOXCPM_REFERENCE_AUDIO_MAX_UPLOAD_BYTES:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
        st.session_state[VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY] = tr(
            "VoxCPM Prompt Audio Upload Too Large"
        ).format(limit=voice.VOXCPM_REFERENCE_AUDIO_MAX_UPLOAD_BYTES // (1024 * 1024))
        return None

    previous_digest = _get_voxcpm_prompt_audio_digest()
    raw_audio = uploaded_file.getvalue()
    upload_digest = hashlib.sha256(raw_audio).hexdigest()
    cached = st.session_state.get(VOXCPM_PROMPT_AUDIO_SESSION_KEY)
    if isinstance(cached, dict) and cached.get("upload_digest") == upload_digest:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)
        return _get_voxcpm_prompt_audio()

    try:
        with st.spinner(tr("Validating VoxCPM Prompt Audio")):
            wav_audio = voice.prepare_voxcpm_reference_audio(
                raw_audio,
                Path(str(getattr(uploaded_file, "name", ""))).suffix,
            )
    except ValueError as exc:
        st.session_state.pop(VOXCPM_PROMPT_AUDIO_SESSION_KEY, None)
        st.session_state[VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY] = str(exc)
        return None

    st.session_state[VOXCPM_PROMPT_AUDIO_SESSION_KEY] = {
        "upload_digest": upload_digest,
        "audio_digest": hashlib.sha256(wav_audio).hexdigest(),
        "audio_bytes": wav_audio,
    }
    if previous_digest != hashlib.sha256(wav_audio).hexdigest():
        _clear_voxcpm_prompt_transcript()
    st.session_state.pop(VOXCPM_PROMPT_AUDIO_ERROR_SESSION_KEY, None)
    return bytes(wav_audio)


def _get_voice_preview_provider_signature(tts_server: str) -> dict:
    """
    Returns non-sensitive Provider configuration that affects the listening results.

    The API Key only participates in the cache fingerprint as a one-way digest, and the original credentials do not enter the cache or logs. model,
    Whenever the service address, region or credentials change, the audition must be regenerated, otherwise the interface may continue to play.
    Audio under the old Provider configuration makes users mistakenly believe that the current settings have taken effect.
    """
    if tts_server == "azure-tts-v2":
        return {
            "speech_region": config.azure.get("speech_region", ""),
            "credential": _credential_signature(config.azure.get("speech_key", "")),
        }
    if tts_server == "siliconflow":
        return {
            "credential": _credential_signature(config.siliconflow.get("api_key", ""))
        }
    if tts_server == "gemini-tts":
        return {
            "credential": _credential_signature(config.app.get("gemini_api_key", ""))
        }
    if tts_server == "mimo-tts":
        return {"credential": _credential_signature(config.app.get("mimo_api_key", ""))}
    if tts_server == "minimax-tts":
        return {
            "base_url": voice.get_minimax_tts_endpoint(),
            "model_id": config.minimax_tts.get("model_id", ""),
            "voice_id": config.minimax_tts.get("voice_id", ""),
            "credential": _credential_signature(voice.get_minimax_tts_api_key()),
        }
    if tts_server == "elevenlabs":
        return {
            "model_id": config.elevenlabs.get("model_id", ""),
            "credential": _credential_signature(config.elevenlabs.get("api_key", "")),
        }
    if tts_server == "chatterbox":
        return {
            "base_url": config.chatterbox.get("base_url", ""),
            "model_id": config.chatterbox.get("model_id", ""),
            "credential": _credential_signature(config.chatterbox.get("api_key", "")),
        }
    if tts_server == "kokoro":
        return {
            "base_url": config.kokoro.get("base_url", ""),
            "model_id": config.kokoro.get("model_id", ""),
            "credential": _credential_signature(config.kokoro.get("api_key", "")),
        }
    if tts_server == "voxcpm":
        return {
            "base_url": config.voxcpm.get("base_url", ""),
            "model_id": config.voxcpm.get("model_id", ""),
            "voice_id": config.voxcpm.get("voice_id", "default"),
            "credential": _credential_signature(config.voxcpm.get("api_key", "")),
            "reference_audio": _get_voxcpm_reference_audio_digest(),
            "prompt_audio": _get_voxcpm_effective_prompt_audio_digest(),
            "prompt_text": _credential_signature(
                _get_voxcpm_prompt_text()
                if st.session_state.get(VOXCPM_HIGH_FIDELITY_SESSION_KEY, False)
                else ""
            ),
        }
    return {}


def _synthesize_voice_preview(
    *,
    content: str,
    preview_type: str,
    selected_tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
    voxcpm_reference_audio: Optional[bytes] = None,
    voxcpm_prompt_audio: Optional[bytes] = None,
    voxcpm_prompt_text: str = "",
) -> Optional[dict]:
    """Auditions are generated once and moved to memory cache, temporary files are not persisted across sessions."""
    if selected_tts_server == "chatterbox":
        _sync_chatterbox_config_from_session_state()
    if selected_tts_server == "kokoro":
        _sync_kokoro_config_from_session_state()

    temp_dir = utils.storage_dir("temp", create=True)
    audio_file = os.path.join(temp_dir, f"tmp-voice-{uuid4()!s}.mp3")
    logger.info(
        f"generating {preview_type} voice preview: "
        f"voice={voice_name}, rate={voice_rate}, volume={voice_volume}, "
        f"text_length={len(content)}"
    )
    try:
        with config.try_runtime_config_lock() as lock_acquired:
            if not lock_acquired:
                return {"busy": True}
            tts_kwargs = {
                "text": content,
                "voice_name": voice_name,
                "voice_rate": voice_rate,
                "voice_file": audio_file,
                "voice_volume": voice_volume,
            }
            if voxcpm_reference_audio is not None:
                tts_kwargs["voxcpm_reference_audio"] = voxcpm_reference_audio
            if voxcpm_prompt_audio is not None:
                tts_kwargs["voxcpm_prompt_audio"] = voxcpm_prompt_audio
                tts_kwargs["voxcpm_prompt_text"] = voxcpm_prompt_text
            sub_maker = voice.tts(**tts_kwargs)
        if not sub_maker or not os.path.exists(audio_file):
            logger.error(f"{preview_type} voice preview did not produce an audio file")
            return None

        with open(audio_file, "rb") as file:
            audio_bytes = file.read()
        if not audio_bytes:
            logger.error(f"voice preview audio file is empty: {audio_file}")
            return None

        duration = voice.get_audio_duration(audio_file)
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            logger.warning(
                f"voice preview duration is unavailable: "
                f"preview_type={preview_type}, voice={voice_name}"
            )
            duration = None

        return {
            "audio_bytes": audio_bytes,
            "mime_type": _detect_audio_mime(audio_file, audio_bytes),
            "duration": duration,
            "preview_type": preview_type,
            "sub_maker": sub_maker,
            # The video panel that precedes the audio panel only uses the
            # Full audition length; short auditions or old copy will never change the number of recommended materials.
            "content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "tts_server": selected_tts_server,
            "voice_name": voice_name,
            "voice_rate": float(voice_rate),
        }
    finally:
        # The browser player uses memory bytes, and the files can be cleaned up after reading to avoid the accumulation of temporary files during frequent listening.
        try:
            os.remove(audio_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Cleanup failures should not overwrite real TTS responses or exceptions, but paths and system errors need to be preserved,
            # It is convenient to troubleshoot environmental issues such as permissions and read-only file systems.
            logger.warning(
                f"failed to delete voice preview file {audio_file}: {exc!s}"
            )


def _render_voice_preview(params, friendly_names, selected_tts_server, voice_name):
    """Render low-cost short auditions, full copywriting duration estimates, and full voiceover previews on demand."""
    if not friendly_names:
        return

    script_content = str(params.video_script or "").strip()
    estimated_range = _estimate_voiceover_duration_range(
        script_content,
        params.voice_rate,
    )
    if estimated_range:
        st.caption(
            tr("Estimated Voiceover Duration").format(
                min=estimated_range[0],
                max=estimated_range[1],
            )
        )
    else:
        st.caption(tr("Voiceover Script Required"))

    sample_content = _get_voice_preview_sample(voice_name)
    provider_signature = _get_voice_preview_provider_signature(selected_tts_server)
    preview_validation_error = _get_voxcpm_preview_validation_error(
        selected_tts_server,
        voice_name,
    )
    preview_columns = st.columns(2)
    short_preview_requested = preview_columns[0].button(
        tr("Play Voice"),
        key="play_voice_button",
        icon=":material/graphic_eq:",
        use_container_width=True,
        disabled=bool(preview_validation_error),
    )
    full_preview_requested = preview_columns[1].button(
        tr("Generate Full Voiceover Preview"),
        key="generate_full_voiceover_preview_button",
        icon=":material/article:",
        help=tr("Full Voiceover Preview Cost Hint"),
        use_container_width=True,
        disabled=not bool(script_content) or bool(preview_validation_error),
    )

    preview_type = ""
    preview_content = ""
    if short_preview_requested:
        preview_type = "sample"
        preview_content = sample_content
    elif full_preview_requested:
        preview_type = "full"
        preview_content = script_content

    sample_fingerprint = _voice_preview_fingerprint(
        preview_type="sample",
        content=sample_content,
        tts_server=selected_tts_server,
        voice_name=voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=provider_signature,
    )
    full_fingerprint = (
        _voice_preview_fingerprint(
            preview_type="full",
            content=script_content,
            tts_server=selected_tts_server,
            voice_name=voice_name,
            voice_rate=params.voice_rate,
            voice_volume=params.voice_volume,
            provider_signature=provider_signature,
        )
        if script_content
        else ""
    )

    if preview_type:
        requested_fingerprint = (
            sample_fingerprint if preview_type == "sample" else full_fingerprint
        )
        cached_preview = st.session_state.get("voice_preview_audio")
        if (
            not cached_preview
            or cached_preview.get("fingerprint") != requested_fingerprint
        ):
            try:
                with st.spinner(tr("Synthesizing Voice")):
                    preview_result = _synthesize_voice_preview(
                        content=preview_content,
                        preview_type=preview_type,
                        selected_tts_server=selected_tts_server,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_volume=params.voice_volume,
                        voxcpm_reference_audio=(
                            _get_voxcpm_reference_audio()
                            if selected_tts_server == "voxcpm"
                            else None
                        ),
                        voxcpm_prompt_audio=(
                            _get_voxcpm_effective_prompt_audio()
                            if selected_tts_server == "voxcpm"
                            else None
                        ),
                        voxcpm_prompt_text=(
                            _get_voxcpm_prompt_text()
                            if selected_tts_server == "voxcpm"
                            else ""
                        ),
                    )
            except Exception as exc:
                logger.exception(f"failed to generate {preview_type} voice preview")
                st.error(tr("Voice Preview Failed").format(error=str(exc)))
            else:
                if preview_result and preview_result.get("busy"):
                    st.warning(tr("Voice Preview Busy"))
                elif preview_result:
                    preview_result["fingerprint"] = requested_fingerprint
                    st.session_state["voice_preview_audio"] = preview_result
                    if (
                        preview_type == "full"
                        and params.video_source == "loomloom"
                        and isinstance(preview_result.get("duration"), (int, float))
                        and math.isfinite(preview_result["duration"])
                        and preview_result["duration"] > 0
                    ):
                        # Video settings are rendered before audio settings. A rerun is triggered after the complete audition is successful.
                        # Let the number of materials above immediately re-recommend according to the actual narration duration and refresh the coverage prompt.
                        st.session_state["loomloom_video_scene_autofill_digest"] = (
                            preview_result["content_digest"]
                        )
                        st.rerun()
                else:
                    st.error(tr("Voice Preview No Audio"))

    cached_preview = st.session_state.get("voice_preview_audio")
    valid_fingerprints = {sample_fingerprint, full_fingerprint}
    if (
        cached_preview
        and cached_preview.get("fingerprint") in valid_fingerprints
        and cached_preview.get("audio_bytes")
    ):
        # It will only play automatically when the user explicitly clicks "Audio Sound" this time. Other controls for Streamlit
        # It will also trigger page rerun; if autoplay is permanently enabled for cached audio, modify any settings
        # It is possible to have old auditions played from the beginning. Continue to keep manual playback for the complete audition to avoid long audio
        # Unexpectedly interrupting the user after the build is complete.
        should_autoplay = bool(
            short_preview_requested
            and cached_preview.get("preview_type") == "sample"
            and cached_preview.get("fingerprint") == sample_fingerprint
        )
        st.audio(
            cached_preview["audio_bytes"],
            format=cached_preview.get("mime_type", "audio/mp3"),
            autoplay=should_autoplay,
        )
        if cached_preview.get("preview_type") == "full":
            duration = cached_preview.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                st.caption(
                    tr("Actual Voiceover Duration").format(duration=f"{duration:.1f}")
                )
            else:
                st.warning(tr("Voice Preview Duration Unavailable"))


def _get_reusable_full_voice_preview(params, voice_mode: str) -> dict | None:
    """
    Returns the complete audition cache that exactly matches the current build parameters.

    Only complete copywriting is reused for audition, and short tone samples can never enter the official task. Fingerprints uniformly cover copywriting,
    Provider, timbre, speech rate, volume and non-sensitive configuration summary; any parameter changes will naturally fall back to
    Normal TTS process. The subtitle timeline and valid duration are also required conditions to avoid just reusing the audio and then
    Edge subtitle link loses SubMaker.
    """
    if voice_mode != VOICE_MODE_TTS:
        return None

    script_content = str(params.video_script or "").strip()
    selected_tts_server = config.ui.get("tts_server", "azure-tts-v1")
    if (
        not script_content
        or not params.voice_name
        # Formal videos will uniformly apply dubbing volume during the MoviePy synthesis stage; some Providers will
        # Volume gain is written directly in the TTS stage. Multiplex listening at non-default volumes may cause secondary gain.
        # Therefore, we first conservatively roll back to the original process to avoid introducing Provider special judgments for a small number of scenarios.
        or not math.isclose(float(params.voice_volume), 1.0)
    ):
        return None

    expected_fingerprint = _voice_preview_fingerprint(
        preview_type="full",
        content=script_content,
        tts_server=selected_tts_server,
        voice_name=params.voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=_get_voice_preview_provider_signature(selected_tts_server),
    )
    cached_preview = st.session_state.get("voice_preview_audio")
    if (
        not cached_preview
        or cached_preview.get("fingerprint") != expected_fingerprint
        or cached_preview.get("preview_type") != "full"
        or not cached_preview.get("audio_bytes")
        or cached_preview.get("sub_maker") is None
    ):
        return None

    duration = cached_preview.get("duration")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return None

    return {
        "audio_bytes": bytes(cached_preview["audio_bytes"]),
        "duration": float(duration),
        "sub_maker": cached_preview["sub_maker"],
        "script": script_content,
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }


def _sync_minimax_tts_api_key_input():
    """
    Synchronize the MiniMax TTS password control and return the currently valid Key.

    MiniMax LLM Key is allowed to be reused when the TTS dedicated Key is empty. Shared Key is only used for the current control and
    Requests are not automatically copied to [minimax_tts] to avoid repeated maintenance of the same credentials in the configuration file.
    """
    widget_key = "minimax_tts_api_key_input"
    configured_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    shared_key = str(
        config.app.get("minimax_api_key", "") or os.getenv("MINIMAX_API_KEY", "") or ""
    ).strip()
    effective_key = configured_key or shared_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # The browser may replay the empty password state when reconnecting. Restore configured credentials to prevent null values from overwriting the configuration.
        # At the same time, ensure that the current rerun audition request can directly use a valid Key.
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored MiniMax TTS API key after empty session replay")
    elif not had_widget_state:
        st.session_state[widget_key] = effective_key
        entered_key = effective_key

    if entered_key and entered_key != effective_key:
        _set_runtime_config("minimax_tts", "api_key", entered_key)

    return entered_key


def _get_cached_minimax_voices(api_key: str, endpoint: str) -> list[dict[str, str]]:
    """Reads MiniMax patch query results for the current session by site and credential summary."""
    cache = st.session_state.get("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cached_voices = cache.get(cache_key, [])
    return cached_voices if isinstance(cached_voices, list) else []


def _cache_minimax_voices(
    api_key: str,
    endpoint: str,
    voices: list[dict[str, str]],
):
    """Cache actively queried timbres to avoid repeated requests to MiniMax after ordinary controls are rerun."""
    cache = st.session_state.setdefault("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cache[cache_key] = voices


def _render_minimax_tts_settings() -> tuple[list[str], dict[str, str]]:
    """Renders a MiniMax TTS configuration and returns the options and text used by the unified patch selector."""
    effective_api_key = _sync_minimax_tts_api_key_input()
    effective_api_key = st.text_input(
        tr("MiniMax TTS API Key"),
        type="password",
        key="minimax_tts_api_key_input",
    ).strip()

    dedicated_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    minimax_tts_endpoints = [voice.MINIMAX_TTS_GLOBAL_URL, voice.MINIMAX_TTS_CN_URL]
    effective_endpoint = voice.get_minimax_tts_endpoint()
    if effective_endpoint not in minimax_tts_endpoints:
        effective_endpoint = voice.MINIMAX_TTS_GLOBAL_URL
    minimax_tts_base_url = stable_selectbox(
        tr("MiniMax TTS Endpoint"),
        options=minimax_tts_endpoints,
        default_value=effective_endpoint,
        key="minimax_tts_endpoint_select",
        # When reusing the LLM Key, you must follow the area where the LLM is located to prevent the interface from allowing you to select an actual
        # The address will not be valid; you can select the site individually after filling in the independent TTS Key.
        disabled=not dedicated_key,
    )
    if dedicated_key:
        _set_runtime_config("minimax_tts", "base_url", minimax_tts_base_url)

    configured_model = config.minimax_tts.get(
        "model_id", voice.MINIMAX_TTS_DEFAULT_MODEL
    )
    if configured_model not in voice.MINIMAX_TTS_MODELS:
        configured_model = voice.MINIMAX_TTS_DEFAULT_MODEL
    minimax_tts_model = stable_selectbox(
        tr("MiniMax TTS Model"),
        options=list(voice.MINIMAX_TTS_MODELS),
        default_value=configured_model,
        key="minimax_tts_model_select",
    )
    _set_runtime_config("minimax_tts", "model_id", minimax_tts_model)

    if st.button(
        tr("Load MiniMax Voices"),
        key="load_minimax_voices_button",
        icon=":material/refresh:",
        use_container_width=True,
    ):
        try:
            available_voices = voice.get_minimax_voice_catalog(
                api_key=effective_api_key,
                endpoint=minimax_tts_base_url,
                voice_type="all",
            )
        except Exception as exc:
            # Exceptions must be exposed to users and logged here. Account area does not match, Key permissions are insufficient
            # Or network failure is common, and silently returning an empty list will make users mistakenly think that the account has no sounds.
            logger.warning(f"load MiniMax voices failed: {exc}")
            st.error(tr("MiniMax Voices Load Failed").format(error=str(exc)))
        else:
            _cache_minimax_voices(
                effective_api_key,
                minimax_tts_base_url,
                available_voices,
            )
            st.success(tr("MiniMax Voices Loaded").format(count=len(available_voices)))

    available_voices = _get_cached_minimax_voices(
        effective_api_key,
        minimax_tts_base_url,
    )
    voice_labels = {
        f"minimax:{item['voice_id']}": (
            f"{item['voice_name']} ({item['voice_id']})"
            if item["voice_name"] != item["voice_id"]
            else item["voice_id"]
        )
        for item in available_voices
    }
    configured_voice_id = str(
        config.minimax_tts.get("voice_id", voice.MINIMAX_TTS_DEFAULT_VOICE)
        or voice.MINIMAX_TTS_DEFAULT_VOICE
    ).strip()
    configured_voice = f"minimax:{configured_voice_id}"
    # If you have not clicked to obtain the sound, the interface is temporarily unavailable, or the cloned sound is not configured to be used in the list, it will still be retained.
    # The current Voice ID ensures that the original generation process does not rely on the remote voice query results.
    voice_labels.setdefault(configured_voice, configured_voice_id)
    return list(voice_labels), voice_labels


def _sync_elevenlabs_api_key_input():
    """
    Synchronize ElevenLabs password control, persistent configuration and environment variables, and return the current valid Key.

    Streamlit may replay an empty password control when a browser tab is connected to a restarted service
    status. This null value cannot be reliably distinguished from user-initiated clearing, so when the configuration file or environment variable still has
    Key, priority is given to restoring the valid value to prevent the empty state from overwriting the configuration and ensure that this rerun can be loaded immediately.
    timbre. When you need to completely delete the Key, you should modify the configuration file or environment variables to avoid misjudgment during reconnection.
    """
    widget_key = "elevenlabs_api_key_input"
    configured_key = str(config.elevenlabs.get("api_key", "") or "").strip()
    env_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    effective_key = configured_key or env_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # The empty state after reconnection cannot overwrite valid credentials and must be restored before rendering the sound list.
        # Otherwise, although the configuration file has not been cleared, the current page will still use an empty Key to request ElevenLabs.
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored ElevenLabs API key after empty session replay")
    elif not had_widget_state:
        # Initialize first and then create the control to avoid passing value and session_state at the same time to trigger Streamlit
        # Default value conflict warning; just initialize it to empty when there is no Key.
        st.session_state[widget_key] = entered_key

    if entered_key and entered_key != effective_key:
        # Only new values actively entered by the user are dropped into config.toml. Environment variables are not backfilled as valid values
        # Injected keys that are copied to a file, container or deployment platform remain only in the runtime environment.
        for cache_key in list(st.session_state.keys()):
            if str(cache_key).startswith("elevenlabs_voices_"):
                del st.session_state[cache_key]
        _set_runtime_config("elevenlabs", "api_key", entered_key)

    return entered_key


def _sync_voxcpm_api_key_input():
    """Restore the empty state of the VoxCPM password control being replayed by Streamlit on reconnection."""
    widget_key = "voxcpm_api_key_input"
    configured_key = str(config.voxcpm.get("api_key", "") or "").strip()
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and configured_key:
        # The browser may replay the empty password state when reconnecting. Keep saved credentials to avoid this rerun
        # Use _set_runtime_config to overwrite the valid Key in config.toml to empty.
        st.session_state[widget_key] = configured_key
        entered_key = configured_key
        if had_widget_state:
            logger.debug("restored VoxCPM API key after empty session replay")
    elif not had_widget_state:
        st.session_state[widget_key] = entered_key

    return entered_key


def _render_elevenlabs_api_key_input(label_key):
    """
    Rendering the unique API Key input state that ElevenLabs TTS shares with the soundtrack.

    If two widget keys are used for TTS and soundtrack on the same page, Streamlit will retain the old values respectively.
    Post-rendered input boxes also override the shared configuration. A key is used here and environment variables are processed centrally.
    Backfilling, configuration updates, and sound cache invalidation ensure that interface display and background tasks always read the same value.
    """
    _sync_elevenlabs_api_key_input()
    return st.text_input(
        tr(label_key),
        type="password",
        key="elevenlabs_api_key_input",
    ).strip()


def _render_background_music_settings(params, elevenlabs_api_key_rendered=False):
    """Render the background music source and volume settings, and return the uploaded file to be saved this time."""
    uploaded_bgm_file = None
    previous_bgm_type = st.session_state.get("last_rendered_bgm_type")
    st.divider()
    bgm_options = [
        (tr("No Background Music"), ""),
        (tr("Random Background Music"), "random"),
        (tr("Preset Song"), "preset"),
        (tr("Custom Background Music"), "custom"),
        (tr("Sonilo Background Music"), "sonilo"),
        (tr("ElevenLabs Background Music"), "elevenlabs"),
    ]
    selected_bgm_type = stable_selectbox(
        tr("Background Music Source"),
        options=[value for _, value in bgm_options],
        default_value=_saved_ui_choice(
            "bgm_type",
            [value for _, value in bgm_options],
            "random",
        ),
        key="bgm_type_select",
        format_func=lambda value: dict((v, label) for label, v in bgm_options)[value],
    )
    params.bgm_type = selected_bgm_type
    _set_runtime_config("ui", "bgm_type", params.bgm_type)
    if params.bgm_type == "sonilo":
        configured_key = str(config.app.get("sonilo_api_key", "") or "").strip()
        effective_key = configured_key or os.getenv("SONILO_API_KEY", "").strip()
        entered_key = st.text_input(
            tr("Sonilo API Key"),
            value=effective_key,
            type="password",
            key="sonilo_api_key_input",
        ).strip()
        # The user requires the configured Key to be directly backfilled into the password input box. Configuration values take precedence over environment variables;
        # Only write back when the user actually changes the input or uses the configuration to avoid changing the Key in the environment variable.
        # Copy into config.toml without any operation.
        if configured_key or entered_key != effective_key:
            _set_runtime_config("app", "sonilo_api_key", entered_key)
    elif params.bgm_type == "elevenlabs":
        if elevenlabs_api_key_rendered:
            # When the shared input box has been rendered in the TTS area, a second widget will no longer be created to avoid two independent widgets.
            # session_state values overwrite each other. Description text helps users locate the shared configuration above.
            st.caption(tr("ElevenLabs API Key Help"))
        else:
            _render_elevenlabs_api_key_input("ElevenLabs Music API Key")

    bgm_volume_options = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    params.bgm_volume = stable_selectbox(
        tr("Background Music Volume"),
        options=bgm_volume_options,
        default_value=_saved_ui_choice("bgm_volume", bgm_volume_options, 0.2),
        key="bgm_volume_select",
        format_func=lambda value: f"{int(value * 100)}%",
        disabled=not params.bgm_type,
    )
    _set_runtime_config("ui", "bgm_volume", params.bgm_volume)
    bgm_enabled = bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)

    if params.bgm_type == "custom":
        uploaded_bgm_file = st.file_uploader(
            tr("Upload Background Music"),
            type=[
                extension.removeprefix(".")
                for extension in bgm_service.SUPPORTED_BGM_EXTENSIONS
            ],
            accept_multiple_files=False,
            key="custom_bgm_uploader",
            help=tr("Upload Background Music Help"),
            # Streamlit displays a global 200MB limit on the control by default. This must be related to the service layer
            # The 30MB hard limit remains consistent to avoid being rejected by the server only when the interface allows selection and submission.
            max_upload_size=bgm_service.MAX_BGM_UPLOAD_BYTES // (1024 * 1024),
        )
        if uploaded_bgm_file is not None and bgm_enabled:
            try:
                safe_name = bgm_service.sanitize_upload_filename(uploaded_bgm_file.name)
                # Streamlit will re-execute the page after adjusting any controls such as volume. Use content hashing
                # Differentiate uploaded files and cache the complete decoding results in the current session. You cannot rely solely on the same name,
                # Misuse of old results for files of the same size also avoids calling FFmpeg repeatedly for each rerun.
                validation_key = (
                    safe_name,
                    uploaded_bgm_file.size,
                    hashlib.sha256(uploaded_bgm_file.getbuffer()).hexdigest(),
                )
                cached_validation = st.session_state.get("custom_bgm_validation")
                if (
                    not cached_validation
                    or cached_validation.get("key") != validation_key
                ):
                    try:
                        bgm_service.validate_bgm_upload(
                            uploaded_bgm_file.name, uploaded_bgm_file
                        )
                    except bgm_service.BgmUploadError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "upload",
                        }
                        # The failed results of the same file fingerprint will be entered into the session cache, so here only
                        # Record it once when the verification is actually executed for the first time to avoid rerun of ordinary controls and refresh the screen.
                        logger.warning(
                            "WebUI background music validation rejected: "
                            f"name={safe_name}, error={exc!s}"
                        )
                    except bgm_service.BgmServiceError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "service",
                        }
                        logger.error(
                            "WebUI background music validation failed: "
                            f"name={safe_name}, error={exc!s}"
                        )
                    else:
                        cached_validation = {
                            "key": validation_key,
                            "error": "",
                            "error_type": "",
                        }
                    st.session_state["custom_bgm_validation"] = cached_validation

                if cached_validation.get("error"):
                    if cached_validation.get("error_type") == "service":
                        raise bgm_service.BgmServiceError(cached_validation["error"])
                    raise bgm_service.BgmUploadError(cached_validation["error"])
            except bgm_service.BgmUploadError:
                # Illegal files cannot inherit the name of the last valid upload, otherwise the task parameters may still point to
                # Historical BGM. Keep the UploadedFile return value so that it will still be finalized when the user clicks Generate
                # The server verifies the interception instead of silently generating a video without background music.
                params.bgm_file = ""
                st.error(tr("Invalid Background Music"))
            except bgm_service.BgmServiceError:
                params.bgm_file = ""
                st.error(tr("Background Music Validation Failed"))
            else:
                # The player and "Ready" will be displayed only after the complete decoding verification is passed. Files are still only clicking
                # Persisted on build, user merely previewing or subsequently removing files does not pollute storage/bgm.
                uploaded_mime_type = str(getattr(uploaded_bgm_file, "type", "") or "")
                preview_mime_type = (
                    uploaded_mime_type
                    if uploaded_mime_type.startswith("audio/")
                    else mimetypes.guess_type(safe_name)[0] or "audio/mpeg"
                )
                st.audio(uploaded_bgm_file, format=preview_mime_type)
                st.info(f"{tr('Background Music Ready')}: {safe_name}")
                params.bgm_file = safe_name

        # Streamlit cleans up the widget state of a conditional widget when it is temporarily not rendering.
        # Use the persisted value to restore when switching back from other BGM sources; under the same source
        # The previous_bgm_type does not change when the user actively clears it, so it will not be bounced by the old value.
        if previous_bgm_type != "custom":
            st.session_state["custom_bgm_file_input"] = _saved_ui_text(
                "custom_bgm_file"
            )
        custom_bgm_file = st.text_input(
            tr("Custom Background Music File"),
            key="custom_bgm_file_input",
            disabled=uploaded_bgm_file is not None,
        )
        _set_runtime_config(
            "ui", "custom_bgm_file", custom_bgm_file.strip()
        )
        if uploaded_bgm_file is None and custom_bgm_file and bgm_enabled:
            # The file name is mapped to storage/bgm or resource/songs by the service layer and then verified.
            # The UI does not accept any paths outside of the two whitelisted directories.
            params.bgm_file = custom_bgm_file.strip()
        elif not bgm_enabled:
            # The upload control continues to retain the files selected by the user, and the next rerun after turning up the volume will automatically
            # Complete verification; the current task parameters must be cleared to prevent the 0 volume task from saving or parsing the file.
            params.bgm_file = ""

    if params.bgm_type == "preset":
        # The service layer has uniformly completed extension, temporary file and symbolic link verification. Directly reuse it here
        # As a result, the UI is prevented from maintaining a second set of enumeration rules, and differences will not occur when subsequent formats are added.
        available_song_paths = bgm_service.list_builtin_bgm_files()
        songs_by_name = {
            os.path.basename(song_path): song_path for song_path in available_song_paths
        }
        available_songs = list(songs_by_name)
        if not available_songs:
            st.warning(tr("No Background Music Available"))
            params.bgm_file = ""
        else:
            default_preset_song = _saved_ui_text("preset_song", available_songs[0])
            requested_preset_song = st.session_state.get(
                localized_widget_key("preset_song_select"), default_preset_song
            )
            if requested_preset_song not in available_songs:
                # Settings exported from historical missions or other versions may reference songs that do not exist in the current installation.
                # After a clear prompt, stable_selectbox will revert to the first song to avoid silently changing songs.
                st.warning(tr("Selected Background Music Unavailable"))
            selected_song = stable_selectbox(
                tr("Preset Song"),
                options=available_songs,
                default_value=(
                    default_preset_song
                    if default_preset_song in available_songs
                    else available_songs[0]
                ),
                key="preset_song_select",
            )
            _set_runtime_config("ui", "preset_song", selected_song)
            # Online listening is provided immediately after the user selects the song. The player reads the data just passed through the service layer
            # The real path obtained by whitelist verification does not accept any file path entered on the page.
            selected_song_path = songs_by_name[selected_song]
            preview_mime_type = (
                mimetypes.guess_type(selected_song_path)[0] or "audio/mpeg"
            )
            preview_available = True
            try:
                # When Streamlit fails to read the path, it will wrap OSError into an internal exception, resulting in the following
                # Unable to handle by file error. Read the bytes yourself first, which not only maintains the player behavior, but also allows
                # Docker mounts that temporarily fail, permissions change, and other situations fall steadily into controllable branches.
                selected_song_bytes = Path(selected_song_path).read_bytes()
            except OSError as exc:
                preview_available = False
                # Files may be deleted by other processes after enumeration. If the audition fails, the page or video cannot be interrupted.
                # Parameter editing, but logs need to be kept to locate running environment and mounting problems.
                logger.warning(
                    "failed to preview preset background music: "
                    f"name={selected_song}, error={exc!s}"
                )
                st.warning(tr("Background Music Preview Failed"))
            else:
                st.audio(selected_song_bytes, format=preview_mime_type)
            if bgm_enabled and preview_available:
                params.bgm_file = selected_song
            else:
                params.bgm_file = ""

    if params.bgm_type == "sonilo":
        if previous_bgm_type != "sonilo":
            st.session_state["sonilo_bgm_prompt_input"] = _saved_ui_text(
                "sonilo_bgm_prompt",
                max_length=sonilo_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("Sonilo Music Prompt"),
            key="sonilo_bgm_prompt_input",
            max_chars=sonilo_service.MAX_PROMPT_LENGTH,
            help=tr("Sonilo Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "sonilo_bgm_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("Sonilo Multiple Videos Warning"))
        if st.button(
            tr("Test Sonilo Connection"),
            key="test_sonilo_connection_button",
            use_container_width=True,
        ):
            try:
                sonilo_service.test_connection()
            except sonilo_service.SoniloError as exc:
                logger.warning(f"Sonilo connection test failed: {exc}")
                st.error(tr("Sonilo Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("Sonilo Connection Test Succeeded"))
    elif params.bgm_type == "elevenlabs":
        if previous_bgm_type != "elevenlabs":
            st.session_state["elevenlabs_music_prompt_input"] = _saved_ui_text(
                "elevenlabs_music_prompt",
                max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("ElevenLabs Music Prompt"),
            key="elevenlabs_music_prompt_input",
            max_chars=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            help=tr("ElevenLabs Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "elevenlabs_music_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("ElevenLabs Multiple Videos Warning"))
        if st.button(
            tr("Test ElevenLabs Connection"),
            key="test_elevenlabs_music_connection_button",
            use_container_width=True,
        ):
            try:
                elevenlabs_music_service.test_connection()
            except elevenlabs_music_service.ElevenLabsPaidPlanRequiredError:
                st.error(tr("ElevenLabs Paid Plan Required"))
            except elevenlabs_music_service.ElevenLabsMusicError as exc:
                logger.warning(f"ElevenLabs connection test failed: {exc}")
                st.error(tr("ElevenLabs Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("ElevenLabs Connection Test Succeeded"))
    if params.bgm_type == "sonilo" and bgm_enabled and not sonilo_service.is_enabled():
        # The task layer does not generate or mix the Sonilo soundtrack at volume 0, so no Key prompt is needed;
        # This judgment shares service layer rules with the task entry to avoid bifurcation between interface prompts and actual execution conditions.
        st.warning(tr("Sonilo API Key Required"))
    elif (
        params.bgm_type == "elevenlabs"
        and bgm_enabled
        and not elevenlabs_music_service.is_enabled()
    ):
        st.warning(tr("ElevenLabs API Key Required"))
    st.session_state["last_rendered_bgm_type"] = params.bgm_type
    return uploaded_bgm_file


def _render_audio_settings(panel, params):
    """Render audio settings and return uploaded audio and current dubbing mode."""
    with panel:
        with st.container(border=True):
            st.write(tr("Audio Settings"))

            # Dubbing mode is the first-level status of audio settings, responsible for clearly distinguishing automatic dubbing, user uploading and no dubbing.
            # When the old configuration does not have voice_mode, the voice-free sentinel according to the original tts_server remains compatible.
            saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
            saved_voice_mode = config.ui.get("voice_mode")
            if saved_voice_mode not in {
                VOICE_MODE_TTS,
                VOICE_MODE_UPLOAD,
                VOICE_MODE_NONE,
            }:
                saved_voice_mode = (
                    VOICE_MODE_NONE
                    if saved_tts_server == voice.NO_VOICE_NAME
                    else VOICE_MODE_TTS
                )
            voice_mode_options = [VOICE_MODE_TTS, VOICE_MODE_UPLOAD, VOICE_MODE_NONE]
            voice_mode_labels = {
                VOICE_MODE_TTS: tr("Automatic Voiceover"),
                VOICE_MODE_UPLOAD: tr("Upload Voiceover"),
                VOICE_MODE_NONE: tr("No Voiceover"),
            }
            voice_mode = stable_segmented_control(
                tr("Voiceover Mode"),
                options=voice_mode_options,
                default_value=saved_voice_mode,
                key="voice_mode_control",
                format_func=lambda value: voice_mode_labels[value],
                width="stretch",
            )
            _set_runtime_config("ui", "voice_mode", voice_mode)
            tts_mode_enabled = voice_mode == VOICE_MODE_TTS

            # The Provider drop-down is only responsible for selecting the automatic dubbing service; no dubbing is already controlled by the upper mode.
            # It is no longer mixed into the list as a TTS Provider to avoid two entries expressing the same state.
            tts_servers = [
                ("azure-tts-v1", "Azure TTS V1 (Edge TTS)"),
                ("azure-tts-v2", "Azure TTS V2"),
                ("siliconflow", "SiliconFlow TTS"),
                ("gemini-tts", "Google Gemini TTS"),
                ("mimo-tts", "Xiaomi MiMo TTS"),
                ("minimax-tts", "MiniMax TTS"),
                ("elevenlabs", "ElevenLabs TTS"),
                ("chatterbox", "Chatterbox TTS"),
                ("kokoro", "Kokoro TTS"),
                ("fish_audio", "Fish Audio TTS"),
                ("voxcpm", "VoxCPM TTS"),
            ]

            tts_server_values = [server_value for server_value, _ in tts_servers]
            if saved_tts_server not in tts_server_values:
                saved_tts_server = "azure-tts-v1"

            if tts_mode_enabled:
                selected_tts_server = stable_selectbox(
                    tr("Voiceover Service"),
                    options=tts_server_values,
                    default_value=saved_tts_server,
                    key="tts_server_select",
                    format_func=lambda value: dict(
                        (v, label) for v, label in tts_servers
                    )[value],
                )
            else:
                # Non-automatic dubbing mode does not render the TTS control, but retains the last selection and can continue to use it after switching back.
                selected_tts_server = saved_tts_server

            _set_runtime_config("ui", "tts_server", selected_tts_server)

            # The service description follows the Provider selection, first telling the user what needs to be prepared, and then entering the timbre and
            # Credential configuration. Providers without description do not render empty hint blocks.
            if tts_mode_enabled:
                provider_tips = get_tts_provider_tips(selected_tts_server)
                if provider_tips:
                    st.info(provider_tips)

            # MiniMax just reuses the generic "Dub Sound" selector below. Provider configuration function is responsible for
            # Refresh the remote voice and return to friendly text, without rendering the Voice ID and voice drop-down boxes.
            minimax_voices = []
            minimax_voice_labels = {}
            if tts_mode_enabled and selected_tts_server == "minimax-tts":
                minimax_voices, minimax_voice_labels = _render_minimax_tts_settings()

            # Get the sound list based on the selected TTS server
            filtered_voices = []
            saved_voice_name = config.ui.get("voice_name", "")
            elevenlabs_api_key_rendered = False

            if not tts_mode_enabled:
                # Upload audio and non-dubbing mode do not load remote sounds, reducing meaningless network requests and interface noise.
                filtered_voices = []
            elif selected_tts_server == "siliconflow":
                # Get a list of silicon-based flowing sounds
                filtered_voices = voice.get_siliconflow_voices()
            elif selected_tts_server == "gemini-tts":
                # Get the sound list for Gemini TTS
                filtered_voices = voice.get_gemini_voices()
            elif selected_tts_server == "mimo-tts":
                # Get the preset tone list for Xiaomi MiMo TTS
                filtered_voices = voice.get_mimo_voices()
            elif selected_tts_server == "minimax-tts":
                filtered_voices = minimax_voices
            elif selected_tts_server == "elevenlabs":
                # The timbre list is rendered before the Key input box. It must be restored to the reconnection state and read.
                # Configuration/environment variables, otherwise the page will load and cache an empty sound list with an empty Key.
                saved_elevenlabs_api_key = _sync_elevenlabs_api_key_input()
                cache_key = f"elevenlabs_voices_{saved_elevenlabs_api_key}"
                if cache_key not in st.session_state:
                    st.session_state[cache_key] = voice.get_elevenlabs_voices(
                        saved_elevenlabs_api_key
                    )
                filtered_voices = st.session_state[cache_key]
            elif selected_tts_server == "chatterbox":
                # Preset voices for self-hosted Chatterbox services (from [chatterbox] voices configuration)
                _sync_chatterbox_config_from_session_state()
                filtered_voices = voice.get_chatterbox_voices()
            elif selected_tts_server == "kokoro":
                # Voices for self-hosted Kokoro services: [kokoro] Read from server /audio/voices when voices is empty
                _sync_kokoro_config_from_session_state()
                filtered_voices = _get_kokoro_voice_options(saved_voice_name)
            elif selected_tts_server == "fish_audio":
                filtered_voices = voice.get_fish_audio_voices()
            elif selected_tts_server == "voxcpm":
                filtered_voices = voice.get_voxcpm_voices()
            else:
                # Get Azure's sound list
                all_voices = voice.get_all_azure_voices(filter_locals=None)

                # Filter sounds based on selected TTS server
                for v in all_voices:
                    if selected_tts_server == "azure-tts-v2":
                        # V2 versions of sounds contain "v2" in their names
                        if "V2" in v:
                            filtered_voices.append(v)
                    else:
                        # The V1 version of the sound does not contain "v2" in its name
                        if "V2" not in v:
                            filtered_voices.append(v)

            def _friendly(v):
                if voice.is_no_voice(v):
                    return tr("No Voice Selected")
                if voice.is_elevenlabs_voice(v):
                    parts = v.split(":", 2)
                    return parts[2] if len(parts) >= 3 else v
                if voice.is_chatterbox_voice(v) or voice.is_kokoro_voice(v):
                    name = v.split(":", 1)[1] if ":" in v else v
                    return name.replace("-Female", "").replace("-Male", "")
                if voice.is_minimax_voice(v):
                    return minimax_voice_labels.get(v, v.split(":", 1)[1])
                if voice.is_fish_audio_voice(v):
                    parts = v.split(":", 2)
                    display_name = parts[2] if len(parts) >= 3 else v
                    return (
                        display_name.replace("Female", tr("Female"))
                        .replace("Male", tr("Male"))
                    )
                if voice.is_voxcpm_voice(v):
                    return v.split(":", 1)[1] or DEFAULT_VOXCPM_VOICE
                return (
                    v.replace("Female", tr("Female"))
                    .replace("Male", tr("Male"))
                    .replace("Neural", "")
                )

            friendly_names = {v: _friendly(v) for v in filtered_voices}

            # Gemini old catalogs put the presumed gender in the value (e.g. Charon-Male). According to basics
            # The voice name is mapped to the new official style value, and the user's original voice will be retained after the upgrade.
            if (
                selected_tts_server == "gemini-tts"
                and saved_voice_name not in friendly_names
            ):
                saved_gemini_voice = voice.parse_gemini_voice_name(saved_voice_name)
                saved_voice_name = next(
                    (
                        candidate
                        for candidate in filtered_voices
                        if voice.parse_gemini_voice_name(candidate)
                        == saved_gemini_voice
                    ),
                    saved_voice_name,
                )

            saved_voice_name_index = 0

            # Check if the saved sound is in the currently filtered sound list
            if saved_voice_name in friendly_names:
                saved_voice_name_index = list(friendly_names.keys()).index(
                    saved_voice_name
                )
            else:
                # If not, selects a default voice based on the current UI language
                for i, v in enumerate(filtered_voices):
                    if v.lower().startswith(st.session_state["ui_language"].lower()):
                        saved_voice_name_index = i
                        break

            # If no matching sound is found, the first sound is used
            if saved_voice_name_index >= len(friendly_names) and friendly_names:
                saved_voice_name_index = 0

            # Make sure there is a sound option
            if tts_mode_enabled and friendly_names:
                voice_name = stable_selectbox(
                    tr("Voiceover Voice"),
                    options=list(friendly_names.keys()),
                    default_value=list(friendly_names.keys())[saved_voice_name_index],
                    key=f"speech_synthesis_select_{selected_tts_server}",
                    format_func=lambda value: friendly_names.get(
                        value,
                        str(value).removeprefix("minimax:"),
                    ),
                    # MiniMax supports users to directly enter clones outside the list or generate sound IDs; others
                    # Provider maintains the original selector behavior and does not expand the scope of influence of this modification.
                    accept_new_options=selected_tts_server == "minimax-tts",
                )

                if selected_tts_server == "minimax-tts":
                    custom_voice_id = str(voice_name or "").strip()
                    if custom_voice_id and not voice.is_minimax_voice(custom_voice_id):
                        voice_name = f"minimax:{custom_voice_id}"
                    if voice.is_minimax_voice(voice_name):
                        _set_runtime_config(
                            "minimax_tts",
                            "voice_id",
                            voice_name.split(":", 1)[1],
                        )

                params.voice_name = voice_name
                if not voice.is_no_voice(voice_name):
                    # The placeholder sentinel is only used for disabled display in non-automatic mode and does not overwrite the user's previous
                    # The actual selected tone can be restored to its original setting after switching back to automatic dubbing.
                    _set_runtime_config("ui", "voice_name", voice_name)
            elif tts_mode_enabled:
                # If there is no sound available, a prompt message is displayed.
                st.warning(
                    tr(
                        "No voices available for the selected TTS server. Please select another server."
                    )
                )
                voice_name = ""
                params.voice_name = ""
                _set_runtime_config("ui", "voice_name", "")
            else:
                # The non-automatic dubbing mode does not display the timbre controls, and only reuses the saved values to maintain a stable parameter structure.
                voice_name = saved_voice_name or voice.NO_VOICE_NAME
                params.voice_name = voice_name

            # When the V2 version is selected or the sound is V2 sound, the service area and API key input box are displayed.
            if tts_mode_enabled and (
                selected_tts_server == "azure-tts-v2"
                or (voice_name and voice.is_azure_v2_voice(voice_name))
            ):
                saved_azure_speech_region = config.azure.get("speech_region", "")
                saved_azure_speech_key = config.azure.get("speech_key", "")
                azure_speech_region = st.text_input(
                    tr("Speech Region"),
                    value=saved_azure_speech_region,
                    key="azure_speech_region_input",
                )
                azure_speech_key = st.text_input(
                    tr("Speech Key"),
                    value=saved_azure_speech_key,
                    type="password",
                    key="azure_speech_key_input",
                )
                _set_runtime_config("azure", "speech_region", azure_speech_region)
                _set_runtime_config("azure", "speech_key", azure_speech_key)

            if tts_mode_enabled and selected_tts_server == "gemini-tts":
                # Gemini TTS and Gemini LLM share the same key; provide direct access in the audio panel,
                # Users do not need to switch LLM Providers first to complete voice configuration.
                gemini_tts_api_key = st.text_input(
                    tr("Gemini API Key"),
                    value=config.app.get("gemini_api_key", ""),
                    type="password",
                    key="gemini_tts_api_key_input",
                )
                _set_runtime_config("app", "gemini_api_key", gemini_tts_api_key)

            # When silicon-based flow is selected, the API key input box and description information are displayed.
            if tts_mode_enabled and (
                selected_tts_server == "siliconflow"
                or (voice_name and voice.is_siliconflow_voice(voice_name))
            ):
                saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

                siliconflow_api_key = st.text_input(
                    tr("SiliconFlow API Key"),
                    value=saved_siliconflow_api_key,
                    type="password",
                    key="siliconflow_api_key_input",
                )

                _set_runtime_config("siliconflow", "api_key", siliconflow_api_key)

            # When Xiaomi MiMo TTS is selected, the API Key of MiMo LLM provider is reused.
            # In this way, if users use MiMo to generate copywriting and speech at the same time, they only need to maintain one key.
            if tts_mode_enabled and (
                selected_tts_server == "mimo-tts"
                or (voice_name and voice.is_mimo_voice(voice_name))
            ):
                saved_mimo_api_key = config.app.get("mimo_api_key", "")

                mimo_api_key = st.text_input(
                    tr("MiMo API Key"),
                    value=saved_mimo_api_key,
                    type="password",
                    key="mimo_tts_api_key_input",
                )

                _set_runtime_config("app", "mimo_api_key", mimo_api_key)

            # ElevenLabs API key section
            if tts_mode_enabled and (
                selected_tts_server == "elevenlabs"
                or (voice_name and voice.is_elevenlabs_voice(voice_name))
            ):
                _render_elevenlabs_api_key_input(
                    "ElevenLabs API Key",
                )
                elevenlabs_api_key_rendered = True

                _elevenlabs_models = [
                    "eleven_multilingual_v2",
                    "eleven_flash_v2_5",
                    "eleven_v3",
                ]
                saved_elevenlabs_model = config.elevenlabs.get(
                    "model_id", "eleven_multilingual_v2"
                )
                if saved_elevenlabs_model not in _elevenlabs_models:
                    saved_elevenlabs_model = "eleven_multilingual_v2"
                elevenlabs_model = stable_selectbox(
                    tr("ElevenLabs Model"),
                    options=_elevenlabs_models,
                    default_value=saved_elevenlabs_model,
                    key="elevenlabs_model_select",
                )
                _set_runtime_config("elevenlabs", "model_id", elevenlabs_model)

            # Fish Audio API settings section
            if tts_mode_enabled and (
                selected_tts_server == "fish_audio"
                or (voice_name and voice.is_fish_audio_voice(voice_name))
            ):
                saved_fish_api_key = (
                    config.fish_audio.get("api_key", "")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else ""
                )
                fish_audio_api_key = st.text_input(
                    tr("Fish Audio API Key"),
                    value=saved_fish_api_key,
                    type="password",
                    key="fish_audio_api_key_input",
                )
                _set_runtime_config("fish_audio", "api_key", fish_audio_api_key)

                _fish_audio_models = [
                    "s2.1-pro-free",
                    "s2.1-pro",
                    "s2-pro",
                ]
                saved_fish_model = (
                    config.fish_audio.get("model", "s2.1-pro-free")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else "s2.1-pro-free"
                )
                if saved_fish_model not in _fish_audio_models:
                    saved_fish_model = "s2.1-pro-free"
                fish_model = stable_selectbox(
                    tr("Fish Audio Model"),
                    options=_fish_audio_models,
                    default_value=saved_fish_model,
                    key="fish_audio_model_select",
                )
                _set_runtime_config("fish_audio", "model", fish_model)

            # ModelBest hosts VoxCPM behind its streaming Audio Speech API.
            # The fixed provider endpoint is still editable for compatible
            # gateways, while the user only has to supply an API key and a
            # speech_synthesis-capable model id for the standard platform.
            if tts_mode_enabled and (
                selected_tts_server == "voxcpm"
                or (voice_name and voice.is_voxcpm_voice(voice_name))
            ):
                _sync_voxcpm_api_key_input()
                voxcpm_api_key = st.text_input(
                    tr("VoxCPM API Key"),
                    type="password",
                    key="voxcpm_api_key_input",
                )
                _set_runtime_config("voxcpm", "api_key", voxcpm_api_key)

                voxcpm_model = st.text_input(
                    tr("VoxCPM Model ID"),
                    value=config.voxcpm.get("model_id", ""),
                    key="voxcpm_model_id_input",
                    placeholder=tr("VoxCPM Model ID Placeholder"),
                )
                _set_runtime_config("voxcpm", "model_id", (voxcpm_model or "").strip())

                voxcpm_base_url = st.text_input(
                    tr("VoxCPM Base URL"),
                    value=config.voxcpm.get("base_url") or DEFAULT_VOXCPM_BASE_URL,
                    key="voxcpm_base_url_input",
                    placeholder=DEFAULT_VOXCPM_BASE_URL,
                )
                _set_runtime_config(
                    "voxcpm",
                    "base_url",
                    (voxcpm_base_url or DEFAULT_VOXCPM_BASE_URL).strip().rstrip("/"),
                )

                uploaded_reference_audio = st.file_uploader(
                    tr("VoxCPM Reference Audio"),
                    type=list(voice.VOXCPM_REFERENCE_AUDIO_FILE_TYPES),
                    key="voxcpm_reference_audio_uploader",
                    help=tr("VoxCPM Reference Audio Help"),
                )
                reference_audio = _sync_voxcpm_reference_audio(
                    uploaded_reference_audio
                )
                st.caption(tr("VoxCPM Reference Audio Notice"))
                reference_audio_error = st.session_state.get(
                    VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY
                )
                if reference_audio_error:
                    st.error(
                        tr("VoxCPM Reference Audio Invalid").format(
                            error=reference_audio_error
                        )
                    )

                if reference_audio:
                    high_fidelity_enabled = st.toggle(
                        tr("VoxCPM High Fidelity Delivery"),
                        key=VOXCPM_HIGH_FIDELITY_SESSION_KEY,
                        help=tr("VoxCPM High Fidelity Delivery Help"),
                    )
                    if high_fidelity_enabled:
                        use_separate_prompt_audio = st.toggle(
                            tr("VoxCPM Separate Prompt Audio"),
                            key=VOXCPM_SEPARATE_PROMPT_AUDIO_SESSION_KEY,
                            help=tr("VoxCPM Separate Prompt Audio Help"),
                        )
                        _sync_voxcpm_prompt_example_mode(use_separate_prompt_audio)
                        if use_separate_prompt_audio:
                            prompt_audio_file = st.file_uploader(
                                tr("VoxCPM Prompt Audio"),
                                type=list(voice.VOXCPM_REFERENCE_AUDIO_FILE_TYPES),
                                key="voxcpm_prompt_audio_uploader",
                                help=tr("VoxCPM Prompt Audio Help"),
                            )
                            _sync_voxcpm_prompt_audio(prompt_audio_file)
                        else:
                            _clear_voxcpm_separate_prompt_audio()
                        effective_prompt_audio = _get_voxcpm_effective_prompt_audio()
                        if effective_prompt_audio is not None and st.button(
                            tr("Transcribe VoxCPM Prompt Audio"),
                            key="transcribe_voxcpm_prompt_audio_button",
                            icon=":material/transcribe:",
                            help=tr("Transcribe VoxCPM Prompt Audio Help"),
                            use_container_width=True,
                        ):
                            try:
                                with st.spinner(tr("Transcribing VoxCPM Prompt Audio")):
                                    recognized_text = subtitle.transcribe_audio_bytes(
                                        effective_prompt_audio
                                    )
                            except Exception:
                                logger.exception(
                                    "failed to transcribe VoxCPM prompt audio"
                                )
                                recognized_text = ""
                            if recognized_text:
                                st.session_state[VOXCPM_PROMPT_TEXT_SESSION_KEY] = (
                                    recognized_text
                                )
                                st.toast(tr("VoxCPM Prompt Audio Transcribed"))
                            else:
                                st.error(tr("VoxCPM Prompt Audio Transcription Failed"))
                        st.text_area(
                            tr("VoxCPM Prompt Text"),
                            key=VOXCPM_PROMPT_TEXT_SESSION_KEY,
                            help=tr("VoxCPM Prompt Text Help"),
                            height=100,
                        )
                        st.caption(tr("VoxCPM Prompt Transcript Review"))
                        prompt_error = _get_voxcpm_prompt_validation_error()
                        if prompt_error:
                            st.error(
                                tr("VoxCPM Prompt Invalid").format(
                                    error=prompt_error
                                )
                            )
                    else:
                        _clear_voxcpm_prompt_state()

            # Chatterbox API settings section (self-hosted, OpenAI-compatible)
            if tts_mode_enabled and (
                selected_tts_server == "chatterbox"
                or (voice_name and voice.is_chatterbox_voice(voice_name))
            ):
                chatterbox_base_url = st.text_input(
                    tr("Chatterbox Base URL"),
                    value=config.chatterbox.get("base_url")
                    or DEFAULT_CHATTERBOX_BASE_URL,
                    key="chatterbox_base_url_input",
                    placeholder=tr("Chatterbox Base URL Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox", "base_url", (chatterbox_base_url or "").strip()
                )

                chatterbox_api_key = st.text_input(
                    tr("Chatterbox API Key"),
                    value=config.chatterbox.get("api_key", ""),
                    type="password",
                    key="chatterbox_api_key_input",
                )
                _set_runtime_config("chatterbox", "api_key", chatterbox_api_key)

                chatterbox_model = st.text_input(
                    tr("Chatterbox Model"),
                    value=config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
                    key="chatterbox_model_input",
                )
                _set_runtime_config(
                    "chatterbox",
                    "model_id",
                    (chatterbox_model or DEFAULT_CHATTERBOX_MODEL).strip(),
                )

                _saved_chatterbox_voices = (
                    _parse_chatterbox_voices(config.chatterbox.get("voices"))
                    or DEFAULT_CHATTERBOX_VOICES
                )
                if isinstance(_saved_chatterbox_voices, list):
                    _saved_chatterbox_voices = ", ".join(_saved_chatterbox_voices)
                chatterbox_voices = st.text_input(
                    tr("Chatterbox Voices"),
                    value=str(_saved_chatterbox_voices or ""),
                    key="chatterbox_voices_input",
                    placeholder=tr("Chatterbox Voices Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox",
                    "voices",
                    _parse_chatterbox_voices(chatterbox_voices),
                )

            # Kokoro API settings section (self-hosted, OpenAI-compatible; voices listed from the server when left empty)
            if tts_mode_enabled and (
                selected_tts_server == "kokoro"
                or (voice_name and voice.is_kokoro_voice(voice_name))
            ):
                kokoro_base_url = st.text_input(
                    tr("Kokoro Base URL"),
                    value=config.kokoro.get("base_url")
                    or DEFAULT_KOKORO_BASE_URL,
                    key="kokoro_base_url_input",
                    placeholder=tr("Kokoro Base URL Placeholder"),
                )
                _set_runtime_config(
                    "kokoro", "base_url", (kokoro_base_url or "").strip()
                )

                kokoro_api_key = st.text_input(
                    tr("Kokoro API Key"),
                    value=config.kokoro.get("api_key", ""),
                    type="password",
                    key="kokoro_api_key_input",
                )
                _set_runtime_config("kokoro", "api_key", kokoro_api_key)

                kokoro_model = st.text_input(
                    tr("Kokoro Model"),
                    value=config.kokoro.get("model_id") or DEFAULT_KOKORO_MODEL,
                    key="kokoro_model_input",
                )
                _set_runtime_config(
                    "kokoro",
                    "model_id",
                    (kokoro_model or DEFAULT_KOKORO_MODEL).strip(),
                )

                _saved_kokoro_voices = (
                    _parse_chatterbox_voices(config.kokoro.get("voices"))
                    or DEFAULT_KOKORO_VOICES
                )
                if isinstance(_saved_kokoro_voices, list):
                    _saved_kokoro_voices = ", ".join(_saved_kokoro_voices)
                kokoro_voices = st.text_input(
                    tr("Kokoro Voices"),
                    value=str(_saved_kokoro_voices or ""),
                    key="kokoro_voices_input",
                    placeholder=tr("Kokoro Voices Placeholder"),
                )
                _set_runtime_config(
                    "kokoro",
                    "voices",
                    _parse_chatterbox_voices(kokoro_voices),
                )

            # The three modes only render the controls really needed for the current task. Automatic dubbing with adjustable volume and speaking speed;
            # Uploading audio only requires file and volume; no dubbing will no longer display invalid settings.
            params.voice_name = (
                voice.NO_VOICE_NAME if voice_mode == VOICE_MODE_NONE else voice_name
            )
            params.voice_volume = 1.0
            params.voice_rate = 1.0
            uploaded_audio_file = None
            voice_volume_options = [0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0]
            voice_rate_options = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0]

            if tts_mode_enabled:
                voice_control_cols = st.columns(2)
                with voice_control_cols[0]:
                    params.voice_volume = stable_selectbox(
                        tr("Voiceover Volume"),
                        options=voice_volume_options,
                        default_value=_saved_ui_choice(
                            "voice_volume", voice_volume_options, 1.0
                        ),
                        key="voice_volume_select",
                        format_func=lambda value: f"{int(value * 100)}%",
                        help=tr("Voiceover Volume Help"),
                    )

                with voice_control_cols[1]:
                    is_voxcpm = bool(
                        selected_tts_server == "voxcpm"
                        or (voice_name and voice.is_voxcpm_voice(voice_name))
                    )
                    params.voice_rate = stable_selectbox(
                        tr("Voiceover Speed"),
                        options=voice_rate_options,
                        default_value=_saved_ui_choice(
                            "voice_rate", voice_rate_options, 1.0
                        ),
                        key="voice_rate_select",
                        format_func=lambda value: f"{value:.1f}×",
                        help=(
                            tr("VoxCPM Speed Not Supported")
                            if is_voxcpm
                            else tr("Voiceover Speed Help")
                        ),
                        disabled=is_voxcpm,
                    )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                _set_runtime_config("ui", "voice_rate", params.voice_rate)

                # Audition must be placed after the volume and speech rate controls, ensuring that the call uses the current control values.
                _render_voice_preview(
                    params,
                    friendly_names,
                    selected_tts_server,
                    voice_name,
                )
            elif voice_mode == VOICE_MODE_UPLOAD:
                custom_audio_file_types = sorted(
                    extension.removeprefix(".") for extension in CUSTOM_AUDIO_EXTENSIONS
                )
                uploaded_audio_file = st.file_uploader(
                    tr("Upload Voiceover File"),
                    type=custom_audio_file_types
                    + [file_type.upper() for file_type in custom_audio_file_types],
                    accept_multiple_files=False,
                    key="custom_audio_file_uploader",
                    help=tr("Upload Voiceover File Help"),
                )
                params.voice_volume = stable_selectbox(
                    tr("Voiceover Volume"),
                    options=voice_volume_options,
                    default_value=_saved_ui_choice(
                        "voice_volume", voice_volume_options, 1.0
                    ),
                    key="voice_volume_select",
                    format_func=lambda value: f"{int(value * 100)}%",
                    help=tr("Voiceover Volume Help"),
                )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                if uploaded_audio_file:
                    st.audio(uploaded_audio_file, format="audio/mp3")
                    st.info(
                        tr(
                            "Custom audio will be used directly. TTS synthesis will be skipped for this task."
                        )
                    )
            uploaded_bgm_file = _render_background_music_settings(
                params,
                elevenlabs_api_key_rendered=elevenlabs_api_key_rendered,
            )
    return uploaded_audio_file, uploaded_bgm_file, voice_mode


def _render_subtitle_settings(panel, params):
    """Render subtitle settings and update generation parameters."""
    with panel:
        with st.container(border=True):
            st.write(tr("Subtitle Settings"))
            st.session_state.setdefault(
                "subtitle_enabled_checkbox",
                _saved_ui_bool(
                    "subtitle_enabled",
                    DEFAULT_SUBTITLE_SETTINGS["subtitle_enabled"],
                ),
            )
            params.subtitle_enabled = st.checkbox(
                tr("Enable Subtitles"),
                key="subtitle_enabled_checkbox",
            )
            _set_runtime_config("ui", "subtitle_enabled", params.subtitle_enabled)
            subtitle_settings_disabled = not params.subtitle_enabled
            font_names = get_all_fonts()
            saved_font_name = config.ui.get(
                "font_name", DEFAULT_SUBTITLE_SETTINGS["font_name"]
            )
            saved_font_name_index = 0
            if saved_font_name in font_names:
                saved_font_name_index = font_names.index(saved_font_name)
            params.font_name = stable_selectbox(
                tr("Font"),
                options=font_names,
                default_value=font_names[saved_font_name_index] if font_names else "",
                key="font_name_select",
                disabled=subtitle_settings_disabled,
            )
            _set_runtime_config("ui", "font_name", params.font_name)

            subtitle_positions = [
                (tr("Top"), "top"),
                (tr("Center"), "center"),
                (tr("Bottom"), "bottom"),
                (tr("2/3 from Bottom"), "two_thirds_bottom"),
                (tr("Custom"), "custom"),
            ]
            saved_subtitle_position = config.ui.get(
                "subtitle_position", DEFAULT_SUBTITLE_SETTINGS["subtitle_position"]
            )
            saved_position_index = 2
            for i, (_, pos_value) in enumerate(subtitle_positions):
                if pos_value == saved_subtitle_position:
                    saved_position_index = i
                    break
            selected_subtitle_position = stable_selectbox(
                tr("Position"),
                options=[value for _, value in subtitle_positions],
                default_value=subtitle_positions[saved_position_index][1],
                key="subtitle_position_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_positions
                ).get(value, value),
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_position = selected_subtitle_position
            _set_runtime_config("ui", "subtitle_position", params.subtitle_position)

            # Subtitle Display Mode (Sentence vs Single Word)
            subtitle_display_modes = [
                (tr("Sentence by Sentence"), "sentence"),
                (tr("Single Word (Word by Word)"), "word_by_word"),
            ]
            saved_display_mode = config.ui.get(
                "subtitle_display_mode",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_display_mode"],
            )
            saved_mode_idx = 0
            for i, (_, mode_val) in enumerate(subtitle_display_modes):
                if mode_val == saved_display_mode:
                    saved_mode_idx = i
                    break
            selected_display_mode = stable_selectbox(
                tr("Display Mode"),
                options=[val for _, val in subtitle_display_modes],
                default_value=subtitle_display_modes[saved_mode_idx][1],
                key="subtitle_display_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_display_modes
                ).get(value, value),
                help=tr("Word-by-word Timing Help"),
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_display_mode = selected_display_mode
            _set_runtime_config(
                "ui", "subtitle_display_mode", params.subtitle_display_mode
            )

            # Subtitle Animation (None vs Pop Spring)
            subtitle_animations = [
                (tr("None (Animation)"), "none"),
                (tr("Pop Up (Spring)"), "pop_spring"),
            ]
            saved_anim = config.ui.get(
                "subtitle_animation",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_animation"],
            )
            saved_anim_idx = 0
            for i, (_, anim_val) in enumerate(subtitle_animations):
                if anim_val == saved_anim:
                    saved_anim_idx = i
                    break
            selected_anim = stable_selectbox(
                tr("Subtitle Animation"),
                options=[val for _, val in subtitle_animations],
                default_value=subtitle_animations[saved_anim_idx][1],
                key="subtitle_animation_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_animations
                ).get(value, value),
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_animation = selected_anim
            _set_runtime_config(
                "ui", "subtitle_animation", params.subtitle_animation
            )

            if params.subtitle_position == "custom":
                saved_custom_position = config.ui.get(
                    "custom_position", DEFAULT_SUBTITLE_SETTINGS["custom_position"]
                )
                st.session_state.setdefault(
                    "custom_position_input", str(saved_custom_position)
                )
                custom_position = st.text_input(
                    tr("Custom Position (% from top)"),
                    key="custom_position_input",
                    disabled=subtitle_settings_disabled,
                )
                try:
                    params.custom_position = float(custom_position)
                    if params.custom_position < 0 or params.custom_position > 100:
                        st.error(tr("Please enter a value between 0 and 100"))
                    else:
                        _set_runtime_config(
                            "ui", "custom_position", params.custom_position
                        )
                except ValueError:
                    st.error(tr("Please enter a valid number"))

            # Color labels for non-Chinese languages are usually longer than for Chinese. Leave appropriate width for color picker,
            # Avoid label wrapping while still leaving enough room for the font size slider to maneuver.
            font_cols = st.columns([0.42, 0.58])
            with font_cols[0]:
                saved_text_fore_color = config.ui.get(
                    "text_fore_color", DEFAULT_SUBTITLE_SETTINGS["text_fore_color"]
                )
                st.session_state.setdefault("font_color_picker", saved_text_fore_color)
                params.text_fore_color = st.color_picker(
                    tr("Font Color"),
                    key="font_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "text_fore_color", params.text_fore_color)

            with font_cols[1]:
                saved_font_size = config.ui.get(
                    "font_size", DEFAULT_SUBTITLE_SETTINGS["font_size"]
                )
                st.session_state.setdefault("font_size_slider", saved_font_size)
                params.font_size = st.slider(
                    tr("Font Size"),
                    30,
                    100,
                    key="font_size_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "font_size", params.font_size)

            stroke_cols = st.columns([0.42, 0.58])
            with stroke_cols[0]:
                st.session_state.setdefault(
                    "stroke_color_picker",
                    _saved_ui_color(
                        "stroke_color", DEFAULT_SUBTITLE_SETTINGS["stroke_color"]
                    ),
                )
                params.stroke_color = st.color_picker(
                    tr("Stroke Color"),
                    key="stroke_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_color", params.stroke_color)
            with stroke_cols[1]:
                st.session_state.setdefault(
                    "stroke_width_slider",
                    _saved_ui_number(
                        "stroke_width",
                        DEFAULT_SUBTITLE_SETTINGS["stroke_width"],
                        0.0,
                        10.0,
                    ),
                )
                params.stroke_width = st.slider(
                    tr("Stroke Width"),
                    0.0,
                    10.0,
                    key="stroke_width_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_width", params.stroke_width)

            # The localized name of the background switch is generally longer than the color label, thus allowing the switch to take up slightly more space.
            subtitle_bg_cols = st.columns([0.55, 0.45])
            saved_subtitle_background_enabled = config.ui.get(
                "subtitle_background_enabled",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_enabled"],
            )
            st.session_state.setdefault(
                "subtitle_background_enabled_checkbox",
                saved_subtitle_background_enabled,
            )
            with subtitle_bg_cols[0]:
                subtitle_background_enabled = st.checkbox(
                    tr("Enable Subtitle Background"),
                    key="subtitle_background_enabled_checkbox",
                    disabled=subtitle_settings_disabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_enabled",
                subtitle_background_enabled,
            )

            # The background color and rounded corner style are both subordinate to the subtitle background switch. Child controls always remain on the page,
            # When the parent switch is turned off, it is disabled uniformly to avoid layout jumping caused by one control disappearing while another control is disabled.
            # Color values are still saved in the UI configuration, and re-enabling the background restores the user's previous selection;
            # The parameter passed to the generation service is set to False to ensure that the off state does not actually render the background.
            saved_subtitle_background_color = config.ui.get(
                "subtitle_background_color",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_color"],
            )
            st.session_state.setdefault(
                "subtitle_background_color_picker",
                saved_subtitle_background_color,
            )
            with subtitle_bg_cols[1]:
                selected_subtitle_background_color = st.color_picker(
                    tr("Subtitle Background Color"),
                    key="subtitle_background_color_picker",
                    disabled=subtitle_settings_disabled
                    or not subtitle_background_enabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_color",
                selected_subtitle_background_color,
            )
            params.text_background_color = (
                selected_subtitle_background_color
                if subtitle_background_enabled
                else False
            )

            saved_rounded_subtitle_background = config.ui.get(
                "rounded_subtitle_background",
                DEFAULT_SUBTITLE_SETTINGS["rounded_subtitle_background"],
            )
            # When background is off, the rounded background has no renderable background. Disable the control here but retain the original configuration.
            # The next time the user re-enables the subtitle background, he or she can continue to use the previously saved rounded corner preference.
            rounded_background_disabled = (
                subtitle_settings_disabled or not subtitle_background_enabled
            )
            st.session_state.setdefault(
                "rounded_subtitle_background_checkbox",
                saved_rounded_subtitle_background,
            )
            selected_rounded_subtitle_background = st.checkbox(
                tr("Rounded Subtitle Background"),
                help=tr("Rounded Subtitle Background Help"),
                disabled=rounded_background_disabled,
                key="rounded_subtitle_background_checkbox",
            )
            params.rounded_subtitle_background = (
                selected_rounded_subtitle_background
                if subtitle_background_enabled
                else False
            )
            if not subtitle_settings_disabled and subtitle_background_enabled:
                _set_runtime_config(
                    "ui",
                    "rounded_subtitle_background",
                    selected_rounded_subtitle_background,
                )

            if video.subtitle_colors_are_indistinguishable(params):
                # The same color configuration is still a legal user choice, so it is only prompted in the subtitle setting area.
                # Does not prevent generation. Users can decide whether to continue based on actual visual needs.
                st.warning(tr("Subtitle Colors Are Indistinguishable"))

            subtitle_preview_text = params.video_script or params.video_subject
            selected_font_path = os.path.join(font_dir, params.font_name)
            if (
                params.subtitle_enabled
                and subtitle_preview_text
                and not video.subtitle_font_supports_text(
                    selected_font_path, subtitle_preview_text
                )
            ):
                st.warning(tr("Subtitle Font Does Not Support Text"))

            if st.button(
                tr("Restore Default Subtitle Settings"),
                key="restore_default_subtitle_settings",
                icon=":material/restart_alt:",
                on_click=reset_subtitle_settings,
                use_container_width=True,
            ):
                st.toast(tr("Default Subtitle Settings Restored"))


def _stage_task_audio(audio_path, audio_bytes):
    """Publish task-local audio only after the complete write succeeds."""
    descriptor = None
    staged_path = None
    try:
        descriptor, staged_path = tempfile.mkstemp(
            dir=os.path.dirname(audio_path), prefix=".task-audio-", suffix=".tmp"
        )
        with os.fdopen(descriptor, "wb") as file:
            descriptor = None  # The file context now owns the descriptor.
            file.write(audio_bytes)
        os.replace(staged_path, audio_path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if staged_path is not None:
            try:
                os.unlink(staged_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(f"failed to remove staged task audio: {exc}")


def _render_generation_controls(
    params, uploaded_files, uploaded_audio_file, uploaded_bgm_file, voice_mode
):
    """
    Verify generated dependencies, submit tasks, and render logs and sharding results.

    Return to this page to check whether the new task was successfully submitted. Non-blocking save has been requested before submission, the caller
    This will skip duplicate requests at the end of the page. The main script must end in time so that the scheduled fragment can continue
    Refresh progress and task logs.
    """
    restore_upload_requirements = st.session_state.get(
        "task_restore_upload_requirements", {}
    )
    has_local_materials = bool(
        uploaded_files or st.session_state.get("local_video_materials", [])
    )
    has_custom_audio = bool(uploaded_audio_file)
    unmet_restore_requirements = _get_unmet_restore_upload_requirements(
        restore_upload_requirements,
        video_source=params.video_source,
        voice_name=params.voice_name or "",
        has_local_materials=has_local_materials,
        has_custom_audio=has_custom_audio,
        voice_mode=voice_mode,
    )
    if "local_materials" in unmet_restore_requirements:
        st.warning(tr("Task Restore Local Materials Warning"))
    if "custom_audio" in unmet_restore_requirements:
        st.warning(tr("Task Restore Custom Audio Warning"))
    if restore_upload_requirements and not unmet_restore_requirements:
        # The user has re-uploaded the file or actively switched the material source/tone. At this time, the upload dependency of historical tasks
        # It has been clearly dealt with and the mark has been cleared to prevent subsequent normal builds from continuing to display the old prompt.
        st.session_state.pop("task_restore_upload_requirements", None)

    _render_settings_transfer(params)

    start_button = st.button(
        tr("Generate Video"),
        use_container_width=True,
        type="primary",
        key="generate_video_button",
        on_click=_prepare_generation_task,
    )
    if start_button:
        _save_runtime_config()
        task_id = st.session_state.get("pending_generation_task_id") or str(uuid4())
        _add_active_generation_task(
            task_id,
            subject=params.video_subject or params.video_script or task_id,
        )
        if not params.video_subject and not params.video_script:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Script and Subject Cannot Both Be Empty"))
            st.stop()

        voxcpm_reference_audio = None
        voxcpm_prompt_audio = None
        voxcpm_prompt_text = ""
        if voice.is_voxcpm_voice(params.voice_name or ""):
            reference_audio_error = st.session_state.get(
                VOXCPM_REFERENCE_AUDIO_ERROR_SESSION_KEY
            )
            if reference_audio_error:
                _remove_active_generation_task(task_id)
                st.error(
                    tr("VoxCPM Reference Audio Invalid").format(
                        error=reference_audio_error
                    )
                )
                st.stop()
            voxcpm_reference_audio = _get_voxcpm_reference_audio()
            prompt_validation_error = _get_voxcpm_prompt_validation_error()
            if prompt_validation_error:
                _remove_active_generation_task(task_id)
                st.error(
                    tr("VoxCPM Prompt Invalid").format(
                        error=prompt_validation_error
                    )
                )
                st.stop()
            voxcpm_prompt_audio = _get_voxcpm_effective_prompt_audio()
            voxcpm_prompt_text = _get_voxcpm_prompt_text()

        if params.video_source not in [
            "pexels",
            "pixabay",
            "coverr",
            "wavespeed",
            "volcengine_seedance",
            "ofox",
            "metaso_minimax",
            "muapi",
            "loomloom",
            "openai_image",
            "local",
        ]:
            _remove_active_generation_task(task_id)
            st.error(tr("Please Select a Valid Video Source"))
            st.stop()

        if params.video_source == "pexels" and not config.app.get(
            "pexels_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pexels API Key"))
            st.stop()

        if params.video_source == "pixabay" and not config.app.get(
            "pixabay_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pixabay API Key"))
            st.stop()

        if params.video_source == "coverr" and not config.app.get(
            "coverr_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Coverr API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not config.app.get(
            "wavespeed_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the WaveSpeed API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not st.session_state.get(
            "wavespeed_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm WaveSpeed Charge Required"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not (
            volcengine_seedance.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Volcano Engine Ark API Key"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not st.session_state.get(
            "volcengine_seedance_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Volcano Engine Seedance Charge Required"))
            st.stop()

        if params.video_source == "ofox" and not (
            ofox.is_enabled(config.snapshot_config_with_pending(config.app))
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the OFox API Key"))
            st.stop()

        if params.video_source == "ofox" and not st.session_state.get(
            "ofox_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm OFox Charge Required"))
            st.stop()

        if params.video_source == "metaso_minimax" and not (
            metaso_minimax.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Metaso MiniMax API Key"))
            st.stop()

        if params.video_source == "metaso_minimax" and not st.session_state.get(
            "metaso_minimax_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Metaso MiniMax Charge Required"))
            st.stop()

        if params.video_source == "muapi" and not (
            muapi.is_enabled(config.snapshot_config_with_pending(config.app))
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the MuAPI API Key"))
            st.stop()

        if params.video_source == "muapi" and not st.session_state.get(
            "muapi_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm MuAPI Charge Required"))
            st.stop()

        if params.video_source == "openai_image" and not material.is_openai_image_enabled(
            config.snapshot_config_with_pending(config.app)
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Configure the OpenAI Image Source"))
            st.stop()

        loomloom_video_request = None
        if params.video_source == "loomloom":
            current_batch, current_signature = _current_loomloom_video_quote_context(
                params
            )
            quoted_batch = st.session_state.get("loomloom_video_batch")
            quote_result = st.session_state.get("loomloom_video_quote")
            quote_is_current = bool(
                current_batch is not None
                and isinstance(quoted_batch, loomloom.LoomLoomVideoBatch)
                and quote_result is not None
                and st.session_state.get("loomloom_video_input_signature")
                == current_signature
            )
            if not quote_is_current or current_batch is None or quote_result is None:
                _remove_active_generation_task(task_id)
                st.error(tr("AI Video Quote Required"))
                st.stop()
            if not st.session_state.get("loomloom_video_confirm_charge", False):
                _remove_active_generation_task(task_id)
                st.error(tr("Confirm AI Video Charge Required"))
                st.stop()
            try:
                video_backend = _create_loomloom_video_backend()
                loomloom_video_request = loomloom.LoomLoomConfirmedVideoRequest(
                    settings=video_backend.settings,
                    batch=current_batch,
                    listing_version_id=quote_result.listing_version_id,
                    client_request_id=st.session_state[
                        "loomloom_video_client_request_id"
                    ],
                )
                loomloom_video_request.validate()
            except (loomloom.LoomLoomError, ValueError) as exc:
                _remove_active_generation_task(task_id)
                st.error(str(exc))
                st.stop()

        if (
            params.bgm_type == "sonilo"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not sonilo_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Sonilo API Key Required"))
            st.stop()

        if (
            params.bgm_type == "elevenlabs"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not elevenlabs_music_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("ElevenLabs API Key Required"))
            st.stop()

        if params.video_source == "local" and not has_local_materials:
            # Continuing execution when the local material is empty will first generate TTS/subtitles, and finally fail in the material preprocessing stage.
            # Interception before the task starts can avoid meaningless API calls and intermediate files.
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Local Materials First"))
            st.stop()

        if voice_mode == VOICE_MODE_UPLOAD and not uploaded_audio_file:
            # Uploading audio is the dubbing method explicitly selected by the user, and TTS cannot be silently returned when the file is missing.
            # Intercept before the task is started to avoid producing films that are inconsistent with the user's selection.
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Voiceover File First"))
            st.stop()

        if "custom_audio" in unmet_restore_requirements:
            # Historical custom audio cannot be automatically backfilled. When the user has not re-uploaded and has not actively changed the timbre,
            # Silent fallback to TTS must be prevented, otherwise the regenerated results will be inconsistent with the original task voice.
            _remove_active_generation_task(task_id)
            st.error(tr("Task Restore Custom Audio Warning"))
            st.stop()

        if uploaded_bgm_file and bgm_service.should_use_bgm(
            params.bgm_type, params.bgm_volume
        ):
            try:
                saved_bgm_name = bgm_service.save_bgm_upload(
                    uploaded_bgm_file.name, uploaded_bgm_file
                )
            except bgm_service.BgmUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI background music upload rejected: {exc!s}")
                st.error(tr("Invalid Background Music"))
                st.stop()
            except bgm_service.BgmServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI background music upload failed: {exc!s}")
                st.error(tr("Background Music Validation Failed"))
                st.stop()
            # After successful saving, only the file name is written into the task parameters. The video service will be in two BGM whitelists
            # Re-parse in the directory to avoid persisting or displaying the absolute path to the server to the user.
            params.bgm_file = saved_bgm_name
        elif uploaded_bgm_file:
            # At 0 volume, the video service will not use any BGM, so uploaded files that have been previewed will no longer be
            # Persist to storage. When the user turns up the volume later, he or she can directly click Generate again to complete the save.
            params.bgm_file = ""

        if uploaded_audio_file:
            try:
                task_dir = utils.task_dir(task_id)
                custom_audio_path = _build_uploaded_file_path(
                    uploaded_audio_file,
                    task_dir,
                    CUSTOM_AUDIO_EXTENSIONS,
                    "custom-audio",
                )
                # Voiceover uploads previously bypassed the same full-decode
                # and size checks used for background-music uploads. Validate
                # before storing or scheduling the generation task.
                bgm_service.validate_bgm_upload(
                    uploaded_audio_file.name, uploaded_audio_file
                )
                _stage_task_audio(custom_audio_path, uploaded_audio_file.getbuffer())
            except OSError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"failed to persist uploaded task audio: {exc}")
                st.error(tr("Video Generation Failed"))
                st.stop()
            except bgm_service.BgmUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI custom audio upload rejected: {exc}")
                st.error(str(exc))
                st.stop()
            except bgm_service.BgmServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI custom audio validation failed: {exc}")
                st.error(str(exc))
                st.stop()
            except ValueError:
                _remove_active_generation_task(task_id)
                st.error(tr("Unsupported Upload File Type"))
                st.stop()
            params.custom_audio_file = custom_audio_path

        if uploaded_files:
            # Each time you re-upload, the material selected this time will be used as the standard to avoid repeated addition of old materials.
            try:
                params.video_materials, persisted_local_materials = (
                    _save_uploaded_local_materials(uploaded_files)
                )
            except material_upload_service.MaterialUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI local material upload rejected: {exc}")
                st.error(str(exc))
                st.stop()
            except material_upload_service.MaterialServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI local material upload failed: {exc}")
                st.error(str(exc))
                st.stop()
            # Write the video material that has been uploaded and saved locally to the session for direct reuse when only the copy is modified later.
            st.session_state["local_video_materials"] = persisted_local_materials
        elif (
            params.video_source == "local" and st.session_state["local_video_materials"]
        ):
            # When the user does not re-upload the file, the local material list that was last saved to disk is reused.
            params.video_materials = []
            for material_entry in st.session_state["local_video_materials"]:
                m = MaterialInfo()
                m.provider = material_entry.get("provider", "local")
                m.url = material_entry.get("url", "")
                m.duration = material_entry.get("duration", 0)
                if m.url:
                    params.video_materials.append(m)

        reusable_voice_preview = _get_reusable_full_voice_preview(
            params,
            voice_mode,
        )
        if reusable_voice_preview:
            # The audition cache only exists for the current Streamlit session. Write the audio to the target task directory before submitting.
            # The background thread then only reads the task's own files; even if the page reruns, the browser is closed, or
            # When users try out other timbres, it will not affect the generation tasks that have already been queued.
            try:
                preview_audio_file = os.path.join(
                    utils.task_dir(task_id),
                    "audio.mp3",
                )
                _stage_task_audio(preview_audio_file, reusable_voice_preview["audio_bytes"])
            except OSError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"failed to persist preview task audio: {exc}")
                st.error(tr("Video Generation Failed"))
                st.stop()
            reusable_voice_preview.pop("audio_bytes")
            reusable_voice_preview["audio_file"] = preview_audio_file
            logger.info(
                f"reuse full voice preview for task: "
                f"task_id={task_id}, duration={reusable_voice_preview['duration']:.2f}s"
            )

        try:
            st.toast(tr("Generating Video"))
            logger.info(tr("Start Generating Video"))
            logger.info(utils.to_json(params))
            webui_task.submit_generation(
                task_id=task_id,
                params=params,
                capture_logs=not config.ui.get("hide_log", False),
                voice_preview=reusable_voice_preview,
                loomloom_video_request=loomloom_video_request,
                voxcpm_reference_audio=voxcpm_reference_audio,
                voxcpm_prompt_audio=voxcpm_prompt_audio,
                voxcpm_prompt_text=voxcpm_prompt_text,
            )
            if loomloom_video_request is not None:
                # An offer is only allowed to be submitted once. The background request comes with a stable idempotent ID; after successful submission
                # Clear the page quotation, and you must re-inquiry and confirm the next time it is generated.
                st.session_state["loomloom_video_batch"] = None
                st.session_state["loomloom_video_quote"] = None
                st.session_state["loomloom_video_input_signature"] = ""
                st.session_state["loomloom_video_client_request_id"] = ""
        except Exception:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Generation Failed"))
            st.stop()

        st.session_state["current_generation_task_id"] = task_id
        logger.info(f"WebUI generation task submitted: task_id={task_id}")

    _render_current_generation_task()
    return start_button



def _get_preview_image_uri(image_path_or_url: Any) -> str:
    """Return an inline data URI or web URL suitable for HTML preview."""
    if not image_path_or_url:
        return ""

    if hasattr(image_path_or_url, "url"):
        image_path_or_url = getattr(image_path_or_url, "url", "")
    elif isinstance(image_path_or_url, dict):
        image_path_or_url = image_path_or_url.get("url") or image_path_or_url.get("path") or ""

    if not isinstance(image_path_or_url, str) or not image_path_or_url:
        return ""

    if image_path_or_url.startswith(("http://", "https://", "data:")):
        return image_path_or_url
    if os.path.exists(image_path_or_url):
        try:
            with open(image_path_or_url, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
                ext = os.path.splitext(image_path_or_url)[1].lower().lstrip(".")
                mime = "jpeg" if ext in ("jpg", "jpeg") else ("png" if ext == "png" else "webp")
                return f"data:image/{mime};base64,{b64}"
        except Exception as exc:
            logger.debug(f"Failed to encode preview image {image_path_or_url}: {exc}")
            return ""
    return ""


def _get_preview_images() -> list[str]:
    """Collect available image URLs or file paths from local materials, article scraper, or user uploads."""
    images = []
    local_materials = st.session_state.get("local_video_materials", [])
    if isinstance(local_materials, list):
        for item in local_materials:
            if isinstance(item, dict) and item.get("url"):
                u = str(item["url"]).strip()
                if u and u not in images:
                    images.append(u)
            elif isinstance(item, str) and item.strip():
                u = item.strip()
                if u not in images:
                    images.append(u)
            else:
                url_attr = getattr(item, "url", None)
                if url_attr:
                    u = str(url_attr).strip()
                    if u and u not in images:
                        images.append(u)

    scraped = st.session_state.get("scraped_article_data")
    if scraped and getattr(scraped, "images", None):
        for img in scraped.images:
            img_url = ""
            if isinstance(img, str):
                img_url = img.strip()
            elif isinstance(img, dict) and img.get("url"):
                img_url = str(img["url"]).strip()
            else:
                img_url_attr = getattr(img, "url", None)
                if img_url_attr:
                    img_url = str(img_url_attr).strip()

            if img_url and img_url not in images:
                images.append(img_url)

    # Also include any downloaded local material files from article
    downloaded = st.session_state.get("downloaded_article_images", [])
    if isinstance(downloaded, list):
        for p in downloaded:
            if isinstance(p, str) and os.path.exists(p) and p not in images:
                images.append(p)

    return images


def _render_creation_mode_selector() -> str:
    """Render high-level creation workflow selector: Non-AI Mode vs AI Mode."""
    st.session_state.setdefault("video_creation_mode", "non_ai")
    current_mode = st.session_state.get("video_creation_mode", "non_ai")

    with st.container(border=True):
        col_mode, col_desc = st.columns([1.1, 2.0], gap="medium")
        with col_mode:
            mode_options = ["non_ai", "ai"]
            mode_labels = {
                "non_ai": "🔰 " + _t("Non-AI Mode (News Auto)"),
                "ai": "🤖 " + _t("AI Mode (Creative Video)"),
            }
            selected_mode = st.radio(
                _t("Video Creation Mode"),
                options=mode_options,
                format_func=lambda m: mode_labels.get(m, m),
                index=0 if current_mode == "non_ai" else 1,
                key="creation_mode_selector_radio",
                help=_t("Creation Mode Help"),
                horizontal=False,
            )
            st.session_state["video_creation_mode"] = selected_mode

        with col_desc:
            if selected_mode == "non_ai":
                st.info(
                    "💡 **" + _t("Non-AI Mode (News Auto)") + "**\n\n"
                    + _t("Non-AI Mode Description")
                )
            else:
                st.info(
                    "✨ **" + _t("AI Mode (Creative Video)") + "**\n\n"
                    + _t("AI Mode Description")
                )
    return selected_mode


def _render_live_video_preview(params: VideoParams):
    """Render interactive real-time video mockup preview showing aspect ratio, news images, Ken Burns, and subtitle style."""
    _sync_overlay_session_state(params)
    with st.container(border=True):
        st.markdown(f"### 📺 {_t('Live Video Preview')}")
        st.caption(_t("Live Video Preview Help"))

        preview_images = _get_preview_images()
        st.session_state.setdefault("preview_image_index", 0)
        curr_idx = st.session_state.get("preview_image_index", 0)
        if preview_images:
            curr_idx = max(0, min(curr_idx, len(preview_images) - 1))
            st.session_state["preview_image_index"] = curr_idx

        col_player, col_details = st.columns([1.1, 0.9], gap="large")

        with col_player:
            aspect_val = getattr(params.video_aspect, "value", str(params.video_aspect or "16:9"))
            if "9:16" in aspect_val or "portrait" in str(aspect_val).lower():
                frame_label = "9:16 (Dọc / Shorts / TikTok)"
            elif "1:1" in aspect_val or "square" in str(aspect_val).lower():
                frame_label = "1:1 (Vuông)"
            else:
                frame_label = "16:9 (Ngang / YouTube)"

            st.checkbox(
                _t("Simulate Ken Burns Effect"),
                value=True,
                key="preview_kenburns_toggle",
            )

            img_data_uri = ""
            if preview_images and curr_idx < len(preview_images):
                img_data_uri = _get_preview_image_uri(preview_images[curr_idx])

            sub_text = ""
            if getattr(params, "subtitle_enabled", True):
                raw_script = (params.video_script or "").strip()
                if raw_script:
                    first_sentence = raw_script.replace("\n", " ").split(".")[0].strip()
                    words = first_sentence.split()
                    sub_text = " ".join(words[:12]) if len(words) > 12 else first_sentence
                    if not sub_text:
                        sub_text = _t("Subtitle Preview Sample")
                else:
                    sub_text = _t("Subtitle Preview Sample")

            # Prepare overlay data for interactive draggable canvas
            headline_title = (st.session_state.get("headline_text") or params.video_subject or "").strip()
            headline_is_enabled = bool(st.session_state.get(
                "headline_enabled", getattr(params, "headline_enabled", True)
            ))
            headline_dur = int(st.session_state.get(
                "headline_duration", getattr(params, "headline_duration", 0)
            ))

            logo_is_enabled = bool(st.session_state.get(
                "logo_enabled", getattr(params, "logo_enabled", False)
            ))
            logo_file_path = st.session_state.get(
                "logo_file", getattr(params, "logo_file", "")
            )
            logo_sz = int(st.session_state.get(
                "logo_size", getattr(params, "logo_size", 140)
            ))
            logo_dur = int(st.session_state.get(
                "logo_duration", getattr(params, "logo_duration", 0)
            ))
            logo_uri = ""
            if logo_is_enabled and logo_file_path and os.path.exists(logo_file_path):
                logo_uri = _get_preview_image_uri(logo_file_path)

            badge_is_enabled = bool(st.session_state.get(
                "source_badge_enabled", getattr(params, "source_badge_enabled", True)
            ))
            badge_text = st.session_state.get(
                "source_badge_text", getattr(params, "source_badge_text", "")
            )
            badge_dur = int(st.session_state.get(
                "source_badge_duration", getattr(params, "source_badge_duration", 0)
            ))

            frame_is_enabled = bool(st.session_state.get(
                "frame_enabled", getattr(params, "frame_enabled", True)
            ))
            frame_tmpl_path = getattr(params, "frame_template", None)
            tmpl_uri = ""
            if frame_tmpl_path and os.path.exists(frame_tmpl_path):
                tmpl_uri = _get_preview_image_uri(frame_tmpl_path)
            frame_dur = int(st.session_state.get(
                "frame_duration", getattr(params, "frame_duration", 0)
            ))

            # Call interactive draggable canvas component
            canvas_res = _draggable_canvas(
                bg_image=img_data_uri,
                aspect=aspect_val,
                subtitle_text=sub_text if getattr(params, "subtitle_enabled", True) else "",
                headline_enabled=bool(headline_is_enabled and headline_title),
                headline_text=headline_title,
                headline_x=float(st.session_state.get("headline_x", 50.0)),
                headline_y=float(st.session_state.get("headline_y", 8.0)),
                headline_duration=int(headline_dur),
                source_enabled=bool(badge_is_enabled and badge_text),
                source_text=badge_text,
                source_x=float(st.session_state.get("source_badge_x", 75.0)),
                source_y=float(st.session_state.get("source_badge_y", 12.0)),
                source_duration=int(badge_dur),
                logo_enabled=bool(logo_is_enabled and logo_uri),
                logo_src=logo_uri,
                logo_x=float(st.session_state.get("logo_x", 8.0)),
                logo_y=float(st.session_state.get("logo_y", 6.0)),
                logo_size=int(logo_sz * 0.45),
                logo_duration=int(logo_dur),
                frame_enabled=bool(frame_is_enabled and tmpl_uri),
                frame_src=tmpl_uri,
                frame_x=float(st.session_state.get("frame_x", 0.0)),
                frame_y=float(st.session_state.get("frame_y", 0.0)),
                frame_duration=int(frame_dur),
                key="interactive_preview_draggable_canvas",
                default=None,
            )

            # Sync dragged coordinates back into Streamlit state
            if canvas_res and isinstance(canvas_res, dict):
                if "headline_x" in canvas_res:
                    st.session_state["headline_x"] = float(canvas_res["headline_x"])
                    params.headline_x = float(canvas_res["headline_x"])
                if "headline_y" in canvas_res:
                    st.session_state["headline_y"] = float(canvas_res["headline_y"])
                    params.headline_y = float(canvas_res["headline_y"])
                if "source_x" in canvas_res:
                    st.session_state["source_badge_x"] = float(canvas_res["source_x"])
                    params.source_badge_x = float(canvas_res["source_x"])
                if "source_y" in canvas_res:
                    st.session_state["source_badge_y"] = float(canvas_res["source_y"])
                    params.source_badge_y = float(canvas_res["source_y"])
                if "logo_x" in canvas_res:
                    st.session_state["logo_x"] = float(canvas_res["logo_x"])
                    params.logo_x = float(canvas_res["logo_x"])
                if "logo_y" in canvas_res:
                    st.session_state["logo_y"] = float(canvas_res["logo_y"])
                    params.logo_y = float(canvas_res["logo_y"])
                if "frame_x" in canvas_res:
                    st.session_state["frame_x"] = float(canvas_res["frame_x"])
                    params.frame_x = float(canvas_res["frame_x"])
                if "frame_y" in canvas_res:
                    st.session_state["frame_y"] = float(canvas_res["frame_y"])
                    params.frame_y = float(canvas_res["frame_y"])

            # Interactive Multi-Tab Control for Headline, Source Badge, Logo, and Frame
            preview_tabs = st.tabs([
                f"📢 {_i18n('Tiêu đề', 'Headline')}",
                f"📌 {_i18n('Nhãn nguồn', 'Source')}",
                f"🛡️ {_i18n('Ảnh / Logo', 'Photo / Logo')}",
                f"🖼️ {_i18n('Khung viền', 'Frame Overlay')}",
            ])

            # Tab 1: Tiêu đề video (Headline)
            with preview_tabs[0]:
                c_hl1, c_hl2 = st.columns([1, 2])
                with c_hl1:
                    cur_hl_en = st.checkbox(
                        _i18n("Hiện tiêu đề", "Show Headline"),
                        value=st.session_state.get("headline_enabled", True),
                        key="chk_preview_headline_en",
                    )
                    st.session_state["headline_enabled"] = cur_hl_en
                    params.headline_enabled = cur_hl_en
                with c_hl2:
                    if cur_hl_en:
                        cur_hl_txt = st.text_input(
                            _i18n("Nội dung tiêu đề", "Headline Text"),
                            value=st.session_state.get("headline_text", ""),
                            placeholder=_i18n("Nhập tiêu đề hoặc theo chủ đề", "Enter headline or follow subject"),
                            key="txt_preview_headline_val",
                            label_visibility="collapsed",
                        )
                        st.session_state["headline_text"] = (cur_hl_txt or "").strip()
                        params.headline_text = st.session_state["headline_text"]

                if cur_hl_en:
                    c_hl_dur_lbl, c_hl_dur_sel = st.columns([1, 1.8])
                    with c_hl_dur_lbl:
                        st.markdown(f"<div style='padding-top: 6px; font-size: 13px;'>⏱ **{_i18n('Thời gian hiện:', 'Duration:')}**</div>", unsafe_allow_html=True)
                    with c_hl_dur_sel:
                        cur_hl_d = int(st.session_state.get("headline_duration", 0))
                        idx_hl_d = next((i for i, (d, _) in enumerate(DURATION_CHOICE_TUPLES) if d == cur_hl_d), 0)
                        sel_hl_d = st.selectbox(
                            _i18n("Thời gian hiển thị", "Duration"),
                            options=[d[0] for d in DURATION_CHOICE_TUPLES],
                            format_func=lambda x: dict(DURATION_CHOICE_TUPLES).get(x, f"{x}s"),
                            index=idx_hl_d,
                            key="preview_headline_duration_select",
                            label_visibility="collapsed",
                        )
                        st.session_state["headline_duration"] = sel_hl_d
                        params.headline_duration = sel_hl_d

                    st.caption(f"📍 **{_i18n('Tọa độ hiện tại (kéo thả trên video hoặc tinh chỉnh):', 'Coordinates (drag on video or adjust):')}** `X: {st.session_state.get('headline_x', 50.0):.1f}%`, `Y: {st.session_state.get('headline_y', 8.0):.1f}%`")
                    c_hx, c_hy, c_hrst = st.columns([1.2, 1.2, 0.8])
                    with c_hx:
                        pv_hl_x = st.slider("X (%)", 0.0, 100.0, float(st.session_state.get("headline_x", 50.0)), step=0.5, key="pv_slider_hl_x")
                        st.session_state["headline_x"] = pv_hl_x
                        params.headline_x = pv_hl_x
                    with c_hy:
                        pv_hl_y = st.slider("Y (%)", 0.0, 100.0, float(st.session_state.get("headline_y", 8.0)), step=0.5, key="pv_slider_hl_y")
                        st.session_state["headline_y"] = pv_hl_y
                        params.headline_y = pv_hl_y
                    with c_hrst:
                        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                        if st.button("↺ " + _i18n("Mặc định", "Reset"), key="btn_pv_rst_hl", use_container_width=True):
                            st.session_state["headline_x"] = 50.0
                            st.session_state["headline_y"] = 8.0
                            params.headline_x = 50.0
                            params.headline_y = 8.0
                            st.rerun(scope="app")

            # Tab 2: Nhãn nguồn (Source Badge)
            with preview_tabs[1]:
                c_src1, c_src2 = st.columns([1, 2])
                with c_src1:
                    src_en = st.checkbox(
                        _i18n("Hiện nhãn nguồn", "Show Source Badge"),
                        value=st.session_state.get("source_badge_enabled", True),
                        key="chk_preview_source_en",
                    )
                    st.session_state["source_badge_enabled"] = src_en
                    params.source_badge_enabled = src_en
                with c_src2:
                    if src_en:
                        entered_src = st.text_input(
                            _t("Source Text"),
                            value=st.session_state.get("source_badge_text", "Nguồn: VnExpress"),
                            placeholder="VD: Nguồn: VnExpress",
                            key="txt_preview_source_val",
                            label_visibility="collapsed",
                        )
                        st.session_state["source_badge_text"] = (entered_src or "").strip()
                        params.source_badge_text = st.session_state["source_badge_text"]

                if src_en:
                    c_sb_dur_lbl, c_sb_dur_sel = st.columns([1, 1.8])
                    with c_sb_dur_lbl:
                        st.markdown(f"<div style='padding-top: 6px; font-size: 13px;'>⏱ **{_i18n('Thời gian hiện:', 'Duration:')}**</div>", unsafe_allow_html=True)
                    with c_sb_dur_sel:
                        cur_sb_d = int(st.session_state.get("source_badge_duration", 0))
                        idx_sb_d = next((i for i, (d, _) in enumerate(DURATION_CHOICE_TUPLES) if d == cur_sb_d), 0)
                        sel_sb_d = st.selectbox(
                            _i18n("Thời gian hiển thị", "Duration"),
                            options=[d[0] for d in DURATION_CHOICE_TUPLES],
                            format_func=lambda x: dict(DURATION_CHOICE_TUPLES).get(x, f"{x}s"),
                            index=idx_sb_d,
                            key="preview_source_badge_duration_select",
                            label_visibility="collapsed",
                        )
                        st.session_state["source_badge_duration"] = sel_sb_d
                        params.source_badge_duration = sel_sb_d

                    st.caption(f"📍 **{_i18n('Tọa độ hiện tại (kéo thả trên video hoặc tinh chỉnh):', 'Coordinates (drag on video or adjust):')}** `X: {st.session_state.get('source_badge_x', 75.0):.1f}%`, `Y: {st.session_state.get('source_badge_y', 12.0):.1f}%`")
                    c_sx, c_sy, c_srst = st.columns([1.2, 1.2, 0.8])
                    with c_sx:
                        pv_sb_x = st.slider("X (%)", 0.0, 100.0, float(st.session_state.get("source_badge_x", 75.0)), step=0.5, key="pv_slider_sb_x")
                        st.session_state["source_badge_x"] = pv_sb_x
                        params.source_badge_x = pv_sb_x
                    with c_sy:
                        pv_sb_y = st.slider("Y (%)", 0.0, 100.0, float(st.session_state.get("source_badge_y", 12.0)), step=0.5, key="pv_slider_sb_y")
                        st.session_state["source_badge_y"] = pv_sb_y
                        params.source_badge_y = pv_sb_y
                    with c_srst:
                        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                        if st.button("↺ " + _i18n("Mặc định", "Reset"), key="btn_pv_rst_sb", use_container_width=True):
                            st.session_state["source_badge_x"] = 75.0
                            st.session_state["source_badge_y"] = 12.0
                            params.source_badge_x = 75.0
                            params.source_badge_y = 12.0
                            st.rerun(scope="app")

            # Tab 3: Logo thương hiệu / Ảnh tùy chỉnh
            with preview_tabs[2]:
                c_lg1, c_lg2 = st.columns([1, 2])
                with c_lg1:
                    cur_lg_en = st.checkbox(
                        _i18n("Chèn Logo / Ảnh", "Overlay Logo / Photo"),
                        value=st.session_state.get("logo_enabled", False),
                        key="chk_preview_logo_en",
                    )
                    st.session_state["logo_enabled"] = cur_lg_en
                    params.logo_enabled = cur_lg_en
                with c_lg2:
                    if cur_lg_en:
                        p_up_logo = st.file_uploader(
                            _i18n("Tải ảnh Logo / Sticker (PNG/JPG)", "Upload Logo / Sticker (PNG/JPG)"),
                            type=["png", "jpg", "jpeg", "webp"],
                            key="preview_logo_file_uploader",
                            label_visibility="collapsed",
                        )
                        if p_up_logo is not None:
                            saved_logo = video_template.save_uploaded_logo(p_up_logo.getvalue(), p_up_logo.name)
                            st.session_state["logo_file"] = saved_logo
                            params.logo_file = saved_logo
                            st.toast(_i18n("Đã tải logo thành công!", "Logo uploaded!"), icon="🛡️")

                if cur_lg_en:
                    c_lsize, c_ldur = st.columns([1.2, 1.8])
                    with c_lsize:
                        sel_ls = st.slider(
                            _i18n("Kích cỡ", "Size"),
                            min_value=40,
                            max_value=240,
                            value=int(st.session_state.get("logo_size", 140)),
                            step=10,
                            key="preview_logo_size_slider",
                        )
                        st.session_state["logo_size"] = sel_ls
                        params.logo_size = sel_ls
                    with c_ldur:
                        cur_ld = int(st.session_state.get("logo_duration", 0))
                        idx_ld = next((i for i, (d, _) in enumerate(DURATION_CHOICE_TUPLES) if d == cur_ld), 0)
                        sel_ld = st.selectbox(
                            _i18n("Thời gian hiển thị", "Duration"),
                            options=[d[0] for d in DURATION_CHOICE_TUPLES],
                            format_func=lambda x: dict(DURATION_CHOICE_TUPLES).get(x, f"{x}s"),
                            index=idx_ld,
                            key="preview_logo_duration_select",
                        )
                        st.session_state["logo_duration"] = sel_ld
                        params.logo_duration = sel_ld

                    st.caption(f"📍 **{_i18n('Tọa độ hiện tại (kéo thả trên video hoặc tinh chỉnh):', 'Coordinates (drag on video or adjust):')}** `X: {st.session_state.get('logo_x', 8.0):.1f}%`, `Y: {st.session_state.get('logo_y', 6.0):.1f}%`")
                    c_lx, c_ly, c_lrst = st.columns([1.2, 1.2, 0.8])
                    with c_lx:
                        pv_lg_x = st.slider("X (%)", 0.0, 100.0, float(st.session_state.get("logo_x", 8.0)), step=0.5, key="pv_slider_lg_x")
                        st.session_state["logo_x"] = pv_lg_x
                        params.logo_x = pv_lg_x
                    with c_ly:
                        pv_lg_y = st.slider("Y (%)", 0.0, 100.0, float(st.session_state.get("logo_y", 6.0)), step=0.5, key="pv_slider_lg_y")
                        st.session_state["logo_y"] = pv_lg_y
                        params.logo_y = pv_lg_y
                    with c_lrst:
                        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                        if st.button("↺ " + _i18n("Mặc định", "Reset"), key="btn_pv_rst_lg", use_container_width=True):
                            st.session_state["logo_x"] = 8.0
                            st.session_state["logo_y"] = 6.0
                            params.logo_x = 8.0
                            params.logo_y = 6.0
                            st.rerun(scope="app")

                    if st.session_state.get("logo_file") and os.path.exists(st.session_state["logo_file"]):
                        col_pv_l, col_rm_l = st.columns([2, 1])
                        with col_pv_l:
                            st.caption("🖼 " + _i18n("Đang dùng:", "Using:") + f" `{os.path.basename(st.session_state['logo_file'])}`")
                        with col_rm_l:
                            if st.button("❌ " + _i18n("Gỡ logo", "Remove"), key="btn_pv_remove_logo", type="tertiary"):
                                st.session_state["logo_file"] = ""
                                st.session_state["logo_enabled"] = False
                                st.rerun(scope="app")

            # Tab 4: Khung viền (Frame Overlay)
            with preview_tabs[3]:
                c_fr1, c_fr2 = st.columns([1, 2])
                with c_fr1:
                    cur_fr_en = st.checkbox(
                        _i18n("Hiển thị Khung viền", "Show Frame Overlay"),
                        value=st.session_state.get("frame_enabled", True),
                        key="chk_preview_frame_en",
                    )
                    st.session_state["frame_enabled"] = cur_fr_en
                    params.frame_enabled = cur_fr_en
                with c_fr2:
                    if cur_fr_en:
                        p_up_frame = st.file_uploader(
                            _i18n("Tải khung viền / khung ảnh (PNG viền trong suốt)", "Upload Custom Frame (PNG)"),
                            type=["png", "webp"],
                            key="preview_frame_file_uploader",
                            label_visibility="collapsed",
                        )
                        if p_up_frame is not None:
                            saved_fr = video_template.save_uploaded_template(p_up_frame.getvalue(), p_up_frame.name)
                            st.session_state["frame_template_id"] = os.path.basename(saved_fr)
                            params.frame_template = saved_fr
                            st.toast(_i18n("Đã tải khung thành công!", "Frame uploaded!"), icon="🎨")

                if cur_fr_en:
                    c_fr_tmpl_sel, c_fr_dur_sel = st.columns([1.2, 1.8])
                    with c_fr_tmpl_sel:
                        aspect_str_pv = getattr(params.video_aspect, "value", str(params.video_aspect or "9:16"))
                        pv_templates = video_template.get_available_templates(aspect=aspect_str_pv)
                        pv_tmpl_choices = [t["id"] for t in pv_templates]
                        pv_tmpl_labels = {t["id"]: t["name"] for t in pv_templates}
                        pv_tmpl_paths = {t["id"]: t["path"] for t in pv_templates}
                        cur_t_id = st.session_state.get("frame_template_id", "none")
                        pv_t_idx = pv_tmpl_choices.index(cur_t_id) if cur_t_id in pv_tmpl_choices else 0
                        sel_t_id = st.selectbox(
                            _t("Frame Template"),
                            options=pv_tmpl_choices if pv_tmpl_choices else ["none"],
                            format_func=lambda x: str(pv_tmpl_labels.get(x, x)),
                            index=pv_t_idx,
                            key="pv_frame_template_select",
                            label_visibility="collapsed",
                        )
                        st.session_state["frame_template_id"] = sel_t_id
                        params.frame_template = pv_tmpl_paths.get(sel_t_id, "")
                    with c_fr_dur_sel:
                        cur_fr_d = int(st.session_state.get("frame_duration", 0))
                        idx_fr_d = next((i for i, (d, _) in enumerate(DURATION_CHOICE_TUPLES) if d == cur_fr_d), 0)
                        sel_fr_d = st.selectbox(
                            _i18n("Thời gian hiển thị", "Duration"),
                            options=[d[0] for d in DURATION_CHOICE_TUPLES],
                            format_func=lambda x: dict(DURATION_CHOICE_TUPLES).get(x, f"{x}s"),
                            index=idx_fr_d,
                            key="preview_frame_duration_select",
                        )
                        st.session_state["frame_duration"] = sel_fr_d
                        params.frame_duration = sel_fr_d

                    st.caption(f"📍 **{_i18n('Tọa độ khung (kéo thả trên video hoặc tinh chỉnh):', 'Frame Coordinates (drag on video or adjust):')}** `X: {st.session_state.get('frame_x', 0.0):.1f}%`, `Y: {st.session_state.get('frame_y', 0.0):.1f}%`")
                    c_fx, c_fy, c_frst = st.columns([1.2, 1.2, 0.8])
                    with c_fx:
                        pv_fr_x = st.slider("X (%)", 0.0, 100.0, float(st.session_state.get("frame_x", 0.0)), step=0.5, key="pv_slider_fr_x")
                        st.session_state["frame_x"] = pv_fr_x
                        params.frame_x = pv_fr_x
                    with c_fy:
                        pv_fr_y = st.slider("Y (%)", 0.0, 100.0, float(st.session_state.get("frame_y", 0.0)), step=0.5, key="pv_slider_fr_y")
                        st.session_state["frame_y"] = pv_fr_y
                        params.frame_y = pv_fr_y
                    with c_frst:
                        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                        if st.button("🔲 " + _i18n("Toàn khung", "Full"), key="btn_pv_rst_fr", use_container_width=True):
                            st.session_state["frame_x"] = 0.0
                            st.session_state["frame_y"] = 0.0
                            params.frame_x = 0.0
                            params.frame_y = 0.0
                            st.rerun(scope="app")

        with col_details:
            if preview_images:
                st.markdown(f"**🖼 {_t('News Material Preview')}:** {curr_idx + 1} / {len(preview_images)}")
                c_prev, c_lbl, c_next = st.columns([1, 1.2, 1])
                if c_prev.button("⬅ " + _t("Previous Image"), key="btn_prev_img_preview", disabled=curr_idx <= 0):
                    st.session_state["preview_image_index"] = max(0, curr_idx - 1)
                    st.rerun(scope="app")
                c_lbl.caption(f"<div style='text-align:center; padding-top:4px;'>{_t('Image')} {curr_idx + 1}</div>", unsafe_allow_html=True)
                if c_next.button(_t("Next Image") + " ➡", key="btn_next_img_preview", disabled=curr_idx >= len(preview_images) - 1):
                    st.session_state["preview_image_index"] = min(len(preview_images) - 1, curr_idx + 1)
                    st.rerun(scope="app")

            cached_voice = st.session_state.get("voice_preview_audio")
            if cached_voice and cached_voice.get("audio_bytes"):
                st.caption(f"🎙 **{_t('Voice Audition Sample')}**: `{params.voice_name or 'Default'}`")
            else:
                st.caption(f"🎙 **{_t('Selected Voice')}**: `{params.voice_name or 'Default'}`")

            st.markdown("---")
            script_words = len((params.video_script or "").split())
            est_duration = max(5, int(script_words / 2.5)) if script_words > 0 else 30
            duration_text = _t("Estimated Duration Words").format(duration=est_duration, words=script_words)

            current_mode = st.session_state.get("video_creation_mode", "non_ai")
            mode_name = _t("Non-AI Mode (News Auto)") if current_mode == "non_ai" else _t("AI Mode (Creative Video)")

            bgm_label = params.bgm_file if params.bgm_file else (params.bgm_type if params.bgm_type else "None")
            st.markdown(
                f"- **{_t('Creation Mode')}**: {mode_name}\n"
                f"- **{_t('Aspect Ratio')}**: `{frame_label}`\n"
                f"- **{_t('Estimated Duration')}**: **{duration_text}**\n"
                f"- **{_t('Background Music')}**: `{bgm_label}`"
            )

            if headline_is_enabled and headline_title:
                hl_dur_str = f"{headline_dur}s" if headline_dur > 0 else _i18n("Vĩnh viễn", "Permanent")
                st.markdown(f"- **{_i18n('Tiêu đề', 'Headline')}**: `{headline_title}` (X: {st.session_state.get('headline_x', 50.0):.1f}%, Y: {st.session_state.get('headline_y', 8.0):.1f}%, {hl_dur_str})")

            if badge_is_enabled and badge_text:
                sb_dur_str = f"{badge_dur}s" if badge_dur > 0 else _i18n("Vĩnh viễn", "Permanent")
                st.markdown(
                    f"- **{_t('News Source Badge')}**: `{badge_text}` (X: {st.session_state.get('source_badge_x', 75.0):.1f}%, Y: {st.session_state.get('source_badge_y', 12.0):.1f}%, {sb_dur_str})"
                )

            if logo_is_enabled and st.session_state.get("logo_file"):
                lg_dur_str = f"{logo_dur}s" if logo_dur > 0 else _i18n("Vĩnh viễn", "Permanent")
                st.markdown(
                    f"- **{_i18n('Logo / Ảnh', 'Brand Logo')}**: `{os.path.basename(st.session_state['logo_file'])}` (X: {st.session_state.get('logo_x', 8.0):.1f}%, Y: {st.session_state.get('logo_y', 6.0):.1f}%, {lg_dur_str})"
                )

            if frame_tmpl_path and os.path.exists(frame_tmpl_path):
                fr_dur = int(st.session_state.get("frame_duration", 0))
                fr_dur_str = f"{fr_dur}s" if fr_dur > 0 else _i18n("Vĩnh viễn", "Permanent")
                st.markdown(
                    f"- **{_i18n('Khung viền', 'Frame Overlay')}**: `{os.path.basename(frame_tmpl_path)}` (X: {st.session_state.get('frame_x', 0.0):.1f}%, Y: {st.session_state.get('frame_y', 0.0):.1f}%, {fr_dur_str})"
                )

            # Download template buttons for quick access
            with st.expander("📥 " + _t("Download Checkerboard Template"), expanded=False):
                st.caption(_t("Download Template Help"))
                templates_dir = video_template.get_templates_dir()
                is_portrait = "9:16" in aspect_val or "portrait" in str(aspect_val).lower()
                c_file = "template_9_16_checkerboard.png" if is_portrait else "template_16_9_checkerboard.png"
                t_file = "template_9_16_transparent.png" if is_portrait else "template_16_9_transparent.png"

                c_path = os.path.join(templates_dir, c_file)
                if os.path.exists(c_path):
                    with open(c_path, "rb") as f:
                        st.download_button(
                            _t("Download Checkerboard Template"),
                            data=f.read(),
                            file_name=c_file,
                            mime="image/png",
                            use_container_width=True,
                            key="btn_download_caro_template_preview",
                        )

                t_path = os.path.join(templates_dir, t_file)
                if os.path.exists(t_path):
                    with open(t_path, "rb") as f:
                        st.download_button(
                            _t("Download Transparent Template"),
                            data=f.read(),
                            file_name=t_file,
                            mime="image/png",
                            use_container_width=True,
                            key="btn_download_trans_template_preview",
                        )

            with st.expander("ℹ " + _t("How Video is Made"), expanded=False):

                st.markdown(_t("Video Pipeline Explanation"))


def _i18n(vi: str, en: str) -> str:
    lang = st.session_state.get("ui_language", "vi")
    return vi if lang == "vi" else en


def _get_deleted_videos_set() -> set[str]:
    deleted_file = os.path.join(utils.storage_dir(), ".deleted_videos.json")
    if os.path.exists(deleted_file):
        try:
            with open(deleted_file, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def _mark_video_deleted(identifier: str):
    if not identifier:
        return
    deleted_file = os.path.join(utils.storage_dir(), ".deleted_videos.json")
    try:
        data = _get_deleted_videos_set()
        data.add(identifier)
        with open(deleted_file, "w", encoding="utf-8") as f:
            json.dump(list(data), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to record deleted video {identifier}: {e}")


def _safe_remove_file(filepath: str) -> bool:
    if not os.path.exists(filepath):
        return True
    import gc
    gc.collect()
    try:
        os.remove(filepath)
        return True
    except OSError:
        import time
        time.sleep(0.15)
        gc.collect()
        try:
            os.remove(filepath)
            return True
        except OSError as e:
            logger.warning(f"Cannot remove file {filepath}: {e}")
            return False


def _delete_generated_video(video_path: str, filename: str) -> bool:
    """Permanently delete a generated video file and clean up its source task files."""
    # 1. Mark in deleted registry so it will never be restored
    _mark_video_deleted(filename)

    # 2. Check and clean up from storage/tasks
    tasks_dir = utils.task_dir()
    if os.path.exists(tasks_dir):
        for task_id in os.listdir(tasks_dir):
            if filename.startswith(task_id):
                _mark_video_deleted(task_id)
                t_path = os.path.join(tasks_dir, task_id)
                if os.path.isdir(t_path):
                    try:
                        shutil.rmtree(t_path, ignore_errors=True)
                    except Exception as e:
                        logger.warning(f"Failed cleaning task folder {t_path}: {e}")
                try:
                    if hasattr(sm.state, "delete_task"):
                        sm.state.delete_task(task_id)
                except Exception:
                    pass
                break

    # 3. Delete the file in final_videos
    return _safe_remove_file(video_path)


def _sync_past_generated_videos():
    """Ensure all videos in storage/tasks are synced to storage/final_videos once, excluding deleted ones."""
    if st.session_state.get("_past_videos_synced_done", False):
        return
    st.session_state["_past_videos_synced_done"] = True

    try:
        deleted_set = _get_deleted_videos_set()
        out_dir = utils.output_videos_dir()
        tasks_dir = utils.task_dir()
        if not os.path.exists(tasks_dir):
            return
        for task_id in os.listdir(tasks_dir):
            if task_id in deleted_set:
                continue
            t_path = os.path.join(tasks_dir, task_id)
            if not os.path.isdir(t_path):
                continue
            for f in os.listdir(t_path):
                if f.startswith("final-") and f.endswith(".mp4"):
                    dest_name = f"{task_id}_{f}"
                    if dest_name in deleted_set:
                        continue
                    src_file = os.path.join(t_path, f)
                    dest_file = os.path.join(out_dir, dest_name)
                    if not os.path.exists(dest_file):
                        try:
                            shutil.copy2(src_file, dest_file)
                        except OSError as copy_err:
                            logger.debug(f"Failed to copy past video {src_file}: {copy_err}")
    except OSError as exc:
        logger.debug(f"Failed to sync past videos: {exc}")


def _render_generated_videos_tab():
    _sync_past_generated_videos()
    out_dir = utils.output_videos_dir()
    os.makedirs(out_dir, exist_ok=True)

    video_exts = (".mp4", ".mov", ".mkv", ".webm", ".avi")
    files = []
    total_bytes = 0
    for fname in os.listdir(out_dir):
        if fname.lower().endswith(video_exts):
            full_path = os.path.join(out_dir, fname)
            if os.path.isfile(full_path):
                stat = os.stat(full_path)
                total_bytes += stat.st_size
                time_formatted = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M:%S")
                files.append({
                    "name": fname,
                    "path": full_path,
                    "size_mb": round(stat.st_size / (1024 * 1024), 2),
                    "size_bytes": stat.st_size,
                    "mtime": stat.st_mtime,
                    "time_str": time_formatted,
                })

    st.markdown(
        f"""
        <div style="background: linear-gradient(135deg, rgba(30, 41, 59, 0.7) 0%, rgba(15, 23, 42, 0.8) 100%); 
                    border: 1px solid rgba(255, 255, 255, 0.08); 
                    border-radius: 14px; 
                    padding: 16px 20px; 
                    margin-bottom: 20px; 
                    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);">
            <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
                <div>
                    <h3 style="margin: 0; font-size: 1.35rem; color: #f8fafc; display: flex; align-items: center; gap: 8px;">
                        <span>📁</span> {_i18n('Quản lý & Thư viện Video đã tạo', 'Generated Videos Library')}
                    </h3>
                    <p style="margin: 4px 0 0 0; font-size: 0.85rem; color: #94a3b8;">
                        {_i18n('Thư mục lưu trữ', 'Storage Folder')}: <code style="color: #38bdf8; background: rgba(56, 189, 248, 0.1); padding: 2px 6px; border-radius: 4px;">{out_dir}</code>
                    </p>
                </div>
                <div style="display: flex; gap: 16px; align-items: center;">
                    <div style="text-align: right;">
                        <span style="font-size: 0.75rem; color: #94a3b8; text-transform: uppercase;">{_i18n('Tổng số video', 'Total Videos')}</span>
                        <div style="font-size: 1.25rem; font-weight: 700; color: #38bdf8;">{len(files)}</div>
                    </div>
                    <div style="height: 32px; width: 1px; background: rgba(255, 255, 255, 0.1);"></div>
                    <div style="text-align: right;">
                        <span style="font-size: 0.75rem; color: #94a3b8; text-transform: uppercase;">{_i18n('Dung lượng đã dùng', 'Total Storage')}</span>
                        <div style="font-size: 1.25rem; font-weight: 700; color: #a78bfa;">{round(total_bytes / (1024 * 1024), 1)} MB</div>
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    c_search, c_sort, c_view, c_open, c_del_all, c_refresh = st.columns([2.0, 1.2, 1.2, 1.0, 0.9, 0.5], vertical_alignment="bottom")
    with c_search:
        search_kw = st.text_input(
            "🔍 " + _i18n("Tìm kiếm video", "Search videos"),
            placeholder=_i18n("Tìm kiếm video theo tên...", "Search videos by name..."),
            label_visibility="collapsed",
            key="search_generated_videos_input",
        ).strip().lower()
    
    with c_sort:
        opt_newest = _i18n("Mới nhất trước", "Newest first")
        opt_oldest = _i18n("Cũ nhất trước", "Oldest first")
        opt_largest = _i18n("Dung lượng lớn nhất", "Largest size")
        opt_name = _i18n("Tên A-Z", "Name A-Z")
        sort_mode = st.selectbox(
            _i18n("Sắp xếp theo", "Sort by"),
            options=[opt_newest, opt_oldest, opt_largest, opt_name],
            index=0,
            label_visibility="collapsed",
            key="sort_generated_videos_select",
        )

    with c_view:
        view_opt_4 = _i18n("Nhỏ gọn (4 cột)", "Compact (4 cols)")
        view_opt_3 = _i18n("Vừa (3 cột)", "Medium (3 cols)")
        view_opt_2 = _i18n("Lớn (2 cột)", "Large (2 cols)")
        layout_mode = st.selectbox(
            _i18n("Kích thước video", "Video size"),
            options=[view_opt_4, view_opt_3, view_opt_2],
            index=0,
            label_visibility="collapsed",
            key="layout_generated_videos_select",
        )

    with c_open:
        if st.button("📂 " + _i18n("Mở thư mục", "Open Folder"), use_container_width=True, key="btn_open_video_folder"):
            try:
                if sys.platform == "win32":
                    os.startfile(out_dir)
                else:
                    webbrowser.open(f"file://{out_dir}")
                st.toast(_i18n("Mở thư mục Video: ", "Open Video Folder: ") + out_dir, icon="📂")
            except OSError as e:
                logger.warning(f"Failed to open video directory: {e}")
                webbrowser.open(f"file://{out_dir}")

    with c_del_all:
        if files:
            with st.popover("🗑️ " + _i18n("Xóa hết", "Delete All"), use_container_width=True):
                st.write(_i18n(f"Xác nhận xóa toàn bộ {len(files)} video?", f"Permanently delete all {len(files)} videos?"))
                if st.button("⚠️ " + _i18n("Xóa tất cả", "Delete All"), key="btn_confirm_del_all", type="primary", use_container_width=True):
                    for vf in files:
                        _delete_generated_video(vf["path"], vf["name"])
                    st.toast(_i18n("Đã xóa tất cả video!", "All videos deleted!"), icon="🗑️")
                    st.rerun(scope="app")

    with c_refresh:
        if st.button("🔄", help=_i18n("Làm mới danh sách", "Refresh List"), use_container_width=True, key="btn_refresh_videos_list"):
            st.rerun(scope="app")

    if search_kw:
        files = [f for f in files if search_kw in f["name"].lower()]

    if sort_mode == opt_newest:
        files.sort(key=lambda x: x["mtime"], reverse=True)
    elif sort_mode == opt_oldest:
        files.sort(key=lambda x: x["mtime"])
    elif sort_mode == opt_largest:
        files.sort(key=lambda x: x["size_bytes"], reverse=True)
    elif sort_mode == opt_name:
        files.sort(key=lambda x: x["name"].lower())

    if not files:
        st.markdown(
            f"""
            <div style="text-align: center; padding: 60px 20px; background: rgba(30, 41, 59, 0.4); border-radius: 16px; border: 1px dashed rgba(255, 255, 255, 0.15); margin-top: 20px;">
                <div style="font-size: 52px; margin-bottom: 12px;">🎬</div>
                <h4 style="color: #f1f5f9; margin: 0 0 8px 0;">{_i18n("Chưa có video nào trong thư mục!", "No generated videos yet!")}</h4>
                <p style="color: #94a3b8; font-size: 0.95rem; max-width: 500px; margin: 0 auto 20px auto;">
                    {_i18n("Hãy chuyển sang tab 'Tạo video' để bắt đầu sản xuất video tự động bằng AI.", "Switch to 'Create Video' tab to start generating videos with AI.")}
                </p>
            </div>
            """,
            unsafe_allow_html=True
        )
        return

    if layout_mode == view_opt_4:
        num_cols = 4
    elif layout_mode == view_opt_3:
        num_cols = 3
    else:
        num_cols = 2

    cols = st.columns(num_cols, gap="small")
    for idx, v in enumerate(files):
        with cols[idx % num_cols], st.container(border=True):
            safe_display_name = v["name"]
            st.markdown(
                f"""
                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
                    <div style="font-weight: 600; font-size: 0.95rem; color: #f8fafc; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 70%;" title="{html.escape(safe_display_name)}">
                        🎞️ {html.escape(safe_display_name)}
                    </div>
                    <span style="font-size: 0.75rem; background: rgba(56, 189, 248, 0.15); color: #38bdf8; padding: 2px 8px; border-radius: 12px; font-weight: 500;">
                        {v['size_mb']} MB
                    </span>
                </div>
                <div style="font-size: 0.75rem; color: #64748b; margin-bottom: 10px;">
                    📅 {v['time_str']}
                </div>
                """,
                unsafe_allow_html=True
            )

            # Read video bytes directly into memory so file is closed immediately (no locks on Windows)
            video_bytes = None
            try:
                with open(v["path"], "rb") as vf:
                    video_bytes = vf.read()
            except Exception as read_err:
                logger.warning(f"Cannot read video {v['path']}: {read_err}")

            if video_bytes:
                st.video(video_bytes)
            else:
                st.caption(f"⚠️ {_i18n('Không thể nạp tệp video', 'Cannot load video file')}")

            c_dl, c_del = st.columns([1.4, 1])
            with c_dl:
                if video_bytes:
                    st.download_button(
                        "⬇️ " + _i18n("Tải về", "Download"),
                        data=video_bytes,
                        file_name=v["name"],
                        mime="video/mp4",
                        key=f"dl_video_{idx}_{abs(hash(v['name']))}",
                        use_container_width=True,
                    )
            with c_del, st.popover("🗑️ " + _i18n("Xóa", "Delete"), use_container_width=True):
                st.markdown(f"**{_i18n('Xác nhận xóa vĩnh viễn?', 'Permanently delete?')}**")
                st.caption(f"`{v['name']}`")
                if st.button("⚠️ " + _i18n("Xác nhận xóa", "Confirm Delete"), key=f"btn_confirm_del_{idx}_{abs(hash(v['name']))}", type="primary", use_container_width=True):
                    ok = _delete_generated_video(v["path"], v["name"])
                    if ok:
                        st.toast(_i18n("Đã xóa video thành công!", "Video deleted successfully!"), icon="🗑️")
                    else:
                        st.warning(_i18n("Đã xóa video khỏi danh sách!", "Video removed from list!"))
                    st.rerun(scope="app")


def _render_api_settings_view():
    st.markdown(
        f"""
        <div style="background: linear-gradient(135deg, rgba(30, 41, 59, 0.7) 0%, rgba(15, 23, 42, 0.8) 100%); 
                    border: 1px solid rgba(255, 255, 255, 0.08); 
                    border-radius: 14px; 
                    padding: 16px 20px; 
                    margin-bottom: 20px; 
                    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);">
            <div>
                <h3 style="margin: 0; font-size: 1.35rem; color: #f8fafc; display: flex; align-items: center; gap: 8px;">
                    <span>🔑</span> {_i18n('Cấu hình API AI & Dịch vụ', 'AI API & Service Settings')}
                </h3>
                <p style="margin: 4px 0 0 0; font-size: 0.85rem; color: #94a3b8;">
                    {_i18n('Cấu hình các khóa API cho Mô hình Ngôn ngữ AI (Gemini, OpenAI, Claude...), Video/Ảnh AI (Pexels, OFox, Seedance) và Tự động đăng tải mạng xã hội.',
                           'Configure API keys for AI Models (Gemini, OpenAI, Claude...), Stock/AI Video (Pexels, OFox, Seedance), and Auto-publishing.')}
                </p>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )
    _render_settings_content(in_dialog=False)


def _render_user_guide_view():
    st.markdown(
        f"""
        <div style="background: linear-gradient(135deg, rgba(30, 41, 59, 0.7) 0%, rgba(15, 23, 42, 0.8) 100%); 
                    border: 1px solid rgba(255, 255, 255, 0.08); 
                    border-radius: 14px; 
                    padding: 18px 24px; 
                    margin-bottom: 24px; 
                    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);">
            <div>
                <h3 style="margin: 0; font-size: 1.4rem; color: #f8fafc; display: flex; align-items: center; gap: 10px;">
                    <span>📖</span> {_i18n('Hướng Dẫn Sử Dụng VietNamNewsVideo', 'VietNamNewsVideo User Guide')}
                </h3>
                <p style="margin: 6px 0 0 0; font-size: 0.9rem; color: #94a3b8; line-height: 1.5;">
                    {_i18n('Giải pháp sản xuất video tin tức, thời sự tự động bằng AI. Hướng dẫn chi tiết từ cấu hình API, viết kịch bản, chỉnh sửa đồ họa kéo thả đến xuất video chất lượng cao.',
                           'Automated AI News Video Production. Detailed guide from API setup, scripting, interactive overlay placement to exporting high-definition videos.')}
                </p>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    with st.expander("🚀 " + _i18n("1. Quy trình tạo video nhanh trong 3 bước", "1. Quickstart in 3 Steps"), expanded=True):
        st.markdown(
            _i18n(
                """
#### 🌟 3 Bước sản xuất video tin tức tự động:

1. **Bước 1 — Cấu hình API ban đầu**:
   - Chuyển sang tab **`🔑 Nhập API AI`**.
   - Cung cấp khóa **LLM API** (khuyên dùng **Google Gemini** vì miễn phí, nhanh và phân tích tiếng Việt chuẩn xác, hoặc OpenAI GPT-4o).
   - Cung cấp khóa **Pexels API** (miễn phí 100%) để AI tự động tìm kiếm footage cảnh quay minh họa cho bản tin.
   - Nhấn **Lưu cấu hình**.

2. **Bước 2 — Nhập nội dung & Chọn nguồn tin**:
   - Quay lại tab **`🎬 Tạo video`**.
   - **Tùy chọn A (Từ link bài báo)**: Dán link bài báo (VnExpress, Tuổi Trẻ, Dân Trí...). Hệ thống sẽ tự động cào bài, trích xuất nguồn tin và tóm tắt thành kịch bản video.
   - **Tùy chọn B (Từ chủ đề)**: Nhập chủ đề bạn muốn (ví dụ: *Thị trường bất động sản cuối năm*, *Dự báo thời tiết bão số 4*...). AI sẽ tự động sáng tạo kịch bản hấp dẫn.
   - **Tùy chọn C (Tự viết kịch bản)**: Tự dán kịch bản chi tiết của bạn vào ô văn bản.

3. **Bước 3 — Tùy biến giao diện trực quan & Bắt đầu tạo**:
   - Sử dụng **Khung xem trước trực tiếp (Interactive Canvas)** để kéo thả:
     - **Tiêu đề video**: Tự do kéo đến vị trí bạn muốn, chọn thời gian hiển thị (ví dụ 5s, 10s hoặc toàn bộ video).
     - **Nhãn nguồn tin**: Tự động nhận diện nguồn báo (📌 Nguồn: VnExpress) hoặc tùy chỉnh kênh của bạn.
     - **Logo thương hiệu**: Tải logo PNG trong suốt lên, kéo đến góc màn hình.
     - **Khung viền tin tức**: Chọn khung đồ họa cờ caro hoặc tùy chỉnh riêng.
   - Bấm nút màu đỏ **🚀 Bắt đầu tạo video**. Xem tiến trình trực tiếp và thưởng thức video hoàn chỉnh tại tab **`📁 Video đã tạo`**!
                """,
                """
#### 🌟 3-Step News Video Production:
1. **Step 1 — Initial API Setup**: Go to **AI API Settings** tab and provide your Gemini/OpenAI API key and Pexels API key.
2. **Step 2 — Input Content**: Paste news URL (VnExpress, Tuổi Trẻ, Dân Trí...) or type your topic/script.
3. **Step 3 — Interactive Preview & Generate**: Drag elements (Headline, Source, Logo, Frame) on the live preview canvas, set durations, and click **Generate Video**.
                """
            )
        )

    with st.expander("🔑 " + _i18n("2. Hướng dẫn lấy khóa API AI miễn phí", "2. Free API Key Setup Guide"), expanded=False):
        st.markdown(
            _i18n(
                """
#### 🎁 Hướng dẫn đăng ký khóa API hoàn toàn miễn phí:

* **1. Google Gemini API (Khuyên dùng cho Kịch bản AI)**:
  - Truy cập: [Google AI Studio](https://aistudio.google.com/)
  - Đăng nhập bằng tài khoản Google (Gmail) của bạn.
  - Chọn **Get API Key** &rarr; **Create API key**.
  - Sao chép khóa dán vào mục **Google Gemini API Key** trong tab *Nhập API AI*.
  - *Ưu điểm:* Hạn mức miễn phí dồi dào, hiểu tiếng Việt cực tốt, tạo kịch bản và từ khóa tìm kiếm cảnh quay rất chính xác.

* **2. Pexels API (Bắt buộc cho Thư viện Video/Ảnh tư liệu miễn phí)**:
  - Truy cập: [Pexels API Documentation](https://www.pexels.com/api/)
  - Bấm **Get Started / Đăng ký tài khoản**.
  - Vào phần **Your API Key** và sao chép khóa.
  - Dán vào mục **Pexels API Key** trong tab *Nhập API AI*.
  - *Ưu điểm:* Hàng triệu video HD/4K chất lượng cao hoàn toàn miễn phí bản quyền.

* **3. Giọng đọc tiếng Việt (EdgeTTS)**:
  - Mặc định hệ thống sử dụng **Microsoft EdgeTTS** tích hợp sẵn.
  - **Hoàn toàn miễn phí, không cần đăng ký tài khoản hay API key**.
  - Hỗ trợ giọng đọc chuẩn tiếng Việt mượt mà: `vi-VN-HoaiMyNeural` (Nữ), `vi-VN-NamMinhNeural` (Nam).
  - Nếu muốn dùng giọng cao cấp đa dạng hơn, bạn có thể chọn **ElevenLabs** trong cài đặt âm thanh.
                """,
                """
#### 🎁 Free API Keys Registration Guide:
* **Google Gemini API**: Register at [Google AI Studio](https://aistudio.google.com/) for generous free tier.
* **Pexels API**: Register at [Pexels Developers](https://www.pexels.com/api/) for high-quality stock videos and photos.
* **EdgeTTS**: Built-in free Vietnamese natural voices (HoaiMy, NamMinh) without any API keys required.
                """
            )
        )

    with st.expander("🎨 " + _i18n("3. Hướng dẫn tùy biến đồ họa kéo thả (Overlay & Khung viền)", "3. Interactive Graphic & Overlay Customization"), expanded=False):
        st.markdown(
            _i18n(
                """
#### 🖱️ Cách kéo thả và thiết lập thời gian hiển thị:

* **Kéo thả chuột trực tiếp trên khung video**:
  - Nhấp giữ chuột vào bất kỳ phần tử nào (Tiêu đề, Nhãn nguồn tin, Logo) và di chuyển đến vị trí mong muốn trên video.
  - Tọa độ `X (%)` và `Y (%)` sẽ tự động cập nhật thời gian thực vào bảng điều khiển.

* **Thời gian xuất hiện (Duration)**:
  - **Vĩnh viễn (0s)**: Phần tử sẽ xuất hiện trong suốt toàn bộ độ dài của video (rất thích hợp cho Logo và Khung viền bản tin).
  - **3 giây, 5 giây, 8 giây, 10 giây, 15 giây...**: Phần tử sẽ chỉ hiển thị ở phần đầu video rồi tự động ẩn đi (rất thích hợp cho Tiêu đề thời sự giật gân mở đầu).

* **Khung viền thời sự (Frame Template)**:
  - Bạn có thể chọn mẫu khung viền 9:16 (dọc) hoặc 16:9 (ngang).
  - Có thể tải ảnh khung viền thiết kế riêng (định dạng PNG trong suốt, vùng trung tâm không có nền) để đóng dấu bản quyền kênh tin tức của bạn.
                """,
                """
#### 🖱️ How to drag & customize overlays:
* Click and drag any overlay (Headline, Source, Logo) directly on the simulated canvas.
* Set display duration: permanent or custom seconds (3s, 5s, 8s, 10s...) for headline intro banners.
* Upload transparent PNG templates or channel logos with custom sizing and positioning.
                """
            )
        )

    with st.expander("❓ " + _i18n("4. Câu hỏi thường gặp & Khắc phục sự cố", "4. FAQ & Troubleshooting"), expanded=False):
        st.markdown(
            _i18n(
                """
#### 🛠️ Các lỗi phổ biến và cách khắc phục:

1. **Lỗi "Vui lòng nhập khóa API Pexels"**:
   - *Nguyên nhân:* Nguồn tư liệu video được chọn là Pexels nhưng chưa có API key.
   - *Khắc phục:* Mở tab **`🔑 Nhập API AI`** &rarr; cuộn xuống mục **Kho tư liệu video & ảnh (Pexels)** &rarr; dán API key và bấm **Lưu cấu hình**.

2. **Lỗi FFmpeg không tìm thấy hoặc bị đứng ở bước ghép video**:
   - Kiểm tra tab **`⚡ Tự kiểm tra & Cập nhật`** để xem FFmpeg đã được nhận diện trong hệ thống chưa.
   - Nếu chưa có, hãy cài đặt FFmpeg hoặc chạy tệp `cai_dat.bat` trong thư mục gốc của dự án.

3. **Lỗi không xóa được video**:
   - Hiện hệ thống đã tối ưu quản lý tệp trên Windows: xóa vĩnh viễn cả video xuất bản và dữ liệu tác vụ gốc, đồng thời ngăn chặn việc tự động nạp lại. Bạn có thể xóa từng video hoặc xóa toàn bộ thư viện bằng nút *Xóa hết*.

4. **Kênh GitHub chính thức**:
   - [https://github.com/Thangvn2006/vietnam-news-video](https://github.com/Thangvn2006/vietnam-news-video)
   - Hãy nhấn **Star ⭐️** để theo dõi các cập nhật mới nhất!
                """,
                """
#### 🛠️ Common Issues & Fixes:
1. **Missing Pexels API Key**: Go to AI API Settings tab, enter your Pexels key and save.
2. **FFmpeg not found**: Check the System Diagnostic tab and ensure FFmpeg is in system PATH.
3. **Official GitHub Repo**: [https://github.com/Thangvn2006/vietnam-news-video](https://github.com/Thangvn2006/vietnam-news-video)
                """
            )
        )


def _render_system_diagnostic_and_update_view():
    st.markdown(
        f"""
        <div style="background: linear-gradient(135deg, rgba(30, 41, 59, 0.7) 0%, rgba(15, 23, 42, 0.8) 100%); 
                    border: 1px solid rgba(255, 255, 255, 0.08); 
                    border-radius: 14px; 
                    padding: 18px 24px; 
                    margin-bottom: 24px; 
                    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
                <div>
                    <h3 style="margin: 0; font-size: 1.4rem; color: #f8fafc; display: flex; align-items: center; gap: 10px;">
                        <span>⚡</span> {_i18n('Tự Kiểm Tra Hệ Thống & Cập Nhật Tự Động', 'System Diagnostic & Auto-Update')}
                    </h3>
                    <p style="margin: 6px 0 0 0; font-size: 0.9rem; color: #94a3b8;">
                        {_i18n('Kiểm tra môi trường chạy (Python, FFmpeg, Git, Storage, API) và tự động nâng cấp phiên bản mới nhất từ GitHub.',
                               'Diagnostic environment check (Python, FFmpeg, Git, Storage, API) and 1-Click upgrade from GitHub.')}
                    </p>
                </div>
                <div>
                    <a href="https://github.com/Thangvn2006/vietnam-news-video" target="_blank" style="text-decoration: none;">
                        <span style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); padding: 6px 14px; border-radius: 8px; font-size: 0.85rem; font-weight: 600;">
                            🐙 GitHub Repository
                        </span>
                    </a>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    # 1. System Health Diagnostic Section
    st.markdown(f"### 🩺 {_i18n('1. Bảng tự kiểm tra hệ thống', '1. System Health Diagnostic')}")
    health = system_updater.get_system_health()

    c1, c2, c3 = st.columns(3)
    with c1:
        with st.container(border=True):
            status_icon = "✅" if health["python"]["ok"] else "❌"
            st.markdown(f"**🐍 Python Runtime**: {status_icon}")
            st.caption(f"Phiên bản: `{health['python']['version']}`")
            st.caption(f"Thực thi: `{health['python']['executable']}`")

    with c2:
        with st.container(border=True):
            status_icon = "✅" if health["ffmpeg"]["ok"] else "❌"
            st.markdown(f"**🎬 FFmpeg Media Engine**: {status_icon}")
            st.caption(f"Trạng thái: {'Sẵn sàng' if health['ffmpeg']['ok'] else 'Chưa cài đặt'}")
            st.caption(f"Đường dẫn: `{health['ffmpeg']['path']}`")

    with c3:
        with st.container(border=True):
            status_icon = "✅" if health["git"]["ok"] else "⚠️"
            st.markdown(f"**🐙 Git Version Control**: {status_icon}")
            st.caption(f"Phiên bản: `{health['git']['version'] or 'Chưa cài đặt'}`")
            st.caption(f"Đường dẫn: `{health['git']['path']}`")

    c4, c5 = st.columns(2)
    with c4:
        with st.container(border=True):
            storage_icon = "✅" if health["storage"]["ok"] else "❌"
            st.markdown(f"**💾 Thư mục lưu trữ (Storage)**: {storage_icon}")
            st.caption(f"Video đã tạo: `{health['storage']['final_videos_dir']}`")
            st.caption(f"Tác vụ xử lý: `{health['storage']['tasks_dir']}`")
            if health["storage"]["notes"]:
                for n in health["storage"]["notes"]:
                    st.warning(n)

    with c5:
        with st.container(border=True):
            api_info = health["api_keys"]
            llm_ok = api_info["llm_configured"]
            pexels_ok = api_info["pexels_configured"]
            st.markdown(f"**🔑 Trạng thái Khóa API**")
            st.caption(f"• Mô hình LLM ({api_info['llm_provider']}): {'✅ Đã cấu hình' if llm_ok else '⚠️ Chưa nhập API Key'}")
            st.caption(f"• Tư liệu Pexels: {'✅ Đã cấu hình' if pexels_ok else '⚠️ Chưa nhập API Key'}")
            st.caption("Cấu hình thêm tại tab **Nhập API AI**.")

    st.markdown("---")

    # 2. Auto-Update Engine Section
    st.markdown(f"### 🚀 {_i18n('2. Tự động kiểm tra & Cập nhật từ GitHub', '2. Auto-Update Engine from GitHub')}")
    st.caption(_i18n(
        "Hệ thống sẽ kết nối với kho lưu trữ chính thức trên GitHub để kiểm tra xem có mã nguồn hoặc tính năng mới hay không.",
        "The system connects to the official GitHub repository to check for newer commits and features."
    ))

    btn_col1, btn_col2 = st.columns([1.5, 2.5], vertical_alignment="center")
    with btn_col1:
        check_now = st.button(
            "🔍 " + _i18n("Kiểm tra bản cập nhật ngay", "Check for Updates Now"),
            use_container_width=True,
            type="primary" if not st.session_state.get("_update_check_done") else "secondary",
            key="btn_trigger_git_check",
        )

    if check_now or st.session_state.get("_update_check_done"):
        st.session_state["_update_check_done"] = True
        with st.spinner(_i18n("Đang kết nối GitHub và kiểm tra các commit mới...", "Connecting to GitHub and checking for updates...")):
            git_info = system_updater.check_git_updates()

        if not git_info.get("ok"):
            st.warning(f"⚠️ {git_info.get('error', 'Không thể kết nối đến máy chủ GitHub.')}")
        else:
            current_branch = git_info.get("current_branch", "main")
            current_commit = git_info.get("current_commit", "N/A")
            has_update = git_info.get("has_update", False)
            commits_behind = git_info.get("commits_behind", 0)

            st.markdown(
                f"""
                <div style="background: rgba(15, 23, 42, 0.6); padding: 14px 18px; border-radius: 10px; border: 1px solid rgba(255, 255, 255, 0.1); margin: 12px 0;">
                    <div>🌱 <b>{_i18n('Nhánh Git hiện tại', 'Current Git Branch')}</b>: <code>{current_branch}</code></div>
                    <div>📌 <b>{_i18n('Commit đang sử dụng', 'Current Commit')}</b>: <code>{current_commit}</code></div>
                </div>
                """,
                unsafe_allow_html=True
            )

            if has_update:
                st.success(f"🎉 **{_i18n(f'Có {commits_behind} bản cập nhật mới trên GitHub!', f'{commits_behind} new updates available on GitHub!')}**")
                new_commits = git_info.get("new_commits", [])
                if new_commits:
                    st.markdown(f"**{_i18n('Các cập nhật mới nhất:', 'Latest Commits:')}**")
                    for nc in new_commits:
                        st.markdown(f"- `{nc}`")

                st.markdown("<br/>", unsafe_allow_html=True)
                if st.button("🚀 " + _i18n("Cập nhật ngay (1-Click Update)", "Upgrade Now (1-Click)"), type="primary", key="btn_execute_update"):
                    with st.spinner(_i18n("Đang tiến hành git pull và đồng bộ mã nguồn...", "Pulling latest code and synchronizing...")):
                        success, update_log = system_updater.perform_git_update()
                    if success:
                        st.balloons()
                        st.success(_i18n("Đã cập nhật lên phiên bản mới nhất thành công! Vui lòng bấm nút bên dưới để tải lại WebUI.",
                                         "Updated to latest version successfully! Click below to reload WebUI."))
                        st.code(update_log)
                        if st.button("🔄 " + _i18n("Tải lại ứng dụng ngay", "Reload Application Now"), key="btn_reload_after_update"):
                            st.rerun(scope="app")
                    else:
                        st.error(_i18n("Cập nhật thất bại. Chi tiết lỗi:", "Update failed. Error details:"))
                        st.code(update_log)
            else:
                st.info(f"✅ **{_i18n('Tuyệt vời! Bạn đang sử dụng phiên bản mới nhất từ GitHub.', 'Great! You are running the latest version from GitHub.')}**")
                c_ahead = git_info.get("commits_ahead", 0)
                if c_ahead > 0:
                    st.caption(f"ℹ️ {_i18n(f'Bạn đang có {c_ahead} commit cục bộ mới hơn remote.', f'You have {c_ahead} local commits ahead of remote.')}")


def _render_create_video_view():
    _render_creation_mode_selector()

    with st.container(key="main_settings_grid"):
        panel = st.columns(4)
    left_panel = panel[0]
    middle_panel = panel[1]
    audio_panel = panel[2]
    right_panel = panel[3]

    params = VideoParams(video_subject="")
    params.match_materials_to_script = bool(
        st.session_state.get("match_materials_to_script", False)
    )
    _render_script_settings(left_panel, params)

    uploaded_files = _render_video_settings(middle_panel, params)
    uploaded_audio_file, uploaded_bgm_file, voice_mode = _render_audio_settings(
        audio_panel, params
    )

    _render_subtitle_settings(right_panel, params)

    _render_live_video_preview(params)

    generation_submitted = _render_generation_controls(
        params,
        uploaded_files,
        uploaded_audio_file,
        uploaded_bgm_file,
        voice_mode,
    )

    if not generation_submitted:
        _save_runtime_config()


def _render_application():
    """Render top bar, handle modal dialogs/presets, and render the 5 main tabs:
    1. Tạo video (Create Video)
    2. Video đã tạo (Generated Videos)
    3. Nhập API AI (AI API Settings)
    4. Hướng dẫn sử dụng (User Guide)
    5. Tự kiểm tra & Cập nhật (System & Update)
    """
    _render_top_bar()

    if st.session_state.get("settings_dialog_open", False):
        _render_settings_dialog()

    if _apply_pending_settings_preset():
        st.success(tr("Settings Preset Imported"))

    restore_applied = _apply_pending_task_restore()
    restore_candidate_id = st.session_state.get("task_restore_candidate_id")
    if restore_candidate_id:
        _render_task_restore_dialog(restore_candidate_id)
    restore_succeeded = st.session_state.pop("task_restore_succeeded", False)
    if restore_applied or restore_succeeded:
        st.success(tr("Task Configuration Loaded"))

    # Main Navigation: 5 Tabs
    tab_labels = [
        f"🎬 {_i18n('Tạo video', 'Create Video')}",
        f"📁 {_i18n('Video đã tạo', 'Generated Videos')}",
        f"🔑 {_i18n('Nhập API AI', 'AI API Settings')}",
        f"📖 {_i18n('Hướng dẫn sử dụng', 'User Guide')}",
        f"⚡ {_i18n('Tự kiểm tra & Cập nhật', 'System & Update')}",
    ]
    main_tabs = st.tabs(tab_labels)

    with main_tabs[0]:
        _render_create_video_view()

    with main_tabs[1]:
        _render_generated_videos_tab()

    with main_tabs[2]:
        _render_api_settings_view()

    with main_tabs[3]:
        _render_user_guide_view()

    with main_tabs[4]:
        _render_system_diagnostic_and_update_view()


_render_application()

