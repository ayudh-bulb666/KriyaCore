from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from .models import db, User
from .helpers import role_required
from .plans import plan_within_staff_limit, PLANS

staff_bp = Blueprint('staff', __name__, url_prefix='/<string:gym_slug>/staff')


@staff_bp.route('/')
@login_required
@role_required('super_admin')
def index():
    gid   = current_user.gym_id
    staff = User.query.filter_by(gym_id=gid, role='staff').order_by(User.name).all()
    return render_template('staff/index.html', staff=staff)


@staff_bp.route('/new', methods=['GET', 'POST'])
@login_required
@role_required('super_admin')
def new():
    # Plan limit check
    gym = current_user.gym
    if not plan_within_staff_limit(gym):
        tier = gym.plan_tier or 'starter'
        limit = PLANS[tier]['max_staff']
        flash(f'Staff limit reached ({limit} on {PLANS[tier]["label"]} plan). Upgrade to add more staff.', 'danger')
        return redirect(url_for('staff.index'))

    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not name or not email or not password:
            flash('Name, email, and password are required.', 'danger')
            return render_template('staff/new.html')

        if User.query.filter_by(email=email).first():
            flash('A user with this email already exists.', 'danger')
            return render_template('staff/new.html')

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
            role='staff',
            gym_id=current_user.gym_id,
        )
        db.session.add(user)
        db.session.commit()
        flash(f'Staff member {name} has been added.', 'success')
        return redirect(url_for('staff.index'))

    return render_template('staff/new.html')


@staff_bp.route('/<int:user_id>/reset-password', methods=['POST'])
@login_required
@role_required('super_admin')
def reset_password(user_id):
    user = User.query.filter_by(id=user_id, gym_id=current_user.gym_id, role='staff').first_or_404()
    new_pw  = request.form.get('new_password', '').strip()
    confirm = request.form.get('confirm_password', '').strip()

    if len(new_pw) < 8:
        flash('Password must be at least 8 characters.', 'danger')
        return redirect(url_for('staff.index'))
    if new_pw != confirm:
        flash('Passwords do not match.', 'danger')
        return redirect(url_for('staff.index'))

    user.password_hash = generate_password_hash(new_pw, method='pbkdf2:sha256')
    db.session.commit()
    flash(f'Password reset for {user.name}.', 'success')
    return redirect(url_for('staff.index'))


@staff_bp.route('/<int:user_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin')
def delete(user_id):
    user = User.query.filter_by(id=user_id, gym_id=current_user.gym_id).first_or_404()
    if user.role in ('super_admin', 'platform_admin'):
        flash('Cannot delete an admin account.', 'danger')
        return redirect(url_for('staff.index'))

    for member in user.assigned_members:
        member.assigned_trainer_id = None

    name = user.name
    db.session.delete(user)
    db.session.commit()
    flash(f'{name} has been removed from staff.', 'info')
    return redirect(url_for('staff.index'))
