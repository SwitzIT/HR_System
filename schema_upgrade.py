"""Lightweight, safe schema upgrade for existing databases (SQLite or PostgreSQL).

db.create_all() creates new tables but never adds columns to existing ones. This adds any
missing columns and indexes, then backfills data for features added later. It only ever ADDS:
nothing is dropped or rewritten. For larger production changes, move to Alembic migrations.
"""
import secrets

from sqlalchemy import inspect, text

from models import Application, ApplicationEvent, Job, db


def upgrade(app):
    engine = db.engine
    insp = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ctype = col.type.compile(dialect=engine.dialect)
                q = engine.dialect.identifier_preparer.quote
                conn.execute(text(f"ALTER TABLE {q(table.name)} ADD COLUMN {q(col.name)} {ctype}"))
                added.append(f"{table.name}.{col.name}")
        for table in db.metadata.sorted_tables:
            for idx in table.indexes:
                idx.create(conn, checkfirst=True)
    if added:
        app.logger.warning("Database upgraded: added columns %s", ", ".join(added))
    _backfill(added)


def _backfill(added=()):
    changed = False
    if "application.screening_state" in added:
        # existing applications were screened when they were submitted
        Application.query.filter(Application.screened_at.isnot(None)).update(
            {"screening_state": "done"}, synchronize_session=False)
        Application.query.filter(Application.screened_at.is_(None)).update(
            {"screening_state": "queued"}, synchronize_session=False)
        changed = True
    if "application.posted_status" in added:
        # Before HR posting existed, candidates saw every status immediately. Keep showing them
        # exactly what they already saw; anything that changes from now on waits for HR to post.
        for a in Application.query.filter(Application.status != "withdrawn"):
            a.posted_status = a.status
            a.posted_at = a.decided_at or a.updated_at or a.applied_at
        changed = True
    for job in Job.query.filter((Job.visibility.is_(None)) | (Job.share_token.is_(None))):
        job.visibility = job.visibility or "public"
        job.share_token = job.share_token or secrets.token_urlsafe(16)
        changed = True
    for a in Application.query.filter(Application.source.is_(None)):
        a.source = "external"
        changed = True
    # Applications created before the decision log existed: record what we know.
    logged = {r[0] for r in db.session.query(ApplicationEvent.application_id).distinct()}
    for a in Application.query.all():
        if a.id in logged:
            continue
        db.session.add(ApplicationEvent(application_id=a.id, job_id=a.job_id, candidate_id=a.candidate_id,
                                        from_status=None, to_status="applied", actor_id=a.candidate_id,
                                        created_at=a.applied_at))
        if a.status != "applied":
            db.session.add(ApplicationEvent(
                application_id=a.id, job_id=a.job_id, candidate_id=a.candidate_id, from_status="applied",
                to_status=a.status, actor_label="Recorded before decision log", score=a.score_final,
                created_at=a.updated_at or a.applied_at))
        changed = True
    if changed:
        db.session.commit()
