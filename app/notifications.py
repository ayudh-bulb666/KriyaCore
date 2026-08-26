from flask import Blueprint, render_template, jsonify, url_for
from flask_login import login_required, current_user
from datetime import datetime, date

from .models import db, Notification, MemberMembership

notifications_bp = Blueprint('notifications', __name__, url_prefix='/<string:gym_slug>/notifications')


# ── Helpers ────────────────────────────────────────────────────────────────

def _time_ago(dt):
    diff = datetime.utcnow() - dt
    s = int(diff.total_seconds())
    if s < 60:    return 'Just now'
    if s < 3600:  return f'{s // 60}m ago'
    if s < 86400: return f'{s // 3600}h ago'
    return f'{s // 86400}d ago'


def _build_feed(gid):
    """Return a unified list of notification dicts for the dropdown/API."""
    today = date.today()
    items = []

    # ── Stored check-in notifications (last 20) ───────────────────────────
    for n in (Notification.query
              .filter_by(gym_id=gid)
              .order_by(Notification.created_at.desc())
              .limit(20).all()):
        items.append({
            'type':    'check_in',
            'message': n.message,
            'is_read': n.is_read,
            'time':    _time_ago(n.created_at),
            'url':     url_for('members.detail', member_id=n.member_id) if n.member_id else '#',
        })

    # ── Expiring memberships (dynamic) ────────────────────────────────────
    expiring = (MemberMembership.expiring_soon_query(gym_id=gid)
                .order_by(MemberMembership.end_date.asc()).all())

    for m in expiring:
        days = (m.end_date - today).days
        when = 'today' if days == 0 else ('tomorrow' if days == 1 else f'in {days} days')
        items.append({
            'type':    'expiring',
            'message': f"{m.member.full_name}'s {m.plan.name} expires {when}",
            'is_read': False,
            'time':    f'{days}d left' if days > 0 else 'Today',
            'url':     url_for('members.detail', member_id=m.member_id),
        })

    # ── Unpaid memberships (dynamic) ──────────────────────────────────────
    unpaid = MemberMembership.unpaid_query(gym_id=gid).all()

    for m in unpaid:
        items.append({
            'type':    'unpaid',
            'message': f"{m.member.full_name}'s {m.plan.name} payment is {m.payment_status}",
            'is_read': False,
            'time':    'Action needed',
            'url':     url_for('members.detail', member_id=m.member_id),
        })

    return items


# ── Routes ─────────────────────────────────────────────────────────────────

@notifications_bp.route('/')
@login_required
def index():
    gid   = current_user.gym_id
    today = date.today()

    Notification.query.filter_by(gym_id=gid, is_read=False).update({'is_read': True})
    db.session.commit()

    broadcasts = (Notification.query
                  .filter_by(gym_id=gid, type='broadcast')
                  .order_by(Notification.created_at.desc())
                  .limit(20).all())

    checkins = (Notification.query
                .filter(Notification.gym_id == gid,
                        Notification.type != 'broadcast')
                .order_by(Notification.created_at.desc())
                .limit(50).all())

    expiring = (MemberMembership.expiring_soon_query(gym_id=gid)
                .order_by(MemberMembership.end_date.asc()).all())
    for e in expiring:
        e.days_left = (e.end_date - today).days

    unpaid = MemberMembership.unpaid_query(gym_id=gid).all()

    return render_template(
        'notifications/index.html',
        checkins=checkins,
        broadcasts=broadcasts,
        expiring=expiring,
        unpaid=unpaid,
    )


@notifications_bp.route('/api/feed')
@login_required
def api_feed():
    return jsonify(_build_feed(current_user.gym_id))


@notifications_bp.route('/mark-read', methods=['POST'])
@login_required
def mark_all_read():
    Notification.query.filter_by(gym_id=current_user.gym_id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'ok': True})
