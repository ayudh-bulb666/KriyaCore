"""
DPDP data rights — access/portability and erasure.

Deliberately NOT plan-gated. Every other feature in GYMPro is gated by
subscription tier because it's a product perk; these are a legal obligation
the gym owes its members under the DPDP Act 2023 regardless of what they
pay us. A Starter-tier gym that couldn't honour an erasure request would be
non-compliant through our design choice, which isn't acceptable.

Requests are staff-mediated: GYMPro has no member login yet, so a member
asks the gym (in person, by phone, by email) and staff record + fulfil it
here. The DataRequest rows are the gym's evidence it responded.
"""
import json
from datetime import datetime

from flask import (Blueprint, render_template, redirect, url_for, request,
                   flash, Response, abort)
from flask_login import login_required, current_user

from .models import (db, Member, Attendance, Notification,
                     WhatsAppMessage, DataRequest)
from .helpers import role_required

privacy_bp = Blueprint('privacy', __name__, url_prefix='/<string:gym_slug>/privacy')


def build_member_export(member):
    """Everything GYMPro holds about one member, as a portable dict.

    Deliberately includes the derived/behavioural data (attendance, message
    history) as well as the profile they gave us — 'personal data' under
    DPDP is broader than just the signup form."""
    gym = member.gym
    return {
        'export_generated_at': datetime.utcnow().isoformat() + 'Z',
        'gym': {'name': gym.name, 'contact_email': gym.email, 'phone': gym.phone},
        'profile': {
            'member_id':     member.id,
            'first_name':    member.first_name,
            'last_name':     member.last_name,
            'email':         member.email,
            'phone':         member.phone,
            'date_of_birth': member.date_of_birth.isoformat() if member.date_of_birth else None,
            'joining_date':  member.joining_date.isoformat(),
            'status':        member.status,
            'assigned_trainer': member.trainer.name if member.trainer else None,
            'staff_notes':   member.notes,
            'record_created_at': member.created_at.isoformat() if member.created_at else None,
        },
        'consents': {
            'face_id': {
                'enrolled': bool(member.face_id_external_id),
                'consented_at': member.face_id_consent_at.isoformat() if member.face_id_consent_at else None,
                'note': ('Face images/templates are held by the gym\'s access-control device '
                         'vendor, not by GYMPro. GYMPro stores only the device-assigned ID.'),
            },
            'whatsapp': {
                'opted_in': member.whatsapp_opt_in,
                'opted_in_at': member.whatsapp_opt_in_at.isoformat() if member.whatsapp_opt_in_at else None,
            },
        },
        'memberships': [
            {
                'plan':           m.plan.name,
                'start_date':     m.start_date.isoformat(),
                'end_date':       m.end_date.isoformat(),
                'status':         m.status,
                'amount':         m.amount,
                'payment_status': m.payment_status,
                'payment_date':   m.payment_date.isoformat() if m.payment_date else None,
                'notes':          m.notes,
            }
            for m in member.memberships
        ],
        'attendance': [
            {
                'visited_at': a.visited_at.isoformat(),
                'source':     a.source,
                'notes':      a.notes,
            }
            for a in Attendance.query.filter_by(member_id=member.id)
                                     .order_by(Attendance.visited_at.desc()).all()
        ],
        'whatsapp_messages': [
            {
                'sent_at':      w.created_at.isoformat(),
                'message_type': w.message_type,
                'content':      w.body_preview,
                'status':       w.status,
            }
            for w in WhatsAppMessage.query.filter_by(member_id=member.id)
                                          .order_by(WhatsAppMessage.created_at.desc()).all()
        ],
    }


def erase_member(member, actor_name):
    """Scrub personal data in place, keeping the financial trail intact.

    NOT a row delete: MemberMembership rows are the gym's books of account
    and have to survive. Deleting the member would take them with it (or
    orphan them), so instead the member row stays as a pseudonymous stub
    that those financial records can still point at.

    Purely behavioural data — attendance, notifications, message history —
    has no such retention need, so it's genuinely deleted.
    """
    label = f'Erased Member #{member.id}'

    # Scrub identifying fields
    member.first_name    = 'Erased'
    member.last_name     = f'Member #{member.id}'
    member.email         = None
    member.phone         = None
    member.date_of_birth = None
    member.notes         = None
    member.status        = 'inactive'
    member.assigned_trainer_id = None

    # Withdraw consents alongside the data they authorised
    member.face_id_external_id = None
    member.face_id_consent_at  = None
    member.whatsapp_opt_in     = False
    member.whatsapp_opt_in_at  = None

    member.is_erased = True
    member.erased_at = datetime.utcnow()

    # Behavioural data — no retention obligation, so actually delete it
    Attendance.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    Notification.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    WhatsAppMessage.query.filter_by(member_id=member.id).delete(synchronize_session=False)

    db.session.commit()
    return label


@privacy_bp.route('/')
@login_required
@role_required('super_admin')
def index():
    gid = current_user.gym_id
    requests_log = (DataRequest.query.filter_by(gym_id=gid)
                    .order_by(DataRequest.requested_at.desc()).all())
    pending = [r for r in requests_log if r.status == 'pending']
    erased_count = Member.query.filter_by(gym_id=gid, is_erased=True).count()

    return render_template(
        'privacy/index.html',
        requests_log=requests_log,
        pending=pending,
        erased_count=erased_count,
    )


@privacy_bp.route('/request/<int:member_id>', methods=['POST'])
@login_required
@role_required('super_admin')
def log_request(member_id):
    """Record that a member asked for their data or asked to be erased.
    Logging the request and fulfilling it are separate steps on purpose —
    the gym may need to verify the requester's identity first."""
    member = Member.query.filter_by(id=member_id, gym_id=current_user.gym_id).first_or_404()
    request_type = request.form.get('request_type', '')

    if request_type not in ('export', 'erasure'):
        flash('Unknown request type.', 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    db.session.add(DataRequest(
        gym_id=current_user.gym_id,
        member_id=member.id,
        member_name_snapshot=member.full_name,
        request_type=request_type,
        notes=request.form.get('notes', '').strip() or None,
    ))
    db.session.commit()

    label = 'Data export' if request_type == 'export' else 'Erasure'
    flash(f'{label} request logged for {member.full_name}. Fulfil it from the Privacy page.', 'success')
    return redirect(url_for('privacy.index'))


@privacy_bp.route('/export/<int:member_id>')
@login_required
@role_required('super_admin')
def export_member(member_id):
    """Download everything GYMPro holds about this member as JSON — the
    machine-readable, portable format DPDP's portability right implies."""
    member = Member.query.filter_by(id=member_id, gym_id=current_user.gym_id).first_or_404()
    if member.is_erased:
        abort(404)  # nothing meaningful left to export

    payload = build_member_export(member)

    # Mark any pending export request for this member as fulfilled
    for req in DataRequest.query.filter_by(
            gym_id=current_user.gym_id, member_id=member.id,
            request_type='export', status='pending').all():
        req.status = 'completed'
        req.completed_at = datetime.utcnow()
        req.handled_by_name = current_user.name
    db.session.commit()

    safe_name = ''.join(c for c in member.full_name if c.isalnum() or c in ' -_').replace(' ', '-')
    return Response(
        json.dumps(payload, indent=2, ensure_ascii=False),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename=gympro-data-{safe_name}-{member.id}.json'},
    )


@privacy_bp.route('/erase/<int:member_id>', methods=['POST'])
@login_required
@role_required('super_admin')
def erase(member_id):
    member = Member.query.filter_by(id=member_id, gym_id=current_user.gym_id).first_or_404()

    if member.is_erased:
        flash(f'{member.full_name} has already been erased.', 'info')
        return redirect(url_for('privacy.index'))

    # Typing the name out is the guard against an accidental click on an
    # action that cannot be undone.
    typed = request.form.get('confirm_name', '').strip()
    if typed.lower() != member.full_name.lower():
        flash('The name you typed didn\'t match. Nothing was erased.', 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    original_name = member.full_name
    erase_member(member, current_user.name)

    for req in DataRequest.query.filter_by(
            gym_id=current_user.gym_id, member_id=member.id,
            request_type='erasure', status='pending').all():
        req.status = 'completed'
        req.completed_at = datetime.utcnow()
        req.handled_by_name = current_user.name
    db.session.commit()

    flash(f'{original_name}\'s personal data has been erased. Billing records were kept, '
          f'pseudonymised, as required for financial record-keeping.', 'success')
    return redirect(url_for('privacy.index'))
