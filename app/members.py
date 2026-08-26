import csv
import io
from flask import Blueprint, render_template, redirect, url_for, request, flash, Response
from flask_login import login_required, current_user
from datetime import date, datetime

from .models import (db, Member, User, MemberMembership, Attendance,
                     Notification, WhatsAppMessage, DataRequest)
from .helpers import role_required
from .plans import plan_within_member_limit, plan_has, PLANS

members_bp = Blueprint('members', __name__, url_prefix='/<string:gym_slug>/members')


def _apply_member_filters(query, search, status_filter, trainer_filter):
    """Shared by index() and export_csv() so the search/filter UI and the
    exported CSV always match the same set of members."""
    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(
                Member.first_name.ilike(like),
                Member.last_name.ilike(like),
                Member.email.ilike(like),
                Member.phone.ilike(like),
            )
        )
    if status_filter:
        query = query.filter_by(status=status_filter)
    if trainer_filter:
        if trainer_filter == 'none':
            query = query.filter(Member.assigned_trainer_id.is_(None))
        else:
            query = query.filter_by(assigned_trainer_id=int(trainer_filter))
    return query


@members_bp.route('/')
@login_required
def index():
    gid            = current_user.gym_id
    search         = request.args.get('search', '').strip()
    status_filter  = request.args.get('status', '')
    trainer_filter = request.args.get('trainer', '')

    query = _apply_member_filters(
        Member.query.filter_by(gym_id=gid), search, status_filter, trainer_filter
    )
    members  = query.order_by(Member.first_name.asc()).all()
    trainers = User.query.filter_by(gym_id=gid, role='staff').order_by(User.name).all()

    return render_template(
        'members/index.html',
        members=members,
        trainers=trainers,
        search=search,
        status_filter=status_filter,
        trainer_filter=trainer_filter,
    )


@members_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    gid      = current_user.gym_id
    trainers = User.query.filter_by(gym_id=gid, role='staff').order_by(User.name).all()

    # Plan limit check
    gym = current_user.gym
    if not plan_within_member_limit(gym):
        tier = gym.plan_tier or 'starter'
        limit = PLANS[tier]['max_members']
        flash(f'Member limit reached ({limit} on {PLANS[tier]["label"]} plan). Upgrade to add more members.', 'danger')
        return redirect(url_for('members.index'))

    if request.method == 'POST':
        first_name  = request.form.get('first_name', '').strip()
        last_name   = request.form.get('last_name', '').strip()
        email       = request.form.get('email', '').strip()
        phone       = request.form.get('phone', '').strip()
        dob_str     = request.form.get('date_of_birth', '')
        joining_str = request.form.get('joining_date', '')
        trainer_id  = request.form.get('assigned_trainer_id') or None
        status      = request.form.get('status', 'active')
        notes       = request.form.get('notes', '').strip()

        if not first_name or not last_name or not joining_str:
            flash('First name, last name, and joining date are required.', 'danger')
            return render_template('members/new.html', trainers=trainers)

        dob     = date.fromisoformat(dob_str) if dob_str else None
        joining = date.fromisoformat(joining_str)

        member = Member(
            gym_id=gid,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            date_of_birth=dob,
            joining_date=joining,
            assigned_trainer_id=int(trainer_id) if trainer_id else None,
            status=status,
            notes=notes,
        )
        db.session.add(member)
        db.session.commit()
        flash(f'{member.full_name} has been added successfully.', 'success')
        return redirect(url_for('members.detail', member_id=member.id))

    return render_template('members/new.html', trainers=trainers, today=date.today().isoformat())


@members_bp.route('/<int:member_id>')
@login_required
def detail(member_id):
    member = Member.query.filter_by(id=member_id, gym_id=current_user.gym_id).first_or_404()
    return render_template('members/detail.html', member=member, today=date.today())


@members_bp.route('/<int:member_id>/face-id', methods=['POST'])
@login_required
def set_face_id(member_id):
    """Record the enrollment ID the gym's access-control terminal assigned
    this member, plus their consent to biometric processing. GYMPro never
    handles the actual face data — only this opaque mapping."""
    gym = current_user.gym
    if not gym or not gym.face_id_enabled:
        flash('Face ID is not enabled for this gym.', 'danger')
        return redirect(url_for('members.index'))

    member = Member.query.filter_by(id=member_id, gym_id=gym.id).first_or_404()
    action = request.form.get('action', '')

    if action == 'unenroll':
        member.face_id_external_id = None
        member.face_id_consent_at  = None
        db.session.commit()
        flash(f'Face ID enrollment removed for {member.full_name}.', 'info')
        return redirect(url_for('members.detail', member_id=member.id))

    external_id = request.form.get('external_id', '').strip()
    consent     = request.form.get('consent') == 'on'

    if not external_id:
        flash('Enter the ID the terminal assigned this member after enrolling their face on it.', 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    if not consent:
        flash("Member consent is required before storing a Face ID enrollment.", 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    member.face_id_external_id = external_id
    member.face_id_consent_at  = datetime.utcnow()
    db.session.commit()
    flash(f'Face ID enrollment saved for {member.full_name}.', 'success')
    return redirect(url_for('members.detail', member_id=member.id))


@members_bp.route('/<int:member_id>/whatsapp-optin', methods=['POST'])
@login_required
def set_whatsapp_optin(member_id):
    """Toggle WhatsApp consent for a member. Required before the gym can
    message them — WhatsApp Business Platform policy, not just courtesy."""
    gym = current_user.gym
    if not gym or not plan_has(gym, 'whatsapp'):
        flash('WhatsApp messaging is not available on this plan.', 'danger')
        return redirect(url_for('members.index'))

    member = Member.query.filter_by(id=member_id, gym_id=gym.id).first_or_404()
    action = request.form.get('action', '')

    if action == 'opt_out':
        member.whatsapp_opt_in    = False
        member.whatsapp_opt_in_at = None
        db.session.commit()
        flash(f'{member.full_name} opted out of WhatsApp messages.', 'info')
        return redirect(url_for('members.detail', member_id=member.id))

    if not member.phone:
        flash('Add a phone number for this member before opting them in to WhatsApp.', 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    member.whatsapp_opt_in    = True
    member.whatsapp_opt_in_at = datetime.utcnow()
    db.session.commit()
    flash(f'{member.full_name} opted in to WhatsApp messages.', 'success')
    return redirect(url_for('members.detail', member_id=member.id))


@members_bp.route('/<int:member_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(member_id):
    gid      = current_user.gym_id
    member   = Member.query.filter_by(id=member_id, gym_id=gid).first_or_404()
    trainers = User.query.filter_by(gym_id=gid, role='staff').order_by(User.name).all()

    if request.method == 'POST':
        member.first_name = request.form.get('first_name', '').strip()
        member.last_name  = request.form.get('last_name', '').strip()
        member.email      = request.form.get('email', '').strip()
        member.phone      = request.form.get('phone', '').strip()
        dob_str           = request.form.get('date_of_birth', '')
        joining_str       = request.form.get('joining_date', '')
        trainer_id        = request.form.get('assigned_trainer_id') or None
        member.status     = request.form.get('status', 'active')
        member.notes      = request.form.get('notes', '').strip()

        if not member.first_name or not member.last_name or not joining_str:
            flash('First name, last name, and joining date are required.', 'danger')
            return render_template('members/edit.html', member=member, trainers=trainers)

        member.date_of_birth        = date.fromisoformat(dob_str) if dob_str else None
        member.joining_date         = date.fromisoformat(joining_str)
        member.assigned_trainer_id  = int(trainer_id) if trainer_id else None

        db.session.commit()
        flash(f'{member.full_name} has been updated.', 'success')
        return redirect(url_for('members.detail', member_id=member.id))

    return render_template('members/edit.html', member=member, trainers=trainers)


@members_bp.route('/export.csv')
@login_required
def export_csv():
    gid            = current_user.gym_id
    search         = request.args.get('search', '').strip()
    status_filter  = request.args.get('status', '')
    trainer_filter = request.args.get('trainer', '')

    query = _apply_member_filters(
        Member.query.filter_by(gym_id=gid), search, status_filter, trainer_filter
    )
    members = query.order_by(Member.first_name).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'ID', 'First Name', 'Last Name', 'Email', 'Phone',
        'Date of Birth', 'Joining Date', 'Trainer', 'Status',
        'Active Plan', 'Plan Expires', 'Notes',
    ])
    for m in members:
        am = m.active_membership
        writer.writerow([
            m.id, m.first_name, m.last_name, m.email or '', m.phone or '',
            m.date_of_birth.isoformat() if m.date_of_birth else '',
            m.joining_date.isoformat(),
            m.trainer.name if m.trainer else '',
            m.status,
            am.plan.name if am else '',
            am.end_date.isoformat() if am else '',
            m.notes or '',
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=members.csv'},
    )


@members_bp.route('/<int:member_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin')
def delete(member_id):
    """Hard delete — wipes the member and everything hanging off them.

    For a DPDP deletion request use privacy.erase instead: that scrubs the
    personal data but keeps the billing rows the gym is legally required to
    retain. This route is for genuine mistakes (duplicate record, test data).
    """
    member = Member.query.filter_by(id=member_id, gym_id=current_user.gym_id).first_or_404()

    # Typing the name is the guard against an accidental irreversible delete.
    # Enforced here, not just in the browser — a client-side confirm() is a
    # convenience, never a control.
    typed = request.form.get('confirm_name', '').strip()
    if typed.lower() != member.full_name.lower():
        flash('The name you typed didn\'t match. Nothing was deleted.', 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    name = member.full_name

    # Every table with an FK to members.id has to be cleared first. Missing
    # any of these doesn't orphan the rows — SQLAlchemy tries to NULL the FK
    # on delete, and those columns are NOT NULL, so the whole request 500s.
    Attendance.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    Notification.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    WhatsAppMessage.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    MemberMembership.query.filter_by(member_id=member_id).delete(synchronize_session=False)

    # DataRequest is the DPDP compliance log — it carries member_name_snapshot
    # precisely so it outlives the member. Unlink rather than delete, so the
    # gym keeps its evidence that it responded to past requests.
    DataRequest.query.filter_by(member_id=member_id).update(
        {'member_id': None}, synchronize_session=False)

    db.session.delete(member)
    db.session.commit()

    flash(f'{name} and all their records have been permanently deleted.', 'info')
    return redirect(url_for('members.index'))
