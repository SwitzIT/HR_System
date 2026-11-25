"""AI layer built on LangChain, with Hugging Face models by default.

Public API (used by services.py and the views; unchanged from the earlier Gemini version):
    generate_jd(job, company, instructions=None, current_text=None) -> (markdown, source, error)
    template_jd(job, company) -> markdown
    screen(job, resume_text, ai_weight=0.7) -> dict
    ai_enabled() / model_name() / status()

Any model failure falls back to deterministic logic, so the portal keeps working.
"""
import hashlib
import logging
import math
import os

import scoring

from . import chains, providers
from .providers import llm_enabled as ai_enabled, model_label as model_name  # noqa: F401
from .templates import template_jd  # noqa: F401

log = logging.getLogger(__name__)


def friendly_error(e):
    """Turn provider errors into something an HR user (or admin) can act on."""
    msg = str(e)
    low = msg.lower()
    if "402" in msg or "payment required" in low or "exceeded your monthly included credits" in low:
        return ("Hugging Face returned 402 Payment Required: this account has no inference credits. "
                "Add credits or a PRO plan in your Hugging Face billing settings, or set "
                "LLM_PROVIDER=huggingface_local to run a free model on this server.")
    if "401" in msg or "unauthorized" in low or "invalid token" in low or "invalid api key" in low:
        return "The AI provider rejected the API key (401). Check the token in .env."
    if "429" in msg or "rate limit" in low or "too many requests" in low:
        return "The AI provider is rate-limiting requests (429). Try again in a minute."
    if "not supported" in low or "model_not_supported" in low or "no inference provider" in low or "404" in msg:
        return (f"Model '{providers.model_id()}' isn't available from the configured provider. "
                "Choose another LLM_MODEL, or set HF_INFERENCE_PROVIDER to a provider that serves it.")
    if "transformers" in low or "sentence_transformers" in low or "torch" in low:
        return ("Local Hugging Face mode needs extra packages. Run: pip install -r requirements-local.txt")
    if isinstance(e, ImportError) or "no module named" in low:
        return f"A package is missing for this provider ({msg}). Install that provider's langchain-* package."
    if isinstance(e, OSError) or "local_files_only" in low or "couldn't connect" in low \
            or "does not appear to have" in low or "is not a local folder" in low:
        return (f"The local model '{providers.model_id()}' couldn't be loaded (not downloaded yet, or no internet "
                "to download it). Run: python download_models.py")
    if type(e).__name__ in ("OutputParserException", "ValidationError", "JSONDecodeError"):
        return "The model's reply couldn't be read as a screening result, so the keyword score was used."
    return msg[:300]


# --------------------------------------------------------------------------- JD

def _jd_inputs(job, company):
    import requisition
    return {"requirements": requisition.prompt_requirements(job, company), "outline": requisition.jd_outline(job)}


# Sections the model must write. Required, Preferred and Position Details are added by the app
# from the confirmed requisition (requisition.assemble_jd).
JD_SECTIONS = ("job overview", "key responsibilities")


def missing_sections(text):
    """Mandatory JD sections (from the prompt's output structure) that the reply doesn't contain."""
    low = (text or "").lower()
    return [s for s in JD_SECTIONS if s not in low]


def generate_jd(job, company, instructions=None, current_text=None):
    """Return (markdown, source, error).

    source is 'ai', 'template' (no model / model failed), or 'not_a_jd' when the model replied
    with clarification questions or without the required sections. In that case markdown is None,
    the caller keeps the existing draft, and `error` carries the model's reply for HR to read.
    """
    if not ai_enabled():
        return template_jd(job, company), "template", None
    try:
        inputs = _jd_inputs(job, company)
        if instructions and current_text:
            text = chains.jd_chain(revise=True).invoke(
                {**inputs, "current_text": current_text, "instructions": instructions})
        else:
            extra = f"\n\nAdditional reviewer instructions: {instructions}" if instructions else ""
            text = chains.jd_chain().invoke({**inputs, "extra_instructions": extra})
        text = chains.clean_text(text)
        if len(text) < 80 and "?" not in text:
            raise ValueError("The model returned an empty or very short draft.")
        gaps = missing_sections(text)
        if not gaps and any(n in text for n in ("[concise",)):
            return None, "not_a_jd", ("The AI copied the outline's placeholder notes instead of replacing them, "
                                      f"so it wasn't saved. Its reply: {text[:900]}")
        if gaps:
            asked = text.count("?") >= 1 and len(gaps) >= 2
            lead = ("The AI asked for more information instead of writing the JD"
                    if asked else "The AI's reply was missing required sections (" + ", ".join(gaps) + ")")
            return None, "not_a_jd", f"{lead}, so it wasn't saved. Its reply: {text[:900]}"
        import requisition
        return requisition.assemble_jd(text, job), "ai", None
    except Exception as e:  # network, credits, bad key, missing package ...
        log.exception("JD generation failed")
        return template_jd(job, company), "template", friendly_error(e)


# --------------------------------------------------------------------------- screening

_jd_vectors = {}


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def semantic_similarity(jd_text, resume_text):
    """Cosine similarity of JD and resume embeddings, or None if embeddings are off/failing."""
    emb = providers.get_embeddings() if providers.embeddings_provider() else None
    if not emb or not jd_text or not resume_text:
        return None
    try:
        key = (providers.embeddings_provider(), hashlib.sha1(jd_text.encode()).hexdigest())
        if key not in _jd_vectors:
            _jd_vectors[key] = emb.embed_query(jd_text[:4000])
        return _cos(_jd_vectors[key], emb.embed_query(resume_text[:4000]))
    except Exception:
        log.exception("Embedding similarity failed; using lexical similarity")
        return None


def screen(job, resume_text, ai_weight=0.7):
    """Score a resume against a job. Always returns a result dict."""
    sem = semantic_similarity(job.jd_text, resume_text) if resume_text.strip() else None
    kw = scoring.keyword_score(job, resume_text, semantic=sem)
    result = {
        "score_keyword": kw["score"], "score_ai": None, "matched": kw["matched"], "missing": kw["missing"],
        "years": kw["years"], "strengths": kw["strengths"], "gaps": kw["gaps"], "summary": None,
        "scorer": "keyword" + (" + embeddings" if sem is not None else ""), "error": None,
    }
    if not resume_text.strip():
        result.update(score_final=0.0, recommendation="Not a match",
                      summary="Could not read any text from this resume (scanned image or protected file?). Review manually.",
                      gaps=["Resume text could not be extracted"])
        return result

    ai = None
    if ai_enabled():
        try:
            ai = chains.screen_structured({
                "title": job.title, "skills": job.skills or "-", "nice_to_have": job.nice_to_have or "-",
                "experience_min": job.experience_min or 0, "jd_text": job.jd_text or "",
                "resume_text": resume_text[:20000],
            })
        except Exception as e:
            log.exception("AI screening failed")
            result["error"] = friendly_error(e)

    if ai:
        result.update(
            score_ai=float(ai.match_score),
            matched=ai.matched_skills or kw["matched"],
            missing=ai.missing_skills if ai.missing_skills is not None else kw["missing"],
            years=ai.years_experience or kw["years"],
            strengths=ai.strengths or kw["strengths"],
            gaps=ai.gaps or kw["gaps"],
            summary=ai.summary or None,
            scorer=f"{model_name()} + {result['scorer']}",
        )
        final = ai_weight * ai.match_score + (1 - ai_weight) * kw["score"]
    else:
        final = kw["score"]

    result["score_final"] = round(final, 1)
    result["recommendation"] = scoring.recommendation_for(final)
    if not result["summary"]:
        m, n = len(job.skill_list) - len(kw["missing"]), len(job.skill_list)
        result["summary"] = (f"Keyword screen: matches {m} of {n} must-have skills"
                             + (f", ~{kw['years']:g} yrs experience mentioned." if kw["years"] else "."))
    return result


# --------------------------------------------------------------------------- diagnostics

def status():
    """Configuration summary for the HR team page and `flask ai-check`."""
    return {
        "llm_provider": providers.provider() or "none",
        "llm_model": providers.model_id(),
        "llm_ready": ai_enabled(),
        "llm_problem": providers.missing_requirement(),
        "local_device": (("GPU " + str(providers.local_device())) if providers.local_device() >= 0 else "CPU")
                        if providers.provider() == providers.HF_LOCAL else "-",
        "structured_output": "native" if providers.supports_native_structured_output() else "prompt + Pydantic parser",
        "embeddings_provider": providers.embeddings_provider() or "off (lexical similarity)",
        "embeddings_model": providers._cfg("EMBEDDINGS_MODEL") if providers.embeddings_provider() else "",
        "langsmith": "on" if (os.getenv("LANGSMITH_TRACING", "").lower() == "true"
                              and os.getenv("LANGSMITH_API_KEY")) else "off",
        "langsmith_project": os.getenv("LANGSMITH_PROJECT", ""),
    }
