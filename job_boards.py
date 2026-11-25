"""Getting approved jobs onto external job sites.

1. Google for Jobs: public job pages carry JobPosting structured data (JSON-LD), following
   https://developers.google.com/search/docs/data-types/job-posting . Google only reads it once the
   careers site is hosted on a public address. Closed jobs return 404, which removes them.
2. "Copy for job board": a plain-text version of the JD plus the public apply link, to paste into
   LinkedIn, Naukri, Indeed, Bayt, etc.
"""
import json
import re

from markupsafe import Markup

# Google's employmentType values are case-sensitive. "Permanent" says nothing about full/part time,
# so it is left out rather than guessed.
_EMPLOYMENT = {"Contract": "CONTRACTOR", "Temporary": "TEMPORARY"}
_UNIT = {"Monthly": "MONTH", "Annual": "YEAR", "Hourly": "HOUR"}


def job_posting_jsonld(job, company, url, description_html):
    data = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": job.title,
        "description": str(description_html),
        "datePosted": (job.approved_at or job.created_at).date().isoformat(),
        "hiringOrganization": {"@type": "Organization", "name": company},
        "identifier": {"@type": "PropertyValue", "name": company, "value": str(job.id)},
        "url": url,
    }
    country = getattr(job, "country", "") or ""
    if country:
        addr = {"@type": "PostalAddress", "addressCountry": country}
        if getattr(job, "city", ""):
            addr["addressLocality"] = job.city
        data["jobLocation"] = {"@type": "Place", "address": addr}
    if job.work_mode == "Remote":
        data["jobLocationType"] = "TELECOMMUTE"
        if country:
            data["applicantLocationRequirements"] = {"@type": "Country", "name": country}
    if job.employment_type in _EMPLOYMENT:
        data["employmentType"] = _EMPLOYMENT[job.employment_type]
    unit = _UNIT.get(getattr(job, "salary_frequency", "") or "")
    if unit and job.salary_min is not None and job.salary_currency:
        value = {"@type": "QuantitativeValue", "unitText": unit}
        if job.salary_structure == "range" and job.salary_max:
            value.update(minValue=job.salary_min, maxValue=job.salary_max)
        else:
            value["value"] = job.salary_min
        data["baseSalary"] = {"@type": "MonetaryAmount", "currency": job.salary_currency, "value": value}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return Markup(f'<script type="application/ld+json">{payload}</script>')


def plain_text(markdown_text):
    """Markdown JD -> clean plain text for job boards that don't accept formatting."""
    out = []
    for line in (markdown_text or "").splitlines():
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"^\s*[-*]\s+", "• ", line)
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def board_text(job, company, apply_url):
    return (f"{job.title} | {company}\n{job.location or ''}\n\n" + plain_text(job.jd_text)
            + f"\n\nApply here: {apply_url}\n")
