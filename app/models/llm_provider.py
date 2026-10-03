from dataclasses import dataclass


DEFAULT_LLM_PROVIDER_ID = "moonshot"


@dataclass(frozen=True, slots=True)
class LLMProviderField:
    """Description Provider's additional configuration fields in addition to API Key, Base URL, and model name."""

    config_suffix: str
    label_key: str
    required: bool = False
    secret: bool = False
    default_value: str = ""


@dataclass(frozen=True, slots=True)
class LLMProviderEndpoint:
    """Describe the supporting entrances and API addresses used by the same Provider in different service areas."""

    endpoint_id: str
    default_label: str
    base_url: str
    api_key_url: str
    model_docs_url: str = ""


@dataclass(frozen=True, slots=True)
class LLMProviderSpec:
    """
    Centralized declaration of LLM Provider.

    This centrally stores stable metadata used across WebUI, configuration loading and service calls, including default
    Displays the name and locale key, but does not save the specific translation copy and does not implement API requests. This way
    The "what" of the Provider is maintained by the Registry, and the "how to call" is still the responsibility of the service layer adapter.
    """

    provider_id: str
    default_label: str
    adapter: str = "openai_compatible"
    api_key_url: str = ""
    default_model: str = ""
    default_base_url: str = ""
    model_docs_url: str = ""
    requires_api_key: bool = True
    requires_model_name: bool = True
    requires_base_url: bool = True
    show_api_key: bool = True
    show_base_url: bool = True
    deprecated_models: tuple[str, ...] = ()
    deprecated_base_urls: tuple[str, ...] = ()
    extra_fields: tuple[LLMProviderField, ...] = ()
    service_endpoints: tuple[LLMProviderEndpoint, ...] = ()
    default_service_endpoint_id: str = ""
    international_service_endpoint_id: str = ""

    @property
    def label_key(self) -> str:
        return f"llm_provider_label.{self.provider_id}"

    @property
    def tips_key(self) -> str:
        return f"llm_provider_tips.{self.provider_id}"

    @property
    def endpoint_selector_label_key(self) -> str:
        return f"llm_provider_endpoint_selector.{self.provider_id}"

    @property
    def endpoint_selector_help_key(self) -> str:
        return f"llm_provider_endpoint_selector_help.{self.provider_id}"

    @property
    def authentication_error_key(self) -> str:
        return f"llm_provider_authentication_error.{self.provider_id}"

    def endpoint_label_key(self, endpoint_id: str) -> str:
        return f"llm_provider_endpoint.{self.provider_id}.{endpoint_id}"

    def config_key(self, suffix: str) -> str:
        return f"{self.provider_id}_{suffix}"

    def resolve_model_name(self, configured_model: str | None) -> str:
        """Unify null values or obsolete historical default values into the current default model."""
        model_name = (configured_model or "").strip()
        if not model_name or model_name in self.deprecated_models:
            return self.default_model
        return model_name

    def resolve_base_url(self, configured_base_url: str | None) -> str:
        """Resolve Base URLs and migrate deactivated historical addresses to current defaults."""
        base_url = (configured_base_url or "").strip()
        deprecated_urls = {url.rstrip("/") for url in self.deprecated_base_urls}
        if not base_url or base_url.rstrip("/") in deprecated_urls:
            return self.effective_default_base_url
        return base_url

    def get_service_endpoint(self, endpoint_id: str) -> LLMProviderEndpoint | None:
        """Get the service area by stable ID to avoid business logic relying on changeable promotion links."""
        return next(
            (
                endpoint
                for endpoint in self.service_endpoints
                if endpoint.endpoint_id == endpoint_id
            ),
            None,
        )

    @property
    def default_service_endpoint(self) -> LLMProviderEndpoint | None:
        """Returns the default service area declared by the Provider."""
        return self.get_service_endpoint(self.default_service_endpoint_id)

    @property
    def international_service_endpoint(self) -> LLMProviderEndpoint | None:
        """Returns the international service area declared by the Provider."""
        return self.get_service_endpoint(self.international_service_endpoint_id)

    @property
    def effective_default_base_url(self) -> str:
        """The Base URL is read from the default service area first, and ordinary Providers still use the original fields."""
        endpoint = self.default_service_endpoint
        return endpoint.base_url if endpoint else self.default_base_url

    def preferred_service_endpoint(
        self, *, prefer_international: bool
    ) -> LLMProviderEndpoint | None:
        """Returns the preferred entrance according to the interface area, and safely falls back to the default entrance when the international entrance is missing."""
        if prefer_international and self.international_service_endpoint:
            return self.international_service_endpoint
        return self.default_service_endpoint

    def effective_api_key_url(self, *, prefer_international: bool = False) -> str:
        """Unified parsing of API Key application entries prevents Endpoint Provider from repeatedly maintaining links."""
        endpoint = self.preferred_service_endpoint(
            prefer_international=prefer_international
        )
        return endpoint.api_key_url if endpoint else self.api_key_url

    def effective_model_docs_url(self, *, prefer_international: bool = False) -> str:
        """Unify the parsing model list and document entry to avoid repeated maintenance of links by the Endpoint Provider."""
        endpoint = self.preferred_service_endpoint(
            prefer_international=prefer_international
        )
        return endpoint.model_docs_url if endpoint and endpoint.model_docs_url else self.model_docs_url

    def find_service_endpoint(
        self, configured_base_url: str | None
    ) -> LLMProviderEndpoint | None:
        """Identifies the Provider's standard service area based on a saved Base URL."""
        normalized_url = (configured_base_url or "").strip().rstrip("/")
        if not normalized_url:
            return None
        return next(
            (
                endpoint
                for endpoint in self.service_endpoints
                if endpoint.base_url.rstrip("/") == normalized_url
            ),
            None,
        )

    def select_service_endpoint(
        self,
        configured_base_url: str | None,
        *,
        has_api_key: bool,
        prefer_international: bool,
    ) -> LLMProviderEndpoint | None:
        """
        Select the standard service area that the WebUI should display.

        Explicitly saved standard addresses take precedence; unknown addresses are reserved as custom. The historical configuration may only be
        API Key without Base URL, such users continue to use the Registry default area to avoid
        After the upgrade, services will be switched due to different interface languages. Only new configurations are selected based on the interface language
        International entrance.
        """
        configured_url = (configured_base_url or "").strip()
        if configured_url:
            return self.find_service_endpoint(configured_url)

        default_endpoint = self.default_service_endpoint
        if has_api_key or not prefer_international:
            return default_endpoint

        return self.preferred_service_endpoint(
            prefer_international=prefer_international
        )


# The tuple order is the WebUI drop-down box order. When adding a common OpenAI-compatible Provider,
# Usually you only need to add one item here and supplement the locale; only Providers with different protocols need to add
# Add the corresponding adapter implementation in app/services/llm.py.
LLM_PROVIDER_REGISTRY = (
    # Recommended Provider
    LLMProviderSpec(
        "moonshot",
        "Kimi / Moonshot AI",
        default_model="kimi-k3",
        service_endpoints=(
            LLMProviderEndpoint(
                endpoint_id="china",
                default_label="China",
                base_url="https://api.moonshot.cn/v1",
                api_key_url=(
                    "https://platform.kimi.com?"
                    "track_id=track-6eec1e56a4494e52adcaebbcbbefce59&"
                    "aff=vietnamnewsvideo"
                ),
                model_docs_url=(
                    "https://platform.kimi.com/docs/models?"
                    "track_id=track-6eec1e56a4494e52adcaebbcbbefce59&"
                    "aff=vietnamnewsvideo"
                ),
            ),
            LLMProviderEndpoint(
                endpoint_id="global",
                default_label="Global",
                base_url="https://api.moonshot.ai/v1",
                api_key_url=(
                    "https://platform.kimi.ai?"
                    "track_id=track-9e3b711aa2594e378f6fe5b8de718a76&"
                    "aff=vietnamnewsvideo"
                ),
                model_docs_url=(
                    "https://platform.kimi.ai/docs/models?"
                    "track_id=track-9e3b711aa2594e378f6fe5b8de718a76&"
                    "aff=vietnamnewsvideo"
                ),
            ),
        ),
        default_service_endpoint_id="china",
        international_service_endpoint_id="global",
    ),
    # Mainstream model original manufacturers and cloud manufacturers
    LLMProviderSpec(
        "openai",
        "OpenAI",
        api_key_url="https://platform.openai.com/api-keys",
        default_model="gpt-5.5",
        default_base_url="https://api.openai.com/v1",
    ),
    LLMProviderSpec(
        "anthropic",
        "Anthropic Claude",
        api_key_url="https://platform.claude.com/settings/keys",
        default_model="claude-sonnet-5",
        default_base_url="https://api.anthropic.com/v1/",
    ),
    LLMProviderSpec(
        "gemini",
        "Google Gemini",
        adapter="gemini",
        api_key_url="https://aistudio.google.com/app/apikey",
        default_model="gemini-3.1-pro-preview",
        requires_base_url=False,
        show_base_url=False,
        deprecated_models=("gemini-pro", "gemini-1.0-pro"),
    ),
    LLMProviderSpec(
        "deepseek",
        "DeepSeek",
        api_key_url="https://platform.deepseek.com/api_keys",
        default_model="deepseek-v4-pro",
        default_base_url="https://api.deepseek.com",
    ),
    LLMProviderSpec(
        "qwen",
        "Alibaba Cloud Qwen",
        adapter="qwen",
        api_key_url="https://dashscope.console.aliyun.com/apiKey",
        default_model="qwen-max",
        requires_base_url=False,
        show_base_url=False,
    ),
    LLMProviderSpec(
        "azure",
        "Microsoft Azure OpenAI",
        adapter="azure",
        api_key_url=(
            "https://portal.azure.com/#view/"
            "Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI"
        ),
        default_model="gpt-35-turbo",
    ),
    LLMProviderSpec(
        "volcengine",
        "ByteDance VolcEngine Ark",
        api_key_url=(
            "https://www.volcengine.com/activity/ai618?utm_campaign=hw&"
            "utm_content=hw&utm_medium=devrel_tool_web&utm_source=OWO&"
            "utm_term=VietNamNewsVideo"
        ),
        default_model="doubao-seed-2-1-turbo-260628",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
    ),
    LLMProviderSpec(
        "grok",
        "xAI Grok",
        api_key_url="https://console.x.ai/",
        default_model="grok-4.3",
        default_base_url="https://api.x.ai/v1",
    ),
    LLMProviderSpec(
        "minimax",
        "MiniMax",
        api_key_url="https://platform.minimax.io/",
        default_model="MiniMax-M3",
        default_base_url="https://api.minimax.io/v1",
    ),
    LLMProviderSpec(
        "mimo",
        "Xiaomi MiMo",
        api_key_url=(
            "https://platform.xiaomimimo.com/docs/zh-CN/quick-start/first-api-call"
        ),
        default_model="mimo-v2.5-pro",
        default_base_url="https://api.xiaomimimo.com/v1",
    ),
    # Aggregation and unified access platform
    LLMProviderSpec(
        "shengsuanyun",
        "Shengsuan Cloud",
        api_key_url="https://www.shengsuanyun.com/?from=CH_XUQ4OTSK",
        default_model="deepseek/deepseek-v4-flash",
        default_base_url="https://router.shengsuanyun.com/api/v1",
    ),
    # APIMart provides both `/api/v1` business interface and `/v1` OpenAI compatible interface.
    # The current LLM service layer relies on OpenAI SDK to read choices directly, so it must be used without
    # The `/v1` entry in the outer package of code/data cannot copy the address of the asynchronous business interface.
    LLMProviderSpec(
        "apimart",
        "APIMart",
        api_key_url="https://go.apimart.ai/gh-vietnamnewsvideo",
        default_model="gpt-5.6-terra",
        default_base_url="https://api.apimart.ai/v1",
    ),
    LLMProviderSpec(
        "cloudflare",
        "Cloudflare AI Gateway",
        adapter="cloudflare_ai_gateway",
        api_key_url="https://dash.cloudflare.com/",
        default_model="openai/gpt-4.1-mini",
        requires_base_url=False,
        show_base_url=False,
        deprecated_models=("@cf/meta/llama-3.1-8b-instruct",),
        extra_fields=(
            LLMProviderField("account_id", "Account ID", required=True),
            LLMProviderField(
                "gateway_id",
                "Gateway ID",
                default_value="default",
            ),
        ),
    ),
    LLMProviderSpec(
        "modelscope",
        "Alibaba ModelScope",
        adapter="modelscope",
        api_key_url=("https://modelscope.cn/docs/model-service/API-Inference/intro"),
        default_model="ZhipuAI/GLM-5.2",
        default_base_url="https://api-inference.modelscope.cn/v1/",
    ),
    LLMProviderSpec(
        "aihubmix",
        "AIHubMix",
        api_key_url="https://aihubmix.com/",
        default_model="gpt-5.4-mini",
        default_base_url="https://aihubmix.com/v1",
    ),
    LLMProviderSpec(
        "aimlapi",
        "AIML API",
        api_key_url="https://aimlapi.com/app/keys",
        default_model="openai/gpt-5-5",
        default_base_url="https://api.aimlapi.com/v1",
    ),
    LLMProviderSpec(
        "evolink",
        "EvoLink",
        api_key_url="https://evolink.ai/dashboard/keys",
        default_model="gpt-5.5",
        default_base_url="https://direct.evolink.ai/v1",
    ),
    LLMProviderSpec(
        "openrouter",
        "OpenRouter",
        api_key_url="https://openrouter.ai/settings/keys",
        default_model="minimax/minimax-m3:free",
        default_base_url="https://openrouter.ai/api/v1",
    ),
    LLMProviderSpec(
        "api_route",
        "API Route",
        api_key_url="https://www.api-route.com",
        default_model="gpt-5.4-mini",
        default_base_url="https://www.api-route.com/v1",
        model_docs_url="https://www.api-route.com/pricing",
    ),
    # Fluxion's OpenAI grouping uses the /v1 interface, reusing existing Chat Completions adapters.
    # The grouping determines the available models and addresses, so only default values are provided here, retaining the user's ability to override the configuration;
    # Anthropic/Gemini native groups cannot use this entry directly to avoid protocol mismatch.
    LLMProviderSpec(
        "fluxionai",
        "Fluxion AI",
        api_key_url=(
            "https://fluxionai.space/register?source=github"
            "&campaign=vietnamnewsvideo&promo=MONEYPRINTERTURBO"
        ),
        default_model="gpt-5.5",
        default_base_url="https://fluxionai.space/v1",
        model_docs_url="https://fluxionai.space/model-plaza",
    ),
    LLMProviderSpec(
        "cheaperinference",
        "Cheaper Inference",
        api_key_url="https://cheaperinference.com/signup",
        default_model="gpt-5.4-mini",
        default_base_url="https://api.cheaperinference.com/v1",
        model_docs_url="https://cheaperinference.com/#models",
    ),
    LLMProviderSpec(
        "requesty",
        "Requesty",
        api_key_url="https://app.requesty.ai/api-keys",
        default_model="openai/gpt-5.4-mini",
        default_base_url="https://router.requesty.ai/v1",
        model_docs_url="https://www.requesty.ai/models",
    ),
    # Local deployment and universal gateway
    LLMProviderSpec(
        "ollama",
        "Ollama",
        requires_api_key=False,
        show_api_key=False,
    ),
    # Claude subscription (Pro/Max/Team) does not issue API Key, the certificate can only be issued by Claude Code
    # The official client uses it, so this Provider does not use the HTTP interface, but calls the local machine to log in.
    # claude CLI. Leave the model name blank to use the CLI's current default model.
    LLMProviderSpec(
        "claude_code",
        "Claude Code (Claude subscription)",
        adapter="claude_code",
        requires_api_key=False,
        show_api_key=False,
        requires_base_url=False,
        show_base_url=False,
        requires_model_name=False,
        extra_fields=(
            LLMProviderField("cli_path", "Claude CLI Path"),
            LLMProviderField("timeout", "Timeout (seconds)", default_value="300"),
        ),
    ),
    LLMProviderSpec(
        "oneapi",
        "OneAPI",
        api_key_url="https://github.com/songquanpeng/one-api",
    ),
    LLMProviderSpec(
        "litellm",
        "LiteLLM",
        adapter="litellm",
        default_model="openai/gpt-4o-mini",
        requires_api_key=False,
        requires_base_url=False,
        show_api_key=False,
        show_base_url=False,
    ),
    # Other reasoning and public services
    LLMProviderSpec(
        "groq",
        "Groq",
        api_key_url="https://console.groq.com/keys",
        default_model="openai/gpt-oss-120b",
        default_base_url="https://api.groq.com/openai/v1",
    ),
    LLMProviderSpec(
        "pollinations",
        "Pollinations AI",
        api_key_url="https://enter.pollinations.ai/",
        default_model="openai-fast",
        default_base_url="https://gen.pollinations.ai/v1",
        deprecated_models=("default",),
        deprecated_base_urls=("https://text.pollinations.ai/openai",),
    ),
)

LLM_PROVIDERS = {provider.provider_id: provider for provider in LLM_PROVIDER_REGISTRY}

if len(LLM_PROVIDERS) != len(LLM_PROVIDER_REGISTRY):
    raise RuntimeError("duplicate LLM provider id in registry")


def get_llm_provider(provider_id: str) -> LLMProviderSpec | None:
    return LLM_PROVIDERS.get((provider_id or "").lower())


def normalize_provider_override(value: str | None, default_value: str | None) -> str:
    """
    Only user override values that differ from the Registry default are retained.

    WebUI needs to display the default value in the input box, but it cannot solidify the default value to config.toml;
    Otherwise, when the Registry default model or address is subsequently upgraded, the old configuration will continue to overwrite the new default value.
    """
    normalized_value = (value or "").strip()
    normalized_default = (default_value or "").strip()
    if normalized_value == normalized_default:
        return ""
    return normalized_value
