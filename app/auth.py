from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash

from .models import db, User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if current_user.is_platform_admin:
            return redirect(url_for('operator.index'))
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            flash(f'Welcome back, {user.name}!', 'success')
            if user.is_platform_admin:
                return redirect(next_page or url_for('operator.index'))
            return redirect(next_page or url_for('dashboard.index'))

        flash('Invalid email or password.', 'danger')

    return render_template('auth/login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/account/password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_pw  = request.form.get('current_password', '')
        new_pw      = request.form.get('new_password', '').strip()
        confirm_pw  = request.form.get('confirm_password', '').strip()

        if not check_password_hash(current_user.password_hash, current_pw):
            flash('Current password is incorrect.', 'danger')
            return render_template('auth/change_password.html')

        if len(new_pw) < 8:
            flash('New password must be at least 8 characters.', 'danger')
            return render_template('auth/change_password.html')

        if new_pw != confirm_pw:
            flash('New passwords do not match.', 'danger')
            return render_template('auth/change_password.html')

        current_user.password_hash = generate_password_hash(new_pw, method='pbkdf2:sha256')
        db.session.commit()
        flash('Password changed successfully.', 'success')

        # Redirect back to where the user came from
        if current_user.is_platform_admin:
            return redirect(url_for('operator.index'))
        return redirect(url_for('dashboard.index'))

    return render_template('auth/change_password.html')
