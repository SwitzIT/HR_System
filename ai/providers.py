"""Model factory: the one place that knows which LLM / embedding provider is in use.

Everything else in the app asks for `get_chat_model("jd")` or `get_embeddings()` and gets a
LangChain object back, so swapping a free Hugging Face model for a paid one is a .env change:

    LLM_PROVIDER=huggingface        LLM_MODEL=Qwen/Qwen2.5-7B-Instruct-1M       (HF Inference Providers)
    LLM_PROVIDER=huggingface_local  LLM_MODEL=Qwen/Qwen2.5-1.5B-Instruct        (runs on this machine)
    LLM_PROVIDER=openai             LLM_MODEL=gpt-4.1-mini                      (+ OPENAI_API_KEY)
    LLM_PROVIDER=anthropic          LLM_MODEL=claude-sonnet-4-5                 (+ ANTHROPIC_API_KEY)
    LLM_PROVIDER=google_genai       LLM_MODEL=gemini-2.5-flash                  (+ GOOGLE_API_KEY)
"""
import logging
import os
import threading

from flask import current_app

log = logging.getLogger(__name__)

HF_API = "huggingface"
HF_LOCAL = "huggingface_local"
NONE = {"", "none", "off", "rules"}

# Providers whose LangChain chat class supports reliable native structured output
# (tool calling / JSON schema). Others get prompt + Pydantic parsing, which works with any model.
NATIVE_STRUCTURED = {"openai", "anthropic", "google_genai", "google_vertexai", "groq", "mistralai",
                     "azure_openai", "bedrock_converse", "fireworks", "together"}

# Environment variable each paid provider's LangChain package reads its key from.
PROVIDER_KEYS = {
    "openai": "OPENAI_API_KEY", "azure_openai": "AZURE_OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "google_genai": "GOOGLE_API_KEY", "groq": "GROQ_API_KEY", "mistralai": "MISTRAL_API_KEY",
    "fireworks": "FIREWORKS_API_KEY", "together": "TOGETHER_API_KEY",
}

_cache = {}
_lock = threading.Lock()


def _cfg(key, default=None):
    return current_app.config.get(key, default)


def provider():
    return (_cfg("LLM_PROVIDER") or "").lower()


def model_id():
    return _cfg("LLM_MODEL") or ""


def missing_requirement():
    """Why the configured LLM can't be used, or None if it looks usable."""
    p = provider()
    if p in NONE:
        return "LLM_PROVIDER is set to none"
    if p == HF_API and not _cfg("HF_TOKEN"):
        return "HUGGINGFACEHUB_API_TOKEN is not set"
    key = PROVIDER_KEYS.get(p)
    if key and not os.getenv(key):
        return f"{key} is not set"
    if not model_id():
        return "LLM_MODEL is not set"
    return None


def llm_enabled():
    return missing_requirement() is None


def model_label():
    p = provider()
    if p in NONE:
        return "rules mode"
    names = {HF_API: "HF API", HF_LOCAL: "HF local"}
    return f"{names.get(p, p)} · {model_id()}"


def supports_native_structured_output():
    return provider() in NATIVE_STRUCTURED


def local_device():
    """Pipeline device for local models: GPU index if one is really available, else -1 (CPU).

    HF_LOCAL_DEVICE=auto picks GPU 0 when PyTorch can see a CUDA GPU. An explicit GPU number that
    doesn't exist (no NVIDIA GPU, or the CPU-only PyTorch build) falls back to CPU with a warning
    instead of failing.
    """
    raw = str(_cfg("HF_LOCAL_DEVICE", "auto")).strip().lower()
    try:
        import torch
        gpus = torch.cuda.device_count() if torch.cuda.is_available() else 0
    except Exception:
        gpus = 0
    if raw in ("", "auto"):
        return 0 if gpus else -1
    if raw in ("cpu", "-1"):
        return -1
    try:
        want = int(raw.replace("cuda:", "").replace("cuda", "0"))
    except ValueError:
        log.warning("HF_LOCAL_DEVICE=%r not understood; using CPU", raw)
        return -1
    if want >= gpus:
        log.warning("HF_LOCAL_DEVICE=%s but PyTorch sees %d GPU(s); using CPU instead", raw, gpus)
        return -1
    return want


def _build_chat_model(temperature):
    p, m = provider(), model_id()
    max_tokens = _cfg("LLM_MAX_TOKENS", 1200)
    timeout = _cfg("LLM_TIMEOUT", 120)

    if p == HF_API:
        from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
        endpoint = HuggingFaceEndpoint(
            repo_id=m,
            task="text-generation",
            provider=_cfg("HF_INFERENCE_PROVIDER") or "auto",
            huggingfacehub_api_token=_cfg("HF_TOKEN"),
            max_new_tokens=max_tokens,
            temperature=max(temperature, 0.01),  # some HF backends reject exactly 0
            timeout=timeout,
        )
        return ChatHuggingFace(llm=endpoint)

    if p == HF_LOCAL:
        # Downloads the model once into the Hugging Face cache, then runs fully offline.
        from langchain_huggingface import ChatHuggingFace
        return ChatHuggingFace.from_model_id(
            model_id=m,
            task="text-generation",
            backend="pipeline",
            device=local_device(),
            pipeline_kwargs={"max_new_tokens": max_tokens, "return_full_text": False},
        )

    # Any other provider: LangChain's universal factory (needs that provider's package,
    # e.g. langchain-openai, langchain-anthropic, langchain-google-genai).
    from langchain.chat_models import init_chat_model
    kwargs = {"temperature": temperature, "timeout": timeout}
    if p in ("openai", "azure_openai", "anthropic", "groq", "mistralai", "together", "fireworks"):
        kwargs["max_tokens"] = max_tokens
    elif p in ("google_genai", "google_vertexai"):
        kwargs["max_output_tokens"] = max_tokens
    elif p == "ollama":
        kwargs = {"temperature": temperature, "num_predict": max_tokens}
    return init_chat_model(m, model_provider=p, **kwargs)


def get_chat_model(purpose="jd"):
    """Cached LangChain chat model. purpose: 'jd' (creative) or 'screen' (deterministic)."""
    temperature = _cfg("LLM_TEMPERATURE_SCREEN", 0.1) if purpose == "screen" else _cfg("LLM_TEMPERATURE_JD", 0.4)
    p = provider()
    # A local model is loaded into memory once and shared, whatever the temperature.
    key = (p, model_id(), "shared" if p == HF_LOCAL else temperature)
    with _lock:
        if key not in _cache:
            log.info("Loading chat model %s (%s)", model_id(), p)
            _cache[key] = _build_chat_model(temperature)
        model = _cache[key]
    if p == HF_LOCAL:
        # One copy in memory; generation settings are applied per call.
        gen = {"max_new_tokens": _cfg("LLM_MAX_TOKENS", 1200), "return_full_text": False}
        if temperature > 0.2:
            gen.update(do_sample=True, temperature=temperature, top_p=0.9, repetition_penalty=1.1)
        else:
            gen.update(do_sample=False)
        return model.bind(pipeline_kwargs=gen)
    return model


# --------------------------------------------------------------------------- embeddings

def embeddings_provider():
    p = (_cfg("EMBEDDINGS_PROVIDER") or "").lower()
    if p in NONE:
        return None
    if p == HF_API and not _cfg("HF_TOKEN"):
        return None
    key = PROVIDER_KEYS.get(p)
    if key and not os.getenv(key):
        return None
    return p


def get_embeddings():
    """Cached LangChain Embeddings object, or None when embeddings are off."""
    p = embeddings_provider()
    if not p:
        return None
    m = _cfg("EMBEDDINGS_MODEL")
    key = ("emb", p, m)
    with _lock:
        if key not in _cache:
            if p == HF_API:
                from langchain_huggingface import HuggingFaceEndpointEmbeddings
                _cache[key] = HuggingFaceEndpointEmbeddings(
                    model=m, task="feature-extraction", huggingfacehub_api_token=_cfg("HF_TOKEN"))
            elif p == HF_LOCAL:
                from langchain_huggingface import HuggingFaceEmbeddings
                _cache[key] = HuggingFaceEmbeddings(
                    model_name=m, model_kwargs={"device": "cpu" if local_device() < 0 else f"cuda:{local_device()}"},
                    encode_kwargs={"normalize_embeddings": True})
            else:
                from langchain.embeddings import init_embeddings
                _cache[key] = init_embeddings(m, provider=p)
        return _cache[key]


def reset_cache():
    with _lock:
        _cache.clear()
