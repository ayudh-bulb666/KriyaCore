"""
Face ID access-control integration.

GYMPro never captures, stores, or matches face data itself. A third-party
access-control terminal (installed at the gym's entrance) does the actual
face capture and matching, then pushes a scan event to the webhook below.
This blueprint's only job is: verify the event came from that gym's real
terminal, find the matching Member, and record it exactly like a manual
check-in/check-out.

Vendor-agnostic on purpose — we don't yet know which hardware the client
will use. The payload contract below is a reasonable, minimal default
(most access-control terminals can be configured to POST *something* like
this on a scan event); once a specific vendor is picked, this is the one
place that needs adjusting to match their actual payload shape.

Expected JSON body:
    {
        "external_id": "<the ID the terminal assigned this person>",
        "timestamp":   "2026-08-24T18:30:00"   # optional, ISO 8601, defaults to now
    }

Every 200 response carries the member's membership standing:

    {
        "ok": true,
        "member": "Rohit Kumar",
        "event": "visit_logged" | "duplicate_ignored" | "access_denied",
        "access": "allow" | "deny",
        "membership": {"state": "expired", "message": "...", "plan": ..., "expires_on": ...}
    }

A terminal that can read the response should act on `access`; one that can't
still leaves staff a flagged arrival in the app. GYMPro reports the standing
either way — whether a lapsed member is actually refused is the gym's call
via Gym.face_id_deny_expired, which defaults to off.

Auth: the gym_id + secret in the URL itself (see Gym.face_id_webhook_secret).
Most low-end access-control terminals can be configured to POST to a fixed
URL but can't easily send custom headers — a path-embedded secret is the
most universally compatible option. Exempted from CSRF (see __init__.py)
since this is called by a device, not a logged-in browser session.
"""
import hmac
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

from .models import db, Gym, Member, Attendance, Notification, VISIT_COOLDOWN_MINUTES

faceid_bp = Blueprint('faceid', __name__, url_prefix='/webhook/faceid')


@faceid_bp.route('/<int:gym_id>/<secret>', methods=['POST'])
def scan_event(gym_id, secret):
    gym = Gym.query.get(gym_id)

    if not gym or not gym.face_id_enabled or not gym.face_id_webhook_secret:
        return jsonify({'ok': False, 'error': 'face_id not enabled for this gym'}), 403

    if not hmac.compare_digest(secret, gym.face_id_webhook_secret):
        return jsonify({'ok': False, 'error': 'invalid webhook secret'}), 403

    payload = request.get_json(silent=True) or {}
    external_id = str(payload.get('external_id', '')).strip()
    if not external_id:
        return jsonify({'ok': False, 'error': 'external_id is required'}), 400

    member = Member.query.filter_by(gym_id=gym.id, face_id_external_id=external_id).first()
    if not member:
        return jsonify({'ok': False, 'error': 'no member enrolled with that external_id'}), 404

    # Attendance timestamps are server-local, not UTC (see the Attendance
    # model). A device that sends an offset-aware timestamp gets converted;
    # a naive one is taken at face value, since the reader sits in the gym
    # and is almost certainly on the same clock as the server.
    ts_raw = payload.get('timestamp')
    try:
        event_time = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now()
        if event_time.tzinfo is not None:
            event_time = event_time.astimezone().replace(tzinfo=None)
    except (ValueError, TypeError):
        event_time = datetime.now()

    # Where this member stands right now — the whole point of checking at the
    # door rather than letting a lapsed member walk straight through.
    state, state_message = member.membership_state
    lapsed = state in ('expired', 'none')

    membership_block = {'state': state, 'message': state_message}
    active = member.active_membership
    if active:
        membership_block['plan'] = active.plan.name
        membership_block['expires_on'] = active.end_date.isoformat()

    # Arrival only — see the Attendance model docstring for why there's no
    # check-out. A door reader re-triggers easily, so a scan inside the
    # cooldown window is acknowledged (the device gets a 200, the door still
    # opens) but doesn't add a second visit for the same arrival.
    duplicate = Attendance.recent_visit(gym.id, member.id, at=event_time)
    if duplicate:
        return jsonify({
            'ok': True,
            'member': member.full_name,
            'event': 'duplicate_ignored',
            'access': 'deny' if (lapsed and gym.face_id_deny_expired) else 'allow',
            'membership': membership_block,
            'existing_visit_at': duplicate.visited_at.isoformat(),
        })

    # Turned away at the door: they never came in, so no visit is recorded —
    # but staff need to know more here than on a normal arrival, not less.
    if lapsed and gym.face_id_deny_expired:
        # Someone facing a door that won't open scans again, and again. With
        # no Attendance row written there's nothing for the usual cooldown to
        # match on, so dedupe against the last denial notice instead.
        # NB: Notification.created_at is UTC (unlike Attendance.visited_at,
        # which is server-local) — compare against utcnow, not event_time.
        already_told = Notification.query.filter(
            Notification.gym_id     == gym.id,
            Notification.member_id  == member.id,
            Notification.type       == 'access_denied',
            Notification.created_at >= datetime.utcnow() - timedelta(minutes=VISIT_COOLDOWN_MINUTES),
        ).first()

        if not already_told:
            db.session.add(Notification(
                gym_id=gym.id,
                type='access_denied',
                message=(f'{member.full_name} was refused entry at '
                         f'{event_time.strftime("%I:%M %p")} — {state_message}'),
                member_id=member.id,
            ))
            db.session.commit()
        return jsonify({
            'ok': True,
            'member': member.full_name,
            'event': 'access_denied',
            'access': 'deny',
            'membership': membership_block,
        })

    db.session.add(Attendance(
        gym_id=gym.id,
        member_id=member.id,
        visited_at=event_time,
        recorded_by_id=None,     # no staff user — this came from the device
        source='face_id',
        membership_status=state,
    ))

    # A clean arrival is routine; one with a problem is the reason staff are
    # sitting there. Say which it is.
    if state == 'active':
        note = f'{member.full_name} arrived at {event_time.strftime("%I:%M %p")} (Face ID)'
    else:
        note = (f'⚠ {member.full_name} arrived at {event_time.strftime("%I:%M %p")} '
                f'— {state_message}')
    db.session.add(Notification(
        gym_id=gym.id,
        type='check_in',
        message=note,
        member_id=member.id,
    ))
    db.session.commit()

    return jsonify({
        'ok': True,
        'member': member.full_name,
        'event': 'visit_logged',
        'access': 'allow',
        'membership': membership_block,
    })
