import re
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from identity import IdentityError, apply_to_user
from models import Job, User, db

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _home_for(user):
    return url_for("hr.dashboard") if user.is_hr else url_for("candidate.applications")


def _safe_next(target):
    if target and not urlparse(target).netloc and target.startswith("/"):
        return target
    return None


def _applying_for():
    """The job a candidate was trying to apply for, if they arrived from an Apply button."""
    m = re.match(r"^/me/apply/(\d+)", _safe_next(request.args.get("next")) or "")
    return db.session.get(Job, int(m.group(1))) if m else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_home_for(current_user))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if user and user.active and user.check_password(request.form.get("password", "")):
            login_user(user, remember=bool(request.form.get("remember")))
            nxt = _safe_next(request.args.get("next"))
            if not user.is_hr and not user.has_identity:
                flash("One more step: add your PAN or passport to continue.", "info")
                return redirect(url_for("candidate.identity", next=nxt))
            return redirect(nxt or _home_for(user))
        flash("We couldn't sign you in with that email and password. Check them and try again, "
              "or if you're a new candidate, create an account below.", "error")
    return render_template("auth/login.html", job=_applying_for(), next=_safe_next(request.args.get("next")))


@bp.route("/register", methods=["GET", "POST"])
def register():
    """Candidate self-registration. HR accounts are created by an HR admin."""
    if current_user.is_authenticated:
        return redirect(_home_for(current_user))
    form = request.form
    if request.method == "POST":
        name, email = form.get("name", "").strip(), form.get("email", "").strip().lower()
        pw = form.get("password", "")
        if not name or not EMAIL_RE.match(email):
            flash("Please enter your name and a valid email.", "error")
        elif len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif pw != form.get("confirm"):
            flash("Passwords don't match.", "error")
        elif User.query.filter_by(email=email).first():
            flash("An account with this email already exists. Please sign in.", "error")
        elif not form.get("id_consent"):
            flash("Please tick the consent box so we can store your ID for the re-application policy.", "error")
        else:
            u = User(name=name, email=email, phone=form.get("phone", "").strip(), role="candidate")
            try:
                apply_to_user(u, form.get("id_type"), form.get("id_number"), form.get("id_country"))
            except IdentityError as e:
                flash(str(e), "error")
                return render_template("auth/register.html", form=form, job=_applying_for(),
                           next=_safe_next(request.args.get("next")))
            u.set_password(pw)
            db.session.add(u)
            db.session.commit()
            login_user(u)
            flash("Welcome! Your account is ready.", "success")
            return redirect(_safe_next(request.args.get("next")) or url_for("public.jobs"))
    return render_template("auth/register.html", form=form, job=_applying_for(),
                           next=_safe_next(request.args.get("next")))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("public.home"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        f = request.form
        if not current_user.check_password(f.get("current", "")):
            flash("Current password is incorrect.", "error")
        elif len(f.get("new", "")) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif f.get("new") != f.get("confirm"):
            flash("New passwords don't match.", "error")
        else:
            current_user.set_password(f["new"])
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("auth.account"))
    return render_template("auth/account.html")
