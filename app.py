import os
import click
from flask import Flask, flash, redirect, render_template, request, url_for

from markupsafe import Markup

from config import Config
from extensions import csrf, login_manager
from models import User, db
from services import render_md

def create_app(config=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    if app.config.get("ID_HASH_KEY_IS_EXAMPLE"):
        app.logger.warning("ID_HASH_KEY in .env is still the example text. Before candidates register, replace it "
                           "with a long random value (run: flask --app app gen-secrets) and never change it afterwards.")

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    from views_auth import bp as auth_bp
    from views_candidate import bp as cand_bp
    from views_hr import bp as hr_bp
    from views_public import bp as public_bp
    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(cand_bp, url_prefix="/me")
    app.register_blueprint(hr_bp, url_prefix="/hr")

    app.jinja_env.filters["md"] = lambda t: Markup(render_md(t))  # sanitised by nh3

    @app.template_filter("dt")
    def _dt(v, fmt="%d %b %Y"):
        return v.strftime(fmt) if v else "—"

    @app.context_processor
    def inject():
        import ai
        from datetime import timedelta
        from models import now as _now
        return {"timedelta": timedelta, "now": _now, "COMPANY": app.config["COMPANY_NAME"], "AI_ON": ai.ai_enabled(),
                "AI_MODEL": ai.model_name(), "LOGO": _brand_file("logo"), "LOGO_INVERSE": _brand_file("logo-inverse"),
                "BRAND": app.config["BRAND_COLOR"], "ACCENT": app.config["BRAND_ACCENT"],
                "WEBSITE": app.config["COMPANY_WEBSITE"], "TAGLINE": app.config["COMPANY_TAGLINE"]}

    def _brand_file(stem):
        folder = os.path.join(app.static_folder, "brand")
        for ext in ("svg", "png", "webp", "jpg", "jpeg"):
            if os.path.exists(os.path.join(folder, f"{stem}.{ext}")):
                return f"brand/{stem}.{ext}"
        return None

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, msg="You don't have access to that page."), 403

    @app.errorhandler(404)
    def notfound(e):
        return render_template("error.html", code=404, msg="That page doesn't exist."), 404

    from flask_wtf.csrf import CSRFError
    from sqlalchemy.exc import OperationalError

    @app.errorhandler(CSRFError)
    def form_expired(e):
        mins = app.config.get("FORM_EXPIRY_MINUTES", 60)
        if "expired" in str(e.description).lower():
            msg = (f"This page was open for more than {mins} minutes, so for security the form expired and nothing "
                   "was saved. The page has been reloaded: please make your change or click the button again.")
        else:
            msg = ("Your session changed (for example, you signed in again in another tab), so nothing was saved. "
                   "The page has been reloaded: please try again.")
        flash(msg, "error")
        back = request.referrer or ""
        if not back.startswith(request.host_url):
            back = url_for("public.home")
        return redirect(back)

    @app.errorhandler(OperationalError)
    def db_busy(e):
        db.session.rollback()
        if "locked" in str(e).lower():
            flash("The system was busy saving another change (for example, an AI draft still being written). "
                  "Nothing was saved. Please wait a moment and try again.", "error")
            return redirect(request.referrer or url_for("public.home"))
        raise e

    @app.errorhandler(413)
    def too_big(e):
        flash(f"File too large. Max {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB.", "error")
        return redirect(request.referrer or url_for("public.home"))

    with app.app_context():
        db.create_all()
        from schema_upgrade import upgrade
        upgrade(app)  # adds new columns to an existing database and backfills them
        _ensure_admin(app)

    @app.before_request
    def _start_screening_worker():
        import screening_queue
        screening_queue.start(app)

    @app.cli.command("screen-pending")
    def screen_pending():
        """Screen every queued application now (normally the background worker does this)."""
        import screening_queue
        n = screening_queue.process_all()
        print(f"Screened {n} application(s).")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Load a sample job and candidate applications."""
        from seed import seed
        seed()
        click.echo("Demo data loaded.")

    @app.cli.command("ai-check")
    def ai_check():
        """Show the AI configuration and make one small test call to the model and embeddings."""
        import time
        import ai
        from ai import chains, providers
        for k, v in ai.status().items():
            click.echo(f"{k:20} {v}")
        if not ai.ai_enabled():
            click.echo("\nLLM not configured: the app runs in rules mode (template JDs, keyword screening).")
        else:
            click.echo("\nCalling the chat model ...")
            t = time.time()
            try:
                reply = (providers.get_chat_model("screen")).invoke(
                    "Reply with exactly this JSON and nothing else: {\"ok\": true}")
                click.echo(f"  OK in {time.time() - t:.1f}s: {chains.clean_text(reply.content)[:120]}")
            except Exception as e:
                click.echo(f"  FAILED: {ai.friendly_error(e)}")
                click.echo(f"  detail: {str(e)[:300]}")
        if providers.embeddings_provider():
            click.echo("Calling the embeddings model ...")
            try:
                v = providers.get_embeddings().embed_query("Senior Data Analyst with Power BI and SQL")
                click.echo(f"  OK: vector of {len(v)} dimensions")
            except Exception as e:
                click.echo(f"  FAILED: {ai.friendly_error(e, 'EMBEDDINGS_PROVIDER')} (screening falls back to word-overlap similarity)")
                click.echo(f"  detail: {str(e)[:300]}")

    @app.cli.command("ai-models")
    @click.argument("search", required=False, default="")
    @click.option("--limit", default=40, help="Rows to show.")
    def ai_models(search, limit):
        """List chat models Hugging Face Inference Providers serve right now (live), cheapest first.

        Uses Hugging Face's public router catalogue: https://router.huggingface.co/v1/models
        Example: flask --app app ai-models qwen
        """
        import json
        import urllib.request
        req = urllib.request.Request("https://router.huggingface.co/v1/models",
                                     headers={"User-Agent": "talentflow-ai-models"})
        try:
            data = json.load(urllib.request.urlopen(req, timeout=30)).get("data", [])
        except Exception as e:
            click.echo(f"Could not reach Hugging Face: {e}")
            return
        rows = []
        for m in data:
            mid = m.get("id", "")
            if search and search.lower() not in mid.lower():
                continue
            for p in m.get("providers") or []:
                if p.get("status", "live") != "live":
                    continue
                price = p.get("pricing") or {}
                cost = (price.get("input") or 0) + (price.get("output") or 0)
                rows.append((cost, mid, p.get("provider", "?"), price.get("input"), price.get("output"),
                             "free" if p.get("is_free") else "", "yes" if p.get("supports_structured_output") else ""))
        rows.sort(key=lambda r: (r[0], r[1]))
        if not rows:
            click.echo("No live chat models match." if search else "The catalogue came back empty.")
            return
        click.echo(f"{'LLM_MODEL':52} {'HF_INFERENCE_PROVIDER':22} {'$/M in':>7} {'$/M out':>8}  free  JSON")
        for _, mid, prov, pin, pout, free, js in rows[:limit]:
            fmt = lambda v: f"{v:.2f}" if isinstance(v, (int, float)) else "-"
            click.echo(f"{mid[:52]:52} {prov[:22]:22} {fmt(pin):>7} {fmt(pout):>8}  {free:4}  {js}")
        click.echo(f"\n{len(rows)} live model/provider pairs" + (f" matching '{search}'" if search else "")
                   + f". Put the pair you choose in .env as LLM_MODEL and HF_INFERENCE_PROVIDER.")

    @app.cli.command("gen-secrets")
    def gen_secrets():
        """Print two random values to paste into .env as SECRET_KEY and ID_HASH_KEY."""
        import secrets
        print("SECRET_KEY=" + secrets.token_urlsafe(48))
        print("ID_HASH_KEY=" + secrets.token_urlsafe(48))
        print("\nSet ID_HASH_KEY once, before candidates register, and never change it afterwards.")

    @app.cli.command("create-hr")
    @click.option("--name", prompt=True)
    @click.option("--email", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--admin", is_flag=True, default=False)
    def create_hr(name, email, password, admin):
        """Create an HR user."""
        u = User(name=name, email=email.lower(), role="hr", is_admin=admin)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.echo(f"HR user {email} created.")

    return app


def _ensure_admin(app):
    """First run: create the HR admin from env (or a default to be changed)."""
    if User.query.filter_by(role="hr").first():
        return
    email = os.getenv("ADMIN_EMAIL", "admin@company.com").lower()
    pw = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
    u = User(name=os.getenv("ADMIN_NAME", "HR Admin"), email=email, role="hr", is_admin=True)
    u.set_password(pw)
    db.session.add(u)
    db.session.commit()
    app.logger.warning("Created HR admin %s — change the password after first login.", email)


app = create_app()

if __name__ == "__main__":
    # HOST=0.0.0.0 lets colleagues on the same network open the app (and the staff links);
    # HOST=127.0.0.1 keeps it on this PC only.
    host, port = os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "5000"))
    if os.getenv("FLASK_DEBUG", "0") == "1":
        app.run(debug=True, host=host, port=port)
    else:
        try:
            from waitress import serve  # production server that also runs on Windows
        except ImportError:
            app.run(host=host, port=port)
        else:
            from services import lan_address
            print(f" * Switz Group Talent app running on http://127.0.0.1:{port}"
                  + (f"  and on the network at http://{lan_address()}:{port}" if host == "0.0.0.0" and lan_address() else ""))
            serve(app, host=host, port=port, threads=8)
