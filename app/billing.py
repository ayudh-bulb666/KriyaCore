import csv
import io
from flask import Blueprint, render_template, redirect, url_for, request, flash, Response
from flask_login import login_required, current_user
from datetime import date, timedelta

from .models import db, Member, MemberMembership, MembershipPlan

billing_bp = Blueprint('billing', __name__, url_prefix='/billing')


@billing_bp.route('/')
@login_required
def index():
    gid           = current_user.gym_id
    status_filter = request.args.get('status', 'all')
    today         = date.today()
    week_later    = today + timedelta(days=7)

    query = MemberMembership.query.filter_by(gym_id=gid)

    if status_filter == 'active':
        query = query.filter(
            MemberMembership.status   == 'active',
            MemberMembership.end_date >  today,
        )
    elif status_filter == 'expiring':
        query = query.filter(
            MemberMembership.status   == 'active',
            MemberMembership.end_date >= today,
            MemberMembership.end_date <= week_later,
        )
    elif status_filter == 'expired':
        query = query.filter_by(status='expired')
    elif status_filter == 'pending':
        query = query.filter(
            MemberMembership.status         == 'active',
            MemberMembership.payment_status == 'pending',
        )
    elif status_filter == 'overdue':
        query = query.filter_by(payment_status='overdue')

    memberships = query.order_by(MemberMembership.end_date.asc()).all()

    base = MemberMembership.query.filter_by(gym_id=gid)
    counts = {
        'all':      base.count(),
        'active':   base.filter(MemberMembership.status == 'active',
                                MemberMembership.end_date > today).count(),
        'expiring': MemberMembership.query.filter(
                        MemberMembership.gym_id   == gid,
                        MemberMembership.status   == 'active',
                        MemberMembership.end_date >= today,
                        MemberMembership.end_date <= week_later,
                    ).count(),
        'expired':  MemberMembership.query.filter_by(gym_id=gid, status='expired').count(),
        'pending':  MemberMembership.query.filter(
                        MemberMembership.gym_id         == gid,
                        MemberMembership.status         == 'active',
                        MemberMembership.payment_status == 'pending',
                    ).count(),
        'overdue':  MemberMembership.query.filter_by(gym_id=gid, payment_status='overdue').count(),
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

    query = MemberMembership.query.filter_by(gym_id=gid)
    if status_filter == 'active':
        query = query.filter(MemberMembership.status == 'active', MemberMembership.end_date > today)
    elif status_filter == 'expiring':
        query = query.filter(
            MemberMembership.status   == 'active',
            MemberMembership.end_date >= today,
            MemberMembership.end_date <= week_later,
        )
    elif status_filter == 'expired':
        query = query.filter_by(status='expired')
    elif status_filter == 'pending':
        query = query.filter(MemberMembership.status == 'active',
                             MemberMembership.payment_status == 'pending')
    elif status_filter == 'overdue':
        query = query.filter_by(payment_status='overdue')

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

        plan       = MembershipPlan.query.filter_by(id=int(plan_id), gym_id=gid).first_or_404()
        start_date = date.fromisoformat(start_str)
        end_date   = start_date + timedelta(days=plan.duration_days)

        mem = MemberMembership(
            gym_id=gid,
            member_id=int(member_id),
            plan_id=plan.id,
            start_date=start_date,
            end_date=end_date,
            status='active',
            payment_status=payment_status,
            amount=plan.price,
            payment_date=date.today() if payment_status == 'paid' else None,
            notes=notes,
        )
        db.session.add(mem)
        db.session.commit()

        member = Member.query.get(int(member_id))
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
        amount=plan.price,
    )
    db.session.add(new_mem)
    db.session.commit()

    flash(f'Membership renewed for {old.member.full_name} until {new_end.strftime("%b %d, %Y")}.', 'success')
    return redirect(url_for('billing.index'))
