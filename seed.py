"""Demo data: two requisitions and five candidates with generated DOCX resumes.

Run:  flask --app app seed-demo
Candidate demo logins: <firstname>@example.com / Candidate123!
"""
import os
import uuid

import docx
from flask import current_app

from ai import template_jd
from models import Application, Job, User, db, log, now
from resume_parser import extract_text
from identity import apply_to_user
from services import record_event, run_screening, save_revision

CANDIDATES = [
    ("Priya Raman", "priya", """Priya Raman — Senior Data Analyst
Summary: 6 years of experience building BI and reporting for FMCG and retail businesses.
Skills: SQL, Power BI, DAX, Python (pandas), Microsoft Excel, Azure Data Factory, Stakeholder management, Data modelling
Experience:
- Lead Analyst, FreshMart Retail (2021–present): built 30+ Power BI dashboards for sales, margin and distributor performance; automated month-end reporting with SQL and Python, cutting close from 4 days to 1.
- Data Analyst, Oceanic Foods (2018–2021): owned SQL Server data mart for route-level sales; trained 40 sales managers on self-service Power BI.
Education: B.Sc. Statistics"""),
    ("Arjun Mehta", "arjun", """Arjun Mehta — Business Analyst
4 years of experience in sales analytics.
Skills: SQL, Microsoft Excel, Tableau, Stakeholder management, Forecasting
- Business Analyst, Metro Distributors (2021–present): weekly sales and stock reports in Excel and Tableau; SQL queries on ERP data.
- Analyst Intern, CityBank (2020).
Education: MBA (Marketing)"""),
    ("Nimali Perera", "nimali", """Nimali Perera — Data Analyst
5 years experience. Skills: Power BI, DAX, SQL, Python, Dynamics 365, Data modelling, Excel
- Data Analyst, Lanka Bakers (2020–present): D365 sales and inventory reporting in Power BI; star-schema models; Python scripts for data quality checks.
- MIS Executive, Prime Traders (2019–2020)
Education: B.Sc. Computer Science"""),
    ("Rahul Verma", "rahul", """Rahul Verma — Graphic Designer
3 years of experience in branding and social media design.
Skills: Adobe Photoshop, Illustrator, Figma, Canva
- Designer at PixelWorks Studio (2022–present)
Education: B.Des"""),
    ("Fatima Khan", "fatima", """Fatima Khan — Junior Data Analyst
2 years of experience. Skills: Python, SQL, Microsoft Excel, Power BI (learning), Statistics
- Junior Analyst, QuickCart (2023–present): ad-hoc SQL analysis and Excel reports for operations team.
Education: B.Sc. Mathematics"""),
]


def _docx(path, text):
    d = docx.Document()
    lines = text.splitlines()
    d.add_heading(lines[0], level=1)
    for line in lines[1:]:
        d.add_paragraph(line)
    d.save(path)


def seed():
    company = current_app.config["COMPANY_NAME"]
    admin = User.query.filter_by(role="hr").first()
    if Job.query.filter_by(title="Senior Data Analyst (Power BI)").first():
        print("Demo data already present.")
        return

    reviewer = User.query.filter_by(email="recruiter@company.com").first()
    if not reviewer:
        reviewer = User(name="Maya Recruiter", email="recruiter@company.com", role="hr")
        reviewer.set_password("Recruiter123!")
        db.session.add(reviewer)
        db.session.flush()

    job = Job(
        title="Senior Data Analyst (Power BI)", department="Finance & Analytics", location="Colombo, Sri Lanka",
        city="Colombo", country="Sri Lanka", industry="FMCG",
        salary_structure="range", salary_min=350000, salary_max=450000, salary_currency="LKR", salary_frequency="Monthly",
        experience_basis="total", certifications_na=True,
        employment_type="Permanent", work_mode="Hybrid", experience_min=4, experience_max=8,
        skills="SQL, Power BI, DAX, Python, Stakeholder management",
        nice_to_have="Dynamics 365, Azure Data Factory, Data modelling",
        education="Bachelor's in a quantitative field or equivalent experience",
        business_need="Replace manual Excel sales reporting across four subsidiaries with automated Power BI "
                      "dashboards, and cut month-end reporting time from 5 days to 1.",
        openings=1, status="approved", shortlist_threshold=70, jd_source="template",
        created_by_id=admin.id, approved_by_id=admin.id, approved_at=now(),
    )
    db.session.add(job)
    db.session.flush()
    import requisition
    job.salary_range = requisition.salary_text(job)
    job.jd_text = template_jd(job, company)
    save_revision(job, job.jd_text, "template", admin)
    log(f"Approved & published '{job.title}'", admin, job)

    draft = Job(
        title="Talent Acquisition Specialist", department="Human Resources", location="Dubai, United Arab Emirates",
        city="Dubai", country="United Arab Emirates", industry="Bakery & Food Manufacturing",
        salary_structure="fixed", salary_min=12000, salary_currency="AED", salary_frequency="Monthly",
        experience_basis="specific", experience_area="end-to-end recruitment", education_na=True, certifications_na=True,
        employment_type="Permanent", work_mode="On-site", experience_min=3,
        skills="End-to-end recruitment, Sourcing, ATS, Stakeholder management, Interviewing",
        nice_to_have="Employer branding, LinkedIn Recruiter",
        business_need="Scale hiring for new retail outlets: 60+ roles over the next 12 months with time-to-hire under 30 days.",
        status="pending_review", jd_source="template", created_by_id=reviewer.id,
    )
    db.session.add(draft)
    db.session.flush()
    draft.salary_range = requisition.salary_text(draft)
    draft.jd_text = template_jd(draft, company)
    save_revision(draft, draft.jd_text, "template", admin)

    folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    for name, slug, text in CANDIDATES:
        u = User.query.filter_by(email=f"{slug}@example.com").first()
        if not u:
            u = User(name=name, email=f"{slug}@example.com", role="candidate")
            # demo-only PAN-format IDs (stored hashed): DEMPA0001A, DEMPA0002A, ...
            apply_to_user(u, "pan", f"DEMPA{CANDIDATES.index((name, slug, text)) + 1:04d}A")
            u.set_password("Candidate123!")
            db.session.add(u)
            db.session.flush()
        stored = f"{uuid.uuid4().hex}.docx"
        _docx(os.path.join(folder, stored), text)
        a = Application(job_id=job.id, candidate_id=u.id, resume_filename=f"{name.replace(' ', '_')}_CV.docx",
                        resume_path=stored, resume_text=extract_text(os.path.join(folder, stored)),
                        notice_period="30 days")
        db.session.add(a)
        db.session.flush()
        a.candidate = u
        a.job = job
        record_event(a, "applied", actor=u, submitted=True)
        log(f"{name} applied", u, job, a)
        run_screening(a)
        if slug == "rahul":
            record_event(a, "rejected", actor=admin, reason="Skills don't match the must-haves",
                         note="Design background; no SQL or BI experience")
    db.session.commit()
