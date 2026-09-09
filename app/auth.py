import base64
import io
import secrets
from datetime import datetime, timedelta

import pyotp
import qrcode
from flask import Blueprint, render_template, redirect, url_for, request, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash

from . import limiter
from .helpers import validate_password
from .models import db, User, TOTPBackupCode

auth_bp = Blueprint('auth', __name__)

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES    = 15
PENDING_2FA_WINDOW_MINUTES = 10
BACKUP_CODE_COUNT  = 10

_BACKUP_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # no 0/O/1/I/L — easy to type by hand


def _generate_backup_code():
    return '-'.join(''.join(secrets.choice(_BACKUP_ALPHABET) for _ in range(4)) for _ in range(2))


def _issue_backup_codes(user):
    """Wipes any existing codes and issues a fresh set of BACKUP_CODE_COUNT.
    Returns the plaintext codes — the ONLY moment they ever exist outside a
    hash. Caller is responsible for showing them to the user exactly once."""
    TOTPBackupCode.query.filter_by(user_id=user.id).delete()
    plaintext_codes = [_generate_backup_code() for _ in range(BACKUP_CODE_COUNT)]
    for code in plaintext_codes:
        db.session.add(TOTPBackupCode(
            user_id=user.id,
            code_hash=generate_password_hash(code, method='pbkdf2:sha256'),
        ))
    return plaintext_codes


def _complete_login(user, remember):
    user.reset_failed_logins()
    db.session.commit()

    # Read what we need out of the pre-login session, then throw the rest
    # away before establishing the authenticated one. Anything an attacker
    # managed to seed into the visitor's session — the classic session
    # fixation move — does not survive into their logged-in session.
    next_page = session.pop('pending_2fa_next', None)
    session.clear()

    login_user(user, remember=remember)
    flash(f'Welcome back, {user.name}!', 'success')
    if user.is_platform_admin:
        return redirect(next_page or url_for('operator.index'))
    return redirect(next_page or url_for('dashboard.index'))


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
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

        # Account-level lockout — applies regardless of which IP is attacking it
        if user and user.is_locked:
            minutes_left = max(1, int((user.locked_until - datetime.utcnow()).total_seconds() // 60) + 1)
            flash(f'This account is temporarily locked due to too many failed attempts. '
                  f'Try again in {minutes_left} minute{"s" if minutes_left != 1 else ""}.', 'danger')
            return render_template('auth/login.html')

        if user and check_password_hash(user.password_hash, password):
            if user.totp_enabled:
                # Password is correct, but don't call login_user() yet —
                # the session stays fully anonymous until the 2FA code also
                # checks out. See verify_2fa() below.
                session['pending_2fa_user_id'] = user.id
                session['pending_2fa_at']      = datetime.utcnow().isoformat()
                session['pending_2fa_remember'] = remember
                session['pending_2fa_next']     = request.args.get('next')
                return redirect(url_for('auth.verify_2fa'))
            return _complete_login(user, remember)

        if user:
            user.register_failed_login(max_attempts=MAX_LOGIN_ATTEMPTS, lockout_minutes=LOCKOUT_MINUTES)
            db.session.commit()
            if user.is_locked:
                flash(f'Too many failed attempts. This account is locked for {LOCKOUT_MINUTES} minutes.', 'danger')
                return render_template('auth/login.html')
            remaining = MAX_LOGIN_ATTEMPTS - user.failed_login_attempts
            flash(f'Invalid email or password. {remaining} attempt{"s" if remaining != 1 else ""} remaining.', 'danger')
        else:
            flash('Invalid email or password.', 'danger')

    return render_template('auth/login.html')


@auth_bp.route('/login/verify-2fa', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def verify_2fa():
    """The second step of login for accounts with 2FA on. Reached only via
    a valid password check in login() above — never a page you can just
    navigate to and try codes against, since there's no way to pick which
    user_id ends up in session without already knowing their password."""
    user_id = session.get('pending_2fa_user_id')
    pending_at_raw = session.get('pending_2fa_at')

    def _expire_pending():
        for key in ('pending_2fa_user_id', 'pending_2fa_at', 'pending_2fa_remember', 'pending_2fa_next'):
            session.pop(key, None)

    if not user_id or not pending_at_raw:
        return redirect(url_for('auth.login'))

    pending_at = datetime.fromisoformat(pending_at_raw)
    if datetime.utcnow() - pending_at > timedelta(minutes=PENDING_2FA_WINDOW_MINUTES):
        _expire_pending()
        flash('That login attempt expired. Please sign in again.', 'warning')
        return redirect(url_for('auth.login'))

    user = User.query.get(user_id)
    if not user or not user.totp_enabled:
        _expire_pending()
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        remember = bool(session.get('pending_2fa_remember'))

        totp = pyotp.TOTP(user.totp_secret)
        if code and totp.verify(code.replace(' ', ''), valid_window=1):
            _expire_pending()
            return _complete_login(user, remember)

        # Not a valid TOTP code — try it as a backup code instead
        matched = None
        for backup in TOTPBackupCode.query.filter_by(user_id=user.id, used_at=None).all():
            if check_password_hash(backup.code_hash, code.upper()):
                matched = backup
                break
        if matched:
            matched.used_at = datetime.utcnow()
            db.session.commit()
            _expire_pending()
            flash('Signed in with a backup code — generate new ones from Account Security when you get a chance.', 'warning')
            return _complete_login(user, remember)

        flash('That code is not valid. Check your authenticator app and try again.', 'danger')

    return render_template('auth/verify_2fa.html', user_name=user.name)


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/account/password', methods=['GET', 'POST'])
@limiter.limit('10 per hour', methods=['POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_pw  = request.form.get('current_password', '')
        new_pw      = request.form.get('new_password', '').strip()
        confirm_pw  = request.form.get('confirm_password', '').strip()

        if not check_password_hash(current_user.password_hash, current_pw):
            flash('Current password is incorrect.', 'danger')
            return render_template('auth/change_password.html')

        pw_error = validate_password(new_pw, email=current_user.email,
                                     name=current_user.name)
        if pw_error:
            flash(pw_error, 'danger')
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


@auth_bp.route('/account/2fa', methods=['GET'])
@login_required
def two_factor():
    return render_template('auth/two_factor.html')


@auth_bp.route('/account/2fa/setup', methods=['GET', 'POST'])
@login_required
def setup_2fa():
    if current_user.totp_enabled:
        return redirect(url_for('auth.two_factor'))

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        totp = pyotp.TOTP(current_user.totp_secret or '')
        if not current_user.totp_secret or not totp.verify(code.replace(' ', ''), valid_window=1):
            flash('That code didn\'t match. Scan the QR code again and try the current 6-digit code.', 'danger')
            return redirect(url_for('auth.setup_2fa'))

        current_user.totp_enabled     = True
        current_user.totp_enrolled_at = datetime.utcnow()
        plaintext_codes = _issue_backup_codes(current_user)
        db.session.commit()

        session['just_generated_backup_codes'] = plaintext_codes
        flash('Two-factor authentication is now on for your account.', 'success')
        return redirect(url_for('auth.backup_codes_display'))

    # Fresh secret every time this page loads (while not yet enabled) — an
    # abandoned setup attempt just gets discarded, no stale state to worry about.
    current_user.totp_secret = pyotp.random_base32()
    db.session.commit()

    uri = pyotp.TOTP(current_user.totp_secret).provisioning_uri(name=current_user.email, issuer_name='KriyaCore')

    buf = io.BytesIO()
    qrcode.make(uri).save(buf, format='PNG')
    qr_data_uri = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')

    return render_template('auth/setup_2fa.html', qr_data_uri=qr_data_uri, secret=current_user.totp_secret)


@auth_bp.route('/account/2fa/backup-codes')
@login_required
def backup_codes_display():
    """Shows a freshly (re)generated set of backup codes exactly once —
    popped from session immediately, so a refresh or back-button doesn't
    display them a second time, and nothing sensitive lingers in session."""
    codes = session.pop('just_generated_backup_codes', None)
    if not codes:
        return redirect(url_for('auth.two_factor'))
    return render_template('auth/backup_codes.html', codes=codes)


@auth_bp.route('/account/2fa/regenerate-backup-codes', methods=['POST'])
@login_required
def regenerate_backup_codes():
    if not current_user.totp_enabled:
        return redirect(url_for('auth.two_factor'))

    password = request.form.get('password', '')
    if not check_password_hash(current_user.password_hash, password):
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('auth.two_factor'))

    plaintext_codes = _issue_backup_codes(current_user)
    db.session.commit()
    session['just_generated_backup_codes'] = plaintext_codes
    flash('New backup codes generated — the old ones no longer work.', 'success')
    return redirect(url_for('auth.backup_codes_display'))


@auth_bp.route('/account/2fa/disable', methods=['POST'])
@limiter.limit('10 per hour', methods=['POST'])
@login_required
def disable_2fa():
    password = request.form.get('password', '')
    if not check_password_hash(current_user.password_hash, password):
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('auth.two_factor'))

    current_user.totp_enabled     = False
    current_user.totp_secret      = None
    current_user.totp_enrolled_at = None
    TOTPBackupCode.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    flash('Two-factor authentication has been turned off.', 'info')
    return redirect(url_for('auth.two_factor'))
