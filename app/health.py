"""
Health endpoints — for uptime monitoring, and for keeping Render warm.

Two routes, deliberately separate, because they answer different questions:

  /healthz       Is the process alive? Never touches the database, so it
                 cannot fail for any reason other than the app being down.
                 This is what Render's own health check should use: a deep
                 check here would fail a deploy over a transient Supabase
                 blip, which is the opposite of helpful.

  /healthz/deep  Is the app actually able to serve? Runs one trivial query,
                 so a dead pooler or an exhausted connection pool shows up
                 as 503. This is what an external monitor should watch —
                 a 200 from /healthz while the database is unreachable is
                 a green light on a broken system.

Both are unauthenticated on purpose: a monitor cannot log in, and neither
route exposes anything. They return no gym names, no counts, no version —
nothing that isn't already true of any HTTP server.

Rate-limit exempt so a monitor on a short interval can never trip the
global limiter and turn a healthy app into a false "site down" alert.
"""
from flask import Blueprint, jsonify
from sqlalchemy import text

from .models import db

health_bp = Blueprint('health', __name__)


@health_bp.route('/healthz')
def healthz():
    """Liveness. If this responds at all, the answer is yes."""
    return jsonify({'status': 'ok'}), 200


@health_bp.route('/healthz/deep')
def healthz_deep():
    """Readiness — liveness plus a working database connection."""
    try:
        db.session.execute(text('SELECT 1'))
    except Exception:
        # Deliberately no exception detail in the response: this route is
        # public, and driver errors leak host names and user names. Sentry
        # already captures the traceback with full context.
        return jsonify({'status': 'error', 'db': 'unreachable'}), 503

    return jsonify({'status': 'ok', 'db': 'ok'}), 200
