import csv
import io
from flask import Blueprint, render_template, redirect, url_for, request, flash, Response, abort
from flask_login import login_required, current_user
from datetime import date, timedelta
from xhtml2pdf import pisa

from .helpers import own_gym_id
from .models import db, Member, MemberMembership, MembershipPlan
from .plans import plan_has
from .whatsapp import send_and_log

billing_bp = Blueprint('billing', __name__, url_prefix='/<string:gym_slug>/billing')


def _apply_status_filter(query, status_filter, today, week_later):
    """Shared by index() and export_csv() so the filter tabs stay in sync."""
    if status_filter == 'active':
        return query.filter(
            MemberMembership.status   == 'active',
            MemberMembership.end_date >  today,
        )
    if status_filter == 'expiring':
        return query.filter(
            MemberMembership.status   == 'active',
            MemberMembership.end_date >= today,
            MemberMembership.end_date <= week_later,
        )
    if status_filter == 'expired':
        return query.filter_by(status='expired')
    if status_filter == 'pending':
        return query.filter(
            MemberMembership.status         == 'active',
            MemberMembership.payment_status == 'pending',
        )
    if status_filter == 'overdue':
        return query.filter_by(payment_status='overdue')
    return query  # 'all'


@billing_bp.route('/')
@login_required
def index():
    gid           = current_user.gym_id
    status_filter = request.args.get('status', 'all')
    today         = date.today()
    week_later    = today + timedelta(days=7)

    base_query  = MemberMembership.query.filter_by(gym_id=gid)
    query       = _apply_status_filter(base_query, status_filter, today, week_later)
    memberships = query.order_by(MemberMembership.end_date.asc()).all()

    counts = {
        key: _apply_status_filter(
            MemberMembership.query.filter_by(gym_id=gid), key, today, week_later
        ).count()
        for key in ('all', 'active', 'expiring', 'expired', 'pending', 'overdue')
    }

    return render_template(
        'billing/index.html',
        memberships=memberships,
        status_filter=status_filter,
        counts=counts,
        today=today,
    )


@billing_bp.route('/export.csv')
@login_required
def export_csv():
    gid           = current_user.gym_id
    status_filter = request.args.get('status', 'all')
    today         = date.today()
    week_later    = today + timedelta(days=7)

    base_query  = MemberMembership.query.filter_by(gym_id=gid)
    query       = _apply_status_filter(base_query, status_filter, today, week_later)
    memberships = query.order_by(MemberMembership.end_date).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'ID', 'Member', 'Email', 'Plan', 'Start Date', 'End Date',
        'Days Remaining', 'Amount (₹)', 'Membership Status',
        'Payment Status', 'Payment Date',
    ])
    for m in memberships:
        writer.writerow([
            m.id,
            m.member.full_name,
            m.member.email or '',
            m.plan.name,
            m.start_date.isoformat(),
            m.end_date.isoformat(),
            m.days_remaining,
            f'{m.amount:.2f}',
            m.status,
            m.payment_status,
            m.payment_date.isoformat() if m.payment_date else '',
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=billing_{status_filter}.csv'},
    )


def _parse_amount(raw, fallback):
    """Read a rupee amount off a form. Blank means 'use the plan price'.

    Returns (amount, error). A bad value is rejected rather than quietly
    coerced — silently turning a typo into ₹0 would corrupt the revenue
    figures the owner makes decisions on.
    """
    raw = (raw or '').strip().replace(',', '')
    if not raw:
        return fallback, None
    try:
        value = float(raw)
    except ValueError:
        return None, 'Amount must be a number.'
    if value < 0:
        return None, 'Amount cannot be negative.'
    return round(value, 2), None


@billing_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    gid     = current_user.gym_id
    members = Member.query.filter_by(gym_id=gid, status='active').order_by(Member.first_name).all()
    plans   = MembershipPlan.query.filter_by(gym_id=gid).order_by(MembershipPlan.price).all()

    if request.method == 'POST':
        member_id      = request.form.get('member_id')
        plan_id        = request.form.get('plan_id')
        start_str      = request.form.get('start_date', '')
        payment_status = request.form.get('payment_status', 'pending')
        notes          = request.form.get('notes', '').strip()

        if not member_id or not plan_id or not start_str:
            flash('Member, plan, and start date are required.', 'danger')
            return render_template('billing/new.html', members=members, plans=plans)

        plan = MembershipPlan.query.filter_by(id=int(plan_id), gym_id=gid).first_or_404()

        # The member has to belong to this gym. The dropdown only offers our
        # own members, but the POST can carry any id — without this check a
        # gym could file a billing row against another gym's member, and that
        # member's name would then show up on this gym's billing page.
        safe_member_id = own_gym_id(Member, member_id, gid)
        if safe_member_id is None:
            flash('That member could not be found in this gym.', 'danger')
            return render_template('billing/new.html', members=members, plans=plans,
                                   today=date.today().isoformat())

        start_date = date.fromisoformat(start_str)
        end_date   = start_date + timedelta(days=plan.duration_days)

        # The plan price is the starting point, not the rule — corporate rates,
        # referral discounts and part-payments are normal, so what the member
        # actually agreed to is stored on the membership itself.
        amount, error = _parse_amount(request.form.get('amount'), plan.price)
        if error:
            flash(error, 'danger')
            return render_template('billing/new.html', members=members, plans=plans,
                                   today=date.today().isoformat())

        mem = MemberMembership(
            gym_id=gid,
            member_id=safe_member_id,
            plan_id=plan.id,
            start_date=start_date,
            end_date=end_date,
            status='active',
            payment_status=payment_status,
            amount=amount,
            payment_date=date.today() if payment_status == 'paid' else None,
            notes=notes,
        )
        db.session.add(mem)
        db.session.commit()

        member = db.session.get(Member, safe_member_id)
        flash(f'Membership assigned to {member.full_name} successfully.', 'success')
        return redirect(url_for('billing.index'))

    return render_template(
        'billing/new.html', members=members, plans=plans,
        today=date.today().isoformat()
    )


@billing_bp.route('/<int:membership_id>/pay', methods=['POST'])
@login_required
def pay(membership_id):
    mem = MemberMembership.query.filter_by(id=membership_id, gym_id=current_user.gym_id).first_or_404()
    mem.payment_status = 'paid'
    mem.payment_date   = date.today()
    db.session.commit()

    gym = current_user.gym
    if plan_has(gym, 'whatsapp'):
        send_and_log(gym, mem.member, 'renewal_confirmation', 'renewal_confirmation', {
            'member_name':     mem.member.first_name,
            'gym_name':        gym.name,
            'plan_name':       mem.plan.name,
            'new_expiry_date': mem.end_date.strftime('%d %b %Y'),
        })

    flash(f'Payment recorded for {mem.member.full_name}.', 'success')
    return redirect(request.referrer or url_for('billing.index'))


@billing_bp.route('/<int:membership_id>/renew', methods=['POST'])
@login_required
def renew(membership_id):
    old  = MemberMembership.query.filter_by(id=membership_id, gym_id=current_user.gym_id).first_or_404()
    plan = old.plan

    today     = date.today()
    new_start = max(today, old.end_date + timedelta(days=1))
    new_end   = new_start + timedelta(days=plan.duration_days)

    if old.status == 'active':
        old.status = 'expired'

    new_mem = MemberMembership(
        gym_id=current_user.gym_id,
        member_id=old.member_id,
        plan_id=plan.id,
        start_date=new_start,
        end_date=new_end,
        status='active',
        payment_status='pending',
        # Carry the last agreed amount forward, not the plan's list price.
        # If this member was on a negotiated rate, renewing them shouldn't
        # silently put the price back up — that surfaces as an argument at
        # the desk. Staff can still change it on the renewed row.
        amount=old.amount,
    )
    db.session.add(new_mem)
    db.session.commit()

    flash(f'Membership renewed for {old.member.full_name} until {new_end.strftime("%b %d, %Y")} '
          f'at ₹{new_mem.amount:.0f}.', 'success')
    return redirect(url_for('billing.index'))


@billing_bp.route('/<int:membership_id>/amount', methods=['POST'])
@login_required
def set_amount(membership_id):
    """Change what a member is charged for an existing membership."""
    mem = MemberMembership.query.filter_by(
        id=membership_id, gym_id=current_user.gym_id).first_or_404()

    amount, error = _parse_amount(request.form.get('amount'), mem.amount)
    if error:
        flash(error, 'danger')
        return redirect(request.referrer or url_for('billing.index'))

    if amount == mem.amount:
        return redirect(request.referrer or url_for('billing.index'))

    was = mem.amount
    mem.amount = amount
    db.session.commit()

    flash(f'{mem.member.full_name}\'s {mem.plan.name} amount changed '
          f'from ₹{was:.0f} to ₹{amount:.0f}.', 'success')
    return redirect(request.referrer or url_for('billing.index'))


@billing_bp.route('/<int:membership_id>/invoice.pdf')
@login_required
def invoice(membership_id):
    mem = MemberMembership.query.filter_by(id=membership_id, gym_id=current_user.gym_id).first_or_404()
    if mem.payment_status != 'paid':
        abort(404)  # no invoice for a payment that hasn't happened yet

    gym            = current_user.gym
    invoice_number = f'{gym.slug.upper().replace("-", "")}-{mem.id:05d}'

    html = render_template(
        'billing/invoice.html',
        membership=mem,
        gym=gym,
        invoice_number=invoice_number,
        issue_date=mem.payment_date or date.today(),
    )

    pdf_buffer = io.BytesIO()
    result = pisa.CreatePDF(html, dest=pdf_buffer)
    if result.err:
        flash('Could not generate the invoice PDF. Please try again.', 'danger')
        return redirect(request.referrer or url_for('billing.index'))

    return Response(
        pdf_buffer.getvalue(),
        mimetype='application/pdf',
        headers={'Content-Disposition': f'inline; filename=invoice-{invoice_number}.pdf'},
    )
