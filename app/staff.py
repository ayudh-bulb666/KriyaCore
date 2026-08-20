from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required
from werkzeug.security import generate_password_hash

from .models import db, User
from .helpers import role_required

staff_bp = Blueprint('staff', __name__, url_prefix='/staff')


@staff_bp.route('/')
@login_required
@role_required('super_admin')
def index():
    staff = User.query.filter_by(role='staff').order_by(User.name).all()
    return render_template('staff/index.html', staff=staff)


@staff_bp.route('/new', methods=['GET', 'POST'])
@login_required
@role_required('super_admin')
def new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
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
        )
        db.session.add(user)
        db.session.commit()
        flash(f'Staff member {name} has been added.', 'success')
        return redirect(url_for('staff.index'))

    return render_template('staff/new.html')


@staff_bp.route('/<int:user_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin')
def delete(user_id):
    user = User.query.get_or_404(user_id)
    if user.role == 'super_admin':
        flash('Cannot delete a super admin account.', 'danger')
        return redirect(url_for('staff.index'))

    # Unassign members
    for member in user.assigned_members:
        member.assigned_trainer_id = None

    name = user.name
    db.session.delete(user)
    db.session.commit()
    flash(f'{name} has been removed from staff.', 'info')
    return redirect(url_for('staff.index'))
