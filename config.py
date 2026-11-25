import os
from dotenv import load_dotenv

load_dotenv()

# LangSmith tracing (LangChain's observability service) reads its settings straight from the
# environment. Accept the older LANGCHAIN_* names too, so either style works in .env.
for _new, _old in (("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"), ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"),
                   ("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"), ("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT")):
    if not os.getenv(_new) and os.getenv(_old):
        os.environ[_new] = os.environ[_old]
if os.getenv("LANGSMITH_API_KEY", "").strip():
    os.environ.setdefault("LANGSMITH_TRACING", "true")
else:
    # No key: switch tracing off, otherwise LangChain tries to upload traces and logs 401 errors.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ.pop("LANGCHAIN_TRACING_V2", None)
os.environ.setdefault("LANGSMITH_PROJECT", "talentflow")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

_PLACEHOLDERS = {"", "dev-only-change-me", "change-me-to-a-long-random-string",
                 "change-me-to-a-different-long-random-string"}


def _stored_secret(name):
    """A random secret created once and kept in instance/ (so it survives restarts)."""
    import secrets
    folder = os.path.join(BASE_DIR, "instance")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f".{name}")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            return value
    value = secrets.token_urlsafe(48)
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    return value


def _env(name, default=""):
    """Read a setting. An empty setting followed by a comment (`KEY=   # note`) is read by
    python-dotenv as the comment text; treat that as empty so the default applies."""
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    if v.startswith("#"):
        return default
    return v


def _db_url():
    url = _env("DATABASE_URL", "")
    if not url:
        return "sqlite:///" + os.path.join(BASE_DIR, "instance", "talentflow.db")
    # Render / Heroku style URLs
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    # Signs sign-in sessions and forms. If .env leaves it blank or as the example text, a random key
    # is created once and kept in instance/.secret_key.
    SECRET_KEY = _env("SECRET_KEY", "") if _env("SECRET_KEY", "") not in _PLACEHOLDERS else _stored_secret("secret_key")
    SQLALCHEMY_DATABASE_URI = _db_url()
    # Branding. Put your logo files in static/brand/ (logo.png|svg|jpg for light backgrounds,
    # logo-inverse.* for the dark footer); without them the company name is shown as a wordmark.
    BRAND_COLOR = _env("BRAND_COLOR", "#c8102e")
    BRAND_ACCENT = _env("BRAND_ACCENT", "#f5a623")
    COMPANY_WEBSITE = _env("COMPANY_WEBSITE", "https://www.switzgroup.com/")
    COMPANY_TAGLINE = _env("COMPANY_TAGLINE", "")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # How long a page's form stays valid after the page is opened (security token, Flask-WTF).
    # Default 480 minutes (a working day). 0 = valid until the user signs out.
    # background = candidates submit instantly and resumes are screened by a worker thread;
    # inline = screen during the request (tests / debugging)
    SCREENING_MODE = (_env("SCREENING_MODE", "background") or "background").lower()
    FORM_EXPIRY_MINUTES = int(_env("FORM_EXPIRY_MINUTES", "480"))
    WTF_CSRF_TIME_LIMIT = FORM_EXPIRY_MINUTES * 60 if FORM_EXPIRY_MINUTES > 0 else None
    # SQLite: wait up to 30 s for another write to finish instead of the default 5 s.
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 30}} if _db_url().startswith("sqlite") else {}

    UPLOAD_FOLDER = _env("UPLOAD_FOLDER", os.path.join(BASE_DIR, "instance", "resumes"))
    MAX_CONTENT_LENGTH = int(_env("MAX_UPLOAD_MB", "5")) * 1024 * 1024
    ALLOWED_RESUME_EXT = {"pdf", "docx"}

    # ------------------------------------------------------------------ AI (LangChain)
    # One switch picks the chat model. Change provider + model to move from free to paid;
    # no code changes needed. Supported: huggingface (HF Inference Providers API),
    # huggingface_local (runs on this machine, free), openai, anthropic, google_genai,
    # groq, mistralai, ollama ... (any provider LangChain's init_chat_model supports),
    # or "none" for rules mode (template JDs + keyword screening only).
    LLM_PROVIDER = _env("LLM_PROVIDER", "huggingface").strip().lower()
    LLM_MODEL = _env("LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct-1M").strip()
    LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", "1200"))
    LLM_TEMPERATURE_JD = float(_env("LLM_TEMPERATURE_JD", "0.4"))
    LLM_TEMPERATURE_SCREEN = float(_env("LLM_TEMPERATURE_SCREEN", "0.1"))
    LLM_TIMEOUT = int(_env("LLM_TIMEOUT", "120"))

    # Hugging Face
    HF_TOKEN = (_env("HUGGINGFACEHUB_API_TOKEN") or _env("HF_TOKEN") or "").strip()
    HF_INFERENCE_PROVIDER = _env("HF_INFERENCE_PROVIDER", "auto").strip()  # auto, together, novita, ...
    HF_LOCAL_DEVICE = _env("HF_LOCAL_DEVICE", "auto").strip().lower()  # auto | -1 (CPU) | 0 (first GPU)

    # Embeddings: semantic JD <-> resume similarity (10% of the keyword score).
    # huggingface (API), huggingface_local (sentence-transformers, free), openai, or none.
    EMBEDDINGS_PROVIDER = _env("EMBEDDINGS_PROVIDER", "huggingface").strip().lower()
    EMBEDDINGS_MODEL = _env("EMBEDDINGS_MODEL", "sentence-transformers/all-MiniLM-L6-v2").strip()

    # Scoring
    AI_WEIGHT = float(_env("AI_WEIGHT", "0.7"))  # final = AI*w + keyword*(1-w)
    DEFAULT_SHORTLIST_THRESHOLD = int(_env("DEFAULT_SHORTLIST_THRESHOLD", "70"))

    # Maker-checker: when true, the HR user who generated/edited a JD cannot approve it.
    REQUIRE_DIFFERENT_APPROVER = _env("REQUIRE_DIFFERENT_APPROVER", "false").lower() == "true"

    COMPANY_NAME = _env("COMPANY_NAME", "Switz Group")
    # Base URL used in emailed links, e.g. https://careers.switzgroup.com (falls back to the request URL)
    APP_BASE_URL = _env("APP_BASE_URL", "").rstrip("/")

    # ------------------------------------------------------------------ Candidate identity & re-apply rule
    # PAN / passport numbers are never stored in plain text: only a keyed hash (to recognise the same
    # person again) and the last 4 characters (for HR to see). Keep ID_HASH_KEY secret and never change
    # it once candidates have registered, or existing IDs will no longer match.
    # Resolution order keeps IDs registered under earlier versions matching: ID_HASH_KEY from .env,
    # else the SECRET_KEY text from .env (the old fallback), else a key kept in instance/.id_hash_key.
    ID_HASH_KEY = _env("ID_HASH_KEY", "") or _env("SECRET_KEY", "") or _stored_secret("id_hash_key")
    ID_HASH_KEY_IS_EXAMPLE = ID_HASH_KEY in _PLACEHOLDERS
    REAPPLY_COOLDOWN_DAYS = int(_env("REAPPLY_COOLDOWN_DAYS", "90"))
    # any_job: after a rejection/withdrawal the candidate can't apply to ANY job until the period ends.
    # same_title: only jobs with the same title are blocked.
    REAPPLY_SCOPE = _env("REAPPLY_SCOPE", "any_job").strip().lower()

    # ------------------------------------------------------------------ Email (sharing jobs with staff)
    # Email: "smtp" (username + password) or "graph" (Microsoft 365 via Microsoft Graph + OAuth,
    # app registration with the Mail.Send application permission). Blank = smtp.
    EMAIL_PROVIDER = (_env("EMAIL_PROVIDER", "smtp") or "smtp").lower()
    GRAPH_TENANT_ID = _env("GRAPH_TENANT_ID", "")
    GRAPH_CLIENT_ID = _env("GRAPH_CLIENT_ID", "")
    GRAPH_CLIENT_SECRET = _env("GRAPH_CLIENT_SECRET", "")
    GRAPH_SENDER = _env("GRAPH_SENDER", "")  # mailbox the mail is sent from, e.g. careers@yourcompany.com
    SMTP_HOST = _env("SMTP_HOST", "").strip()
    SMTP_PORT = int(_env("SMTP_PORT", "587"))
    SMTP_USER = _env("SMTP_USER", "").strip()
    SMTP_PASSWORD = _env("SMTP_PASSWORD", "")
    SMTP_SENDER = _env("SMTP_SENDER", "").strip() or _env("SMTP_USER", "").strip()
    SMTP_USE_TLS = _env("SMTP_USE_TLS", "true").lower() == "true"  # STARTTLS
    # Comma-separated staff email domains, e.g. switzgroup.com. Blank = any address allowed.
    INTERNAL_EMAIL_DOMAINS = [d.strip().lower().lstrip("@") for d in _env("INTERNAL_EMAIL_DOMAINS", "").split(",") if d.strip()]
