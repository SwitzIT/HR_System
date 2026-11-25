from flask import Blueprint, abort, render_template, request, session
from flask_login import current_user
from sqlalchemy import or_

from models import Application, Job, db

bp = Blueprint("public", __name__)


def _listed():
    return Job.query.filter(Job.status == "approved",
                            (Job.visibility.is_(None)) | (Job.visibility != "internal"))


@bp.route("/")
def home():
    jobs = _listed().order_by(Job.approved_at.desc()).limit(6).all()
    return render_template("public/home.html", jobs=jobs, total=_listed().count())


@bp.route("/jobs")
def jobs():
    q = request.args.get("q", "").strip()
    dept = request.args.get("dept", "")
    loc = request.args.get("loc", "")
    query = _listed()
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Job.title.ilike(like), Job.skills.ilike(like), Job.jd_text.ilike(like)))
    if dept:
        query = query.filter(Job.department == dept)
    if loc:
        query = query.filter(Job.location == loc)
    open_jobs = _listed().all()
    depts = sorted({j.department for j in open_jobs if j.department})
    locs = sorted({j.location for j in open_jobs if j.location})
    return render_template("public/jobs.html", jobs=query.order_by(Job.approved_at.desc()).all(),
                           q=q, dept=dept, loc=loc, depts=depts, locs=locs)


@bp.route("/jobs/<int:job_id>")
def job_detail(job_id):
    job = db.get_or_404(Job, job_id)
    ref = request.args.get("ref")
    if ref and job.share_token and ref == job.share_token:
        session[f"ref_{job.id}"] = job.share_token  # remembered until the browser session ends
    internal = session.get(f"ref_{job.id}") == job.share_token
    preview = current_user.is_authenticated and current_user.is_hr
    if not preview and (job.status != "approved" or (job.is_internal_only and not internal)):
        abort(404)
    existing = block = None
    if current_user.is_authenticated and not current_user.is_hr:
        existing = Application.query.filter_by(job_id=job.id, candidate_id=current_user.id).first()
        if not existing:
            from services import reapply_block
            block = reapply_block(current_user, job)
    jsonld = None
    if job.status == "approved" and not job.is_internal_only and not preview:
        from flask import current_app, url_for
        from markupsafe import Markup
        from job_boards import job_posting_jsonld
        from services import render_md
        jsonld = job_posting_jsonld(job, current_app.config["COMPANY_NAME"],
                                    url_for("public.job_detail", job_id=job.id, _external=True),
                                    Markup(render_md(job.jd_text)))
    return render_template("public/job_detail.html", job=job, existing=existing, preview=preview,
                           internal=internal, block=block, jsonld=jsonld)
