import csv
import io
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, Response
from flask_login import login_required, current_user
from datetime import date

from .models import db, Member, User, MemberMembership
from .helpers import role_required
from .plans import plan_within_member_limit, PLANS

members_bp = Blueprint('members', __name__, url_prefix='/members')


@members_bp.route('/')
@login_required
def index():
    gid            = current_user.gym_id
    search         = request.args.get('search', '').strip()
    status_filter  = request.args.get('status', '')
    trainer_filter = request.args.get('trainer', '')

    query = Member.query.filter_by(gym_id=gid)

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

    query = Member.query.filter_by(gym_id=gid)
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(
            Member.first_name.ilike(like), Member.last_name.ilike(like),
            Member.email.ilike(like), Member.phone.ilike(like),
        ))
    if status_filter:
        query = query.filter_by(status=status_filter)
    if trainer_filter:
        if trainer_filter == 'none':
            query = query.filter(Member.assigned_trainer_id.is_(None))
        else:
            query = query.filter_by(assigned_trainer_id=int(trainer_filter))

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
    member = Member.query.filter_by(id=member_id, gym_id=current_user.gym_id).first_or_404()
    name   = member.full_name

    MemberMembership.query.filter_by(member_id=member_id).delete()
    db.session.delete(member)
    db.session.commit()

    flash(f'{name} has been removed.', 'info')
    return redirect(url_for('members.index'))
