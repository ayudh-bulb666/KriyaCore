"""
Scheduled jobs — triggered by an external cron, not an in-process scheduler.

Nothing runs on a timer inside GYMPro itself. If it did (e.g. via
APScheduler), a multi-worker gunicorn deploy would spin up one scheduler
per worker and send every reminder multiple times. Instead, an external
cron (crontab on the VPS, or Railway's built-in Cron trigger) hits this one
HTTP endpoint once a day, which is a single request handled by a single
worker — no duplicate-send risk, and it's just a normal route you can also
curl by hand to test.

See deploy/SETUP.md for the crontab line, and CRON_SECRET in .env.example.
"""
import hmac
import os

from flask import Blueprint, request, jsonify

from .models import Gym
from .plans import plan_has
from .whatsapp import get_or_create_settings, run_expiry_reminders

cron_bp = Blueprint('cron', __name__, url_prefix='/internal/cron')


@cron_bp.route('/whatsapp-reminders', methods=['POST'])
def whatsapp_reminders():
    secret = os.environ.get('CRON_SECRET', '')
    given  = request.headers.get('X-Cron-Secret', '')

    if not secret or not hmac.compare_digest(given, secret):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 403

    results = []
    for gym in Gym.query.filter_by(is_active=True).all():
        if not plan_has(gym, 'whatsapp'):
            continue
        settings = get_or_create_settings(gym)
        if not settings.auto_expiry_reminders_enabled:
            continue
        sent, skipped = run_expiry_reminders(gym)
        results.append({'gym': gym.slug, 'sent': sent, 'skipped': skipped})

    return jsonify({'ok': True, 'gyms_processed': len(results), 'results': results})
