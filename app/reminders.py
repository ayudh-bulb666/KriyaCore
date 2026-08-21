import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, current_app, request)
from flask_login import login_required, current_user
from datetime import date, timedelta

from .models import MemberMembership
from .helpers import role_required

reminders_bp = Blueprint('reminders', __name__, url_prefix='/reminders')


# ── Helpers ────────────────────────────────────────────────────────────────────

def _mail_config():
    return {
        'server':   current_app.config.get('MAIL_SERVER', ''),
        'port':     int(current_app.config.get('MAIL_PORT', 587)),
        'use_tls':  current_app.config.get('MAIL_USE_TLS', True),
        'username': current_app.config.get('MAIL_USERNAME', ''),
        'password': current_app.config.get('MAIL_PASSWORD', ''),
        'sender':   current_app.config.get('MAIL_DEFAULT_SENDER', 'GYMPro <noreply@gympro.com>'),
    }


def _is_configured():
    return bool(
        current_app.config.get('MAIL_SERVER') and
        current_app.config.get('MAIL_USERNAME')
    )


def _send_one(subject, to_email, html_body, cfg):
    """Send a single HTML email via SMTP. Raises on failure."""
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = cfg['sender']
    msg['To']      = to_email
    msg.attach(MIMEText(html_body, 'html'))

    with smtplib.SMTP(cfg['server'], cfg['port'], timeout=10) as server:
        if cfg['use_tls']:
            server.starttls()
        if cfg['username']:
            server.login(cfg['username'], cfg['password'])
        server.sendmail(cfg['sender'], to_email, msg.as_string())


def _expiring_memberships():
    from flask_login import current_user
    today      = date.today()
    week_later = today + timedelta(days=7)
    return (MemberMembership.query
            .filter(
                MemberMembership.gym_id   == current_user.gym_id,
                MemberMembership.status   == 'active',
                MemberMembership.end_date >= today,
                MemberMembership.end_date <= week_later,
            )
            .order_by(MemberMembership.end_date)
            .all())


# ── Routes ─────────────────────────────────────────────────────────────────────

@reminders_bp.route('/')
@login_required
@role_required('super_admin')
def index():
    expiring  = _expiring_memberships()
    can_email = [m for m in expiring if m.member.email]
    no_email  = [m for m in expiring if not m.member.email]

    return render_template(
        'reminders/index.html',
        can_email=can_email,
        no_email=no_email,
        email_configured=_is_configured(),
        mail_config=_mail_config(),
        today=date.today(),
    )


@reminders_bp.route('/send', methods=['POST'])
@login_required
@role_required('super_admin')
def send():
    if not _is_configured():
        flash('Email is not configured. Set MAIL_SERVER and MAIL_USERNAME first.', 'danger')
        return redirect(url_for('reminders.index'))

    cfg      = _mail_config()
    expiring = _expiring_memberships()
    targets  = [m for m in expiring if m.member.email]

    if not targets:
        flash('No members with email addresses are expiring this week.', 'warning')
        return redirect(url_for('reminders.index'))

    sent, failed = 0, 0

    for mem in targets:
        days_left = mem.days_remaining
        if days_left == 0:
            day_phrase = 'today'
        elif days_left == 1:
            day_phrase = 'tomorrow'
        else:
            day_phrase = f'in {days_left} days'

        subject   = f'⏰ Your GYMPro membership expires {day_phrase}'
        html_body = render_template(
            'reminders/email.html',
            membership=mem,
            days_left=days_left,
            day_phrase=day_phrase,
        )
        try:
            _send_one(subject, mem.member.email, html_body, cfg)
            sent += 1
        except Exception as e:
            failed += 1
            current_app.logger.error(f'Reminder email failed for {mem.member.email}: {e}')

    if sent:
        flash(f'✅ {sent} reminder email{"s" if sent != 1 else ""} sent successfully.', 'success')
    if failed:
        flash(f'❌ {failed} email{"s" if failed != 1 else ""} failed — check the server logs.', 'danger')

    return redirect(url_for('reminders.index'))


@reminders_bp.route('/test', methods=['POST'])
@login_required
@role_required('super_admin')
def test_email():
    """Send a test email to the currently logged-in admin to verify SMTP config."""
    if not _is_configured():
        flash('Email is not configured.', 'danger')
        return redirect(url_for('reminders.index'))

    cfg = _mail_config()
    try:
        html_body = render_template('reminders/test_email.html', user=current_user)
        _send_one(
            subject='GYMPro — SMTP Test ✅',
            to_email=current_user.email,
            html_body=html_body,
            cfg=cfg,
        )
        flash(f'Test email sent to {current_user.email}. Check your inbox.', 'success')
    except Exception as e:
        flash(f'Test failed: {e}', 'danger')

    return redirect(url_for('reminders.index'))
