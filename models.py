import json
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(40))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="candidate")  # hr | candidate
    is_admin = db.Column(db.Boolean, default=False)  # HR admin can manage HR team
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)

    # --- candidate identity (PAN or passport). Only a keyed hash + last 4 characters are stored. ---
    id_type = db.Column(db.String(20))  # pan | passport
    id_country = db.Column(db.String(60))  # issuing country (passport); "IN" for PAN
    id_hash = db.Column(db.String(64))  # HMAC-SHA256, unique (see __table_args__)
    id_last4 = db.Column(db.String(8))
    id_consent_at = db.Column(db.DateTime)

    __table_args__ = (db.Index("ix_user_id_hash", "id_hash", unique=True),)

    applications = db.relationship("Application", back_populates="candidate", lazy="dynamic",
                                   foreign_keys="Application.candidate_id")

    @property
    def has_identity(self):
        return bool(self.id_hash)

    @property
    def id_masked(self):
        if not self.id_hash:
            return "Not provided"
        label = "PAN" if self.id_type == "pan" else f"Passport ({self.id_country})"
        return f"{label} ••••{self.id_last4}"

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_hr(self):
        return self.role == "hr"

    @property
    def is_active(self):
        return bool(self.active)


class Job(db.Model):
    STATUSES = ["draft", "pending_review", "approved", "closed"]

    id = db.Column(db.Integer, primary_key=True)
    # --- requisition inputs (business requirement) ---
    title = db.Column(db.String(160), nullable=False)
    department = db.Column(db.String(120))
    location = db.Column(db.String(120))
    employment_type = db.Column(db.String(40), default="Full-time")
    work_mode = db.Column(db.String(40), default="On-site")
    experience_min = db.Column(db.Integer, default=0)
    experience_max = db.Column(db.Integer)
    skills = db.Column(db.Text, default="")  # comma separated, must-have
    nice_to_have = db.Column(db.Text, default="")  # comma separated
    education = db.Column(db.String(200))
    salary_range = db.Column(db.String(80))
    openings = db.Column(db.Integer, default=1)
    business_need = db.Column(db.Text)  # why the role exists / key outcomes
    tone = db.Column(db.String(40), default="Professional")
    # --- fields required by the TA JD prompt (Phase 1) ---
    industry = db.Column(db.String(120))
    city = db.Column(db.String(80))
    country = db.Column(db.String(80))
    employment_type_other = db.Column(db.String(80))
    salary_structure = db.Column(db.String(10))  # fixed | range
    salary_min = db.Column(db.Float)
    salary_max = db.Column(db.Float)
    salary_currency = db.Column(db.String(10))
    salary_frequency = db.Column(db.String(20))  # Monthly | Annual | Hourly | Other
    salary_frequency_other = db.Column(db.String(60))
    experience_basis = db.Column(db.String(20))  # total | specific
    experience_area = db.Column(db.String(200))
    education_na = db.Column(db.Boolean, default=False)
    certifications = db.Column(db.Text)
    certifications_na = db.Column(db.Boolean, default=False)
    preferred_qualifications = db.Column(db.Text)
    preferred_industry_exp = db.Column(db.Text)
    reporting_to = db.Column(db.String(160))
    team_management = db.Column(db.Text)
    key_stakeholders = db.Column(db.Text)
    additional_expectations = db.Column(db.Text)

    # --- JD + workflow ---
    jd_text = db.Column(db.Text, default="")  # markdown
    jd_source = db.Column(db.String(20))  # ai | template | manual
    status = db.Column(db.String(20), default="draft", index=True)
    shortlist_threshold = db.Column(db.Integer, default=70)
    review_notes = db.Column(db.Text)
    # public: careers site + internal link. internal: only people with the internal link can see/apply.
    visibility = db.Column(db.String(20), default="public")
    share_token = db.Column(db.String(40), default=lambda: secrets.token_urlsafe(16))

    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    last_edited_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    approved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    approved_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    last_edited_by = db.relationship("User", foreign_keys=[last_edited_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])
    applications = db.relationship("Application", back_populates="job", lazy="dynamic",
                                   cascade="all, delete-orphan")
    revisions = db.relationship("JDRevision", back_populates="job", lazy="dynamic",
                                cascade="all, delete-orphan", order_by="JDRevision.created_at.desc()")

    @property
    def skill_list(self):
        return [s.strip() for s in (self.skills or "").split(",") if s.strip()]

    @property
    def nice_list(self):
        return [s.strip() for s in (self.nice_to_have or "").split(",") if s.strip()]

    @property
    def experience_label(self):
        lo, hi = self.experience_min or 0, self.experience_max
        if hi and hi > lo:
            return f"{lo}–{hi} yrs"
        return f"{lo}+ yrs" if lo else "Fresher / any"

    @property
    def employment_label(self):
        return (self.employment_type_other if self.employment_type == "Other" and self.employment_type_other
                else self.employment_type or "")

    @property
    def is_open(self):
        return self.status == "approved"

    @property
    def is_internal_only(self):
        return (self.visibility or "public") == "internal"

    @property
    def is_listed(self):
        """Shown on the public careers site."""
        return self.status == "approved" and not self.is_internal_only

    def ensure_share_token(self):
        if not self.share_token:
            self.share_token = secrets.token_urlsafe(16)
        return self.share_token


class JDRevision(db.Model):
    """Audit trail of every JD version (AI generations and human edits)."""
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(20))  # ai | template | hr_edit
    note = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=now)

    job = db.relationship("Job", back_populates="revisions")
    created_by = db.relationship("User")


class Application(db.Model):
    STATUSES = ["applied", "shortlisted", "interview", "offered", "hired", "rejected"]  # HR can set these
    POSITIVE = ("shortlisted", "interview", "offered", "hired")
    NEGATIVE = ("rejected", "withdrawn")
    PENDING_LABEL = "Pending"  # what candidates see until HR posts a status
    SCREENING_STATES = {"queued": "Waiting for AI screening", "running": "AI screening in progress",
                        "done": "Screened", "failed": "Screening failed"}
    CANDIDATE_LABELS = {
        "applied": "Under review",
        "shortlisted": "Shortlisted",
        "interview": "Interview stage",
        "offered": "Offer extended",
        "hired": "Hired",
        "rejected": "Not selected",
        "withdrawn": "Withdrawn",
    }
    REJECTION_REASONS = [
        "Skills don't match the must-haves",
        "Not enough relevant experience",
        "Salary expectation out of range",
        "Notice period too long",
        "Not successful at interview",
        "Position filled / on hold",
        "Candidate declined",
        "Duplicate or incomplete application",
        "Other",
    ]

    __table_args__ = (db.UniqueConstraint("job_id", "candidate_id", name="uq_job_candidate"),)

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), nullable=False, index=True)
    candidate_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)

    resume_filename = db.Column(db.String(255))  # original name
    resume_path = db.Column(db.String(255))  # stored name
    resume_text = db.Column(db.Text)
    cover_note = db.Column(db.Text)
    current_ctc = db.Column(db.String(60))
    notice_period = db.Column(db.String(60))
    linkedin = db.Column(db.String(255))

    # --- screening ---
    score_ai = db.Column(db.Float)
    score_keyword = db.Column(db.Float)
    score_final = db.Column(db.Float, index=True)
    years_experience = db.Column(db.Float)
    matched_skills_json = db.Column(db.Text, default="[]")
    missing_skills_json = db.Column(db.Text, default="[]")
    strengths_json = db.Column(db.Text, default="[]")
    gaps_json = db.Column(db.Text, default="[]")
    summary = db.Column(db.Text)
    recommendation = db.Column(db.String(40))
    scorer = db.Column(db.String(160))  # e.g. "HF API · Qwen/Qwen2.5-7B-Instruct-1M + keyword + embeddings"
    screening_error = db.Column(db.Text)
    screened_at = db.Column(db.DateTime)
    auto_shortlisted = db.Column(db.Boolean, default=False)
    # Screening runs in the background after the candidate submits: queued -> running -> done | failed
    screening_state = db.Column(db.String(12), default="queued", index=True)

    # What the candidate sees. HR (and the AI) change `status` internally; the candidate only sees a
    # status once HR posts it. Until then they see "Pending".
    posted_status = db.Column(db.String(20))
    posted_at = db.Column(db.DateTime)
    posted_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    status = db.Column(db.String(20), default="applied", index=True)
    hr_notes = db.Column(db.Text)
    source = db.Column(db.String(20), default="external")  # external | internal (applied via staff link)
    # latest decision (full history lives in ApplicationEvent)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    decided_by_label = db.Column(db.String(120))  # "System · auto-shortlist" etc. when not a person
    decided_at = db.Column(db.DateTime)
    decision_reason = db.Column(db.String(255))
    applied_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)

    job = db.relationship("Job", back_populates="applications")
    candidate = db.relationship("User", back_populates="applications", foreign_keys=[candidate_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_id])
    posted_by = db.relationship("User", foreign_keys=[posted_by_id])
    events = db.relationship("ApplicationEvent", back_populates="application", lazy="dynamic",
                             cascade="all, delete-orphan",
                             order_by=lambda: [ApplicationEvent.created_at.desc(), ApplicationEvent.id.desc()])

    @property
    def decided_by_name(self):
        return self.decided_by.name if self.decided_by else (self.decided_by_label or "")

    def _get(self, field):
        try:
            return json.loads(getattr(self, field) or "[]")
        except ValueError:
            return []

    matched_skills = property(lambda self: self._get("matched_skills_json"))
    missing_skills = property(lambda self: self._get("missing_skills_json"))
    strengths = property(lambda self: self._get("strengths_json"))
    gaps = property(lambda self: self._get("gaps_json"))

    @property
    def visible_status(self):
        """The status the candidate may see: their own withdrawal, or what HR last posted, else pending."""
        if self.status == "withdrawn":
            return "withdrawn"
        return self.posted_status or "pending"

    @property
    def candidate_status(self):
        s = self.visible_status
        if s == "pending":
            return self.PENDING_LABEL
        return self.CANDIDATE_LABELS.get(s, s.title())

    @property
    def visible_decided_at(self):
        """When the candidate learned the outcome (used for the re-apply date they're shown)."""
        if self.status == "withdrawn":
            return self.decided_at
        return self.posted_at

    @property
    def needs_posting(self):
        """True when the internal status differs from what the candidate currently sees."""
        return self.status != "withdrawn" and self.status != (self.posted_status or "applied")

    @property
    def screening_label(self):
        return self.SCREENING_STATES.get(self.screening_state or "done", "")


class ApplicationEvent(db.Model):
    """Decision log: one row per status change of an application, with who, when and why."""
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False, index=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), index=True)
    candidate_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    from_status = db.Column(db.String(20))
    to_status = db.Column(db.String(20), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"))  # HR user or candidate; null = system
    actor_label = db.Column(db.String(120))  # shown when actor_id is null, e.g. "System · auto-shortlist"
    reason = db.Column(db.String(255))
    note = db.Column(db.Text)
    score = db.Column(db.Float)  # final score at the time of the decision
    posted = db.Column(db.Boolean, default=False)  # True = HR posted this status to the candidate
    created_at = db.Column(db.DateTime, default=now, index=True)

    application = db.relationship("Application", back_populates="events")
    job = db.relationship("Job")
    candidate = db.relationship("User", foreign_keys=[candidate_id])
    actor = db.relationship("User", foreign_keys=[actor_id])

    @property
    def actor_name(self):
        if self.actor:
            role = "" if self.actor.is_hr else " (candidate)"
            return self.actor.name + role
        return self.actor_label or "System"

    @property
    def outcome(self):
        if self.posted:
            return "posted to candidate"
        if self.to_status in Application.POSITIVE:
            return "shortlisted"
        if self.to_status in Application.NEGATIVE:
            return "not shortlisted"
        return "submitted" if self.to_status == "applied" and not self.from_status else "back to review"


class JobShare(db.Model):
    """Every time HR sends a job's application link to internal staff."""
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), nullable=False, index=True)
    sent_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    recipients = db.Column(db.Text)  # comma separated
    message = db.Column(db.Text)
    channel = db.Column(db.String(20), default="email")  # email
    sent_ok = db.Column(db.Integer, default=0)
    failed = db.Column(db.Text)  # "address: error" lines
    created_at = db.Column(db.DateTime, default=now)

    job = db.relationship("Job")
    sent_by = db.relationship("User")


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"))
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"))
    action = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=now, index=True)

    user = db.relationship("User")
    job = db.relationship("Job")


def log(action, user=None, job=None, application=None):
    db.session.add(ActivityLog(
        action=action,
        user_id=getattr(user, "id", None),
        job_id=getattr(job, "id", None),
        application_id=getattr(application, "id", None),
    ))
