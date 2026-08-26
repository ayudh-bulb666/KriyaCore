from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_required, current_user
from datetime import datetime, date, timedelta

from .models import db, Member, Attendance, Notification, VISIT_COOLDOWN_MINUTES

attendance_bp = Blueprint('attendance', __name__, url_prefix='/<string:gym_slug>/attendance')


@attendance_bp.route('/')
@login_required
def index():
    gid           = current_user.gym_id
    date_str      = request.args.get('date', date.today().isoformat())
    member_filter = request.args.get('member_id', '')

    try:
        filter_date = date.fromisoformat(date_str)
    except ValueError:
        filter_date = date.today()

    day_start = datetime.combine(filter_date, datetime.min.time())
    day_end   = datetime.combine(filter_date, datetime.max.time())

    query = Attendance.query.filter(
        Attendance.gym_id     == gid,
        Attendance.visited_at >= day_start,
        Attendance.visited_at <= day_end,
    )
    if member_filter:
        query = query.filter_by(member_id=int(member_filter))

    records = query.order_by(Attendance.visited_at.desc()).all()

    unique_members = len({r.member_id for r in records})
    via_face_id    = sum(1 for r in records if r.source == 'face_id')
    busiest_hour   = None
    if records:
        hours = {}
        for r in records:
            hours[r.visited_at.hour] = hours.get(r.visited_at.hour, 0) + 1
        peak = max(hours.items(), key=lambda kv: kv[1])
        busiest_hour = datetime.strptime(str(peak[0]), '%H').strftime('%-I %p')

    members = Member.query.filter_by(gym_id=gid, status='active').order_by(Member.first_name).all()

    return render_template(
        'attendance/index.html',
        records=records,
        filter_date=filter_date,
        date_str=date_str,
        prev_date=(filter_date - timedelta(days=1)).isoformat(),
        next_date=(filter_date + timedelta(days=1)).isoformat(),
        is_today=filter_date == date.today(),
        unique_members=unique_members,
        via_face_id=via_face_id,
        busiest_hour=busiest_hour,
        members=members,
        member_filter=member_filter,
        cooldown_minutes=VISIT_COOLDOWN_MINUTES,
    )


@attendance_bp.route('/log-visit', methods=['POST'])
@login_required
def log_visit():
    """Manual arrival logging, for gyms without a Face ID reader (or when
    the reader doesn't recognise someone). Same cooldown as the webhook, so
    staff tapping twice doesn't double-log."""
    gid       = current_user.gym_id
    member_id = request.form.get('member_id')
    notes     = request.form.get('notes', '').strip()

    if not member_id:
        flash('Please select a member.', 'danger')
        return redirect(url_for('attendance.index'))

    member = Member.query.filter_by(id=int(member_id), gym_id=gid).first_or_404()

    duplicate = Attendance.recent_visit(gid, member.id)
    if duplicate:
        flash(f'{member.full_name} was already logged at '
              f'{duplicate.visited_at.strftime("%I:%M %p")} — nothing added.', 'warning')
        return redirect(url_for('attendance.index'))

    now = datetime.now()
    state, state_message = member.membership_state

    db.session.add(Attendance(
        gym_id=gid,
        member_id=member.id,
        visited_at=now,
        notes=notes,
        recorded_by_id=current_user.id,
        source='manual',
        membership_status=state,
    ))
    db.session.add(Notification(
        gym_id=gid,
        type='check_in',
        message=f'{member.full_name} arrived at {now.strftime("%I:%M %p")}',
        member_id=member.id,
    ))
    db.session.commit()

    # Staff logging someone in by hand have the person in front of them —
    # this is the moment to mention a lapsed membership, not later.
    if state == 'active':
        flash(f'✓ {member.full_name} logged in at {now.strftime("%I:%M %p")}.', 'success')
    else:
        flash(f'{member.full_name} logged in at {now.strftime("%I:%M %p")} — '
              f'heads up: {state_message}.', 'warning')
    return redirect(url_for('attendance.index'))


@attendance_bp.route('/search-members')
@login_required
def search_members():
    gid = current_user.gym_id
    q   = request.args.get('q', '').strip()
    members = Member.query.filter(
        Member.gym_id  == gid,
        Member.status  == 'active',
        db.or_(
            Member.first_name.ilike(f'%{q}%'),
            Member.last_name.ilike(f'%{q}%'),
        )
    ).limit(8).all()
    return jsonify([{'id': m.id, 'name': m.full_name, 'initials': m.initials} for m in members])
