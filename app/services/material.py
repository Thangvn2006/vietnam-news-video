import base64
from concurrent.futures import ThreadPoolExecutor
import io
import math
import os
import random
import tempfile
import threading
import time
import uuid
import warnings
from pathlib import Path
from typing import Any, Callable, List
from urllib.parse import quote_plus, urlencode, urlsplit, urlunsplit

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip
from PIL import Image, UnidentifiedImageError

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import (
    material_cache,
    metaso_minimax,
    muapi,
    ofox,
    task_artifacts,
    video,
    volcengine_seedance,
)
from app.utils import logging_utils, utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()

# A provider URL can point to an unexpectedly large object or never-ending stream.
# Short stock and generated clips should stay well below this conservative cap.
MAX_VIDEO_DOWNLOAD_BYTES = 512 * 1024 * 1024

# The default is to maintain serialization, which is consistent with the behavior of the old version; if necessary, the number of concurrency of inventory materials can be increased in the configuration.
_DEFAULT_MATERIAL_CONCURRENCY = 1


def _get_material_concurrency() -> int:
    try:
        concurrency = int(config.app.get("material_concurrency", _DEFAULT_MATERIAL_CONCURRENCY))
    except (TypeError, ValueError):
        concurrency = _DEFAULT_MATERIAL_CONCURRENCY
    return max(1, min(8, concurrency))


class _OpenAIImageDecodeError(ValueError):
    """Indicates that the bytes returned by the compatible interface cannot be decoded into images, and does not include local file writing failures."""


class OpenAIImagePaidResultError(RuntimeError):
    """A paid image request cannot provide a usable local video material."""


class OpenAIImageUnconfirmedError(OpenAIImagePaidResultError):
    """A paid image request may have succeeded without returning a response."""


def _safe_public_url(value: Any) -> str | None:
    """
    Keep only publicly viewable HTTP(S) page addresses and remove query parameters and credentials.

    The material download address may carry API Key, signed JWT or temporary token. The task list only requires
    To help users return to the supplier's public material page, authentication parameters should not be saved; URL in the form of user information
    Also reject and avoid content such as ``https://user:pass@example.com``.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _creator_info(value: Any) -> dict[str, str] | None:
    """Extract unified public fields from author structures from different vendors."""
    if isinstance(value, str) and value.strip():
        return {"name": value.strip()}
    if not isinstance(value, dict):
        return None

    creator: dict[str, str] = {}
    creator_id = value.get("id")
    creator_name = value.get("name") or value.get("username")
    creator_page = _safe_public_url(
        value.get("url") or value.get("profile_url") or value.get("profile_page")
    )
    if creator_id is not None:
        creator["id"] = str(creator_id)
    if creator_name:
        creator["name"] = str(creator_name)
    if creator_page:
        creator["profile_page"] = creator_page
    return creator or None


def _material_source_record(item: MaterialInfo, local_path: str) -> dict[str, Any]:
    """
    Generate lightweight provenance records for successfully downloaded assets.

    ``source_info`` may come from cache or even from an externally constructed ``MaterialInfo``, so
    Cannot be written as is. It is restructured according to the whitelist, and only the public page, business logo and size are retained.
    And only record the local file name to prevent the user directory or Docker mounting path from entering the task file.
    """
    source = item.source_info if isinstance(item.source_info, dict) else {}
    record: dict[str, Any] = {
        "provider": str(item.provider or source.get("provider") or ""),
        "local_file": Path(local_path).name,
        "duration": int(item.duration),
    }

    search_term = source.get("search_term")
    asset_id = source.get("asset_id")
    source_page = _safe_public_url(source.get("source_page"))
    if isinstance(search_term, str) and search_term.strip():
        record["search_term"] = search_term.strip()
    if asset_id not in (None, ""):
        record["asset_id"] = str(asset_id)
    if source_page:
        record["source_page"] = source_page

    creator = _creator_info(source.get("creator"))
    if creator:
        record["creator"] = creator

    raw_rendition = source.get("rendition")
    if isinstance(raw_rendition, dict):
        rendition = {}
        for field in ("id", "width", "height"):
            value = raw_rendition.get(field)
            if value not in (None, ""):
                rendition[field] = str(value) if field == "id" else value
        if rendition:
            record["rendition"] = rendition
    return record


def _persist_material_sources(
    task_id: str,
    material_sources: list[dict[str, Any]],
) -> None:
    """
    Add the currently successfully downloaded material sources to the task list.

    Task recording is an auxiliary capability and cannot change the return value of the video download function, nor can it be changed due to disk writing failure.
    Interrupt the main process of film production. ``patch_script_data`` will be responsible for atomic replacement and exception logging; here only
    After success, the quantity is recorded to facilitate confirmation of whether the task traceability information has been placed on the market.
    """
    try:
        saved = task_artifacts.patch_script_data(
            task_id,
            material_sources=material_sources,
        )
        if saved:
            logger.info(
                f"saved material source records: "
                f"task_id={task_id}, count={len(material_sources)}"
            )
    except Exception as exc:
        # task_artifacts itself has been designed for failure degradation, and the last isolation is still retained here.
        # This prevents future implementation adjustments or directory parsing anomalies from accidentally affecting the material download return value.
        logger.warning(
            "failed to persist material source records: "
            f"task_id={task_id}, error={type(exc).__name__}, detail={exc}"
        )


def _get_tls_verify() -> bool:
    # TLS certificate verification is enabled by default to prevent the material search and download process from being tampered with by middlemen.
    # Only in clearly required scenarios such as corporate agency and self-signed certificates, users are allowed to pass
    # Explicitly setting `tls_verify = false` in `config.toml` is temporarily disabled.
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\n"
            f"Please set it in the config.toml file: {config.config_file}\n"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


def _redact_secret(message: str, secret: str) -> str:
    """
    Minimally desensitize the abnormal text that will be written to the log.

    The connection exception for requests may contain the full request URL, while the Pixabay API Key is queried by
    Parameter passing. Here, the original value and the URL-encoded value are replaced at the same time, which not only retains the network error information for troubleshooting,
    This also prevents the key from entering the log file.
    """
    safe_message = str(message)
    if not secret:
        return safe_message

    safe_message = safe_message.replace(secret, "***")
    encoded_secret = quote_plus(secret)
    if encoded_secret != secret:
        safe_message = safe_message.replace(encoded_secret, "***")
    return safe_message


def _redact_request_error(error: Exception, *secrets: str) -> str:
    """
    Keep troubleshooting information for network anomalies while removing API keys and proxy credentials.

    Directly logging only the exception type will lose key context such as DNS, certificates, timeouts, etc. Directly logging the original exception
    It is also possible to echo the full request URL. The unified entrance allows three material suppliers to use the same desensitization rules.
    """
    safe_message = str(error)
    for secret in secrets:
        safe_message = _redact_secret(safe_message, str(secret or ""))
    for proxy_url in config.proxy.values():
        safe_message = _redact_secret(safe_message, str(proxy_url))
    return safe_message


def _is_cloudflare_challenge(response: requests.Response) -> bool:
    """
    Recognize the HTML Challenge returned by Cloudflare instead of treating it as Pixabay JSON.

    Cloudflare usually sets `cf-mitigated: challenge`; some deployments only return `cf-mitigated: challenge` with
    "Just a moment" or challenge-platform HTML, thus preserving content characteristics.
    The response body is only judged in memory and not written to the log to avoid recording large sections of worthless HTML.
    """
    headers = getattr(response, "headers", {}) or {}
    if str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True

    content_type = str(headers.get("content-type", "")).lower()
    if "text/html" not in content_type:
        return False

    body = str(getattr(response, "text", "")).lower()
    return "just a moment" in body or "/cdn-cgi/challenge-platform/" in body


def _matches_video_aspect(
    width: Any,
    height: Any,
    video_aspect: VideoAspect,
    *,
    is_vertical: Any = None,
) -> bool:
    """
    Determine whether the remote material is in the same direction as the target screen.

    The response fields of Pexels, Pixabay and Coverr are not uniform, so use width and height to make a reliable judgment first;
    Coverr uses an explicit ``is_vertical`` Boolean value when some historical responses are missing dimensions.
    Materials whose orientation cannot be confirmed are skipped directly to avoid vertical screen tasks being mixed with horizontal screen materials and causing black borders in the final film.
    """
    aspect = VideoAspect(video_aspect)
    try:
        normalized_width = int(float(width))
        normalized_height = int(float(height))
    except (OverflowError, TypeError, ValueError):
        normalized_width = 0
        normalized_height = 0

    if normalized_width > 0 and normalized_height > 0:
        if aspect == VideoAspect.portrait:
            return normalized_height > normalized_width
        if aspect == VideoAspect.landscape:
            return normalized_width > normalized_height
        return normalized_width == normalized_height

    if isinstance(is_vertical, bool) and aspect != VideoAspect.square:
        return is_vertical == (aspect == VideoAspect.portrait)
    return False


def _filter_materials_by_aspect(
    items: List[MaterialInfo],
    video_aspect: VideoAspect,
) -> List[MaterialInfo]:
    """
    Verify the direction of the cached results again.

    The material search cache is retained for up to 24 hours, and caches written before the upgrade may contain material with mismatched orientations.
    Filtering on the unified cache entry allows the fix to take effect immediately and also protects against third-party providers or old caches
    Distal screening is missed. Old entries whose rendition dimensions cannot be read are treated as unvalidated and skipped.
    """
    aspect = VideoAspect(video_aspect)
    if aspect == VideoAspect.square:
        # Pixabay and Coverr rarely offer native square footage. Square output follows the existing behavior,
        # Accept available candidates and hand them over to the video synthesis stage for cropping to avoid having no material for 1:1 tasks after the upgrade.
        return list(items)

    filtered_items = []
    for item in items:
        source_info = item.source_info if isinstance(item.source_info, dict) else {}
        rendition = source_info.get("rendition")
        rendition = rendition if isinstance(rendition, dict) else {}
        if _matches_video_aspect(
            rendition.get("width"),
            rendition.get("height"),
            aspect,
        ):
            filtered_items.append(item)
    return filtered_items


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/v1/videos/search?{urlencode(params)}"
    logger.info(f"searching videos on pexels: term={search_term!r}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if not isinstance(response, dict) or not isinstance(
            response.get("videos"), list
        ):
            logger.error("pexels video search returned an unsupported response")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            if not isinstance(v, dict):
                continue
            duration = v.get("duration")
            # check if video has desired minimum duration
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration < minimum_duration
            ):
                continue
            video_files = v.get("video_files")
            if not isinstance(video_files, list):
                continue
            # loop through each url to determine the best quality
            for video in video_files:
                if not isinstance(video, dict):
                    continue
                try:
                    w = int(video.get("width"))
                    h = int(video.get("height"))
                except (OverflowError, TypeError, ValueError):
                    continue
                video_url = video.get("link")
                if not isinstance(video_url, str) or not video_url:
                    continue
                if (
                    _matches_video_aspect(w, h, aspect)
                    and w == video_width
                    and h == video_height
                ):
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video_url
                    item.duration = duration
                    item.source_info = {
                        "provider": "pexels",
                        "search_term": search_term,
                        "asset_id": (
                            str(v.get("id")) if v.get("id") is not None else None
                        ),
                        "source_page": _safe_public_url(v.get("url")),
                        "creator": _creator_info(v.get("user")),
                        "rendition": {
                            "id": (
                                str(video.get("id"))
                                if video.get("id") is not None
                                else None
                            ),
                            "width": w,
                            "height": h,
                        },
                    }
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(
            "pexels video search failed: "
            f"error={type(e).__name__}, detail={_redact_request_error(e, api_key)}"
        )

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(
        f"searching videos on pixabay: term={search_term!r}, "
        f"proxy_enabled={bool(config.proxy)}"
    )

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        status_code = int(getattr(r, "status_code", 200))
        headers = getattr(r, "headers", {}) or {}
        content_type = str(headers.get("content-type", ""))
        retry_after = headers.get("retry-after")
        cf_ray = headers.get("cf-ray")

        if _is_cloudflare_challenge(r):
            logger.error(
                "pixabay search was blocked by a Cloudflare challenge: "
                f"status={status_code}, cf_ray={cf_ray or 'unknown'}. "
                "Check the server network or proxy, or use Pexels/Coverr instead."
            )
            return []

        if status_code == 429:
            logger.error(
                "pixabay API rate limit exceeded: "
                f"status=429, retry_after={retry_after or 'unknown'}"
            )
            return []

        if status_code >= 400:
            logger.error(
                "pixabay search request failed: "
                f"status={status_code}, content_type={content_type or 'unknown'}"
            )
            return []

        try:
            response = r.json()
        except ValueError:
            logger.error(
                "pixabay returned an unexpected non-JSON response: "
                f"status={status_code}, content_type={content_type or 'unknown'}"
            )
            return []

        video_items = []
        if not isinstance(response, dict) or not isinstance(
            response.get("hits"), list
        ):
            logger.error("pixabay video search returned an unsupported response")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            if not isinstance(v, dict):
                continue
            duration = v.get("duration")
            # check if video has desired minimum duration
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration < minimum_duration
            ):
                continue
            video_files = v.get("videos")
            if not isinstance(video_files, dict):
                continue
            # loop through each url to determine the best quality
            for video_type, video in video_files.items():
                if not isinstance(video, dict):
                    continue
                try:
                    w = int(video["width"])
                    h = int(video["height"])
                except (KeyError, OverflowError, TypeError, ValueError):
                    continue
                video_url = video.get("url")
                if not isinstance(video_url, str) or not video_url:
                    continue
                # Pixabay rarely returns native square video; 1:1 output continues to accept resolution-satisfying
                # candidates and pruned by the synthesis stage. Horizontal and vertical screens must strictly match the target orientation.
                orientation_matches = aspect == VideoAspect.square or (
                    _matches_video_aspect(w, h, aspect)
                )
                if orientation_matches and w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video_url
                    item.duration = duration
                    item.source_info = {
                        "provider": "pixabay",
                        "search_term": search_term,
                        "asset_id": (
                            str(v.get("id")) if v.get("id") is not None else None
                        ),
                        "source_page": _safe_public_url(v.get("pageURL")),
                        "creator": _creator_info(
                            {
                                "id": v.get("user_id"),
                                "name": v.get("user"),
                            }
                        ),
                        "rendition": {
                            "id": video_type,
                            "width": w,
                            "height": video.get("height"),
                        },
                    }
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        error_message = _redact_request_error(e, api_key)
        logger.error(
            "pixabay search request failed: "
            f"error={type(e).__name__}, detail={error_message}"
        )

    return []


def search_videos_coverr(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Coverr (https://coverr.co) - free HD/4K stock videos,
    subject to Coverr license terms (https://coverr.co/license).

    Coverr API notes (based on official docs at api.coverr.co/docs/):
      - Authentication: Authorization: Bearer <api_key>
      - Search endpoint: GET /videos?query=..., response structure {"hits": [...], ...}
      - Add ?urls=true to directly return the mp4 direct link in the search response
      - The URL is signed JWT (bound API key, no expiration time)
      - Coverr supports filtering horizontal and vertical screen materials through filter=is_vertical:true/false;
        After the response is returned, local verification is still performed based on max_width/max_height or is_vertical.
      - The duration field exists in both number and string forms, and this function accepts both.

    This function uses the urls.mp4_download field as the download address - as per Coverr official documentation
    (https://api.coverr.co/docs/videos/#download-a-video),
    The GET URL itself is treated as a legitimate download event by Coverr and included in statistics.
    No need to call PATCH /videos/:id/stats/downloads anymore.
    """
    aspect = VideoAspect(video_aspect)
    api_key = get_api_key("coverr_api_keys")
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "query": search_term,
        "page_size": 20,
        "urls": "true",
        "sort": "popular",
    }
    # Server-side filtering can directly return target materials from complete search results, avoiding the need to fetch popular results first and then
    # Local filtering results in empty portrait candidates. Square materials do not correspond to Boolean conditions and continue to rely on local width and height verification.
    if aspect == VideoAspect.portrait:
        params["filter"] = "is_vertical:true"
    elif aspect == VideoAspect.landscape:
        params["filter"] = "is_vertical:false"
    query_url = f"https://api.coverr.co/videos?{urlencode(params)}"
    logger.info(f"searching videos on coverr: term={search_term!r}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items: List[MaterialInfo] = []

        if not isinstance(response, dict) or not isinstance(
            response.get("hits"), list
        ):
            logger.error("coverr video search returned an unsupported response")
            return video_items

        for v in response["hits"]:
            if not isinstance(v, dict):
                continue
            # duration may be number(11.625) or string("10.500000") in different responses
            try:
                duration = int(float(v.get("duration") or 0))
            except (OverflowError, TypeError, ValueError):
                continue
            if duration < minimum_duration:
                continue

            video_id = v.get("id")
            urls = v.get("urls")
            if not isinstance(urls, dict):
                continue
            mp4_download_url = urls.get("mp4_download")
            if (
                not video_id
                or not isinstance(mp4_download_url, str)
                or not mp4_download_url
            ):
                continue
            if aspect != VideoAspect.square and not _matches_video_aspect(
                v.get("max_width"),
                v.get("max_height"),
                aspect,
                is_vertical=v.get("is_vertical"),
            ):
                continue

            item = MaterialInfo()
            item.provider = "coverr"
            item.url = mp4_download_url
            item.duration = duration
            item.source_info = {
                "provider": "coverr",
                "search_term": search_term,
                "asset_id": str(video_id),
                "source_page": _safe_public_url(v.get("canonical_url") or v.get("url")),
                "creator": _creator_info(v.get("creator") or v.get("author")),
                "rendition": {
                    "id": "mp4_download",
                    "width": v.get("max_width"),
                    "height": v.get("max_height"),
                },
            }
            video_items.append(item)
        return video_items
    except Exception as e:
        logger.error(
            "coverr video search failed: "
            f"error={type(e).__name__}, detail={_redact_request_error(e, api_key)}"
        )

    return []


# WaveSpeed AI (https://wavespeed.ai) uses Wensheng video model to directly generate materials based on script keywords.
# Shares the MaterialInfo result structure and subsequent download and editing processes with three stock material sources.
WAVESPEED_API_BASE_URL = "https://api.wavespeed.ai/api/v3"
WAVESPEED_DEFAULT_T2V_MODEL = "bytedance/seedance-2.0-fast/text-to-video"
WAVESPEED_POLL_INTERVAL_SECONDS = 2.0
WAVESPEED_RUN_TIMEOUT_SECONDS = 600.0
# Default model bytedance/seedance-2.0-fast/text-to-video only accepts 4-15 seconds; exceeds
# Requests for the range will be directly rejected by the API. The default fragment length of WebUI is 3 seconds, so it must be submitted before
# Before it converges to the model support range, the excess duration will be cut off by the existing editing process according to the duration of the clip.
WAVESPEED_MIN_DURATION_SECONDS = 4
WAVESPEED_MAX_DURATION_SECONDS = 15
# The three failure states have different semantics (model error / user cancellation / platform timeout), but they all mean to the material process
# There is no product for this keyword, so it will be treated as an empty result and handed over to the upper layer to skip the fragment and continue to generate it.
WAVESPEED_FAILURE_STATUSES = frozenset({"failed", "cancelled", "timeout"})
# Keep the same caliber as WaveSpeed official Python SDK / n8n node: 429 and 5xx are temporary
# Failures merit limited backoff retries; 4xx are clear client errors and fail quickly.
WAVESPEED_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# The number of consecutive temporary failures allowed in a single poll. An unlucky GET cannot cause an already billed task to become disconnected.
WAVESPEED_MAX_POLL_RETRIES = 5
# Linear backoff base, nth retry waits for base * n seconds.
WAVESPEED_RETRY_BASE_SECONDS = 1.0
# The number of retries for the same signature address when product download fails. The material has been generated for a fee. Priority will be given to retrying the original one.
# Address, you cannot resubmit a paid generation task just because of a download jitter.
WAVESPEED_MAX_DOWNLOAD_RETRIES = 2


class WaveSpeedUnconfirmedTaskError(RuntimeError):
    """
    The paid build task was submitted, but the final status cannot be confirmed locally.

    This type of exception is by no means equivalent to "the task failed and can be repeated": the remote task may still be running or may have
    Completed and billed. The material process must stop here, and no new paid tasks will be submitted for subsequent keywords.
    And pass the submitted prediction id to the task status for manual retrieval.
    """

    def __init__(self, message: str, prediction_id: str = ""):
        super().__init__(message)
        self.prediction_id = prediction_id


class WaveSpeedDownloadError(RuntimeError):
    """A paid prediction completed, but its video could not be saved locally."""

    def __init__(self, message: str, prediction_id: str = ""):
        super().__init__(message)
        self.prediction_id = prediction_id


def _wavespeed_status_code(response: Any) -> int:
    """Read the response status code; handle it as 200 when this field is missing from the test double or exception object."""
    try:
        return int(getattr(response, "status_code", 200))
    except (TypeError, ValueError):
        return 200


def _is_wavespeed_retryable_error(error: Exception) -> bool:
    """
    Determine whether the polling exception is worth retrying.

    Network abnormalities such as connection and timeout do not have status codes and are handled as temporary faults; responses with status codes are only
    429 and 5xx, consistent with the official SDK's retry set.
    """
    if isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    response = getattr(error, "response", None)
    if response is not None:
        return _wavespeed_status_code(response) in WAVESPEED_RETRYABLE_STATUS_CODES
    return False


def _wavespeed_duration_bounds() -> tuple[int, int]:
    """
    Returns the generation duration interval (seconds) supported by the current model.

    The default interval corresponds to the default Seedance model; when users switch to other Vincent video models, they can
    Synchronize the adjustment interval in the configuration. Any abnormal configuration will fall back to the default value and ensure min <= max,
    Avoid turning user input into a remote request that must fail.
    """

    def read_bound(key: str, fallback: int) -> int:
        try:
            value = int(config.app.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if value >= 1 else fallback

    min_duration = read_bound("wavespeed_min_duration", WAVESPEED_MIN_DURATION_SECONDS)
    max_duration = read_bound("wavespeed_max_duration", WAVESPEED_MAX_DURATION_SECONDS)
    return min_duration, max(max_duration, min_duration)


def generate_videos_wavespeed(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    Use the WaveSpeed Vincent video model to generate a piece of footage for a script keyword.

    Maintaining the same signature and empty list failure convention as the search_videos_* of the stock source,
    Allowing it to be directly connected to the universal download and duration accounting process of ``download_videos``.
    ``minimum_duration`` in the context of generation is the target segment duration in seconds.
    """
    aspect = VideoAspect(video_aspect)
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("wavespeed_api_keys")
    model_id = (
        str(
            config.app.get("wavespeed_text_to_video_model", "")
            or WAVESPEED_DEFAULT_T2V_MODEL
        )
        .strip()
        .strip("/")
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    requested_duration = max(int(minimum_duration), 1)
    min_duration, max_duration = _wavespeed_duration_bounds()
    duration = min(max(requested_duration, min_duration), max_duration)
    if duration != requested_duration:
        # Generating longer than requested will not affect the final film: the editing process is still trimmed according to the duration of the clip; generating longer than requested
        # The shorter situation only occurs when the request exceeds the upper limit of the model, and it can only converge to the upper limit at this time.
        logger.info(
            f"wavespeed clip duration clamped to model-supported range: "
            f"requested={requested_duration}s, using={duration}s "
            f"(supported {min_duration}-{max_duration}s)"
        )
    payload = {
        "prompt": search_term,
        "aspect_ratio": aspect.value,
        "duration": duration,
    }
    logger.info(
        f"generating video on wavespeed: model={model_id}, "
        f"term={search_term!r}, duration={duration}s"
    )

    # Submitting POST will never automatically retry: the request may have created a paid task on the remote end, and resending will cause
    # Repeated generation and repeated deductions (consistent with the submission policy of the official SDK).
    try:
        submit_response = requests.post(
            f"{WAVESPEED_API_BASE_URL}/{model_id}",
            json=payload,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
    except Exception as e:
        # Not receiving a response does not mean that the task was not created. The status is unknown at this time and the entire generation must be terminated
        # process instead of continuing to submit new paid tasks for the next keyword.
        raise WaveSpeedUnconfirmedTaskError(
            "wavespeed submission did not return a response, the task may "
            "already exist remotely: "
            f"error={type(e).__name__}, detail={_redact_request_error(e, api_key)}"
        ) from e

    submit_status = _wavespeed_status_code(submit_response)
    if submit_status >= 500:
        # 5xx may occur after the task is created, and it is impossible to determine whether it has been billed.
        raise WaveSpeedUnconfirmedTaskError(
            f"wavespeed submission failed with HTTP {submit_status}, "
            "the task may already exist remotely"
        )
    try:
        submit_body = submit_response.json()
    except Exception as e:
        raise WaveSpeedUnconfirmedTaskError(
            "wavespeed submission returned an unreadable response, the task "
            f"may already exist remotely: error={type(e).__name__}"
        ) from e

    submit_data = submit_body.get("data") if isinstance(submit_body, dict) else None
    if not isinstance(submit_body, dict) or submit_body.get("code") != 200:
        # 4xx and business error codes are clear rejections. There is no task created at the remote end, so there is no duplication.
        # Billing risk, return empty result and continue according to existing material source agreement.
        logger.error(
            "wavespeed video generation request rejected: "
            f"http_status={submit_status}, "
            f"code={submit_body.get('code') if isinstance(submit_body, dict) else None}, "
            f"detail={_redact_secret(str((submit_body or {}).get('message') or ''), api_key)}"
        )
        return []
    prediction_id = (
        str(submit_data.get("id") or "") if isinstance(submit_data, dict) else ""
    )
    if not prediction_id:
        # The submission was accepted but no ID was obtained: the task may already exist but cannot be tracked, and the order cannot be continued.
        raise WaveSpeedUnconfirmedTaskError(
            "wavespeed accepted the submission without returning a prediction id"
        )
    # If the generation task is successfully submitted, remote billing side effects will occur. The log record task ID is first entered.
    # Even if subsequent polling fails, users can still retrieve the product in the WaveSpeed console with the ID.
    logger.info(f"wavespeed prediction created: id={prediction_id}")

    result_data = _wait_for_wavespeed_prediction(
        prediction_id=prediction_id,
        headers=headers,
        api_key=api_key,
    )
    if result_data is None:
        return []

    try:
        video_items = []
        outputs = result_data.get("outputs")
        for output in outputs if isinstance(outputs, list) else []:
            # The product URL is a signed temporary download address and must be retained in its entirety (query parameters cannot be stripped off).
            # number), so source_info is not written and is only used for subsequent immediate downloads.
            if not isinstance(output, str) or not output.startswith(
                ("http://", "https://")
            ):
                continue
            item = MaterialInfo()
            item.provider = "wavespeed"
            item.url = output
            item.duration = duration
            item.source_info = {
                "provider": "wavespeed",
                "search_term": search_term,
                "asset_id": prediction_id,
                "rendition": {
                    "id": None,
                    "width": video_width,
                    "height": video_height,
                },
            }
            video_items.append(item)
        if not video_items:
            logger.error(
                "wavespeed prediction completed without downloadable outputs: "
                f"id={prediction_id}"
            )
        return video_items
    except Exception as e:
        # The product has been generated and billed, and the exception here can only come from local parsing. After recording, press the empty result
        # Return, allowing the upper layer to skip the segment, but the task status itself is determined and subsequent segments can be continued.
        logger.error(
            "wavespeed output parsing failed: "
            f"id={prediction_id}, error={type(e).__name__}, "
            f"detail={_redact_request_error(e, api_key)}"
        )

    return []


def _wait_for_wavespeed_prediction(
    *,
    prediction_id: str,
    headers: dict,
    api_key: str,
) -> dict | None:
    """
    Poll the same prediction id until a confirmed result occurs.

    Returns ``completed`` data; the remote end explicitly failed (failed / canceled / timeout)
    Returning None indicates that the task has ended and it is safe to continue with subsequent fragments. Temporary fault button
    Linear backoff retries the same ID and never resubmits the task; thrown when the status cannot be confirmed
    :class:`WaveSpeedUnconfirmedTaskError`, the caller terminates the entire generation process.
    """
    deadline = time.monotonic() + WAVESPEED_RUN_TIMEOUT_SECONDS
    consecutive_failures = 0
    while True:
        try:
            response = requests.get(
                f"{WAVESPEED_API_BASE_URL}/predictions/{prediction_id}/result",
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(30, 60),
            )
            status_code = _wavespeed_status_code(response)
            if status_code in WAVESPEED_RETRYABLE_STATUS_CODES:
                raise requests.exceptions.HTTPError(
                    f"HTTP {status_code}", response=response
                )
            result_body = response.json()
            result_data = (
                result_body.get("data") if isinstance(result_body, dict) else None
            )
            if not isinstance(result_body, dict) or result_body.get("code") != 200:
                # When polling is explicitly rejected (e.g. 4xx) the task status remains unknown: the task has been submitted,
                # It’s just that the results cannot be found locally, and you cannot continue to submit new paid tasks.
                raise WaveSpeedUnconfirmedTaskError(
                    "wavespeed prediction status is unknown: "
                    f"http_status={status_code}, "
                    f"code={result_body.get('code') if isinstance(result_body, dict) else None}, "
                    f"detail={_redact_secret(str((result_body or {}).get('message') or ''), api_key)}",
                    prediction_id=prediction_id,
                )
            if not isinstance(result_data, dict):
                raise WaveSpeedUnconfirmedTaskError(
                    "wavespeed prediction result payload is malformed",
                    prediction_id=prediction_id,
                )
        except WaveSpeedUnconfirmedTaskError:
            raise
        except Exception as e:
            if not _is_wavespeed_retryable_error(e):
                raise WaveSpeedUnconfirmedTaskError(
                    "wavespeed prediction polling failed and the task state is "
                    f"unknown: error={type(e).__name__}, "
                    f"detail={_redact_request_error(e, api_key)}",
                    prediction_id=prediction_id,
                ) from e
            consecutive_failures += 1
            if consecutive_failures > WAVESPEED_MAX_POLL_RETRIES:
                raise WaveSpeedUnconfirmedTaskError(
                    "wavespeed prediction polling failed after "
                    f"{WAVESPEED_MAX_POLL_RETRIES + 1} attempts, the task may "
                    "still be running remotely: "
                    f"error={type(e).__name__}, "
                    f"detail={_redact_request_error(e, api_key)}",
                    prediction_id=prediction_id,
                ) from e
            delay = WAVESPEED_RETRY_BASE_SECONDS * consecutive_failures
            logger.warning(
                "wavespeed prediction polling hit a transient error, retry the "
                f"same task: id={prediction_id}, "
                f"attempt={consecutive_failures}/{WAVESPEED_MAX_POLL_RETRIES}, "
                f"error={type(e).__name__}, retry_in={delay:.1f}s"
            )
            time.sleep(delay)
            continue

        # The count is reset when a valid response is received, and the retry quota is consumed only if there are consecutive failures.
        consecutive_failures = 0
        status = str(result_data.get("status") or "")
        if status == "completed":
            return result_data
        if status in WAVESPEED_FAILURE_STATUSES:
            logger.error(
                "wavespeed prediction did not produce a video: "
                f"id={prediction_id}, status={status}, "
                f"detail={_redact_secret(str(result_data.get('error') or ''), api_key)}"
            )
            return None
        if time.monotonic() > deadline:
            # The remote task is still executing, and the final status cannot be confirmed locally, so orders must be stopped.
            raise WaveSpeedUnconfirmedTaskError(
                f"wavespeed prediction is still {status or 'pending'} after "
                f"{WAVESPEED_RUN_TIMEOUT_SECONDS:.0f}s of local waiting",
                prediction_id=prediction_id,
            )
        time.sleep(WAVESPEED_POLL_INTERVAL_SECONDS)


def _save_generated_video_with_retry(
    video_url: str, save_dir: str, provider: str
) -> str:
    """
    Download the product that has been paid for. If it fails, try again with the same address first.

    The cost of regenerating a remote task is to pay again, so downloading jitter must first be done at the original address.
    Make a limited number of backoff retries. After the retries are exhausted, the caller will report a recoverable paid task failure.
    """
    for attempt in range(WAVESPEED_MAX_DOWNLOAD_RETRIES + 1):
        try:
            saved_video_path = save_video(video_url=video_url, save_dir=save_dir)
            if saved_video_path:
                return saved_video_path
            failure_detail = "empty result"
        except Exception as e:
            failure_detail = (
                f"error={type(e).__name__}, "
                f"detail={_redact_request_error(e, video_url)}"
            )
        if attempt >= WAVESPEED_MAX_DOWNLOAD_RETRIES:
            break
        delay = WAVESPEED_RETRY_BASE_SECONDS * (attempt + 1)
        logger.warning(
            "failed to download generated video, retry the same url: "
            f"provider={provider}, "
            f"attempt={attempt + 1}/{WAVESPEED_MAX_DOWNLOAD_RETRIES}, "
            f"{failure_detail}, retry_in={delay:.1f}s"
        )
        time.sleep(delay)
    logger.error(
        "failed to download generated video after "
        f"{WAVESPEED_MAX_DOWNLOAD_RETRIES + 1} attempts: "
        f"provider={provider}, {failure_detail}"
    )
    return ""


def _get_downloaded_video_duration(video_path: str) -> float:
    """Read the usable duration from the downloaded media file."""
    clip = None
    try:
        clip = VideoFileClip(video_path)
        duration = float(clip.duration or 0)
    except Exception as exc:
        raise ValueError(
            f"downloaded video duration could not be measured: {video_path}"
        ) from exc
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception as close_error:
                logger.warning(
                    "failed to close downloaded video after duration probe: "
                    f"path={video_path}, error={type(close_error).__name__}, "
                    f"detail={close_error}"
                )
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(
            f"downloaded video duration is not positive and finite: {video_path}"
        )
    return duration


# No logs are generated during requests streaming download. A single 4K clip takes several minutes to download on a slow network.
# The task looks like it's stuck. If the download is not completed after this interval, the downloaded size and speed will be recorded; within the interval
# The completed small file does not generate additional logs and avoids screen refresh.
_DOWNLOAD_HEARTBEAT_SECONDS = 15.0
_BYTES_PER_MEGABYTE = 1024 * 1024


def _describe_download_progress(
    downloaded_bytes: int, declared_size: int, elapsed_seconds: float
) -> str:
    """Organize the download progress into a log text; no percentage will be given when the server does not declare the size."""
    downloaded_mb = downloaded_bytes / _BYTES_PER_MEGABYTE
    speed = downloaded_mb / elapsed_seconds if elapsed_seconds > 0 else 0.0
    if declared_size > 0:
        percent = min(100, round(downloaded_bytes * 100 / declared_size))
        return (
            f"{downloaded_mb:.1f} of {declared_size / _BYTES_PER_MEGABYTE:.1f} MB "
            f"({percent}%), {speed:.2f} MB/s"
        )
    return f"{downloaded_mb:.1f} MB, {speed:.2f} MB/s"


def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    # Query parameters can identify the asset itself (for example,
    # /download?file_id=123). Dropping the query makes unrelated paid videos
    # share one cache entry and silently reuse the first scene. Fragments are
    # not sent in HTTP requests, so they do not affect the downloaded bytes.
    url_hash = utils.md5(video_url.split("#", 1)[0])
    video_id = f"vid-{url_hash}"
    video_path = os.path.join(save_dir, f"{video_id}.mp4")

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # A nonempty file is treated as a cache hit above. Keep the final path
    # unpublished until the complete download has passed media validation, so a
    # failed download cannot poison this or another concurrent task's cache.
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{video_id}-",
            suffix=".mp4",
            dir=save_dir,
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            with requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(60, 240),
                stream=True,
            ) as response:
                response.raise_for_status()
                headers = getattr(response, "headers", {}) or {}
                try:
                    declared_size = int(headers.get("Content-Length", ""))
                except (TypeError, ValueError):
                    declared_size = 0
                if declared_size > MAX_VIDEO_DOWNLOAD_BYTES:
                    raise ValueError("video download exceeds 512 MB limit")

                downloaded_bytes = 0
                started_at = time.monotonic()
                last_report_at = started_at
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        downloaded_bytes += len(chunk)
                        if downloaded_bytes > MAX_VIDEO_DOWNLOAD_BYTES:
                            raise ValueError("video download exceeds 512 MB limit")
                        temp_file.write(chunk)
                        now = time.monotonic()
                        if now - last_report_at >= _DOWNLOAD_HEARTBEAT_SECONDS:
                            last_report_at = now
                            # Only cached file names are recorded: the download address may have a signature or key.
                            logger.info(
                                f"downloading video {video_id}.mp4: "
                                + _describe_download_progress(
                                    downloaded_bytes,
                                    declared_size,
                                    now - started_at,
                                )
                            )

        if os.path.getsize(temp_path) == 0:
            return ""

        clip = None
        try:
            clip = VideoFileClip(temp_path)
            duration = clip.duration
            fps = clip.fps
            if not (duration > 0 and fps > 0):
                logger.warning(f"invalid video file: {temp_path} => invalid duration or fps")
                return ""
        except Exception as e:
            logger.warning(f"invalid video file: {temp_path} => {str(e)}")
            return ""
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {temp_path}, error: {str(close_error)}"
                    )

        os.replace(temp_path, video_path)
        temp_path = ""
        return video_path
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
            except OSError as remove_error:
                logger.warning(
                    f"failed to remove temporary video file: {temp_path}, "
                    f"error: {str(remove_error)}"
                )


# OpenAI is compatible with generation graphs (Issue #1274) through the /images/generations protocol as script keywords
# Generate picture materials, which can be pointed to the local ComfyUI/SD gateway or used for various OpenAI protocol transfers
# service. The generated image is immediately rendered into a "slowly enlarged" mp4 fragment of the same style as the local material, which is useful for downstream
# The editing process is completely transparent.
OPENAI_IMAGE_ENDPOINT_PATH = "images/generations"
# OpenAI's official image interface only accepts the size specified by the model and cannot directly transfer video resolution (such as 1080x1920).
# When openai_image_size is left blank, the following compatible default values are used according to the frame; the local gateway can be explicitly configured to override.
OPENAI_IMAGE_DEFAULT_SIZES = {
    VideoAspect.portrait: "1024x1536",
    VideoAspect.landscape: "1536x1024",
    VideoAspect.square: "1024x1024",
}
# Keep the same retry caliber as WaveSpeed: 429 and 5xx are temporary failures and require a limited number of backoff retries.
OPENAI_IMAGE_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# 401/403 means that the current key is explicitly rejected. Get_api_key rotates the key every time it is called, and multiple configurations are configured.
# The key will be automatically changed when retrying; when there is only one key, it will fail quickly and there will be no meaningless retries.
OPENAI_IMAGE_KEY_ERROR_STATUS_CODES = frozenset({401, 403})
OPENAI_IMAGE_MAX_ATTEMPTS = 3
OPENAI_IMAGE_MAX_BYTES = 25 * 1024 * 1024
OPENAI_IMAGE_MAX_PIXELS = 50_000_000
# Serial output + linear backoff, compatible with the common current limit recovery window of transit services.
OPENAI_IMAGE_RETRY_BACKOFF_SECONDS = (5, 15, 30)
# The synchronous generation interface may take tens of seconds to return the image, and the read timeout gives sufficient margin.
OPENAI_IMAGE_REQUEST_TIMEOUT = (30, 300)
# Retry downloading after the picture has been charged on a per-picture basis: Give priority to retrying the original address instead of regenerating the same picture.
OPENAI_IMAGE_MAX_DOWNLOAD_ATTEMPTS = 3
OPENAI_IMAGE_DOWNLOAD_BACKOFF_SECONDS = 2


def is_openai_image_enabled(app_config: dict | None = None) -> bool:
    """
    Determine whether the OpenAI compatible Vincent picture material source has completed the minimum configuration.

    API Key is allowed to be empty: completely local ComfyUI/SD gateway usually does not require authentication, when it is empty
    Request without Authorization header. For task pre-checking and WebUI, LLM and TTS credits are consumed
    Pre-intercept tasks with missing configurations.
    """
    app_config = config.app if app_config is None else app_config
    return bool(
        str(app_config.get("openai_image_base_url", "") or "").strip()
        and str(app_config.get("openai_image_model", "") or "").strip()
    )


def _openai_image_endpoint() -> tuple[str, str]:
    """
    Read the Vincent diagram endpoint and model name, and throw an error with configuration guidance when missing.
    """
    base_url = (
        str(config.app.get("openai_image_base_url", "") or "").strip().rstrip("/")
    )
    model = str(config.app.get("openai_image_model", "") or "").strip()
    if not base_url:
        raise ValueError(
            "\n\n##### openai_image_base_url is not set #####\n\n"
            f"Please set it in the config.toml file: {config.config_file}\n"
        )
    if not model:
        raise ValueError(
            "\n\n##### openai_image_model is not set #####\n\n"
            f"Please set it in the config.toml file: {config.config_file}\n"
        )
    return f"{base_url}/{OPENAI_IMAGE_ENDPOINT_PATH}", model


def _openai_image_size(video_aspect: VideoAspect) -> str:
    """
    Parse the requested image size.

    The official OpenAI interface only accepts the size specified by the model (such as 1024x1536) and directly transmits the video resolution.
    (such as 1080x1920) will return 400. By default, the compatible size is taken according to the frame; ``openai_image_size``
    Overrides can be configured explicitly for use by native gateways (such as SD WebUI) that support any resolution.
    """
    configured = str(config.app.get("openai_image_size", "") or "").strip()
    if configured:
        return configured
    return OPENAI_IMAGE_DEFAULT_SIZES.get(VideoAspect(video_aspect), "1024x1024")


def _openai_image_prompt(search_term: str) -> str:
    """
    Wrap script keywords into final prompt words.

    Optional configuration ``openai_image_prompt_template`` supports ``{term}`` placeholder,
    Used to unify additional style modifications (such as image quality, composition, lens language) and improve image-text matching:

    .. code-block:: toml

        openai_image_prompt_template = "cinematic photo of {term}, photorealistic"

    If left blank or without placeholder, the original text of the keyword will be returned, and the behavior will be exactly the same as the old version. placeholder
    If the replacement fails (for example, the formatting syntax is mistakenly written in the template), the original text will also be rolled back to prevent configuration errors from interrupting.
    The entire build task.
    """
    template = str(config.app.get("openai_image_prompt_template", "") or "").strip()
    if not template or "{term}" not in template:
        return search_term
    try:
        return template.replace("{term}", search_term)
    except Exception:
        return search_term


def _response_json_safely(response: Any) -> Any:
    """Read response JSON; returns None if test double or exception response fails to parse."""
    try:
        return response.json()
    except Exception:
        return None


def _openai_image_response_message(body: Any) -> str:
    """
    Extract human-readable error descriptions from OpenAI-compatible responses.

    The standard format is ``{"error": {"message": ...}}``, and transit services often degenerate into
    ``{"message": ...}`` or give a string directly. If neither can be obtained, an empty string is returned, which is determined by the caller
    Determines whether to fall back to the response body.
    """
    if not isinstance(body, dict):
        return str(body or "")[:300]
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")[:300]
    if error is not None:
        return str(error)[:300]
    return str(body.get("message") or "")[:300]


def _openai_image_http_failure(response: Any, status: int, api_key: str) -> str:
    """Organize HTTP error responses into a desensitized log readable description."""
    message = _openai_image_response_message(_response_json_safely(response))
    if not message:
        message = str(getattr(response, "text", "") or "")[:300]
    return f"HTTP {status}: {_redact_secret(message, api_key)}"


def _openai_image_download_bytes(
    image_url: str,
    api_key: str,
) -> tuple[bytes | None, str]:
    """
    Download the temporary URL of the generated image.

    Pictures are charged on a per-picture basis. When downloading fails, priority is given to retrying the original address instead of falling back to regenerating.
    Avoid paying twice for the same picture. An error is thrown when all retries fail, preventing the outer layer from purchasing the next picture.
    """
    failure_detail = "no download attempt was made"
    for attempt in range(1, OPENAI_IMAGE_MAX_DOWNLOAD_ATTEMPTS + 1):
        response = None
        try:
            response = requests.get(
                image_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/115.0.0.0 Safari/537.36"
                },
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(30, 120),
                stream=True,
            )
            if response.status_code == 200:
                try:
                    declared_size = int(response.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    declared_size = 0
                if declared_size > OPENAI_IMAGE_MAX_BYTES:
                    return None, "generated image exceeds the 25 MB download limit"
                image_bytes = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if len(image_bytes) + len(chunk) > OPENAI_IMAGE_MAX_BYTES:
                        return None, "generated image exceeds the 25 MB download limit"
                    image_bytes.extend(chunk)
                if image_bytes:
                    return bytes(image_bytes), ""
                failure_detail = "generated image download was empty"
            else:
                failure_detail = f"HTTP {response.status_code} while downloading image"
        except Exception as e:
            failure_detail = (
                f"error={type(e).__name__}, "
                f"detail={_redact_request_error(e, api_key, image_url)}"
            )
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception as exc:
                    logger.warning(
                        "failed to close generated image download response: "
                        f"error={type(exc).__name__}"
                    )
        if attempt < OPENAI_IMAGE_MAX_DOWNLOAD_ATTEMPTS:
            logger.warning(
                "generated image download failed, retrying the same url: "
                f"attempt={attempt}/{OPENAI_IMAGE_MAX_DOWNLOAD_ATTEMPTS}, "
                f"{failure_detail}"
            )
            time.sleep(OPENAI_IMAGE_DOWNLOAD_BACKOFF_SECONDS)
    raise OpenAIImagePaidResultError(
        "generated image could not be downloaded after retries"
    )


def _parse_openai_image_response(
    response: Any,
    api_key: str,
) -> tuple[bytes | None, str]:
    """
    Parse the /images/generations response and retrieve url or b64_json image data.

    If the parsing failure belongs to a clear business rejection (such as content policy) or an abnormal response format, it will be returned directly.
    Error description, retry without backoff - resending the same request will only get the same result.
    """
    body = _response_json_safely(response)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data:
        return None, _redact_secret(_openai_image_response_message(body), api_key)

    entry = data[0]
    if not isinstance(entry, dict):
        return None, "invalid image data entry"

    b64_payload = entry.get("b64_json")
    if b64_payload:
        if (
            not isinstance(b64_payload, str)
            or len(b64_payload) > 4 * ((OPENAI_IMAGE_MAX_BYTES + 2) // 3)
        ):
            return None, "generated image exceeds the 25 MB response limit"
        try:
            image_bytes = base64.b64decode(b64_payload)
        except Exception as e:
            return None, f"invalid b64_json payload: {type(e).__name__}"
        if len(image_bytes) > OPENAI_IMAGE_MAX_BYTES:
            return None, "generated image exceeds the 25 MB response limit"
        return image_bytes, ""

    image_url = entry.get("url")
    if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
        return _openai_image_download_bytes(image_url, api_key)

    return None, "image response has neither url nor b64_json"


def _request_openai_image(endpoint: str, payload: dict) -> tuple[bytes | None, str]:
    """
    Call the OpenAI compatible /images/generations interface with backoff retries and key rotation.

    429/5xx Retry according to temporary failure backoff; 401/403 Retry only when multiple keys are configured
    (Use the rotation mechanism of get_api_key to change the key); the remaining 4xx is explicitly rejected and fails quickly.
    Leave it to the upper level to skip this keyword.

    Billing security: POST read timeout and connection interruption are regarded as "unconfirmed" status - the server may have
    Generate and charge, but the response is not returned. Automatic resubmission may cause repeated generation and duplication.
    Billing is therefore thrown to terminate the local task with a dedicated exception. Only the connection phase timeout (ConnectTimeout,
    (The request has not been delivered to the server) Only then it is confirmed that no generation task has been created and you can safely try again.

    API Key is allowed to be empty: completely local ComfyUI/SD gateway usually does not require authentication, when it is empty
    No Authorization header is sent.
    """
    api_keys = config.app.get("openai_image_api_keys")
    if isinstance(api_keys, (list, tuple)):
        configured_keys = [k for k in api_keys if str(k or "").strip()]
    elif str(api_keys or "").strip():
        configured_keys = [api_keys]
    else:
        configured_keys = []

    failure_detail = "no request attempt was made"
    for attempt in range(1, OPENAI_IMAGE_MAX_ATTEMPTS + 1):
        api_key = get_api_key("openai_image_api_keys") if configured_keys else ""
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        retryable = False
        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=OPENAI_IMAGE_REQUEST_TIMEOUT,
            )
        except requests.exceptions.ConnectTimeout as e:
            # Connection phase timeout: The request was determined not to be delivered to the server, and the generation task was not created.
            # It's safe to try again.
            failure_detail = (
                f"connect timeout: detail={_redact_request_error(e, api_key)}"
            )
            retryable = True
        except Exception as e:
            # Read timeout/connection interruption, etc. are in the "unconfirmed" status: the server may have accepted it and deducted the fee.
            # Automatic resubmission or processing of subsequent keywords may be billed repeatedly, and the local task must be terminated.
            raise OpenAIImageUnconfirmedError(
                "unconfirmed image request (no retry to avoid double billing): "
                f"{type(e).__name__}, detail={_redact_request_error(e, api_key)}"
            ) from e
        else:
            status = int(getattr(response, "status_code", 200) or 200)
            if status in OPENAI_IMAGE_KEY_ERROR_STATUS_CODES:
                failure_detail = _openai_image_http_failure(response, status, api_key)
                # Only in multi-key configuration, retries can rotate to available keys.
                retryable = len(configured_keys) > 1
            elif status in OPENAI_IMAGE_RETRYABLE_STATUS_CODES:
                failure_detail = _openai_image_http_failure(response, status, api_key)
                retryable = True
            elif status >= 400:
                return None, _openai_image_http_failure(response, status, api_key)
            else:
                image_bytes, parse_error = _parse_openai_image_response(
                    response, api_key
                )
                if image_bytes is not None:
                    return image_bytes, ""
                failure_detail = parse_error

        if retryable and attempt < OPENAI_IMAGE_MAX_ATTEMPTS:
            backoff_seconds = OPENAI_IMAGE_RETRY_BACKOFF_SECONDS[
                min(attempt - 1, len(OPENAI_IMAGE_RETRY_BACKOFF_SECONDS) - 1)
            ]
            logger.warning(
                "openai image request failed, retrying: "
                f"attempt={attempt}/{OPENAI_IMAGE_MAX_ATTEMPTS}, "
                f"next_retry_in={backoff_seconds}s, detail={failure_detail}"
            )
            time.sleep(backoff_seconds)
            continue
        return None, failure_detail

    return None, failure_detail


def _save_openai_image_file(
    image_bytes: bytes,
    save_dir: str,
) -> tuple[str, int, int]:
    """
    Standardize the generated result into a PNG and return (path, width, height).

    Unified conversion to PNG can avoid two types of problems: the transfer service returns WebP/JPEG but is not reliable
    extension, and pictures carrying abnormal metadata cause MoviePy to fail to parse (with local materials
    In response to the purification logic, standardization is completed during the placement stage).
    """
    if not save_dir:
        save_dir = utils.storage_dir("cache_images", create=True)
    elif not os.path.isdir(save_dir):
        os.makedirs(save_dir, exist_ok=True)

    image_path = os.path.join(save_dir, f"openai-image-{uuid.uuid4().hex[:12]}.png")
    if len(image_bytes) > OPENAI_IMAGE_MAX_BYTES:
        raise _OpenAIImageDecodeError("generated image exceeds the 25 MB limit")

    # Image decoding failure can be downgraded to "skip current keyword", but directory permissions, disk space and file
    # Write failures must continue to be thrown, otherwise the on-demand generation loop will continue to create the file when it cannot be saved locally.
    # Subsequent paid tasks. Image.open only reads memory bytes, so the OSError here belongs to the format
    # Recognition failed; the OSError of image.load corresponds to truncated or damaged image data.
    image = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(image_bytes))
            if image.width * image.height > OPENAI_IMAGE_MAX_PIXELS:
                raise ValueError("generated image exceeds the 50 million pixel limit")
            image.load()
    except (
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        if image is not None:
            image.close()
        raise _OpenAIImageDecodeError(f"{type(exc).__name__}: {exc}") from exc

    with image:
        if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            image = image.convert("RGB")
        # save is not placed in the decoding exception protection zone: a write error indicates that the operating environment continues to be unavailable and should be
        # Terminate the entire task to prevent subsequent keywords from continuing to generate paid images that cannot be placed on the market.
        image.save(image_path, format="PNG")
        width, height = image.size
    return image_path, width, height


def generate_images_openai(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
    save_dir: str = "",
) -> List[MaterialInfo]:
    """
    Use the OpenAI compatible Vincent picture interface to generate an image for a script keyword and save it locally.

    Keeps the same signature and empty list failure convention as generate_videos_wavespeed. No pictures
    Native duration, ``duration`` records the target fragment duration (seconds) for calculation in the on-demand download process
    Have you collected enough dubbing time? The actual size returned by the API may be inconsistent with the request, here it is
    The real size of the image is written into rendition and does not depend on the request parameters.
    """
    aspect = VideoAspect(video_aspect)
    clip_duration = max(int(minimum_duration), 1)
    endpoint, model = _openai_image_endpoint()
    image_size = _openai_image_size(aspect)
    payload = {
        "model": model,
        "prompt": _openai_image_prompt(search_term),
        "n": 1,
        "size": image_size,
    }
    logger.info(
        f"generating image via openai-compatible endpoint: model={model}, "
        f"term={search_term!r}, size={image_size}"
    )
    image_bytes, failure_detail = _request_openai_image(endpoint, payload)
    if image_bytes is None:
        logger.error(
            f"openai image generation failed: term={search_term!r}, "
            f"detail={failure_detail}"
        )
        return []

    try:
        image_path, width, height = _save_openai_image_file(image_bytes, save_dir)
    except _OpenAIImageDecodeError as e:
        # The compatibility layer may return 200 but the body is not an image (such as an HTML error page disguised as JSON,
        # Gateway downgrade prompt page). Pictures that cannot be decoded belong to "The generation has failed", according to the material source
        # It is agreed to return an empty list to allow the upper layer to skip the keyword and continue, rather than letting the exception interrupt the entire task.
        logger.error(
            "openai image response is not a decodable image, skipping term: "
            f"term={search_term!r}, error={type(e).__name__}, detail={e}"
        )
        return []
    item = MaterialInfo()
    item.provider = "openai_image"
    item.url = image_path
    item.duration = clip_duration
    item.source_info = {
        "provider": "openai_image",
        "search_term": search_term,
        "rendition": {
            "id": None,
            "width": width,
            "height": height,
        },
    }
    return [item]


def _render_openai_image_video(image_path: str, clip_duration: int) -> str:
    """
    Render the generated image into an mp4 fragment and reuse the "image → dynamic fragment" pipeline of the local material.

    If rendering fails, an empty string is returned, and the caller stops subsequent payment requests.
    """
    try:
        return video.render_image_zoom_video(image_path, clip_duration)
    except Exception as e:
        logger.error(
            "failed to render generated image as a video clip: "
            f"image={image_path}, error={type(e).__name__}, detail={e}"
        )
        return ""


def _download_videos_openai_image_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """
    Generate OpenAI-compatible Vincent picture materials one by one according to the sequence of script fragments, and stop immediately when the required total time is reached.

    The same payment security semantics as WaveSpeed's on-demand generation: Vincent pictures are charged on a per-picture basis, and the full amount is generated first and then
    Selections will pay for unused footage. Each image is immediately rendered into mp4 fragments and accumulated
    Valid duration (consistent with the inventory process, capped by segment duration), after the required dubbing duration is reached in total
    No new payment requests will be initiated. A single image that is explicitly rejected can be skipped; the request result is unknown and payment is required.
    When the result download fails or local rendering fails, the task must be terminated to avoid subsequent keyword billing again.
    """
    if not material_directory:
        # Generated images are billed on a per-task basis and cannot be reused. By default, they are placed in the task directory for easy traceability.
        material_directory = utils.task_dir(task_id)

    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []
    total_duration = 0.0

    # Non-positive dubbing duration will make the judgment of "the accumulated duration reached the required duration" meaningless, and you will directly return empty-handed.
    # Avoid continuously paying per-ticket for tasks that are impossible to scrape together (consistent with Seedance preflight semantics).
    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError):
        required_duration = 0.0
    if required_duration <= 0:
        logger.warning(
            "skip openai image generation because required audio duration is "
            f"not positive: duration={audio_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    for search_term in search_terms:
        try:
            items = generate_images_openai(
                search_term=search_term,
                minimum_duration=max_clip_duration,
                video_aspect=video_aspect,
                save_dir=material_directory,
            )
        except OpenAIImagePaidResultError:
            _persist_material_sources(task_id, material_sources)
            raise
        for item in items:
            video_file = _render_openai_image_video(item.url, max_clip_duration)
            if not video_file:
                _persist_material_sources(task_id, material_sources)
                raise OpenAIImagePaidResultError(
                    "generated image could not be rendered locally"
                )
            logger.info(f"image material rendered: {video_file}")
            video_paths.append(video_file)
            try:
                material_sources.append(_material_source_record(item, video_file))
            except Exception as source_error:
                # Consistent with the inventory source: the source record exception cannot be generated and successfully rendered for a fee.
                # The material is treated as a failure, and the video generation cannot be blocked.
                logger.warning(
                    "failed to prepare generated material source record: "
                    f"provider=openai_image, "
                    f"error={type(source_error).__name__}, detail={source_error}"
                )
            total_duration += min(max_clip_duration, item.duration)
            # Same as WaveSpeed, use >= to judge: if you just get enough, you will pay one more fee if you generate another one.
            # The two judgments inside and outside must maintain the same semantics.
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated image materials cover the required duration, stop "
                f"generating more images: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break

    logger.success(f"generated and rendered {len(video_paths)} image materials")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _search_videos_with_cache(
    provider: str,
    search_videos: Callable[..., List[MaterialInfo]],
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect,
) -> List[MaterialInfo]:
    """
    Unified processing of 24-hour search caching for three online sources.

    The cache only wraps the search API and does not change the subsequent video download and deduplication logic. Do not write when the remote end returns an empty list
    Caching, because the existing provider interface uses an empty list to represent both "no results" and "request failed";
    Before the two are split into clear result types, it is better to try again next time than to cache temporary failures for a day.
    """
    cache_args = {
        "provider": provider,
        "search_term": search_term,
        "minimum_duration": minimum_duration,
        "video_aspect": video_aspect,
    }

    def load_cache_safely() -> List[MaterialInfo] | None:
        try:
            return material_cache.load_material_search_cache(**cache_args)
        except Exception as exc:
            # Caching is an optional optimization. Any cache implementation exception must be handled as a miss and cannot be blocked.
            # Normal remote search from Pexels, Pixabay or Coverr.
            logger.warning(
                "material search cache read failed, continue with remote search: "
                f"provider={provider}, error={type(exc).__name__}, detail={exc}"
            )
            return None

    def load_matching_cache() -> tuple[List[MaterialInfo] | None, int]:
        cached_items = load_cache_safely()
        if cached_items is None:
            return None, 0

        filtered_cached_items = _filter_materials_by_aspect(
            cached_items,
            video_aspect,
        )
        ignored_count = len(cached_items) - len(filtered_cached_items)
        if ignored_count:
            # Older version caches may contain material from other directions. Refresh even if there are still a few entries available
            # Complete candidate set, otherwise the same batch of small videos will be used repeatedly during the cache validity period.
            return None, ignored_count
        return filtered_cached_items, 0

    cached_items, ignored_count = load_matching_cache()
    if cached_items is not None:
        return cached_items
    if ignored_count:
        logger.info(
            "material search cache contains mismatched orientations, "
            f"refresh from provider: provider={provider}, term={search_term!r}, "
            f"ignored={ignored_count}"
        )

    cache_lock = material_cache.get_material_search_cache_lock(**cache_args)
    with cache_lock:
        # Wait for threads with the same search conditions to complete before reading again to avoid multiple API tasks being cached for the first time.
        # When there is a miss, the remote end is requested at the same time, reducing the probability of third-party interface current limiting and risk control triggering.
        cached_items, _ = load_matching_cache()
        if cached_items is not None:
            return cached_items

        items = search_videos(
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=video_aspect,
        )
        # Provider will normally write the current keyword, but test doubles, third-party extensions or old implementations may
        # Missing or carrying wrong values. A cached read restores the field based on the cache key, so the remote result is also
        # The same entry correction ensures that the first search and cache hit task source records are consistent.
        for item in items:
            if isinstance(item.source_info, dict):
                item.source_info = dict(item.source_info)
                item.source_info["search_term"] = search_term
        if items:
            try:
                material_cache.save_material_search_cache(
                    **cache_args,
                    items=items,
                )
            except Exception as exc:
                logger.warning(
                    "material search cache write failed, use remote results: "
                    f"provider={provider}, error={type(exc).__name__}, detail={exc}"
                )
        return items


def _search_terms_in_parallel(
    search_terms: List[str],
    search_videos: Callable[[str, int, VideoAspect], List[MaterialInfo]],
    minimum_duration: int,
    video_aspect: VideoAspect,
) -> list[tuple[str, List[MaterialInfo]]]:
    """Search keywords in parallel, and return empty results for failed keywords without blocking other materials."""
    if not search_terms:
        return []

    workers = min(_get_material_concurrency(), len(search_terms))
    if workers == 1:
        results = []
        for search_term in search_terms:
            items = search_videos(
                search_term=search_term,
                minimum_duration=minimum_duration,
                video_aspect=video_aspect,
            )
            logger.info(f"found {len(items)} videos for '{search_term}'")
            results.append((search_term, items))
        return results

    futures = {}
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="material-search",
    ) as executor:
        for search_term in search_terms:
            futures[executor.submit(
                logging_utils.bind_log_scope(search_videos),
                search_term,
                minimum_duration,
                video_aspect,
            )] = search_term

        results = []
        for future in futures:
            search_term = futures[future]
            try:
                items = future.result()
            except Exception as exc:
                logger.error(
                    "failed to search material videos: "
                    f"search_term={search_term!r}, "
                    f"error={type(exc).__name__}, detail={exc}"
                )
                items = []
            logger.info(f"found {len(items)} videos for '{search_term}'")
            results.append((search_term, items))
        return results


def _report_material_downloaded(
    position: int,
    total: int,
    item: MaterialInfo,
    saved_video_path: str,
    on_downloaded: Callable[[MaterialInfo], None] | None,
) -> None:
    """
    Record that a material has been downloaded and notify the caller of the update progress.

    It is always called in the thread that initiates the download. File-by-file logs and progress updates do not depend on the number of concurrent downloads.
    """
    description = os.path.basename(saved_video_path)
    try:
        size_mb = os.path.getsize(saved_video_path) / _BYTES_PER_MEGABYTE
        description += f" ({size_mb:.1f} MB)"
    except OSError:
        # The file size is only auxiliary information and will not affect the download result if it cannot be read.
        pass
    logger.info(f"downloaded material {position}/{total}: {description}")
    if on_downloaded is None:
        return
    try:
        on_downloaded(item)
    except Exception as exc:
        # Progress is just a display of information. A failed callback (e.g. the status backend is temporarily unavailable) must not allow the already
        # Successfully downloaded materials will be invalidated, and the entire task cannot be interrupted.
        logger.warning(
            "failed to report material download progress: "
            f"error={type(exc).__name__}, detail={exc}"
        )


def _covered_duration_reporter(
    progress_callback: Callable[[float], None] | None,
    required_duration: float,
    max_clip_duration: int,
) -> Callable[[MaterialInfo], None] | None:
    """
    Generate a material-by-material callback: convert the "covered dubbing duration" into a download progress of 0~1.

    The caliber is consistent with the stopping condition of the download cycle - each material contributes at most one segment duration, covering
    It takes as long as it takes to complete, so the excess is capped at 1.0. Returns None if there is no progress callback.
    """
    if progress_callback is None:
        return None
    covered_duration = 0.0

    def report(item: MaterialInfo) -> None:
        nonlocal covered_duration
        covered_duration += min(max_clip_duration, item.duration)
        fraction = 1.0
        if required_duration > 0:
            fraction = min(1.0, covered_duration / required_duration)
        progress_callback(fraction)

    return report


def _download_materials_in_parallel(
    materials: List[tuple[str, MaterialInfo]],
    material_directory: str,
    on_downloaded: Callable[[MaterialInfo], None] | None = None,
) -> list[tuple[str, MaterialInfo, str]]:
    """
    Download a round of materials in parallel, preserving the candidate order passed in by the caller.

    After each material is successfully downloaded, a log is recorded and ``on_downloaded`` is called, and the caller updates accordingly.
    Task progress. Both callbacks and logging occur in the thread that calls this function.
    """
    if not materials:
        return []

    total = len(materials)
    workers = min(_get_material_concurrency(), total)
    if workers == 1:
        downloaded = []
        for position, (search_term, item) in enumerate(materials, start=1):
            try:
                saved_video_path = save_video(
                    video_url=item.url,
                    save_dir=material_directory,
                )
            except Exception as exc:
                logger.error(
                    "failed to download material video: "
                    f"provider={item.provider}, "
                    f"error={type(exc).__name__}, "
                    f"detail={_redact_request_error(exc, item.url)}"
                )
                continue
            if saved_video_path:
                downloaded.append((search_term, item, saved_video_path))
                _report_material_downloaded(
                    position, total, item, saved_video_path, on_downloaded
                )
        return downloaded

    futures = {}
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="material-download",
    ) as executor:
        for search_term, item in materials:
            futures[executor.submit(
                logging_utils.bind_log_scope(save_video),
                item.url,
                material_directory,
            )] = (search_term, item)

        downloaded = []
        for position, future in enumerate(futures, start=1):
            search_term, item = futures[future]
            try:
                saved_video_path = future.result()
            except Exception as exc:
                logger.error(
                    "failed to download material video: "
                    f"provider={item.provider}, "
                    f"error={type(exc).__name__}, "
                    f"detail={_redact_request_error(exc, item.url)}"
                )
                continue
            if saved_video_path:
                downloaded.append((search_term, item, saved_video_path))
                _report_material_downloaded(
                    position, total, item, saved_video_path, on_downloaded
                )
        return downloaded


def _select_materials_until_duration(
    materials: List[tuple[str, MaterialInfo]],
    max_clip_duration: int,
    audio_duration: float,
    current_duration: float = 0.0,
) -> List[tuple[str, MaterialInfo]]:
    """
    Select a round of materials in the order of candidates to avoid downloading candidates that have exceeded the dubbing time during concurrency.

    Returns the prefix subset of ``materials``: starting from the first candidate and accumulating for the first time
    Stop when the dubbing duration is exceeded (including the exceeded one). The caller determines which
    Candidates are actually tried this round.
    """
    selected = []
    total_duration = current_duration
    for material in materials:
        selected.append(material)
        total_duration += min(max_clip_duration, material[1].duration)
        if total_duration > audio_duration:
            break
    return selected


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
    progress_callback: Callable[[float], None] | None = None,
) -> List[str]:
    """
    Search and download the materials required to cover the dubbing duration, returning the local file path.

    ``progress_callback`` Optional: Inventory materials (Pexels, Pixabay, Coverr) every time one is downloaded
    The file is called once with a completion ratio of 0~1 for the task layer to advance the progress bar. Pay-as-you-go generation
    The source does not report download progress.
    """
    provider = "pexels"
    remote_search_videos = search_videos_pexels
    if source == "pixabay":
        provider = "pixabay"
        remote_search_videos = search_videos_pixabay
    elif source == "coverr":
        provider = "coverr"
        remote_search_videos = search_videos_coverr

    def search_videos(
        search_term: str,
        minimum_duration: int,
        video_aspect: VideoAspect,
    ) -> List[MaterialInfo]:
        return _search_videos_with_cache(
            provider=provider,
            search_videos=remote_search_videos,
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=video_aspect,
        )

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    if source == "wavespeed":
        # AI generation is billed on a per-item basis, and the inventory source cannot be used to "retrieve candidates for all keywords first, and then select"
        # process, otherwise you will be charged for unused segments. The generation source is changed to generate on-demand segment by segment, which is enough
        # Stops immediately if required; also does not participate in 24-hour search caching - product URLs will expire
        # signature address, and reusing the cache will allow different tasks to repeatedly obtain the same generated video.
        return _download_videos_wavespeed_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )
    if source == "volcengine_seedance":
        # Like WaveSpeed, Ark’s official interface creates asynchronous paid tasks. Must be segmented as needed
        # Generate and only purchase the materials that are really needed for the current dubbing duration.
        return _download_videos_seedance_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )
    if source == "ofox":
        # Same pay-as-you-go semantics as WaveSpeed/Ark: OFox gateway's /v1/videos will create
        # Asynchronous payment tasks must be generated piece by piece and stopped immediately when the required time is reached; the product address will be
        # Temporary direct links will not participate in the 24-hour search cache.
        return _download_videos_ofox_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )
    if source == "muapi":
        # MuAPI's model endpoint creates asynchronous paid tasks. Submit segment by segment and after covering dubbing duration
        # Stop to avoid sending unused script keywords to the remote end.
        return _download_videos_muapi_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )
    if source == "metaso_minimax":
        # Secret Tower MiniMax is also billed based on remote asynchronous tasks. It is similar to the request body of Volcano Ark,
        # However, the task query path and response structure are different, so only local on-demand generation semantics are shared and not reused.
        # Supplier clients prevent protocol differences from seeping into the material orchestration layer.
        return _download_videos_metaso_minimax_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )
    if source == "openai_image":
        # The same pay-as-you-go semantics as WaveSpeed: Vincent pictures are charged on a per-picture basis, generated segment by segment, and collected.
        # The desired duration stops immediately. The generated result is a one-time local image file and does not participate in 24
        # Hourly search cache - Caching will allow different tasks to get the same image over and over again.
        return _download_videos_openai_image_on_demand(
            task_id=task_id,
            search_terms=search_terms,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
        )

    if match_script_order:
        return _download_videos_by_script_order(
            task_id=task_id,
            search_terms=search_terms,
            search_videos=search_videos,
            video_aspect=video_aspect,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            material_directory=material_directory,
            progress_callback=progress_callback,
        )

    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    for search_term, video_items in _search_terms_in_parallel(
        search_terms=search_terms,
        search_videos=search_videos,
        minimum_duration=max_clip_duration,
        video_aspect=video_aspect,
    ):
        for item in video_items:
            if item.url not in valid_video_urls:
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []
    material_sources: list[dict[str, Any]] = []

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    on_downloaded = _covered_duration_reporter(
        progress_callback, audio_duration, max_clip_duration
    )
    pending_items = list(valid_video_items)
    # The default/random path is also controlled by the material concurrency configuration: each round is preselected according to the stop condition of the serial logic
    # Candidates (sequential accumulation, stop when the dubbing duration is exceeded for the first time). When all downloads are successful, each round of downloads will be aggregated
    # Exactly the same as the serial logic; candidates that failed to download are supplemented with subsequent candidates in the next round.
    while pending_items and total_duration <= audio_duration:
        batch = []
        projected_duration = total_duration
        for item in pending_items:
            batch.append(item)
            projected_duration += min(max_clip_duration, item.duration)
            if projected_duration > audio_duration:
                break
        for item in batch:
            source_info = item.source_info if isinstance(item.source_info, dict) else {}
            logger.info(
                f"downloading {item.provider} video: "
                f"asset_id={source_info.get('asset_id') or 'unknown'}"
            )
        downloaded_materials = _download_materials_in_parallel(
            materials=[("", item) for item in batch],
            material_directory=material_directory,
            on_downloaded=on_downloaded,
        )
        pending_items = pending_items[len(batch):]
        for _, item, saved_video_path in downloaded_materials:
            try:
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    try:
                        material_sources.append(
                            _material_source_record(item, saved_video_path)
                        )
                    except Exception as source_error:
                        # If the source record is abnormal, the successfully downloaded material cannot be regarded as a download failure, let alone
                        # Block video generation; retain suppliers and anomaly types for subsequent positioning.
                        logger.warning(
                            "failed to prepare material source record: "
                            f"provider={item.provider}, "
                            f"error={type(source_error).__name__}, detail={source_error}"
                        )
                    seconds = min(max_clip_duration, item.duration)
                    total_duration += seconds
                    if total_duration > audio_duration:
                        logger.info(
                            f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                        )
                        break
            except Exception as e:
                logger.error(
                    "failed to download material video: "
                    f"provider={item.provider}, error={type(e).__name__}, "
                    f"detail={_redact_request_error(e, item.url)}"
                )
    logger.success(f"downloaded {len(video_paths)} videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_wavespeed_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """
    Generate WaveSpeed material segment by segment in the order of script segments, and stop immediately when the required total duration is reached.

    Each keyword naturally corresponds to a script fragment, and you pay when you generate it: Generate it in full first and then select it.
    Pay for unused footage. Every time a paragraph is generated here, it is downloaded immediately and the validity period is accumulated (with the inventory
    The process is the same, capped according to the duration of the clip), and no new generation will be triggered after the required dubbing duration is cumulatively covered.
    request. Fragments that clearly failed on the remote end can be skipped; paid tasks with unknown status or failed download must
    Keep the ID and terminate the local task.
    """
    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []

    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("WaveSpeed audio duration must be finite") from exc
    if not math.isfinite(required_duration):
        raise ValueError("WaveSpeed audio duration must be finite")
    if required_duration <= 0:
        logger.warning(
            "skip WaveSpeed paid generation because required audio duration "
            f"is not positive: duration={required_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    try:
        clip_duration = int(max_clip_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("WaveSpeed clip duration must be a positive integer") from exc
    if clip_duration <= 0:
        raise ValueError("WaveSpeed clip duration must be a positive integer")

    total_duration = 0.0
    for search_term in search_terms:
        try:
            video_items = generate_videos_wavespeed(
                search_term=search_term,
                minimum_duration=clip_duration,
                video_aspect=video_aspect,
            )
        except WaveSpeedUnconfirmedTaskError as e:
            # The status of submitted paid tasks is unknown: the remote end may still be running or may have been completed and billed.
            # Continuing to place orders for subsequent keywords will cause repeated generation and repeated deductions, so stop it on the spot.
            # Give the prediction id to the task layer to write the failure status for the user to retrieve the product.
            logger.error(
                "stop submitting new wavespeed tasks, the last submitted task "
                f"is unconfirmed: prediction_id={e.prediction_id or 'unknown'}, "
                f"detail={e}"
            )
            _persist_material_sources(task_id, material_sources)
            raise
        for item in video_items:
            saved_video_path = _save_generated_video_with_retry(
                item.url, material_directory, "wavespeed"
            )
            if not saved_video_path:
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                prediction_id = str(source_info.get("asset_id") or "").strip()
                _persist_material_sources(task_id, material_sources)
                raise WaveSpeedDownloadError(
                    "WaveSpeed generated a paid video but the result could not be "
                    f"downloaded: id={prediction_id or 'unknown'}",
                    prediction_id=prediction_id,
                )
            logger.info(f"video saved: {saved_video_path}")
            video_paths.append(saved_video_path)
            try:
                material_sources.append(_material_source_record(item, saved_video_path))
            except Exception as source_error:
                # Consistent with the inventory source: the source record is abnormal and cannot be generated and successfully downloaded for a fee.
                # The material is treated as a failure, and the video generation cannot be blocked.
                logger.warning(
                    "failed to prepare material source record: "
                    f"provider={item.provider}, "
                    f"error={type(source_error).__name__}, detail={source_error}"
                )
            total_duration += min(clip_duration, item.duration)
            # Use >= to judge: when the accumulated time is exactly equal to the required time, it is enough, and it will be regenerated.
            # Pay one more fee. The two judgments inside and outside must maintain the same semantics.
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated materials cover the required duration, stop "
                f"generating more clips: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break
    logger.success(f"generated and downloaded {len(video_paths)} videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_seedance_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """Generate Ark Seedance materials sequentially, and stop paying for orders immediately after covering the dubbing duration."""
    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []

    # Paid generation loops must first verify the two durations that control the number of loops. NaN/Infinity will make
    # ``total_duration >= audio_duration`` will never hold, and non-positive clip durations will make
    # The cumulative value cannot be increased, and both may create useless paid tasks for all keywords.
    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError) as exc:
        raise volcengine_seedance.VolcEngineSeedanceError(
            "Seedance audio duration must be a finite number"
        ) from exc
    if not math.isfinite(required_duration):
        raise volcengine_seedance.VolcEngineSeedanceError(
            "Seedance audio duration must be a finite number"
        )
    if required_duration <= 0:
        logger.warning(
            "skip Seedance paid generation because required audio duration is "
            f"not positive: duration={required_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    try:
        clip_duration = int(max_clip_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise volcengine_seedance.VolcEngineSeedanceError(
            "Seedance clip duration must be a positive integer"
        ) from exc
    if clip_duration <= 0:
        raise volcengine_seedance.VolcEngineSeedanceError(
            "Seedance clip duration must be a positive integer"
        )

    total_duration = 0.0
    for search_term in search_terms:
        try:
            video_items = volcengine_seedance.generate_videos(
                search_term=search_term,
                minimum_duration=clip_duration,
                video_aspect=video_aspect,
            )
        except volcengine_seedance.VolcEngineSeedanceUnconfirmedTaskError as exc:
            # Remote paid tasks may still succeed. Stop placing orders immediately and keep the task ID for convenience
            # The user then confirms or retrieves the results on the Ark console.
            logger.error(
                "stop submitting new Seedance tasks because the last paid task "
                f"is unconfirmed: task_id={exc.task_id or 'unknown'}, detail={exc}"
            )
            _persist_material_sources(task_id, material_sources)
            raise
        except volcengine_seedance.VolcEngineSeedanceError as exc:
            logger.error(f"Seedance generation failed before completion: {exc}")
            _persist_material_sources(task_id, material_sources)
            raise

        for item in video_items:
            saved_video_path = _save_generated_video_with_retry(
                item.url, material_directory, "volcengine_seedance"
            )
            if not saved_video_path:
                # The remote task has been completed and fees have been generated. When the local download fails, the remote task ID must be
                # Bring back the task status so that users can go to the Ark console to retrieve the results. Throw it directly here
                # Special error, while preventing subsequent keywords from continuing to create new paid tasks.
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                remote_task_id = str(source_info.get("asset_id") or "").strip()
                _persist_material_sources(task_id, material_sources)
                raise volcengine_seedance.VolcEngineSeedanceDownloadError(
                    "Seedance generated a paid video but the result could not be "
                    f"downloaded: id={remote_task_id or 'unknown'}",
                    task_id=remote_task_id,
                )
            logger.info(f"video saved: {saved_video_path}")
            video_paths.append(saved_video_path)
            try:
                material_sources.append(_material_source_record(item, saved_video_path))
            except Exception as source_error:
                logger.warning(
                    "failed to prepare generated material source record: "
                    f"provider=volcengine_seedance, "
                    f"error={type(source_error).__name__}, detail={source_error}"
                )
            total_duration += min(clip_duration, item.duration)
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated Seedance materials cover the required duration; stop "
                f"submitting paid tasks: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break

    logger.success(
        f"generated and downloaded {len(video_paths)} Volcano Engine Seedance videos"
    )
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_ofox_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """OFox materials are generated sequentially, and paid orders are immediately stopped after the dubbing duration is covered."""
    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []

    # Paid generation loops must first verify the two durations that control the number of loops. NaN/Infinity will make
    # ``total_duration >= audio_duration`` will never hold, and non-positive clip durations will make
    # The cumulative value cannot be increased, and both may create useless paid tasks for all keywords.
    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError) as exc:
        raise ofox.OFoxError("OFox audio duration must be a finite number") from exc
    if not math.isfinite(required_duration):
        raise ofox.OFoxError("OFox audio duration must be a finite number")
    if required_duration <= 0:
        logger.warning(
            "skip OFox paid generation because required audio duration is "
            f"not positive: duration={required_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    try:
        clip_duration = int(max_clip_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ofox.OFoxError("OFox clip duration must be a positive integer") from exc
    if clip_duration <= 0:
        raise ofox.OFoxError("OFox clip duration must be a positive integer")

    total_duration = 0.0
    for search_term in search_terms:
        try:
            video_items = ofox.generate_videos(
                search_term=search_term,
                minimum_duration=clip_duration,
                video_aspect=video_aspect,
            )
        except ofox.OFoxUnconfirmedTaskError as exc:
            # Remote paid tasks may still succeed. Stop placing orders immediately and keep the task ID for convenience
            # The user then confirms or retrieves the results in the OFox console.
            logger.error(
                "stop submitting new OFox tasks because the last paid task "
                f"is unconfirmed: task_id={exc.task_id or 'unknown'}, detail={exc}"
            )
            _persist_material_sources(task_id, material_sources)
            raise
        except ofox.OFoxError as exc:
            logger.error(f"OFox generation failed before completion: {exc}")
            _persist_material_sources(task_id, material_sources)
            raise

        # When a single keyword fails to be explicitly judged by the remote end (such as triggering content review), an empty list is returned: the task has been
        # Ending, no suspense of billing, skip this segment and continue to generate subsequent keywords.
        for item in video_items:
            saved_video_path = _save_generated_video_with_retry(
                item.url, material_directory, "ofox"
            )
            if not saved_video_path:
                # The remote task has been completed and fees have been generated. When the local download fails, the remote task ID must be
                # Bring back the task status so that users can go to the OFox console to retrieve the results. Throw it directly here
                # Special error, while preventing subsequent keywords from continuing to create new paid tasks.
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                remote_task_id = str(source_info.get("asset_id") or "").strip()
                _persist_material_sources(task_id, material_sources)
                raise ofox.OFoxDownloadError(
                    "OFox generated a paid video but the result could not be "
                    f"downloaded: id={remote_task_id or 'unknown'}",
                    task_id=remote_task_id,
                )
            logger.info(f"video saved: {saved_video_path}")
            video_paths.append(saved_video_path)
            try:
                material_sources.append(_material_source_record(item, saved_video_path))
            except Exception as source_error:
                logger.warning(
                    "failed to prepare generated material source record: "
                    f"provider=ofox, "
                    f"error={type(source_error).__name__}, detail={source_error}"
                )
            total_duration += min(clip_duration, item.duration)
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated OFox materials cover the required duration; stop "
                f"submitting paid tasks: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break

    logger.success(f"generated and downloaded {len(video_paths)} OFox videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_muapi_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """Generate MuAPI materials until the narration duration is covered."""
    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []

    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError) as exc:
        raise muapi.MuAPIError(
            "MuAPI audio duration must be a finite number"
        ) from exc
    if not math.isfinite(required_duration):
        raise muapi.MuAPIError("MuAPI audio duration must be a finite number")
    if required_duration <= 0:
        logger.warning(
            "skip MuAPI paid generation because required audio duration is "
            f"not positive: duration={required_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    try:
        clip_duration = int(max_clip_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise muapi.MuAPIError(
            "MuAPI clip duration must be a positive integer"
        ) from exc
    if clip_duration <= 0:
        raise muapi.MuAPIError("MuAPI clip duration must be a positive integer")

    total_duration = 0.0
    for search_term in search_terms:
        try:
            video_items = muapi.generate_videos(
                search_term=search_term,
                minimum_duration=clip_duration,
                video_aspect=video_aspect,
            )
        except muapi.MuAPIUnconfirmedTaskError as exc:
            # An ambiguous paid submission must stop the loop: retrying with the
            # next keyword could create a duplicate charge.  The task ID is kept
            # for task-service recovery when the provider returned one.
            logger.error(
                "stop submitting new MuAPI tasks because the last paid task "
                f"is unconfirmed: task_id={exc.task_id or 'unknown'}, "
                f"detail={exc}"
            )
            _persist_material_sources(task_id, material_sources)
            raise
        except muapi.MuAPIError as exc:
            logger.error(f"MuAPI generation failed before completion: {exc}")
            _persist_material_sources(task_id, material_sources)
            raise

        for item in video_items:
            saved_video_path = _save_generated_video_with_retry(
                item.url, material_directory, "muapi"
            )
            if not saved_video_path:
                # The remote job completed and may already have incurred a
                # charge.  Do not submit a replacement task after download
                # failure; return the remote ID for manual recovery instead.
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                remote_task_id = str(source_info.get("asset_id") or "").strip()
                _persist_material_sources(task_id, material_sources)
                raise muapi.MuAPIDownloadError(
                    "MuAPI generated a paid video but the result could not be "
                    f"downloaded: id={remote_task_id or 'unknown'}",
                    task_id=remote_task_id,
                )
            logger.info(f"video saved: {saved_video_path}")
            video_paths.append(saved_video_path)
            try:
                material_sources.append(_material_source_record(item, saved_video_path))
            except Exception as source_error:
                logger.warning(
                    "failed to prepare generated material source record: "
                    f"provider=muapi, error={type(source_error).__name__}, "
                    f"detail={source_error}"
                )
            try:
                downloaded_duration = _get_downloaded_video_duration(saved_video_path)
            except Exception as duration_error:
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                remote_task_id = str(source_info.get("asset_id") or "").strip()
                _persist_material_sources(task_id, material_sources)
                raise muapi.MuAPIDownloadError(
                    "MuAPI generated a paid video but its downloaded duration "
                    "could not be measured: "
                    f"id={remote_task_id or 'unknown'}",
                    task_id=remote_task_id,
                ) from duration_error
            total_duration += min(clip_duration, downloaded_duration)
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated MuAPI materials cover the required duration; stop "
                f"submitting paid tasks: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break

    logger.success(f"generated and downloaded {len(video_paths)} MuAPI videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_metaso_minimax_on_demand(
    *,
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
) -> List[str]:
    """The Secret Tower MiniMax material is generated sequentially, and the paid order is stopped immediately after the dubbing duration is covered."""
    video_paths: List[str] = []
    material_sources: list[dict[str, Any]] = []

    # The minimum generated by the remote end is 4 seconds, but the local segment is still trimmed and accumulated according to the user segment duration. Ahead of validation loop
    # Control parameters to avoid NaN, Infinity or non-positive numbers that will never satisfy the stop condition, thereby
    # All keywords are submitted as paid tasks.
    try:
        required_duration = float(audio_duration)
    except (TypeError, ValueError) as exc:
        raise metaso_minimax.MetasoMiniMaxError(
            "Metaso MiniMax audio duration must be a finite number"
        ) from exc
    if not math.isfinite(required_duration):
        raise metaso_minimax.MetasoMiniMaxError(
            "Metaso MiniMax audio duration must be a finite number"
        )
    if required_duration <= 0:
        logger.warning(
            "skip Metaso MiniMax paid generation because required audio duration "
            f"is not positive: duration={required_duration}"
        )
        _persist_material_sources(task_id, material_sources)
        return video_paths

    try:
        clip_duration = int(max_clip_duration)
    except (TypeError, ValueError, OverflowError) as exc:
        raise metaso_minimax.MetasoMiniMaxError(
            "Metaso MiniMax clip duration must be a positive integer"
        ) from exc
    if clip_duration <= 0:
        raise metaso_minimax.MetasoMiniMaxError(
            "Metaso MiniMax clip duration must be a positive integer"
        )

    total_duration = 0.0
    for search_term in search_terms:
        try:
            video_items = metaso_minimax.generate_videos(
                search_term=search_term,
                minimum_duration=clip_duration,
                video_aspect=video_aspect,
            )
        except metaso_minimax.MetasoMiniMaxUnconfirmedTaskError as exc:
            # When the request or polling status is unknown, the remote task may still succeed and be billed. Stop the entire
            # Generate a loop to prevent subsequent keyword orders from being placed, and give the task ID to the task service for storage.
            logger.error(
                "stop submitting new Metaso MiniMax tasks because the last paid "
                f"task is unconfirmed: task_id={exc.task_id or 'unknown'}, "
                f"detail={exc}"
            )
            _persist_material_sources(task_id, material_sources)
            raise
        except metaso_minimax.MetasoMiniMaxError as exc:
            logger.error(f"Metaso MiniMax generation failed before completion: {exc}")
            _persist_material_sources(task_id, material_sources)
            raise

        for item in video_items:
            saved_video_path = _save_generated_video_with_retry(
                item.url, material_directory, "metaso_minimax"
            )
            if not saved_video_path:
                # If the generation is successful, fees have been incurred. If the download fails, new tasks cannot be created to replace it.
                # Throws a dedicated error carrying the remote ID for use by task status and manual recovery.
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                remote_task_id = str(source_info.get("asset_id") or "").strip()
                _persist_material_sources(task_id, material_sources)
                raise metaso_minimax.MetasoMiniMaxDownloadError(
                    "Metaso MiniMax generated a paid video but the result could "
                    f"not be downloaded: id={remote_task_id or 'unknown'}",
                    task_id=remote_task_id,
                )
            logger.info(f"video saved: {saved_video_path}")
            video_paths.append(saved_video_path)
            try:
                material_sources.append(_material_source_record(item, saved_video_path))
            except Exception as source_error:
                logger.warning(
                    "failed to prepare generated material source record: "
                    f"provider=metaso_minimax, error={type(source_error).__name__}, "
                    f"detail={source_error}"
                )

            # Local movie generation only uses the user-selected segment length; even if H3 is generated due to the minimum duration constraint
            # Even if the material is longer, the unused portion cannot be counted as the coverage time and less necessary scenes will be generated.
            total_duration += min(clip_duration, item.duration)
            if total_duration >= required_duration:
                break
        if total_duration >= required_duration:
            logger.info(
                "generated Metaso MiniMax materials cover the required duration; "
                f"stop submitting paid tasks: generated={total_duration:.1f}s, "
                f"required={required_duration:.1f}s"
            )
            break

    logger.success(f"generated and downloaded {len(video_paths)} Metaso MiniMax videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def _download_videos_by_script_order(
    task_id: str,
    search_terms: List[str],
    search_videos,
    video_aspect: VideoAspect,
    audio_duration: float,
    max_clip_duration: int,
    material_directory: str,
    progress_callback: Callable[[float], None] | None = None,
) -> List[str]:
    """
    Download materials in the order of script copywriting.

    The default download logic will merge all keyword candidate materials into a large list; if the first
    The keyword returns a lot of results, and the material of this keyword may be consumed in the final download.
    Script topics cannot be scheduled on the timeline. Here we group by keywords and then poll for downloads:
    Round 1 takes the 1st candidate for each keyword, and round 2 takes the 2nd candidate for each keyword.
    In this way, without rewriting the video synthesis engine, try to ensure that the order of the materials is as close to the order of the copywriting as possible.
    """
    logger.info("downloading videos with script-order material matching")
    candidate_groups = []
    valid_video_urls = set()
    found_duration = 0.0

    for search_term, video_items in _search_terms_in_parallel(
        search_terms=search_terms,
        search_videos=search_videos,
        minimum_duration=max_clip_duration,
        video_aspect=video_aspect,
    ):
        term_items = []
        for item in video_items:
            if item.url in valid_video_urls:
                continue
            term_items.append(item)
            valid_video_urls.add(item.url)
            found_duration += item.duration

        if term_items:
            candidate_groups.append((search_term, term_items))

    logger.info(
        f"found total ordered video candidates: {sum(len(items) for _, items in candidate_groups)}, "
        f"required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )

    video_paths = []
    material_sources: list[dict[str, Any]] = []
    total_duration = 0.0
    on_downloaded = _covered_duration_reporter(
        progress_callback, audio_duration, max_clip_duration
    )
    # Each keyword advances the candidate subscript independently: only the candidates that are actually selected for download in this round are advanced.
    # Unselected candidates are retained until the next round to avoid being skipped by the same subscript throughout the round.
    next_candidate_indices = [0] * len(candidate_groups)
    while candidate_groups and total_duration <= audio_duration:
        round_materials = [
            (
                group_position,
                search_term,
                term_items[next_candidate_indices[group_position]],
            )
            for group_position, (search_term, term_items) in enumerate(
                candidate_groups
            )
            if next_candidate_indices[group_position] < len(term_items)
        ]
        if not round_materials:
            break

        selected_materials = _select_materials_until_duration(
            materials=[
                (search_term, item) for _, search_term, item in round_materials
            ],
            max_clip_duration=max_clip_duration,
            audio_duration=audio_duration,
            current_duration=total_duration,
        )
        # _select_materials_until_duration returns a prefixed subset of the candidate list,
        # Therefore, the first len(selected_materials) are the candidates that are actually tried to download in this round.
        # Only the subscripts of the groups in which these candidates belong are advanced.
        for group_position, _, _ in round_materials[: len(selected_materials)]:
            next_candidate_indices[group_position] += 1

        downloaded_materials = _download_materials_in_parallel(
            materials=selected_materials,
            material_directory=material_directory,
            on_downloaded=on_downloaded,
        )
        for search_term, item, saved_video_path in downloaded_materials:
            try:
                source_info = (
                    item.source_info if isinstance(item.source_info, dict) else {}
                )
                logger.info(
                    f"downloading ordered {item.provider} video for {search_term!r}: "
                    f"asset_id={source_info.get('asset_id') or 'unknown'}"
                )
                if saved_video_path:
                    logger.info(f"video saved: {saved_video_path}")
                    video_paths.append(saved_video_path)
                    try:
                        material_sources.append(
                            _material_source_record(item, saved_video_path)
                        )
                    except Exception as source_error:
                        logger.warning(
                            "failed to prepare ordered material source record: "
                            f"provider={item.provider}, "
                            f"error={type(source_error).__name__}, "
                            f"detail={source_error}"
                        )
                    total_duration += min(max_clip_duration, item.duration)
                    if total_duration > audio_duration:
                        logger.info(
                            f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                        )
                        break
            except Exception as e:
                logger.error(
                    "failed to download ordered material video: "
                    f"provider={item.provider}, error={type(e).__name__}, "
                    f"detail={_redact_request_error(e, item.url)}"
                )

    logger.success(f"downloaded {len(video_paths)} ordered videos")
    _persist_material_sources(task_id, material_sources)
    return video_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
