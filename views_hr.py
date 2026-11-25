import csv
import io
import re
from datetime import datetime, timedelta

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user
from sqlalchemy import func

import ai
import requisition
from job_boards import board_text
from extensions import admin_required, hr_required
from models import ActivityLog, Application, ApplicationEvent, Job, JDRevision, JobShare, User, db, log, now
import screening_queue
from services import (apply_threshold, email_configured, post_status, record_event, render_md, save_revision,
                      send_emails)

bp = Blueprint("hr", __name__)

def _vals(job):
    return requisition.form_values(job)


def _fill_job(job, form):
    requisition.fill(job, form)


def _check_requisition(form):
    """Issues found in the requisition (empty list = ready for the AI)."""
    return requisition.check(form)


def _generate(job, instructions=None, current_text=None):
    # Save everything first so the database isn't locked while the model writes (can take minutes).
    db.session.commit()
    text, source, err = ai.generate_jd(job, current_app.config["COMPANY_NAME"], instructions, current_text)
    if source == "not_a_jd":
        # Keep the existing draft; show HR what the model said (usually clarification questions).
        flash(err + " Check the requisition (Edit requisition) and try again.", "error")
        return
    job.jd_text, job.jd_source = text, source
    job.last_edited_by_id = current_user.id
    if job.status == "approved":
        job.status = "pending_review"
    save_revision(job, text, source, current_user, note=instructions)
    if err:
        flash("AI generation failed, so a template draft was created instead. "
              f"Details: {err[:200]}", "error")
    elif source == "template":
        flash("Draft created from the built-in template (no AI key configured). Review and edit it below.", "info")
    else:
        flash("AI draft ready. Please review and edit before approving.", "success")


# --------------------------------------------------------------------------- dashboard

@bp.route("/")
@hr_required
def dashboard():
    since = now() - timedelta(days=30)
    stats = {
        "open": Job.query.filter_by(status="approved").count(),
        "review": Job.query.filter(Job.status.in_(["draft", "pending_review"])).count(),
        "apps30": Application.query.filter(Application.applied_at >= since).count(),
        "shortlisted": Application.query.filter(Application.status.in_(["shortlisted", "interview"])).count(),
    }
    to_review = Job.query.filter(Job.status.in_(["draft", "pending_review"])).order_by(Job.updated_at.desc()).limit(6).all()
    top = (Application.query.join(Job).filter(Job.status == "approved", Application.status.in_(["applied", "shortlisted"]))
           .order_by(Application.score_final.desc()).limit(8).all())
    activity = (ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(12).all()
                if current_user.is_admin else [])
    jobs = Job.query.filter_by(status="approved").order_by(Job.approved_at.desc()).all()
    counts = dict(db.session.query(Application.job_id, func.count()).group_by(Application.job_id).all())
    unposted = dict(db.session.query(Application.job_id, func.count())
                    .filter(Application.status != "withdrawn",
                            func.coalesce(Application.posted_status, "applied") != Application.status)
                    .group_by(Application.job_id).all())
    in_queue = Application.query.filter(Application.screening_state.in_(("queued", "running"))).count()
    return render_template("hr/dashboard.html", stats=stats, to_review=to_review, top=top,
                           activity=activity, jobs=jobs, counts=counts, unposted=unposted, in_queue=in_queue,
                           unposted_jobs=Job.query.filter(Job.id.in_(list(unposted))).all() if unposted else [])


# --------------------------------------------------------------------------- jobs / JD

@bp.route("/jobs")
@hr_required
def jobs():
    status = request.args.get("status", "")
    q = Job.query
    if status:
        q = q.filter_by(status=status)
    jobs = q.order_by(Job.updated_at.desc()).all()
    counts = dict(db.session.query(Application.job_id, func.count()).group_by(Application.job_id).all())
    short = dict(db.session.query(Application.job_id, func.count())
                 .filter(Application.status.in_(["shortlisted", "interview", "offered", "hired"]))
                 .group_by(Application.job_id).all())
    return render_template("hr/jobs.html", jobs=jobs, status=status, counts=counts, short=short)


@bp.route("/jobs/new", methods=["GET", "POST"])
@hr_required
def job_new():
    if request.method == "POST":
        action = request.form.get("action")
        if not request.form.get("title", "").strip():
            flash("Enter at least a job title to save the requisition.", "error")
            return render_template("hr/job_form.html", job=None, v=request.form.to_dict(), opts=requisition)
        issues = [] if action == "manual" else _check_requisition(request.form)
        if issues:
            return render_template("hr/job_form.html", job=None, v=request.form.to_dict(), opts=requisition,
                                   issues=issues)
        job = Job(created_by_id=current_user.id, status="draft",
                  shortlist_threshold=current_app.config["DEFAULT_SHORTLIST_THRESHOLD"])
        _fill_job(job, request.form)
        db.session.add(job)
        db.session.flush()
        log(f"Created requisition '{job.title}'", current_user, job)
        if action == "manual":
            job.jd_source = "manual"
            flash("Requisition saved. Write the JD below.", "success")
        else:
            _generate(job)
        db.session.commit()
        return redirect(url_for("hr.job_review", job_id=job.id))
    return render_template("hr/job_form.html", job=None, opts=requisition,
                           v={"shortlist_threshold": current_app.config["DEFAULT_SHORTLIST_THRESHOLD"], "openings": 1})


@bp.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@hr_required
def job_edit(job_id):
    job = db.get_or_404(Job, job_id)
    if request.method == "POST":
        action = request.form.get("action")
        issues = _check_requisition(request.form) if action == "regenerate" else []
        if issues:
            return render_template("hr/job_form.html", job=job, v=request.form.to_dict(), opts=requisition,
                                   issues=issues)
        _fill_job(job, request.form)
        job.last_edited_by_id = current_user.id
        log(f"Edited requisition '{job.title}'", current_user, job)
        if action == "regenerate":
            _generate(job)
        else:
            if _check_requisition(request.form):
                flash("Requisition saved, but it isn't complete yet, so the AI can't write the JD. "
                      "Open Edit requisition to see the highlighted fields.", "info")
            else:
                flash("Requisition updated.", "success")
        db.session.commit()
        return redirect(url_for("hr.job_review", job_id=job.id))
    issues = _check_requisition(_vals(job)) if request.args.get("check") else []
    return render_template("hr/job_form.html", job=job, v=_vals(job), opts=requisition, issues=issues)


@bp.route("/jobs/<int:job_id>", methods=["GET"])
@hr_required
def job_review(job_id):
    job = db.get_or_404(Job, job_id)
    revisions = job.revisions.limit(15).all()
    n_apps = job.applications.count()
    return render_template("hr/job_review.html", job=job, revisions=revisions, n_apps=n_apps,
                           flags=requisition.draft_flags(job.jd_text, job),
                           maker_checker=current_app.config["REQUIRE_DIFFERENT_APPROVER"])


@bp.route("/jobs/<int:job_id>/action", methods=["POST"])
@hr_required
def job_action(job_id):
    job = db.get_or_404(Job, job_id)
    action = request.form.get("action")
    text = request.form.get("jd_text")

    if text is not None and text.strip() != (job.jd_text or "").strip() and action in ("save", "submit", "approve"):
        job.jd_text = text.strip()
        job.last_edited_by_id = current_user.id
        save_revision(job, job.jd_text, "hr_edit", current_user)
        if job.status == "approved":
            job.status = "pending_review"
            flash("The published JD was changed, so it's back in review until re-approved.", "info")
        log(f"Edited JD for '{job.title}'", current_user, job)

    if action == "save":
        flash("Draft saved.", "success")

    elif action == "regenerate":
        if _check_requisition(requisition.form_values(job)):
            db.session.commit()
            return redirect(url_for("hr.job_edit", job_id=job.id, check=1))
        _generate(job, instructions=request.form.get("instructions", "").strip() or None,
                  current_text=job.jd_text)
        log(f"Regenerated JD for '{job.title}'", current_user, job)

    elif action == "submit":
        if not (job.jd_text or "").strip():
            flash("Write or generate a JD first.", "error")
        else:
            job.status = "pending_review"
            log(f"Submitted '{job.title}' for review", current_user, job)
            flash("Submitted for review.", "success")

    elif action == "approve":
        if not (job.jd_text or "").strip():
            flash("The JD is empty.", "error")
        elif not request.form.get("confirm_review"):
            flash("Tick the confirmation that you've reviewed the JD before approving.", "error")
        elif current_app.config["REQUIRE_DIFFERENT_APPROVER"] and current_user.id in (job.last_edited_by_id, job.created_by_id):
            flash("Maker-checker is on: a different HR user must approve this JD.", "error")
        else:
            job.status = "approved"
            job.approved_by_id = current_user.id
            job.approved_at = now()
            job.review_notes = request.form.get("review_notes", "").strip() or job.review_notes
            job.visibility = "internal" if request.form.get("visibility") == "internal" else "public"
            job.ensure_share_token()
            where = "for internal staff only" if job.is_internal_only else "on the careers site"
            log(f"Approved & published '{job.title}' {where}", current_user, job)
            flash(f"JD approved and published {where}. Next: share the application link with staff.", "success")
            db.session.commit()
            return redirect(url_for("hr.share_job", job_id=job.id))

    elif action == "send_back":
        job.status = "draft"
        job.review_notes = request.form.get("review_notes", "").strip()
        log(f"Sent '{job.title}' back to draft", current_user, job)
        flash("Sent back to draft with your notes.", "info")

    elif action == "close":
        job.status = "closed"
        log(f"Closed '{job.title}'", current_user, job)
        flash("Job closed. It no longer accepts applications.", "info")

    elif action == "reopen":
        job.status = "pending_review"
        log(f"Reopened '{job.title}' for review", current_user, job)
        flash("Reopened. Approve it again to publish.", "info")

    elif action == "threshold":
        try:
            job.shortlist_threshold = max(0, min(100, int(request.form.get("shortlist_threshold"))))
        except (TypeError, ValueError):
            flash("Threshold must be a number 0–100.", "error")
            return redirect(request.referrer or url_for("hr.job_review", job_id=job.id))
        for a in job.applications:
            apply_threshold(a, job.shortlist_threshold)
        log(f"Set shortlist threshold to {job.shortlist_threshold}% on '{job.title}'", current_user, job)
        flash(f"Shortlist threshold set to {job.shortlist_threshold}%. Automatic shortlists updated.", "success")
        db.session.commit()
        return redirect(request.referrer or url_for("hr.job_review", job_id=job.id))

    elif action == "delete":
        if job.applications.count():
            flash("This job has applications. Close it instead of deleting.", "error")
        else:
            db.session.delete(job)
            db.session.commit()
            flash("Requisition deleted.", "info")
            return redirect(url_for("hr.jobs"))

    db.session.commit()
    return redirect(url_for("hr.job_review", job_id=job.id))


@bp.route("/jobs/<int:job_id>/revision/<int:rev_id>/restore", methods=["POST"])
@hr_required
def restore_revision(job_id, rev_id):
    job = db.get_or_404(Job, job_id)
    rev = db.get_or_404(JDRevision, rev_id)
    if rev.job_id != job.id:
        abort(404)
    job.jd_text = rev.text
    job.last_edited_by_id = current_user.id
    if job.status == "approved":
        job.status = "pending_review"
    save_revision(job, rev.text, "hr_edit", current_user, note=f"Restored version from {rev.created_at:%d %b %H:%M}")
    db.session.commit()
    flash("Earlier version restored.", "success")
    return redirect(url_for("hr.job_review", job_id=job.id))


@bp.route("/preview", methods=["POST"])
@hr_required
def preview_md():
    return render_md(request.form.get("text", ""))


# --------------------------------------------------------------------------- ATS pipeline

@bp.route("/jobs/<int:job_id>/candidates")
@hr_required
def pipeline(job_id):
    job = db.get_or_404(Job, job_id)
    status = request.args.get("status", "")
    try:
        min_score = int(request.args.get("min", "0") or 0)
    except ValueError:
        min_score = 0
    source = request.args.get("source", "")
    posting = request.args.get("posting", "")
    q = job.applications
    if status:
        q = q.filter(Application.status == status)
    if posting == "unposted":
        q = q.filter(Application.status != "withdrawn",
                     db.func.coalesce(Application.posted_status, "applied") != Application.status)
    if source:
        q = q.filter(Application.source == source)
    if min_score:
        q = q.filter(Application.score_final >= min_score)
    apps = q.order_by(Application.score_final.desc().nullslast(), Application.applied_at).all()
    by_status = dict(db.session.query(Application.status, func.count())
                     .filter(Application.job_id == job.id).group_by(Application.status).all())
    n_unposted = sum(1 for a in job.applications if a.needs_posting)
    n_screening = job.applications.filter(Application.screening_state.in_(("queued", "running"))).count()
    return render_template("hr/pipeline.html", job=job, apps=apps, status=status, min_score=min_score,
                           by_status=by_status, statuses=Application.STATUSES + ["withdrawn"], source=source,
                           posting=posting, n_unposted=n_unposted, n_screening=n_screening)


@bp.route("/jobs/<int:job_id>/post", methods=["POST"])
@hr_required
def post_statuses(job_id):
    """Post the selected candidates' current statuses so they can see them."""
    job = db.get_or_404(Job, job_id)
    ids = {int(x) for x in request.form.getlist("app_ids") if x.isdigit()}
    n = 0
    for a in job.applications.filter(Application.id.in_(ids)):
        if post_status(a, current_user):
            n += 1
    if n:
        log(f"Posted status to {n} candidate(s) for '{job.title}'", current_user, job)
    db.session.commit()
    flash(f"Posted to {n} candidate(s). They can now see their status." if n
          else "Nothing to post: the selected candidates already see their current status.", "success" if n else "info")
    nxt = request.form.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("hr.pipeline", job_id=job.id))


@bp.route("/jobs/<int:job_id>/rescreen", methods=["POST"])
@hr_required
def rescreen_all(job_id):
    job = db.get_or_404(Job, job_id)
    n = 0
    for a in job.applications:
        screening_queue.enqueue(a)
        n += 1
    log(f"Queued {n} applications for re-screening against the current JD", current_user, job)
    db.session.commit()
    screening_queue.notify()
    flash(f"{n} application(s) queued for re-screening against the current JD. Scores update here as each "
          "one finishes.", "success")
    return redirect(url_for("hr.pipeline", job_id=job.id))


@bp.route("/jobs/<int:job_id>/export.csv")
@hr_required
def export_csv(job_id):
    job = db.get_or_404(Job, job_id)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Rank", "Candidate", "Email", "Phone", "Final score", "AI score", "Keyword score",
                "Recommendation", "Status", "Candidate sees", "Posted on", "Years exp", "Matched skills", "Missing skills",
                "Notice period", "Current CTC", "Applied on", "Source", "Decided by", "Decided on",
                "Decision reason", "HR notes"])
    apps = job.applications.order_by(Application.score_final.desc().nullslast()).all()
    for i, a in enumerate(apps, 1):
        w.writerow([i, a.candidate.name, a.candidate.email, a.candidate.phone or "",
                    a.score_final, a.score_ai if a.score_ai is not None else "", a.score_keyword,
                    a.recommendation, a.status, a.candidate_status,
                    a.posted_at.strftime("%Y-%m-%d %H:%M") if a.posted_at else "", a.years_experience or "",
                    "; ".join(a.matched_skills), "; ".join(a.missing_skills),
                    a.notice_period or "", a.current_ctc or "", a.applied_at.strftime("%Y-%m-%d"),
                    a.source or "external", a.decided_by_name, a.decided_at.strftime("%Y-%m-%d %H:%M") if a.decided_at else "",
                    a.decision_reason or "", a.hr_notes or ""])
    fname = "".join(c if c.isalnum() else "_" for c in job.title) + "_candidates.csv"
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@bp.route("/applications/<int:app_id>")
@hr_required
def application(app_id):
    a = db.get_or_404(Application, app_id)
    ranked = [x.id for x in a.job.applications.order_by(Application.score_final.desc().nullslast(),
                                                       Application.applied_at).all()]
    i = ranked.index(a.id)
    prev_id = ranked[i - 1] if i > 0 else None
    next_id = ranked[i + 1] if i < len(ranked) - 1 else None
    events = a.events.all()
    others = (Application.query.filter(Application.candidate_id == a.candidate_id, Application.id != a.id)
              .order_by(Application.applied_at.desc()).all())
    return render_template("hr/application.html", a=a, job=a.job, rank=i + 1, total=len(ranked),
                           prev_id=prev_id, next_id=next_id, statuses=Application.STATUSES, events=events,
                           others=others, reasons=Application.REJECTION_REASONS,
                           preset_status=request.args.get("status", ""))


@bp.route("/applications/<int:app_id>/update", methods=["POST"])
@hr_required
def application_update(app_id):
    a = db.get_or_404(Application, app_id)
    action = request.form.get("action", "status")
    if action == "rescreen":
        screening_queue.enqueue(a)
        log("Queued application for re-screening", current_user, a.job, a)
        db.session.commit()
        screening_queue.notify()
        flash("Queued for re-screening against the current JD. The score updates when it finishes.", "success")
    elif action == "post":
        if post_status(a, current_user, note=request.form.get("decision_note", "").strip() or None):
            log(f"Posted status '{a.candidate_status}' to {a.candidate.name}", current_user, a.job, a)
            flash(f"Posted. {a.candidate.name} now sees: {a.candidate_status}.", "success")
        else:
            flash("The candidate already sees the current status.", "info")
    else:
        new = request.form.get("status")
        reason = request.form.get("reason", "").strip()
        other = request.form.get("reason_other", "").strip()
        if reason == "Other":
            reason = f"Other: {other}" if other else ""
        note = request.form.get("decision_note", "").strip()
        if new in Application.STATUSES and new != a.status:
            if new == "rejected" and not reason:
                flash("Choose a reason for rejecting. It's recorded with the decision.", "error")
                return redirect(url_for("hr.application", app_id=a.id, status="rejected") + "#decision")
            if a.status == "withdrawn":
                flash("The candidate withdrew this application, so its stage can't be changed.", "error")
                return redirect(url_for("hr.application", app_id=a.id))
            log(f"Status {a.status} → {new} for {a.candidate.name}", current_user, a.job, a)
            record_event(a, new, actor=current_user, reason=reason or None, note=note or None)
            a.auto_shortlisted = False  # a human decision now owns this status
        if "hr_notes" in request.form:
            a.hr_notes = request.form.get("hr_notes", "").strip()
        if action == "status_post" and a.status != "withdrawn":
            if post_status(a, current_user, note=note or None):
                log(f"Posted status '{a.candidate_status}' to {a.candidate.name}", current_user, a.job, a)
            flash(f"Saved and posted. {a.candidate.name} now sees: {a.candidate_status}.", "success")
        elif a.needs_posting:
            flash("Saved. Not visible to the candidate yet: they still see "
                  f"“{a.candidate_status}” until you post it.", "success")
        else:
            flash("Saved.", "success")
    db.session.commit()
    nxt = request.form.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("hr.application", app_id=a.id))


# --------------------------------------------------------------------------- share with internal staff

EMAIL_SPLIT = re.compile(r"[\s,;]+")
EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _base_url():
    """(base_url, note). APP_BASE_URL wins. Without it, a link built while HR is browsing on
    localhost would only open on this PC, so use this PC's network address instead."""
    from services import lan_address
    if current_app.config["APP_BASE_URL"]:
        return current_app.config["APP_BASE_URL"], ""
    root = request.url_root.rstrip("/")
    host = request.host.rsplit(":", 1)[0].strip("[]")
    if host in ("127.0.0.1", "localhost", "::1"):
        ip = lan_address()
        if ip:
            port = request.host.rsplit(":", 1)[1] if ":" in request.host else ""
            return (f"{request.scheme}://{ip}{':' + port if port else ''}",
                    f"This link uses this PC's network address ({ip}). It works for colleagues on the same "
                    "network while this PC is on and the app is running. Set APP_BASE_URL in .env once the app "
                    "is hosted.")
        return root, "This link only opens on this PC. Set APP_BASE_URL in .env to the address colleagues use."
    return root, ""


def _internal_link(job):
    return _base_url()[0] + url_for("public.job_detail", job_id=job.id, ref=job.ensure_share_token())


@bp.route("/jobs/<int:job_id>/share", methods=["GET", "POST"])
@hr_required
def share_job(job_id):
    job = db.get_or_404(Job, job_id)
    if job.status != "approved":
        flash("Approve the JD before sharing it.", "error")
        return redirect(url_for("hr.job_review", job_id=job.id))
    domains = current_app.config["INTERNAL_EMAIL_DOMAINS"]
    form = request.form
    if request.method == "POST":
        action = form.get("action")
        if action == "visibility":
            job.visibility = "internal" if form.get("visibility") == "internal" else "public"
            log(f"Set '{job.title}' to {'internal only' if job.is_internal_only else 'public + internal'}", current_user, job)
            db.session.commit()
            flash("Posting updated: " + ("only people with the staff link can see and apply."
                  if job.is_internal_only else "listed on the careers site, and the staff link still works."), "success")
            return redirect(url_for("hr.share_job", job_id=job.id))
        if action == "reset_link":
            import secrets
            job.share_token = secrets.token_urlsafe(16)
            log(f"Reset the staff link for '{job.title}'", current_user, job)
            db.session.commit()
            flash("New staff link created. The old link no longer works.", "success")
            return redirect(url_for("hr.share_job", job_id=job.id))
        if action == "send":
            raw = [e.strip().lower() for e in EMAIL_SPLIT.split(form.get("recipients", "")) if e.strip()]
            recipients = list(dict.fromkeys(raw))
            bad = [e for e in recipients if not EMAIL_OK.match(e)]
            outside = [e for e in recipients if EMAIL_OK.match(e) and domains
                       and e.rsplit("@", 1)[1] not in domains]
            if not recipients:
                flash("Add at least one email address.", "error")
            elif bad:
                flash("These don't look like email addresses: " + ", ".join(bad[:10]), "error")
            elif outside:
                flash("Only staff addresses (" + ", ".join("@" + d for d in domains) + ") are allowed: "
                      + ", ".join(outside[:10]), "error")
            elif len(recipients) > 500:
                flash("Send to at most 500 people at a time.", "error")
            elif not email_configured():
                flash("Email isn't set up yet (SMTP settings in .env). Copy the link or use 'Open in your email app'.", "error")
            else:
                link = _internal_link(job)
                company = current_app.config["COMPANY_NAME"]
                subject = form.get("subject", "").strip() or f"Internal opening: {job.title}"
                note = form.get("message", "").strip()
                text = (f"{note}\n\n" if note else "") + (
                    f"{job.title} — {job.department or ''} {('· ' + job.location) if job.location else ''}\n"
                    f"{job.employment_type} · {job.work_mode} · {job.experience_label}\n\n"
                    f"Read the job description and apply here:\n{link}\n\n"
                    f"Know someone suitable? Feel free to forward this link.\n— {company} Talent Acquisition")
                html = render_template("hr/email_share.html", job=job, link=link, note=note, company=company,
                                       brand=current_app.config["BRAND_COLOR"])
                try:
                    sent, failures = send_emails(recipients, subject, text, html)
                except Exception as e:  # connection / login problems
                    sent, failures = 0, {r: str(e)[:200] for r in recipients}
                db.session.add(JobShare(job_id=job.id, sent_by_id=current_user.id, recipients=", ".join(recipients),
                                        message=note, channel="email", sent_ok=sent,
                                        failed="\n".join(f"{k}: {v}" for k, v in failures.items()) or None))
                log(f"Sent '{job.title}' link to {sent} staff", current_user, job)
                db.session.commit()
                if failures:
                    why = next(iter(failures.values()))
                    hint = ""
                    low = why.lower()
                    if any(k in low for k in ("5.7.139", "5.7.3", "basic authentication", "smtpclientauthentication")):
                        where = "for your whole organisation" if "tenant" in low else "for this mailbox"
                        hint = (f" Microsoft 365 refused the password sign-in because SMTP AUTH is turned off {where}. "
                                "Ask IT to turn it on for the sending mailbox, or switch to EMAIL_PROVIDER=graph "
                                "(see README). Meanwhile, use “Open in your email app”.")
                    flash(f"Sent to {sent} of {len(recipients)}. Failed: " + "; ".join(list(failures)[:5])
                          + f". Reason: {why}.{hint}", "error")
                else:
                    flash(f"Application link sent to {sent} staff member{'s' if sent != 1 else ''}.", "success")
                return redirect(url_for("hr.share_job", job_id=job.id))
    shares = JobShare.query.filter_by(job_id=job.id).order_by(JobShare.created_at.desc()).all()
    internal_apps = job.applications.filter(Application.source == "internal").count()
    return render_template("hr/share.html", job=job, link=_internal_link(job), link_note=_base_url()[1], shares=shares, form=form,
                           board_text=board_text(job, current_app.config["COMPANY_NAME"],
                                                 _base_url()[0] + url_for("public.job_detail", job_id=job.id)),
                           domains=domains, email_on=email_configured(), internal_apps=internal_apps)


# --------------------------------------------------------------------------- decision log

def _decision_query():
    """ApplicationEvent query filtered by the request's query string."""
    q = ApplicationEvent.query.filter(ApplicationEvent.from_status.isnot(None))  # decisions, not submissions
    job_id = request.args.get("job", type=int)
    outcome = request.args.get("outcome", "")
    who = request.args.get("by", "")
    dfrom, dto = request.args.get("from", ""), request.args.get("to", "")
    if job_id:
        q = q.filter(ApplicationEvent.job_id == job_id)
    if outcome == "shortlisted":
        q = q.filter(ApplicationEvent.to_status.in_(Application.POSITIVE))
    elif outcome == "not_shortlisted":
        q = q.filter(ApplicationEvent.to_status.in_(Application.NEGATIVE))
    if who == "system":
        q = q.filter(ApplicationEvent.actor_id.is_(None))
    elif who == "candidate":
        q = q.join(User, ApplicationEvent.actor_id == User.id).filter(User.role == "candidate")
    elif who.isdigit():
        q = q.filter(ApplicationEvent.actor_id == int(who))
    try:
        if dfrom:
            q = q.filter(ApplicationEvent.created_at >= datetime.strptime(dfrom, "%Y-%m-%d"))
        if dto:
            q = q.filter(ApplicationEvent.created_at < datetime.strptime(dto, "%Y-%m-%d") + timedelta(days=1))
    except ValueError:
        flash("Dates must be in YYYY-MM-DD format.", "error")
    return q.order_by(ApplicationEvent.created_at.desc(), ApplicationEvent.id.desc())


@bp.route("/decisions")
@admin_required
def decisions():
    q = _decision_query()
    events = q.limit(500).all()
    total = q.count()
    # summary: who made how many positive / negative decisions (current filter)
    summary = {}
    for e in q:
        row = summary.setdefault(e.actor_name, {"shortlisted": 0, "not shortlisted": 0, "back to review": 0})
        row[e.outcome] = row.get(e.outcome, 0) + 1
    jobs = Job.query.order_by(Job.title).all()
    hr_users = User.query.filter_by(role="hr").order_by(User.name).all()
    return render_template("hr/decisions.html", events=events, total=total, summary=summary, jobs=jobs,
                           hr_users=hr_users, args=request.args)


@bp.route("/decisions.csv")
@admin_required
def decisions_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Candidate", "Candidate email", "Candidate ID (masked)", "Job", "From", "To",
                "Outcome", "Decided by", "Reason", "Note", "Score at decision", "Source"])
    for e in _decision_query():
        a = e.application
        w.writerow([e.created_at.strftime("%Y-%m-%d %H:%M"), e.candidate.name if e.candidate else "",
                    e.candidate.email if e.candidate else "", e.candidate.id_masked if e.candidate else "",
                    e.job.title if e.job else "", e.from_status or "", e.to_status, e.outcome, e.actor_name,
                    e.reason or "", e.note or "", "" if e.score is None else round(e.score),
                    (a.source if a else "") or "external"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=decision_log.csv"})


# --------------------------------------------------------------------------- team

@bp.route("/team", methods=["GET", "POST"])
@admin_required
def team():
    if request.method == "POST":
        if not current_user.is_admin:
            abort(403)
        f = request.form
        if f.get("action") == "toggle":
            u = db.get_or_404(User, int(f.get("user_id")))
            if u.id == current_user.id:
                flash("You can't deactivate yourself.", "error")
            else:
                u.active = not u.active
                db.session.commit()
                flash(f"{u.name} {'activated' if u.active else 'deactivated'}.", "success")
            return redirect(url_for("hr.team"))
        email = f.get("email", "").strip().lower()
        if not f.get("name") or "@" not in email or len(f.get("password", "")) < 8:
            flash("Name, valid email and an 8+ character temporary password are required.", "error")
        elif User.query.filter_by(email=email).first():
            flash("A user with that email already exists.", "error")
        else:
            u = User(name=f["name"].strip(), email=email, role="hr", is_admin=bool(f.get("is_admin")))
            u.set_password(f["password"])
            db.session.add(u)
            log(f"Added HR user {u.name}", current_user)
            db.session.commit()
            flash(f"HR user {u.name} added. Share the temporary password securely.", "success")
        return redirect(url_for("hr.team"))
    users = User.query.filter_by(role="hr").order_by(User.created_at).all()
    n_candidates = User.query.filter_by(role="candidate").count()
    return render_template("hr/team.html", users=users, n_candidates=n_candidates, ai_status=ai.status())
