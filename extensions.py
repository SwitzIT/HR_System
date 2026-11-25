from functools import wraps

from flask import abort, flash, redirect, request, url_for
from flask_login import LoginManager, current_user
from flask_wtf.csrf import CSRFProtect

from models import User, db

csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


def hr_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.is_hr:
            abort(403)
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    """HR admins only (HR team, decision log). Other HR users are sent back to the dashboard."""
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.is_hr:
            abort(403)
        if not current_user.is_admin:
            from flask import flash, redirect, url_for
            flash("That page is available to HR admins only.", "info")
            return redirect(url_for("hr.dashboard"))
        return f(*a, **kw)
    return wrapper


def candidate_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if current_user.is_hr:
            flash("You're signed in as HR. Use a candidate account to apply.", "info")
            return redirect(url_for("hr.dashboard"))
        if not current_user.has_identity and request.endpoint != "candidate.identity":
            return redirect(url_for("candidate.identity", next=request.full_path if request.method == "GET" else None))
        return f(*a, **kw)
    return wrapper
