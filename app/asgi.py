"""Application implementation - ASGI."""

import math
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import config
from app.controllers import base
from app.models.exception import HttpException
from app.router import root_api_router
from app.utils import utils


@asynccontextmanager
async def application_lifespan(_: FastAPI):
    """Centralized handling of API process startup recovery and shutdown logs."""
    logger.info("startup event")

    configured_api_key = config.app.get("api_key", "")
    if configured_api_key in (None, ""):
        logger.warning(
            "API key authentication is disabled; keep the API on a trusted network"
        )
    elif isinstance(configured_api_key, str):
        # Only the protection scope is recorded, and the Key, length or summary must not be output to prevent credentials from entering the log system.
        logger.info("API key authentication is enabled for /api/v1 and /tasks")
    else:
        logger.error(
            "API key authentication is misconfigured: app.api_key must be a string"
        )

    # Cross-platform publishing is performed by the current process thread pool and will not resume after the service is restarted. Put Redis at startup
    # Confirm that the activity status of the lost execution process has converged to failure to prevent the task from being permanently deleted.
    from app.services import task as task_service

    task_service.recover_interrupted_cross_posts()

    # Redis queue entries persist across API restarts. No task_done callback
    # exists in the new process to dispatch them, so fill its worker slots now.
    from app.controllers.manager.redis_manager import RedisTaskManager
    from app.controllers.v1 import video as video_controller

    if isinstance(video_controller.task_manager, RedisTaskManager):
        video_controller.task_manager.resume_queued_tasks()
    try:
        yield
    finally:
        logger.info("shutdown event")


def exception_handler(request: Request, e: HttpException):
    return JSONResponse(
        status_code=e.status_code,
        content=utils.get_response(e.status_code, e.data, e.message),
    )


def validation_exception_handler(request: Request, e: RequestValidationError):
    # Rejected inputs can contain NaN/Infinity, and custom validators attach
    # exception objects to ctx. Neither can be emitted by JSONResponse directly.
    errors = jsonable_encoder(
        e.errors(),
        custom_encoder={
            float: lambda value: value if math.isfinite(value) else str(value),
            Exception: str,
        },
    )
    return JSONResponse(
        status_code=400,
        content=utils.get_response(
            status=400, data=errors, message="field required"
        ),
    )


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _normalize_allowed_origin(raw_origin: str) -> str | None:
    """Collapse configuration items into the Origin form actually sent by the browser.

    The Origin request header is fixed by RFC 6454 to ``scheme://host[:port]``: no path, no tail
    Slashes, scheme and host have been normalized to lowercase, and the protocol's default port is omitted. User in browser
    ``https://frontend.example/`` copied from the address bar, and the default port explicitly written out
    ``https://frontend.example:443``, both are related to what the browser actually sends
    ``https://frontend.example`` is not equal character by character, so the whitelist fails silently: the front end only sees
    CORS error is reported, and the server only leaves one line of blocked log, and neither side can refer to the configuration itself.

    Returning ``None`` means that the writing method cannot match any Origin (scheme is missing, scheme is not
    http/https, the host name is empty or contains spaces, the IPv6 literal is malformed, the port is illegal or out of bounds), the caller should
    Discard and alert. Parsing failure must converge to ``None``: this function will be executed during the module import period, so
    The ``ValueError`` of ``urlsplit()`` means that the entire API cannot be started.
    """

    if raw_origin == "*":
        return raw_origin

    try:
        parsed = urlsplit(raw_origin)
        scheme = parsed.scheme.lower()
        # Validate netloc only when reading hostname/port: malformed IPv6 literals by urlsplit()
        # Thrown when reading port for non-numeric ports and ports beyond 0-65535.
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None

    # Only http/https have browser Origin semantics, and both have default ports that can be omitted.
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if any(character.isspace() for character in host):
        return None

    # Explicitly lowercase, independent of urlsplit's case handling on different Python versions.
    host = host.lower()
    # The browser retains square brackets for IPv6 literals when serializing Origin; ``:443``, ``:0443``,
    # ``:80`` This type of default port writing must be folded into a form without ports to match the real front end.
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def parse_cors_allowed_origins(raw_origins: str | None) -> list[str]:
    """Parse browser cross-domain source whitelist.

    CORS only restricts cross-domain JavaScript in browsers and does not affect curl, Postman, and n8n.
    Or server SDK. When not configured, an empty list is returned, indicating that cross-domain access is not allowed by default; the user does
    When deploying an independent web front-end, explicitly enable it through ``CORS_ALLOWED_ORIGINS``.

    Each origin is first collapsed into the canonical form of the Origin request header, so ``https://a.example/``,
    ``HTTPS://A.Example`` gets the same as ``https://a.example:443`` with an explicit default port
    As a result, writing that cannot match any Origin will be discarded and a warning will be left. Malformed entries only discard themselves,
    The parsing here will not be interrupted - this function is called during module import, and parsing exceptions will prevent the API from starting.
    """

    if not raw_origins:
        return []

    # Remove whitespace around comma-separated items and ignore empty items, avoiding common environment variable formats
    # ``https://a.example, https://b.example,`` produces origins that can never be matched.
    origins: list[str] = []
    for candidate in raw_origins.split(","):
        item = candidate.strip()
        if not item:
            continue
        origin = _normalize_allowed_origin(item)
        if origin is None:
            logger.warning(
                f"ignoring configured CORS origin that cannot match a browser "
                f"Origin header: {item!r}"
            )
            continue
        if origin not in origins:
            origins.append(origin)
    return origins


def configure_cors(instance: FastAPI, allowed_origins: list[str]) -> None:
    """Configure CORS with an explicit whitelist; keep the default same-origin policy for empty lists."""

    if not allowed_origins:
        logger.info(
            "browser cross-origin API access is disabled; set "
            "CORS_ALLOWED_ORIGINS to enable trusted origins"
        )
        return

    allow_all_origins = "*" in allowed_origins
    configured_api_key = config.app.get("api_key", "")
    if allow_all_origins and configured_api_key in (None, ""):
        # ``*`` is a compatibility mode explicitly selected by the user, so startup is not forcibly refused; however, it does not require authentication
        # In this state, it will allow any web page to read and call the API, and must leave a locationable security alert.
        logger.warning(
            "CORS allows every browser origin while API key authentication is "
            "disabled; configure app.api_key or restrict CORS_ALLOWED_ORIGINS"
        )

    instance.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        # Starlette will reflect any Origin when ``*`` is enabled at the same time as credentials.
        # Wildcard mode does not require cookie authentication, so credentials are actively turned off; explicit source
        # The old behavior is still retained to avoid affecting the credentials request mode of the existing independent web front end.
        allow_credentials=not allow_all_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        # When a remote HTTPS front end accesses the local or LAN API, modern browsers will additionally send
        # Private Network Access preflight. Only precisely whitelisted sources can be licensed;
        # Wildcard mode continues to deny, preventing arbitrary websites from probing the user's private network services.
        allow_private_network=not allow_all_origins,
    )


def is_browser_origin_allowed(
    request: Request, allowed_origins: list[str]
) -> bool:
    """Determine whether the browser request source is from the same origin or an explicit whitelist source."""

    origin = request.headers.get("origin")
    if not origin:
        # curl, Postman, n8n, and server-side SDKs generally do not send Origin. Reserve this type of request,
        # Avoid security fixes that mistakenly change the calling contract of existing API clients.
        return True
    if "*" in allowed_origins or origin in allowed_origins:
        return True

    # The browser may also send Origin for same-origin POST. Only compare scheme + authority, ignore
    # Path and query parameters; if the reverse proxy deployment does not correctly forward the public network scheme/host, you can explicitly
    # CORS_ALLOWED_ORIGINS declares external sources to avoid relying on untrusted forwarding headers.
    request_url = urlsplit(str(request.url))
    request_origin = f"{request_url.scheme}://{request_url.netloc}"
    return origin == request_origin


def configure_browser_access(instance: FastAPI, allowed_origins: list[str]) -> None:
    """Configure server-side Origin protection and browser CORS response policy at the same time."""

    @instance.middleware("http")
    async def reject_untrusted_browser_origin(request: Request, call_next):
        """Proactively reject untrusted browser sources, covering simple requests without CORS preflight."""

        if not is_browser_origin_allowed(request, allowed_origins):
            origin = request.headers.get("origin", "")
            logger.warning(
                f"blocked untrusted browser origin: method={request.method}, "
                f"path={request.url.path}, origin={origin}"
            )
            return JSONResponse(
                status_code=403,
                content=utils.get_response(
                    status=403,
                    message="cross-origin browser request is not allowed",
                ),
            )

        return await call_next(request)

    # The CORS middleware is finally registered and located in the outer layer of Origin protection: trusted pre-check can succeed directly.
    # Untrusted preflights are rejected by CORS; actual requests without preflights will still go into the 403 guard above.
    configure_cors(instance, allowed_origins)


def get_application() -> FastAPI:
    """Initialize FastAPI application.

    Returns:
       FastAPI: Application object instance.

    """
    instance = FastAPI(
        title=config.project_name,
        description=config.project_description,
        version=config.project_version,
        debug=False,
        lifespan=application_lifespan,
    )
    instance.include_router(root_api_router)
    instance.add_exception_handler(HttpException, exception_handler)
    instance.add_exception_handler(RequestValidationError, validation_exception_handler)
    return instance


app = get_application()


@app.middleware("http")
async def protect_generated_task_files(request: Request, call_next):
    """Protect static routing of task products to prevent direct downloading bypassing API authentication.

    ``/tasks`` is mounted independently by StaticFiles and cannot reuse APIRouter dependencies.
    So the same verify_token is called in the middleware. The authentication function will be used if it is not configured
    api_key; OPTIONS preflight requests are also reserved for CORS middleware processing.
    """

    request_path = request.url.path
    is_task_file = request_path == "/tasks" or request_path.startswith("/tasks/")
    if is_task_file and request.method != "OPTIONS":
        try:
            base.verify_token(request)
        except HttpException as exception:
            return exception_handler(request, exception)

    return await call_next(request)


# By default, the browser's same-origin policy is followed; cross-domain is only enabled when the user explicitly configures a trusted web page source.
cors_allowed_origins = parse_cors_allowed_origins(
    os.getenv("CORS_ALLOWED_ORIGINS", "")
)
configure_browser_access(app, cors_allowed_origins)

task_dir = utils.task_dir()
app.mount("/tasks", StaticFiles(directory=task_dir, html=True), name="")

public_dir = utils.public_dir()
app.mount("/", StaticFiles(directory=public_dir, html=True), name="")
