from flask import Blueprint, render_template
from flask_login import login_required, current_user
from datetime import date, datetime, timedelta
from sqlalchemy import func

from .models import db, Member, MemberMembership, MembershipPlan, Attendance

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/')
@login_required
def index():
    gid        = current_user.gym_id
    today      = date.today()
    week_later = today + timedelta(days=7)
    month_start = today.replace(day=1)

    # ── Stats ──────────────────────────────────────────────────────────────────
    total_members  = Member.query.filter_by(gym_id=gid).count()
    active_members = Member.query.filter_by(gym_id=gid, status='active').count()

    new_this_month = Member.query.filter(
        Member.gym_id == gid,
        Member.joining_date >= month_start,
    ).count()

    expiring_soon_count = MemberMembership.query.filter(
        MemberMembership.gym_id   == gid,
        MemberMembership.status   == 'active',
        MemberMembership.end_date >= today,
        MemberMembership.end_date <= week_later,
    ).count()

    monthly_revenue = db.session.query(func.sum(MemberMembership.amount)).filter(
        MemberMembership.gym_id        == gid,
        MemberMembership.payment_date  >= month_start,
        MemberMembership.payment_status == 'paid',
    ).scalar() or 0.0

    # ── Today's check-ins ─────────────────────────────────────────────────────
    today_start   = datetime.combine(today, datetime.min.time())
    checkins_today = Attendance.query.filter(
        Attendance.gym_id   == gid,
        Attendance.check_in >= today_start,
    ).count()

    currently_in = Attendance.query.filter(
        Attendance.gym_id    == gid,
        Attendance.check_in  >= today_start,
        Attendance.check_out.is_(None),
    ).count()

    class Stats:
        pass
    stats = Stats()
    stats.total_members      = total_members
    stats.active_members     = active_members
    stats.new_this_month     = new_this_month
    stats.expiring_soon      = expiring_soon_count
    stats.revenue_this_month = monthly_revenue
    stats.checkins_today     = checkins_today
    stats.currently_in       = currently_in

    # ── Renewal alerts ────────────────────────────────────────────────────────
    renewal_alerts = (
        MemberMembership.query
        .filter(
            MemberMembership.gym_id   == gid,
            MemberMembership.status   == 'active',
            MemberMembership.end_date >= today,
            MemberMembership.end_date <= week_later,
        )
        .order_by(MemberMembership.end_date.asc())
        .all()
    )
    for alert in renewal_alerts:
        alert.days_left = (alert.end_date - today).days

    # ── Revenue chart — last 6 months ─────────────────────────────────────────
    chart_months = []
    for i in range(5, -1, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        chart_months.append(date(y, m, 1))

    raw_revenue = db.session.query(
        func.strftime('%Y-%m', MemberMembership.payment_date).label('ym'),
        func.sum(MemberMembership.amount).label('rev'),
    ).filter(
        MemberMembership.gym_id        == gid,
        MemberMembership.payment_status == 'paid',
        MemberMembership.payment_date.isnot(None),
    ).group_by('ym').all()

    rev_by_month   = {r.ym: float(r.rev) for r in raw_revenue}
    revenue_labels = [m.strftime('%b %Y') for m in chart_months]
    revenue_data   = [rev_by_month.get(m.strftime('%Y-%m'), 0) for m in chart_months]

    # ── Plan breakdown ────────────────────────────────────────────────────────
    plan_rows = db.session.query(
        MembershipPlan.name,
        func.count(MemberMembership.id).label('cnt'),
    ).join(MemberMembership, MembershipPlan.id == MemberMembership.plan_id).filter(
        MemberMembership.gym_id   == gid,
        MemberMembership.status   == 'active',
        MemberMembership.end_date >= today,
    ).group_by(MembershipPlan.id).order_by(MembershipPlan.price).all()

    plan_labels = [r.name for r in plan_rows]
    plan_data   = [r.cnt for r in plan_rows]

    # ── Recent members ────────────────────────────────────────────────────────
    recent_members = (
        Member.query
        .filter_by(gym_id=gid)
        .order_by(Member.joining_date.desc())
        .limit(5)
        .all()
    )

    return render_template(
        'dashboard/index.html',
        stats=stats,
        renewal_alerts=renewal_alerts,
        recent_members=recent_members,
        revenue_labels=revenue_labels,
        revenue_data=revenue_data,
        plan_labels=plan_labels,
        plan_data=plan_data,
        now=today,
    )
