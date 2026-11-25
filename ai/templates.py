"""Rules-mode JD: a structured draft built only from the requisition fields (no model).

Follows the same output structure as the JD prompt: Job Overview, Key Responsibilities,
Required Qualifications & Skills, Preferred Qualifications & Skills (only if supplied),
Position Details. Nothing is added that the hiring manager didn't provide.
"""


def template_jd(job, company):
    import requisition

    must = job.skill_list
    nice = job.nice_list
    dept = job.department or "the team"
    industry = (getattr(job, "industry", "") or "").strip()
    context = (job.business_need or "").strip()

    overview = (f"{company} is looking for a {job.title} to join {dept}"
                + (f" in {job.city}, {job.country}" if getattr(job, "city", None) and getattr(job, "country", None)
                   else (f" in {job.location}" if job.location else ""))
                + (f", supporting the business in the {industry} sector" if industry else "") + ".")
    if context:
        overview += " " + context.rstrip(".") + "."

    duties = []
    if context:
        duties.append(f"Deliver the role's core objective: {context.rstrip('.')}")
    duties += [f"Apply {s} to day-to-day work and to improving how {dept} operates" for s in must[:3]]
    if getattr(job, "key_stakeholders", None):
        duties.append(f"Work closely with {job.key_stakeholders}")
    if getattr(job, "reporting_to", None):
        duties.append(f"Report progress and results to the {job.reporting_to}")
    if getattr(job, "team_management", None):
        duties.append(job.team_management.rstrip("."))
    if getattr(job, "additional_expectations", None):
        duties.append(job.additional_expectations.rstrip("."))
    duties.append("Document work clearly and keep improving processes")

    lines = ["## Job Overview", overview, "", "## Key Responsibilities"]
    lines += [f"- {d}" for d in duties]
    if getattr(job, "experience_basis", None):
        return requisition.assemble_jd("\n".join(lines), job)
    # older jobs created before the structured requisition fields
    lines += ["", "## Required Qualifications & Skills", f"- {job.experience_label} of relevant experience"]
    lines += [f"- {s}" for s in must] + ([f"- {job.education}"] if job.education else [])
    if nice:
        lines += ["", "## Preferred Qualifications & Skills"] + [f"- {p}" for p in nice]
    lines += ["", "## Position Details", f"- **Job Title:** {job.title}"]
    for k, v in (("Department", job.department), ("Location", job.location), ("Employment Type", job.employment_type),
                 ("Salary", job.salary_range), ("Experience Required", job.experience_label)):
        if v:
            lines.append(f"- **{k}:** {v}")
    return "\n".join(lines)
