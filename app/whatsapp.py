from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user

from .models import db, Member, MemberMembership, WhatsAppMessage, WhatsAppSettings
from .helpers import role_required
from .whatsapp_provider import TEMPLATES, is_configured, get_template_text, render_preview, send_whatsapp_template

whatsapp_bp = Blueprint('whatsapp', __name__, url_prefix='/<string:gym_slug>/whatsapp')


def get_or_create_settings(gym):
    """Every gym gets sensible defaults (auto-send off, 7 days, built-in
    wording) without needing a row until they actually open Settings."""
    settings = gym.whatsapp_settings
    if settings is None:
        settings = WhatsAppSettings(gym_id=gym.id)
        db.session.add(settings)
        db.session.commit()
    return settings


def send_and_log(gym, member, message_type, template_name, variables):
    """Shared by every trigger — manual buttons, the auto-reminder cron, and
    the billing.pay() hook. Always leaves an audit row behind, whether the
    message actually went out or not."""
    if not member.whatsapp_reachable:
        record = WhatsAppMessage(
            gym_id=gym.id, member_id=member.id,
            message_type=message_type, template_name=template_name,
            body_preview=render_preview(gym, template_name, variables),
            status='skipped_no_optin',
        )
        db.session.add(record)
        db.session.commit()
        return record

    status, provider_message_id, error = send_whatsapp_template(member.phone, template_name, variables)
    record = WhatsAppMessage(
        gym_id=gym.id, member_id=member.id,
        message_type=message_type, template_name=template_name,
        body_preview=render_preview(gym, template_name, variables),
        status=status,
        provider_message_id=provider_message_id,
        error_message=error,
        sent_at=datetime.utcnow() if status == 'sent' else None,
    )
    db.session.add(record)
    db.session.commit()
    return record


def run_expiry_reminders(gym):
    """The actual send loop — called by the manual button below and by the
    daily cron endpoint (app/cron.py). Only reminds a given membership once
    per billing cycle (see MemberMembership.reminder_sent_at); reachable vs
    not-yet-opted-in members are still logged either way for visibility."""
    settings = get_or_create_settings(gym)
    expiring = (MemberMembership.expiring_soon_query(gym_id=gym.id, days=settings.remind_days_before)
                .filter(MemberMembership.reminder_sent_at.is_(None))
                .order_by(MemberMembership.end_date.asc())
                .all())

    sent = skipped = 0
    for mem in expiring:
        member = mem.member
        variables = {
            'member_name': member.first_name,
            'gym_name':    gym.name,
            'plan_name':   mem.plan.name,
            'expiry_date': mem.end_date.strftime('%d %b %Y'),
            'days_left':   str(mem.days_remaining),
        }
        record = send_and_log(gym, member, 'expiry_reminder', 'expiry_reminder', variables)
        if record.status in ('sent', 'queued_no_provider'):
            mem.reminder_sent_at = datetime.utcnow()
            sent += 1
        else:
            skipped += 1
    db.session.commit()
    return sent, skipped


@whatsapp_bp.route('/')
@login_required
@role_required('super_admin')
def index():
    gym = current_user.gym
    gid = gym.id
    settings = get_or_create_settings(gym)

    opted_in_count = Member.query.filter_by(gym_id=gid, whatsapp_opt_in=True).count()
    total_members  = Member.query.filter_by(gym_id=gid, status='active').count()

    recent = (WhatsAppMessage.query
              .filter_by(gym_id=gid)
              .order_by(WhatsAppMessage.created_at.desc())
              .limit(30).all())

    expiring = (MemberMembership.expiring_soon_query(gym_id=gid, days=settings.remind_days_before)
                .filter(MemberMembership.reminder_sent_at.is_(None))
                .order_by(MemberMembership.end_date.asc()).all())
    expiring_reachable = [m for m in expiring if m.member.whatsapp_reachable]

    return render_template(
        'whatsapp/index.html',
        provider_configured=is_configured(),
        settings=settings,
        opted_in_count=opted_in_count,
        total_members=total_members,
        recent=recent,
        expiring=expiring,
        expiring_reachable=expiring_reachable,
        templates=TEMPLATES,
    )


@whatsapp_bp.route('/send-expiry-reminders', methods=['POST'])
@login_required
@role_required('super_admin')
def send_expiry_reminders():
    gym = current_user.gym
    sent, skipped = run_expiry_reminders(gym)

    if is_configured():
        flash(f'{sent} expiry reminder{"s" if sent != 1 else ""} sent, {skipped} skipped (no opt-in or failed).', 'success')
    else:
        flash(f'No WhatsApp provider connected yet — {sent} reminder(s) logged as queued, {skipped} skipped. '
              f'Set WHATSAPP_PROVIDER once a provider is chosen.', 'warning')
    return redirect(url_for('whatsapp.index'))


@whatsapp_bp.route('/broadcast', methods=['GET', 'POST'])
@login_required
@role_required('super_admin')
def broadcast():
    gym = current_user.gym
    reachable_members = (Member.query
                          .filter_by(gym_id=gym.id, whatsapp_opt_in=True, status='active')
                          .order_by(Member.first_name).all())

    if request.method == 'POST':
        kind    = request.form.get('kind', 'event')       # 'closure' or 'event'
        message = request.form.get('message', '').strip()
        target  = request.form.get('target', 'all')
        member_ids = request.form.getlist('member_ids', type=int)

        if not message:
            flash('Write a message before sending.', 'danger')
            return render_template('whatsapp/broadcast.html', members=reachable_members, form=request.form)

        targets = reachable_members if target == 'all' else [m for m in reachable_members if m.id in member_ids]
        if not targets:
            flash('No recipients selected (or no members have opted in to WhatsApp yet).', 'danger')
            return render_template('whatsapp/broadcast.html', members=reachable_members, form=request.form)

        sent = skipped = 0
        for member in targets:
            variables = {'member_name': member.first_name, 'gym_name': gym.name, 'message_body': message}
            record = send_and_log(gym, member, kind, 'announcement', variables)
            if record.status == 'sent':
                sent += 1
            else:
                skipped += 1

        if is_configured():
            flash(f'Sent to {sent} member{"s" if sent != 1 else ""}, {skipped} skipped.', 'success')
        else:
            flash(f'No WhatsApp provider connected yet — logged for {sent + skipped} member(s), none actually sent.', 'warning')
        return redirect(url_for('whatsapp.index'))

    return render_template('whatsapp/broadcast.html', members=reachable_members, form={})


@whatsapp_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@role_required('super_admin')
def settings():
    gym = current_user.gym
    wa_settings = get_or_create_settings(gym)

    if request.method == 'POST':
        wa_settings.auto_expiry_reminders_enabled = request.form.get('auto_enabled') == 'on'

        try:
            days = int(request.form.get('remind_days_before', 7))
        except ValueError:
            days = 7
        wa_settings.remind_days_before = max(1, min(days, 30))

        expiry_text  = request.form.get('expiry_reminder_template', '').strip()
        renewal_text = request.form.get('renewal_confirmation_template', '').strip()
        wa_settings.expiry_reminder_template      = expiry_text or None
        wa_settings.renewal_confirmation_template = renewal_text or None

        db.session.commit()
        flash('WhatsApp settings saved.', 'success')
        return redirect(url_for('whatsapp.settings'))

    preview_vars = {
        'member_name': 'Aarav', 'gym_name': gym.name, 'plan_name': 'Quarterly',
        'expiry_date': '31 Aug 2026', 'days_left': str(wa_settings.remind_days_before),
        'new_expiry_date': '30 Nov 2026',
    }

    return render_template(
        'whatsapp/settings.html',
        settings=wa_settings,
        templates=TEMPLATES,
        expiry_default=get_template_text(None, 'expiry_reminder'),
        renewal_default=get_template_text(None, 'renewal_confirmation'),
        expiry_preview=render_preview(gym, 'expiry_reminder', preview_vars),
        renewal_preview=render_preview(gym, 'renewal_confirmation', preview_vars),
        provider_configured=is_configured(),
    )
