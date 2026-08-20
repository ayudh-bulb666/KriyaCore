from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_required, current_user
from datetime import datetime, date, timedelta

from .models import db, Member, Attendance

attendance_bp = Blueprint('attendance', __name__, url_prefix='/attendance')


@attendance_bp.route('/')
@login_required
def index():
    # Date filter — default today
    date_str = request.args.get('date', date.today().isoformat())
    member_filter = request.args.get('member_id', '')

    try:
        filter_date = date.fromisoformat(date_str)
    except ValueError:
        filter_date = date.today()

    day_start = datetime.combine(filter_date, datetime.min.time())
    day_end = datetime.combine(filter_date, datetime.max.time())

    query = Attendance.query.filter(
        Attendance.check_in >= day_start,
        Attendance.check_in <= day_end,
    )

    if member_filter:
        query = query.filter_by(member_id=int(member_filter))

    records = query.order_by(Attendance.check_in.desc()).all()

    # Stats for selected day
    unique_members = len({r.member_id for r in records})
    checked_in_now = [r for r in records if not r.check_out]
    completed = [r for r in records if r.check_out]
    avg_duration = (
        int(sum(r.duration_minutes for r in completed) / len(completed))
        if completed else 0
    )

    members = Member.query.filter_by(status='active').order_by(Member.first_name).all()

    # Navigation helpers
    prev_date = (filter_date - timedelta(days=1)).isoformat()
    next_date = (filter_date + timedelta(days=1)).isoformat()
    is_today = filter_date == date.today()

    return render_template(
        'attendance/index.html',
        records=records,
        filter_date=filter_date,
        date_str=date_str,
        prev_date=prev_date,
        next_date=next_date,
        is_today=is_today,
        unique_members=unique_members,
        checked_in_now=checked_in_now,
        avg_duration=avg_duration,
        members=members,
        member_filter=member_filter,
        now=datetime.now(),
    )


@attendance_bp.route('/checkin', methods=['POST'])
@login_required
def checkin():
    member_id = request.form.get('member_id')
    notes = request.form.get('notes', '').strip()

    if not member_id:
        flash('Please select a member.', 'danger')
        return redirect(url_for('attendance.index'))

    member = Member.query.get_or_404(int(member_id))

    # Check if already checked in today without checking out
    today_start = datetime.combine(date.today(), datetime.min.time())
    existing = Attendance.query.filter(
        Attendance.member_id == member.id,
        Attendance.check_in >= today_start,
        Attendance.check_out.is_(None),
    ).first()

    if existing:
        flash(f'{member.full_name} is already checked in (since {existing.check_in.strftime("%I:%M %p")}). Record a check-out first.', 'warning')
        return redirect(url_for('attendance.index'))

    record = Attendance(
        member_id=member.id,
        check_in=datetime.now(),
        notes=notes,
        recorded_by_id=current_user.id,
    )
    db.session.add(record)
    db.session.commit()

    flash(f'✓ {member.full_name} checked in at {record.check_in.strftime("%I:%M %p")}.', 'success')
    return redirect(url_for('attendance.index'))


@attendance_bp.route('/<int:record_id>/checkout', methods=['POST'])
@login_required
def checkout(record_id):
    record = Attendance.query.get_or_404(record_id)
    if record.check_out:
        flash('Already checked out.', 'warning')
        return redirect(url_for('attendance.index'))

    record.check_out = datetime.now()
    db.session.commit()

    duration = record.duration_minutes
    flash(
        f'✓ {record.member.full_name} checked out — {duration} min session.',
        'success'
    )
    return redirect(request.referrer or url_for('attendance.index'))


@attendance_bp.route('/search-members')
@login_required
def search_members():
    """AJAX endpoint for member search in check-in form."""
    q = request.args.get('q', '').strip()
    members = Member.query.filter(
        Member.status == 'active',
        db.or_(
            Member.first_name.ilike(f'%{q}%'),
            Member.last_name.ilike(f'%{q}%'),
        )
    ).limit(8).all()
    return jsonify([{'id': m.id, 'name': m.full_name, 'initials': m.initials} for m in members])
