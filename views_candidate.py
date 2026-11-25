import os

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   send_from_directory, session, url_for)
from flask_login import current_user, login_required

from extensions import candidate_required
from identity import ID_TYPES, IdentityError, apply_to_user
from models import Application, Job, db, log
import screening_queue
from services import allowed_resume, reapply_block, record_event, store_resume

bp = Blueprint("candidate", __name__)


def _safe(target):
    return target if target and target.startswith("/") and not target.startswith("//") else None


def internal_ref_ok(job):
    """True if this browser opened the job through its internal staff link."""
    return session.get(f"ref_{job.id}") == job.share_token


@bp.route("/identity", methods=["GET", "POST"])
@candidate_required
def identity():
    """PAN / passport step, for accounts created before IDs were required."""
    nxt = _safe(request.values.get("next"))
    if current_user.has_identity:
        return redirect(nxt or url_for("candidate.applications"))
    if request.method == "POST":
        if not request.form.get("id_consent"):
            flash("Please tick the consent box to continue.", "error")
        else:
            try:
                apply_to_user(current_user, request.form.get("id_type"), request.form.get("id_number"),
                              request.form.get("id_country"))
                db.session.commit()
                flash("Thanks, your ID is saved.", "success")
                return redirect(nxt or url_for("public.jobs"))
            except IdentityError as e:
                flash(str(e), "error")
    return render_template("candidate/identity.html", id_types=ID_TYPES, form=request.form, next=nxt)


@bp.route("/applications")
@candidate_required
def applications():
    apps = current_user.applications.order_by(Application.applied_at.desc()).all()
    days = current_app.config["REAPPLY_COOLDOWN_DAYS"]
    return render_template("candidate/applications.html", apps=apps, cooldown_days=days)


@bp.route("/apply/<int:job_id>", methods=["GET", "POST"])
@candidate_required
def apply(job_id):
    job = db.get_or_404(Job, job_id)
    if job.status != "approved" or (job.is_internal_only and not internal_ref_ok(job)):
        flash("This position is not accepting applications.", "error")
        return redirect(url_for("public.jobs"))
    if Application.query.filter_by(job_id=job.id, candidate_id=current_user.id).first():
        flash("You've already applied for this role.", "info")
        return redirect(url_for("candidate.applications"))
    block = reapply_block(current_user, job)
    if block:
        flash(f"You can apply again from {block[0]:%d %b %Y}: there's a "
              f"{current_app.config['REAPPLY_COOLDOWN_DAYS']}-day wait after an application is closed.", "info")
        return redirect(url_for("public.job_detail", job_id=job.id))

    if request.method == "POST":
        f = request.files.get("resume")
        if not f or not f.filename:
            flash("Please attach your resume.", "error")
        elif not allowed_resume(f.filename):
            flash("Resume must be a PDF or DOCX file.", "error")
        else:
            stored, original, text = store_resume(f)
            a = Application(
                job_id=job.id, candidate_id=current_user.id,
                resume_filename=original, resume_path=stored, resume_text=text,
                cover_note=request.form.get("cover_note", "").strip()[:3000],
                current_ctc=request.form.get("current_ctc", "").strip()[:60],
                notice_period=request.form.get("notice_period", "").strip()[:60],
                linkedin=request.form.get("linkedin", "").strip()[:255],
                source="internal" if internal_ref_ok(job) else "external",
            )
            if request.form.get("phone") and not current_user.phone:
                current_user.phone = request.form["phone"].strip()[:40]
            db.session.add(a)
            db.session.flush()
            a.job, a.candidate = job, current_user
            record_event(a, "applied", actor=current_user, submitted=True,
                         note="Applied via internal staff link" if a.source == "internal" else None)
            log(f"{current_user.name} applied", current_user, job, a)
            # Screening runs in the background, so submitting is instant. HR reviews the result and
            # posts a status; until then the candidate sees "Pending".
            screening_queue.enqueue(a)
            db.session.commit()
            screening_queue.notify()
            flash("Application submitted! Its status shows as Pending until our Talent team has reviewed it.",
                  "success")
            return redirect(url_for("candidate.applications"))
    return render_template("candidate/apply.html", job=job)


@bp.route("/applications/<int:app_id>/withdraw", methods=["POST"])
@candidate_required
def withdraw(app_id):
    a = db.get_or_404(Application, app_id)
    if a.candidate_id != current_user.id:
        abort(403)
    if a.visible_status in ("offered", "hired"):
        flash("Please contact HR to withdraw at this stage.", "info")
    elif a.visible_status == "rejected":
        flash("This application is already closed.", "info")
    elif a.status == "withdrawn":
        flash("This application is already withdrawn.", "info")
    else:
        record_event(a, "withdrawn", actor=current_user, reason="Withdrawn by candidate")
        log(f"{current_user.name} withdrew application", current_user, a.job, a)
        db.session.commit()
        flash(f"Application withdrawn. You can apply again after "
              f"{current_app.config['REAPPLY_COOLDOWN_DAYS']} days.", "success")
    return redirect(url_for("candidate.applications"))


@bp.route("/resume/<int:app_id>")
@login_required
def resume(app_id):
    """Resume download: HR, or the candidate who owns it."""
    a = db.get_or_404(Application, app_id)
    if not (current_user.is_hr or a.candidate_id == current_user.id):
        abort(403)
    folder = current_app.config["UPLOAD_FOLDER"]
    if not a.resume_path or not os.path.exists(os.path.join(folder, a.resume_path)):
        abort(404)
    return send_from_directory(folder, a.resume_path, as_attachment=request.args.get("dl") == "1",
                               download_name=a.resume_filename)
