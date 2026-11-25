"""Background resume screening.

A candidate's submission is saved immediately and marked `queued`. One background worker thread
picks queued applications one at a time (oldest first), runs the AI + keyword screening and
stores the result for HR. Nothing here is shown to the candidate: they see "Pending" until HR posts
a status.

- One worker per app process, started on the first web request (not by CLI commands).
- Claiming a row is a conditional UPDATE (queued -> running), so two processes never screen the
  same application.
- Anything left `running` by a crash or restart is put back in the queue on start.
- SCREENING_MODE=inline (used by tests) screens during the request instead.
"""
import threading
import time
import traceback

from flask import current_app

_wake = threading.Event()
_started = False
_lock = threading.Lock()


def enqueue(application):
    """Mark an application for (re-)screening. The caller commits; the worker is woken up."""
    application.screening_state = "queued"
    application.screening_error = None


def notify():
    if current_app.config.get("SCREENING_MODE", "background") == "inline":
        process_all()
    else:
        _wake.set()


def process_all():
    """Screen every queued application now, in this thread (inline mode, or `flask screen-pending`)."""
    n = 0
    while _process_one():
        n += 1
    return n


def _process_one():
    from models import Application, db
    from services import run_screening

    nxt = (Application.query.filter_by(screening_state="queued")
           .order_by(Application.applied_at, Application.id).first())
    if not nxt:
        return False
    claimed = (Application.query.filter_by(id=nxt.id, screening_state="queued")
               .update({"screening_state": "running"}, synchronize_session=False))
    db.session.commit()
    if not claimed:  # another process took it
        return True
    a = db.session.get(Application, nxt.id)
    db.session.refresh(a)
    try:
        run_screening(a)  # commits before the slow AI call, so the database stays free
        # A re-screen may have been requested while this one ran: keep it queued.
        with db.session.no_autoflush:
            fresh = db.session.query(Application.screening_state).filter_by(id=a.id).scalar()
        a.screening_state = "queued" if fresh == "queued" else "done"
        db.session.commit()
    except Exception as e:  # never let one bad resume stop the queue
        db.session.rollback()
        current_app.logger.error("Screening application %s failed: %s\n%s", a.id, e, traceback.format_exc())
        a = db.session.get(Application, nxt.id)
        a.screening_state = "failed"
        a.screening_error = f"Screening failed: {str(e)[:300]}"
        db.session.commit()
    return True


def _loop(app):
    while True:
        try:
            with app.app_context():
                while _process_one():
                    pass
                from models import db
                db.session.remove()
        except Exception as e:  # database busy etc.: try again shortly
            app.logger.error("Screening worker error: %s", e)
            time.sleep(5)
        _wake.wait(timeout=30)
        _wake.clear()


def start(app):
    """Start the worker once per process (idempotent)."""
    global _started
    if app.config.get("SCREENING_MODE", "background") == "inline":
        return
    with _lock:
        if _started:
            return
        _started = True
    with app.app_context():
        from models import Application, db
        stuck = Application.query.filter_by(screening_state="running").update(
            {"screening_state": "queued"}, synchronize_session=False)
        db.session.commit()
        if stuck:
            app.logger.warning("Re-queued %s application(s) whose screening was interrupted.", stuck)
    threading.Thread(target=_loop, args=(app,), name="screening-worker", daemon=True).start()
    _wake.set()
