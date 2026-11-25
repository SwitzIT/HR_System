"""Requisition fields, validation and the text sent to the JD prompt.

This implements Phase 1 (collection) and Phase 2 (validation & clarification) of the Talent
Acquisition JD prompt inside the app, so the model is only asked to write once every mandatory
detail is confirmed. Missing fields are reported in ONE consolidated message; conflicting inputs
are shown back to HR to confirm; "Not applicable" is a separate, explicit choice, never the same
as leaving a field blank.
"""

EMPLOYMENT_TYPES = ["Permanent", "Contract", "Temporary", "Other"]
WORK_MODES = ["On-site", "Hybrid", "Remote"]
SALARY_FREQUENCIES = ["Monthly", "Annual", "Hourly", "Other"]
SALARY_STRUCTURES = {"fixed": "Fixed amount", "range": "Range"}
CURRENCIES = ["AED", "INR", "LKR", "SAR", "QAR", "OMR", "KWD", "BHD", "USD", "EUR", "GBP"]
EXPERIENCE_BASIS = {"total": "Total professional experience", "specific": "Experience in a specific area"}
INDUSTRIES = ["FMCG", "Bakery & Food Manufacturing", "Food & Beverage", "Retail", "Distribution & Logistics",
              "Hospitality", "Manufacturing", "Information Technology", "Financial Services", "Real Estate"]

TEXT_FIELDS = [
    "title", "department", "industry", "city", "country", "employment_type", "employment_type_other",
    "work_mode", "salary_structure", "salary_currency", "salary_frequency", "salary_frequency_other",
    "experience_basis", "experience_area", "skills", "nice_to_have", "education", "certifications",
    "preferred_qualifications", "preferred_industry_exp", "reporting_to", "team_management",
    "key_stakeholders", "business_need", "additional_expectations", "tone",
]
NUM_FIELDS = ["salary_min", "salary_max"]
INT_FIELDS = ["experience_min", "experience_max", "openings", "shortlist_threshold"]
BOOL_FIELDS = ["education_na", "certifications_na"]
FORM_COLS = TEXT_FIELDS + NUM_FIELDS + INT_FIELDS + BOOL_FIELDS


def form_values(job):
    v = {c: getattr(job, c, None) for c in FORM_COLS}
    # older jobs only had a single free-text location
    if not v.get("city") and not v.get("country") and job.location:
        v["city"] = job.location
    return v


def _num(s):
    s = ("" if s is None else str(s)).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return "invalid"


def _int(s):
    s = ("" if s is None else str(s)).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return "invalid"


def _skills(s):
    return ", ".join(dict.fromkeys(x.strip() for x in (s or "").split(",") if x.strip()))


def check(form):
    """Validate a requisition. Returns a list of issues, in form order:
    (field_id, label, message, kind) where kind is "missing" or "problem" and field_id is the
    form input's id, so the page can highlight it and link to it."""
    g = lambda k: str(form.get(k) or "").strip()
    out = []
    miss = lambda fid, label, msg="Required": out.append((fid, label, msg, "missing"))
    bad = lambda fid, label, msg: out.append((fid, label, msg, "problem"))

    for key, fid, label in [("title", "f-title", "Job title"), ("department", "f-dept", "Department"),
                            ("industry", "f-ind", "Industry / business sector"), ("city", "f-city", "City"),
                            ("country", "f-country", "Country"), ("employment_type", "f-emp", "Employment type")]:
        if not g(key):
            miss(fid, label)
    if g("employment_type") == "Other" and not g("employment_type_other"):
        miss("emp-other", "Employment type: describe 'Other'")

    structure = g("salary_structure")
    lo, hi = _num(form.get("salary_min")), _num(form.get("salary_max"))
    if structure not in SALARY_STRUCTURES:
        miss("f-sstr", "Salary structure", "Choose fixed amount or range")
    elif structure == "fixed":
        if lo is None:
            miss("f-smin", "Salary amount")
        if hi not in (None, "invalid"):
            bad("f-smax", "Salary maximum", "A fixed amount can't have a maximum. Choose 'Range', or clear it.")
    else:
        if lo is None:
            miss("f-smin", "Salary minimum")
        if hi is None:
            miss("f-smax", "Salary maximum")
        if isinstance(lo, float) and isinstance(hi, float) and lo >= hi:
            bad("f-smax", "Salary range", f"Minimum ({lo:,.0f}) must be lower than the maximum ({hi:,.0f}).")
    if lo == "invalid":
        bad("f-smin", "Salary amount", "Enter a number, e.g. 15000")
    elif isinstance(lo, float) and lo <= 0:
        bad("f-smin", "Salary amount", "Must be more than zero")
    if hi == "invalid":
        bad("f-smax", "Salary maximum", "Enter a number, e.g. 18000")
    if not g("salary_currency"):
        miss("f-cur", "Salary currency")
    if g("salary_frequency") not in SALARY_FREQUENCIES:
        miss("f-sfreq", "Salary frequency", "Choose monthly, annual, hourly or other")
    elif g("salary_frequency") == "Other" and not g("salary_frequency_other"):
        miss("sfreq-other", "Salary frequency: describe 'Other'")

    emin, emax = _int(form.get("experience_min")), _int(form.get("experience_max"))
    if emin is None:
        miss("f-emin", "Required years of experience", "Required (enter 0 if none)")
    elif emin == "invalid":
        bad("f-emin", "Required years of experience", "Enter a whole number")
    if emax == "invalid":
        bad("f-emax", "Maximum years", "Enter a whole number")
    if isinstance(emin, int) and isinstance(emax, int) and emax < emin:
        bad("f-emax", "Maximum years", f"Lower than the minimum ({emin})")
    if g("experience_basis") not in EXPERIENCE_BASIS:
        miss("f-ebasis", "Experience refers to", "Choose total experience or a specific area")
    elif g("experience_basis") == "specific" and not g("experience_area"):
        miss("exp-area", "Experience area", "Which area? e.g. Power BI development")

    if not g("skills"):
        miss("f-skills", "Mandatory technical skills / competencies")

    for key, label in [("education", "Educational qualifications"),
                       ("certifications", "Mandatory professional certifications")]:
        na = bool(form.get(f"{key}_na"))
        if na and g(key):
            bad(f"f-{key}", label, "'Not applicable' is ticked but text was also entered. Untick it, or clear the text.")
        elif not na and not g(key):
            miss(f"f-{key}", label, "Required: fill in, or tick 'Not applicable'")
    return out


def validate(form):
    """Return (missing_labels, problem_texts) for messages and callers that only need text."""
    issues = check(form)
    return ([label for _, label, _, kind in issues if kind == "missing"],
            [f"{label}: {msg}" for _, label, msg, kind in issues if kind == "problem"])


def fill(job, form):
    """Copy form values onto a Job (no validation)."""
    for k in TEXT_FIELDS:
        setattr(job, k, (form.get(k) or "").strip())
    for k in NUM_FIELDS:
        n = _num(form.get(k))
        setattr(job, k, n if isinstance(n, float) else None)
    for k in BOOL_FIELDS:
        setattr(job, k, bool(form.get(k)))
    emin, emax, openings, thr = (_int(form.get(k)) for k in INT_FIELDS)
    job.experience_min = emin if isinstance(emin, int) else 0
    job.experience_max = emax if isinstance(emax, int) else None
    job.openings = openings if isinstance(openings, int) and openings > 0 else 1
    if isinstance(thr, int):
        job.shortlist_threshold = max(0, min(100, thr))
    if job.salary_structure == "fixed":
        job.salary_max = None
    if job.education_na:
        job.education = ""
    if job.certifications_na:
        job.certifications = ""
    job.skills, job.nice_to_have = _skills(job.skills), _skills(job.nice_to_have)
    job.location = ", ".join(x for x in (job.city, job.country) if x)
    job.salary_range = salary_text(job)


def _fmt(n):
    return f"{n:,.0f}" if n is not None and float(n).is_integer() else f"{n:,.2f}"


def salary_text(job):
    if job.salary_min is None:
        return ""
    freq = job.salary_frequency_other if job.salary_frequency == "Other" else (job.salary_frequency or "")
    per = {"Monthly": "per month", "Annual": "per year", "Hourly": "per hour"}.get(freq, freq.lower())
    amount = (f"{_fmt(job.salary_min)} – {_fmt(job.salary_max)}" if job.salary_structure == "range" and job.salary_max
              else _fmt(job.salary_min))
    return f"{job.salary_currency} {amount} {per}".strip()


def employment_text(job):
    return job.employment_type_other if job.employment_type == "Other" else (job.employment_type or "")


def experience_text(job):
    lo, hi = job.experience_min or 0, job.experience_max
    span = f"{lo}–{hi} years" if hi and hi > lo else f"{lo}+ years" if lo else "No minimum (0 years)"
    if job.experience_basis == "specific" and job.experience_area:
        return f"{span} of experience in {job.experience_area}"
    return f"{span} of total professional experience"


def has_preferred(job):
    return any((getattr(job, k, "") or "").strip()
               for k in ("nice_to_have", "preferred_qualifications", "preferred_industry_exp"))


def jd_outline(job):
    """The outline appended to the end of the request. The model writes only the two sections that
    need professional judgement (Job Overview, Key Responsibilities); the app writes the sections
    that must contain confirmed inputs only (Required, Preferred, Position Details)."""
    parts = [
        "## Job Overview",
        "[concise introduction: purpose of the role, primary focus areas, expected contribution to the "
        "department and organisation, relevance to the industry and business context]",
        "",
        "## Key Responsibilities",
        "- [concise, specific, outcome-oriented bullet points]",
    ]
    return ("\n\nOUTPUT FORMAT (mandatory): start directly with \"## Job Overview\" and write exactly these "
            "2 sections, with these headings word for word. Replace each [bracketed note] with the content. "
            "Do not add a title line, other headings or sub-headings. Do not write the Required "
            "Qualifications & Skills, Preferred Qualifications & Skills or Position Details sections: the "
            "application adds them from the confirmed inputs. Stop after Key Responsibilities."
            "\n\n" + "\n".join(parts))


def required_section(job):
    """Required Qualifications & Skills: only what the hiring manager confirmed as mandatory."""
    rows = [experience_text(job)]
    rows += [x for x in job.skill_list]
    if not job.education_na and (job.education or "").strip():
        rows.append(job.education.strip())
    if not job.certifications_na and (job.certifications or "").strip():
        rows.append(job.certifications.strip())
    return "## Required Qualifications & Skills\n" + "\n".join(f"- {r}" for r in rows if r)


def preferred_section(job):
    """Preferred Qualifications & Skills: only items the hiring manager supplied; '' if none."""
    rows = list(job.nice_list)
    if (job.preferred_qualifications or "").strip():
        rows.append(job.preferred_qualifications.strip())
    if (job.preferred_industry_exp or "").strip():
        rows.append(job.preferred_industry_exp.strip())
    return ("## Preferred Qualifications & Skills\n" + "\n".join(f"- {r}" for r in rows)) if rows else ""


def position_details(job):
    """The Position Details section, built from the confirmed requisition (never by the model),
    with the fields listed in the prompt's Phase 3 E."""
    loc = ", ".join(x for x in (job.city, job.country) if x) or (job.location or "")
    rows = [("Job Title", job.title), ("Department", job.department), ("Industry", job.industry),
            ("Location", f"{loc} ({job.work_mode})" if loc and job.work_mode else loc),
            ("Employment Type", employment_text(job)), ("Salary", salary_text(job)),
            ("Experience Required", experience_text(job))]
    return "## Position Details\n" + "\n".join(f"- **{k}:** {v}" for k, v in rows if v)


_APP_SECTIONS = r"(?im)^#{1,6}\s*(required qualifications|preferred qualifications|position details)\b.*$"


def assemble_jd(text, job):
    """Keep the model's Job Overview and Key Responsibilities; drop any Required / Preferred /
    Position Details it wrote anyway, and append the app's own sections built from the requisition."""
    import re
    body = re.split(_APP_SECTIONS, text or "", maxsplit=1)[0].rstrip()
    parts = [body, required_section(job), preferred_section(job), position_details(job)]
    return "\n\n".join(p for p in parts if p)


attach_position_details = assemble_jd  # older name


def prompt_requirements(job, company):
    """The hiring-manager input block sent to the model. Optional fields that weren't filled in are
    left out entirely (rather than labelled "Not provided"), and "Not applicable" items are named
    only as items to leave out, so the model has no internal labels to copy into the JD."""
    lines = [
        "MANDATORY POSITION DETAILS",
        f"Company: {company}",
        f"Job title: {job.title}",
        f"Department: {job.department}",
        f"Industry / business sector: {job.industry}",
        f"Job location: {job.city}, {job.country} (work mode: {job.work_mode or 'On-site'})",
        f"Employment type: {employment_text(job)}",
        f"Salary: {salary_text(job)} "
        f"(structure: {SALARY_STRUCTURES.get(job.salary_structure, '')}; currency: {job.salary_currency}; "
        f"frequency: {job.salary_frequency_other if job.salary_frequency == 'Other' else job.salary_frequency})",
        f"Required experience: {experience_text(job)}",
        f"Mandatory technical skills / functional competencies: {job.skills}",
    ]
    if not job.education_na:
        lines.append(f"Educational qualifications: {job.education}")
    if not job.certifications_na:
        lines.append(f"Mandatory professional certifications: {job.certifications}")
    na = [n for n, flag in (("educational qualifications", job.education_na),
                            ("professional certifications", job.certifications_na)) if flag]
    if na:
        lines.append("Leave out of the JD entirely (no requirement): " + " and ".join(na))

    optional = [("Preferred technical skills", job.nice_to_have),
                ("Preferred qualifications", job.preferred_qualifications),
                ("Preferred industry experience", job.preferred_industry_exp),
                ("Reporting to", job.reporting_to),
                ("Team management responsibilities", job.team_management),
                ("Key stakeholders", job.key_stakeholders),
                ("Specific business or project context", job.business_need),
                ("Additional role expectations", job.additional_expectations)]
    given = [f"{k}: {v.strip()}" for k, v in optional if v and v.strip()]
    if (job.openings or 1) > 1:
        given.append(f"Number of openings: {job.openings}")
    if given:
        lines += ["", "OPTIONAL INFORMATION (only these were supplied)"] + given
    return "\n".join(lines)


# Internal labels, notes and placeholders that must never appear in a published JD
# (prompt section 8: no internal notes or placeholders such as TBD, N/A or Insert Details).
LEAK_PATTERNS = [
    (r"confirmed by (the )?hiring manager", "internal note"),
    (r"\bnot provided\b", "placeholder"),
    (r"\bnot applicable\b", "placeholder"),
    (r"\bN/A\b", "placeholder"),
    (r"\bTBD\b|\bTBC\b", "placeholder"),
    (r"insert (details|here)|\[insert", "placeholder"),
    (r"\[(concise|only the )[^\]]*\]", "outline note left in"),
    (r"leave out of the jd|optional information|mandatory position details", "internal label"),
]


# Technologies that often get added by a model without being asked for. Any of these in the draft
# that the hiring manager didn't mention anywhere in the requisition is flagged for review.
COMMON_TECH = ["TensorFlow", "PyTorch", "Keras", "Scikit-Learn", "scikit-learn", "Pandas", "NumPy", "Matplotlib",
               "Hugging Face", "LangChain", "OpenAI", "GPT", "DALL-E", "LLM", "NLP", "Computer Vision",
               "AWS", "Azure", "Google Cloud", "GCP", "Kubernetes", "Docker", "Terraform", "Spark", "Hadoop",
               "Databricks", "Snowflake", "Airflow", "Kafka", "MLflow", "Tableau", "Power BI", "Qlik", "Looker",
               "SAP", "Oracle", "Salesforce", "Dynamics 365", "D365", "Microsoft Fabric", "SQL", "NoSQL", "MongoDB",
               "PostgreSQL", "MySQL", "Java", "JavaScript", "TypeScript", "C++", "C#", ".NET", "React", "Angular",
               "Node.js", "Excel", "VBA", "R ", "Scala", "Go ", "Jira", "Git", "CI/CD", "A/B testing", "Six Sigma",
               "PMP", "ITIL", "Agile", "Scrum"]


def _requisition_text(job):
    keys = ("title", "department", "industry", "skills", "nice_to_have", "education", "certifications",
            "preferred_qualifications", "preferred_industry_exp", "reporting_to", "team_management",
            "key_stakeholders", "business_need", "additional_expectations", "experience_area")
    return " ".join(str(getattr(job, k, "") or "") for k in keys).lower()


def draft_flags(text, job=None):
    """Lines of a JD draft that need a human look before approval: [(line, reason), ...].
    Always: internal notes and placeholders. With the job: things the requisition doesn't support,
    e.g. a reporting line, leadership duties, technologies or numeric targets nobody asked for."""
    import re
    found = {}  # line -> [reasons], in draft order

    def add(line, why):
        reasons = found.setdefault(line.strip(), [])
        if why not in reasons:
            reasons.append(why)

    req = _requisition_text(job) if job is not None else ""
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        for pat, why in LEAK_PATTERNS:
            if re.search(pat, line, re.I if pat != r"\bN/A\b" else 0):
                add(line, why)
                break
        if job is None:
            continue
        if not (job.reporting_to or "").strip() and re.search(r"\breport(s|ing)? (directly )?(in)?to\b", line, re.I):
            add(line, "reporting line not in the requisition")
        if not (job.team_management or "").strip() and re.search(
                r"\b(lead|leads|leading|manage[sd]?|managing|mentor(s|ing)?|supervis\w*|head(s|ing)?)\b"
                r"(?! to\b)", line, re.I):
            add(line, "leadership / team duties not in the requisition")
        for tech in COMMON_TECH:
            t = tech.strip()
            if re.search(r"(?<![\w.+#])" + re.escape(t) + r"(?![\w+#])", line) and t.lower() not in req:
                add(line, f"'{t}' isn't in the requisition")
                break
        for num in re.findall(r"\b\d+(?:\.\d+)?\s?%", line):
            if num.replace(" ", "") not in req.replace(" ", ""):
                add(line, "numeric target not in the requisition")
                break
    return [(line, "; ".join(reasons)) for line, reasons in found.items()]
