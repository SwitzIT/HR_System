"""Deterministic, explainable resume-vs-JD scorer.

Used on its own when no AI key is configured, and blended with the AI score otherwise,
so a single LLM judgement never decides a shortlist by itself.

keyword_score = 60% must-have skill coverage
              + 15% nice-to-have coverage
              + 15% experience fit
              + 10% JD <-> resume similarity (embeddings when configured, else word overlap)
"""
import math
import re
from collections import Counter

# Common alternative spellings -> canonical skill
ALIASES = {
    "javascript": ["js", "ecmascript"],
    "typescript": ["ts"],
    "node.js": ["nodejs", "node js", "node"],
    "react": ["react.js", "reactjs"],
    "machine learning": ["ml"],
    "artificial intelligence": ["ai"],
    "natural language processing": ["nlp"],
    "power bi": ["powerbi", "power-bi"],
    "microsoft excel": ["excel", "ms excel", "advanced excel"],
    "postgresql": ["postgres"],
    "kubernetes": ["k8s"],
    "amazon web services": ["aws"],
    "google cloud": ["gcp", "google cloud platform"],
    "microsoft azure": ["azure"],
    "ci/cd": ["cicd", "ci cd", "continuous integration"],
    "dynamics 365": ["d365", "dynamics365"],
    "sql": ["t-sql", "tsql", "mysql", "sql server"],
    "python": ["py"],
    "human resources": ["hr"],
}

STOP = set("""a an and are as at be by for from has have in is it its of on or that the to was were will with
you your we our they their this these those i me my he she his her them who what which when where why how
not no can could should would may might must shall about into over under than then also such using use used
work working role responsibilities requirements experience years year team strong good ability skills""".split())


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _variants(skill):
    s = _norm(skill)
    out = {s}
    for canon, alts in ALIASES.items():
        if s == canon or s in alts:
            out.add(canon)
            out.update(alts)
    # very short aliases like "ai", "ml", "hr" are noisy unless the skill itself is that short
    return {v for v in out if len(v) > 2 or v == s}


def has_skill(text_norm, skill):
    for v in _variants(skill):
        if re.search(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])", text_norm):
            return True
    return False


def estimate_years(text):
    """Largest 'N years' / 'N+ yrs' mention, capped to a sane range."""
    t = _norm(text)
    nums = [float(m) for m in re.findall(r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years|yrs|year)", t)]
    nums = [n for n in nums if 0 < n <= 45]
    return max(nums) if nums else None


def _tokens(text):
    return [w for w in re.findall(r"[a-z][a-z0-9+#.]{1,}", _norm(text)) if w not in STOP]


def cosine(a, b):
    ca, cb = Counter(_tokens(a)), Counter(_tokens(b))
    if not ca or not cb:
        return 0.0
    dot = sum(ca[k] * cb[k] for k in ca.keys() & cb.keys())
    return dot / (math.sqrt(sum(v * v for v in ca.values())) * math.sqrt(sum(v * v for v in cb.values())))


def keyword_score(job, resume_text, semantic=None):
    """semantic: optional embedding cosine (JD vs resume) from the AI layer; replaces lexical similarity."""
    t = _norm(resume_text)
    must, nice = job.skill_list, job.nice_list

    matched = [s for s in must if has_skill(t, s)]
    missing = [s for s in must if s not in matched]
    nice_hit = [s for s in nice if has_skill(t, s)]

    must_cov = len(matched) / len(must) if must else 0.5
    nice_cov = len(nice_hit) / len(nice) if nice else 0.5

    yrs = estimate_years(resume_text)
    need = job.experience_min or 0
    if need == 0:
        exp_fit = 1.0
    elif yrs is None:
        exp_fit = 0.4  # unknown: don't zero it, the AI / human will judge
    else:
        exp_fit = min(1.0, yrs / need)

    if semantic is not None:
        # sentence-embedding cosine for related texts sits roughly 0.2-0.7; stretch to 0-1
        sim = max(0.0, min(1.0, (semantic - 0.2) / 0.5))
    else:
        # lexical JD vs resume similarity is naturally low (0.1-0.4); stretch it to 0-1
        sim = min(1.0, cosine(job.jd_text or "", resume_text) / 0.35)

    score = 100 * (0.60 * must_cov + 0.15 * nice_cov + 0.15 * exp_fit + 0.10 * sim)

    strengths, gaps = [], []
    if matched:
        strengths.append(f"Has {len(matched)} of {len(must)} must-have skills")
    if nice_hit:
        strengths.append("Nice-to-have: " + ", ".join(nice_hit))
    if yrs is not None and need and yrs >= need:
        strengths.append(f"About {yrs:g} years of experience mentioned (role needs {need}+)")
    if missing:
        gaps.append("Missing must-have: " + ", ".join(missing))
    if need and (yrs is None or yrs < need):
        gaps.append(f"Experience: role needs {need}+ yrs; resume shows {('~%g' % yrs) if yrs else 'no clear figure'}")

    return {
        "score": round(score, 1),
        "matched": matched + nice_hit,
        "missing": missing,
        "years": yrs,
        "strengths": strengths,
        "gaps": gaps,
    }


def recommendation_for(score):
    if score >= 80:
        return "Strong match"
    if score >= 65:
        return "Good match"
    if score >= 45:
        return "Partial match"
    return "Not a match"
