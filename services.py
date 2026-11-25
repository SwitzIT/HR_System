import json
import os
import smtplib
import uuid
from datetime import timedelta
from email.message import EmailMessage

import markdown as md
import nh3
from flask import current_app
from werkzeug.utils import secure_filename

import ai
from models import Application, ApplicationEvent, JDRevision, db, log, now
from resume_parser import extract_text

ALLOWED_TAGS = {"h1", "h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "em", "b", "i",
                "a", "br", "hr", "blockquote", "code", "pre", "table", "thead", "tbody", "tr", "th", "td"}


def render_md(text):
    """Markdown -> sanitised HTML (AI output is never trusted as raw HTML)."""
    html = md.markdown(text or "", extensions=["extra", "sane_lists"])
    return nh3.clean(html, tags=ALLOWED_TAGS, attributes={"a": {"href"}})


def save_revision(job, text, source, user=None, note=None):
    db.session.add(JDRevision(job_id=job.id, text=text, source=source, note=note,
                              created_by_id=getattr(user, "id", None)))


def allowed_resume(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in current_app.config["ALLOWED_RESUME_EXT"]


def store_resume(file_storage):
    """Save upload under a random name. Returns (stored_name, original_name, text)."""
    original = secure_filename(file_storage.filename) or "resume"
    ext = original.rsplit(".", 1)[1].lower()
    stored = f"{uuid.uuid4().hex}.{ext}"
    folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, stored)
    file_storage.save(path)
    return stored, original, extract_text(path)


# --------------------------------------------------------------------------- decision log

def record_event(application, to_status, actor=None, actor_label=None, reason=None, note=None,
                 submitted=False):
    """Change an application's status and write the decision log entry (who, when, why, score).
    submitted=True records the original submission (no previous status)."""
    frm = None if submitted else application.status
    if not submitted and frm == to_status:
        return None
    application.status = to_status
    ev = ApplicationEvent(application_id=application.id, job_id=application.job_id,
                          candidate_id=application.candidate_id, from_status=frm, to_status=to_status,
                          actor_id=getattr(actor, "id", None), actor_label=None if actor else (actor_label or "System"),
                          reason=(reason or None) and reason[:255], note=note or None,
                          score=application.score_final)
    db.session.add(ev)
    if to_status != "applied":
        application.decided_by_id = getattr(actor, "id", None)
        application.decided_by_label = None if actor else (actor_label or "System")
        application.decided_at = now()
        application.decision_reason = (reason or None) and reason[:255]
    return ev


def post_status(application, actor, note=None):
    """Make the current internal status visible to the candidate, and log who posted it."""
    if application.status == "withdrawn" or application.posted_status == application.status:
        return None
    before = application.posted_status or "pending"
    application.posted_status = application.status
    application.posted_at = now()
    application.posted_by_id = getattr(actor, "id", None)
    ev = ApplicationEvent(application_id=application.id, job_id=application.job_id,
                          candidate_id=application.candidate_id, from_status=before,
                          to_status=application.status, actor_id=getattr(actor, "id", None),
                          reason=f"Posted to candidate as '{application.candidate_status}'", note=note or None,
                          score=application.score_final, posted=True)
    db.session.add(ev)
    return ev


# --------------------------------------------------------------------------- re-apply rule

def reapply_block(user, job):
    """If `user` may not apply to `job` yet, return (date_allowed, previous_application); else None.

    Rule: after an application is rejected or withdrawn, the candidate waits REAPPLY_COOLDOWN_DAYS.
    REAPPLY_SCOPE=any_job blocks every job in that period; same_title blocks only the same job title.
    The candidate is identified by their PAN/passport, so a new email address doesn't reset it.
    """
    days = current_app.config["REAPPLY_COOLDOWN_DAYS"]
    if days <= 0 or not user or not user.is_authenticated or user.is_hr:
        return None
    from models import User
    ids = [user.id]
    if user.id_hash:  # in case the same ID was ever attached to another account
        ids = [u.id for u in User.query.filter_by(id_hash=user.id_hash)] or [user.id]
    cutoff = now() - timedelta(days=days)
    # Only outcomes the candidate knows about count: their own withdrawal, or a rejection HR posted.
    q = Application.query.filter(Application.candidate_id.in_(ids),
                                 (Application.status == "withdrawn") |
                                 ((Application.status == "rejected") & (Application.posted_status == "rejected")))
    latest = None
    for a in q:
        when = a.visible_decided_at or a.decided_at or a.updated_at or a.applied_at
        if when < cutoff:
            continue
        if current_app.config["REAPPLY_SCOPE"] == "same_title" and \
                (a.job.title or "").strip().lower() != (job.title or "").strip().lower():
            continue
        if latest is None or when > latest[0]:
            latest = (when, a)
    if not latest:
        return None
    return latest[0] + timedelta(days=days), latest[1]


# --------------------------------------------------------------------------- email

def lan_address():
    """This PC's address on the local network (no traffic is sent), or '' if it can't be found."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""


def email_configured():
    c = current_app.config
    if c.get("EMAIL_PROVIDER") == "graph":
        return all(c.get(k) for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_SENDER"))
    return bool(c["SMTP_HOST"] and c["SMTP_SENDER"])


def send_emails(recipients, subject, text, html=None):
    """Send one message per recipient. Returns (sent_count, {address: error})."""
    if current_app.config.get("EMAIL_PROVIDER") == "graph":
        return _send_graph(recipients, subject, text, html)
    return _send_smtp(recipients, subject, text, html)


def _graph_token(c):
    import requests
    r = requests.post(f"https://login.microsoftonline.com/{c['GRAPH_TENANT_ID']}/oauth2/v2.0/token", timeout=30, data={
        "client_id": c["GRAPH_CLIENT_ID"], "client_secret": c["GRAPH_CLIENT_SECRET"],
        "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"})
    if r.status_code != 200:
        try:
            why = r.json().get("error_description", r.text)
        except ValueError:
            why = r.text
        raise RuntimeError(f"Microsoft sign-in failed ({r.status_code}): {why.splitlines()[0][:200]}")
    return r.json()["access_token"]


def _send_graph(recipients, subject, text, html=None):
    """Microsoft 365 via Microsoft Graph POST /users/{sender}/sendMail (202 = accepted)."""
    import requests
    from urllib.parse import quote
    c = current_app.config
    token = _graph_token(c)
    url = f"https://graph.microsoft.com/v1.0/users/{quote(c['GRAPH_SENDER'])}/sendMail"
    failures, sent = {}, 0
    for to in recipients:
        body = {"message": {"subject": subject,
                            "body": {"contentType": "HTML" if html else "Text", "content": html or text},
                            "toRecipients": [{"emailAddress": {"address": to}}]},
                "saveToSentItems": True}
        try:
            r = requests.post(url, json=body, timeout=30, headers={"Authorization": f"Bearer {token}"})
        except requests.RequestException as e:
            failures[to] = str(e)[:200]
            continue
        if r.status_code == 202:
            sent += 1
        else:
            try:
                why = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                why = r.text
            failures[to] = f"{r.status_code}: {why}"[:200]
    return sent, failures


def _send_smtp(recipients, subject, text, html=None):
    c = current_app.config
    failures, sent = {}, 0
    with smtplib.SMTP(c["SMTP_HOST"], c["SMTP_PORT"], timeout=30) as smtp:
        if c["SMTP_USE_TLS"]:
            smtp.starttls()
        if c["SMTP_USER"]:
            smtp.login(c["SMTP_USER"], c["SMTP_PASSWORD"])
        for to in recipients:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, c["SMTP_SENDER"], to
            msg.set_content(text)
            if html:
                msg.add_alternative(html, subtype="html")
            try:
                smtp.send_message(msg)
                sent += 1
            except smtplib.SMTPRecipientsRefused as e:
                code, why = next(iter(e.recipients.values()), (None, b""))
                why = why.decode(errors="ignore") if isinstance(why, bytes) else str(why)
                failures[to] = f"refused ({code} {why})".strip()[:200]
            except smtplib.SMTPException as e:
                failures[to] = str(e)[:200]
    return sent, failures


def run_screening(application: Application, actor=None):
    # Save the application first so the database isn't locked while the model scores it.
    db.session.commit()
    job = application.job
    res = ai.screen(job, application.resume_text or "", current_app.config["AI_WEIGHT"])
    application.score_ai = res["score_ai"]
    application.score_keyword = res["score_keyword"]
    application.score_final = res["score_final"]
    application.years_experience = res["years"]
    application.matched_skills_json = json.dumps(res["matched"])
    application.missing_skills_json = json.dumps(res["missing"])
    application.strengths_json = json.dumps(res["strengths"])
    application.gaps_json = json.dumps(res["gaps"])
    application.summary = res["summary"]
    application.recommendation = res["recommendation"]
    application.scorer = res["scorer"]
    application.screening_error = res["error"]
    application.screened_at = now()
    application.screening_state = "done"

    threshold = job.shortlist_threshold or current_app.config["DEFAULT_SHORTLIST_THRESHOLD"]
    apply_threshold(application, threshold)
    return res


def apply_threshold(application, threshold):
    """Auto-shortlist at/above the job's threshold; undo only automatic shortlists below it."""
    if application.score_final is None:
        return
    if application.status == "applied" and application.score_final >= threshold:
        application.auto_shortlisted = True
        record_event(application, "shortlisted", actor_label="System · auto-shortlist",
                     reason=f"Score {application.score_final:.0f}% ≥ threshold {threshold}%")
    elif application.auto_shortlisted and application.status == "shortlisted" \
            and application.score_final < threshold:
        application.auto_shortlisted = False
        record_event(application, "applied", actor_label="System · auto-shortlist",
                     reason=f"Score {application.score_final:.0f}% now below threshold {threshold}%")
