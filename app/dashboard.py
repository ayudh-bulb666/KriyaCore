from types import SimpleNamespace

from flask import Blueprint, render_template, request, session, jsonify, url_for
from flask_login import login_required, current_user
from datetime import date, datetime, timedelta
from sqlalchemy import func

from .models import (db, Member, MemberMembership, MembershipPlan, Attendance,
                     Notification)

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/<string:gym_slug>')

# How recently someone must have been refused at the door for it to still be
# worth shouting about — they're probably still at the desk.
DENIED_ALERT_WINDOW_MINUTES = 30


def _default_view_for(user):
    """Staff work the door; owners run the business. Either can switch."""
    return 'business' if user.is_super_admin else 'frontdesk'


@dashboard_bp.route('/')
@login_required
def index():
    # ?view= flips it and sticks for the session, so an owner covering the
    # desk doesn't have to re-choose on every page load.
    requested = request.args.get('view')
    if requested in ('frontdesk', 'business'):
        session['dashboard_view'] = requested
    view = session.get('dashboard_view') or _default_view_for(current_user)

    if view == 'frontdesk':
        return _front_desk()
    return _business()


def _front_desk():
    """What the person at the desk needs: who just walked in, who to catch,
    and a way to answer 'is this person good to go?' in one keystroke."""
    gid   = current_user.gym_id
    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())

    arrivals = (Attendance.query
                .filter(Attendance.gym_id == gid, Attendance.visited_at >= today_start)
                .order_by(Attendance.visited_at.desc())
                .all())

    # Anyone refused at the door in the last half hour is very likely still
    # standing there — surface it loudly, or not at all.
    denied_cutoff = datetime.utcnow() - timedelta(minutes=DENIED_ALERT_WINDOW_MINUTES)
    denied = (Notification.query
              .filter(Notification.gym_id == gid,
                      Notification.type == 'access_denied',
                      Notification.created_at >= denied_cutoff)
              .order_by(Notification.created_at.desc())
              .all())

    # Who to catch while they're physically here.
    expiring = (MemberMembership.expiring_soon_query(gym_id=gid)
                .order_by(MemberMembership.end_date.asc()).all())
    unpaid   = MemberMembership.unpaid_query(gym_id=gid).all()

    # Lapsed members who still turned up — only meaningful once a door reader
    # is running, so this quietly stays empty for gyms without one.
    lapsed_visitors = []
    seen = set()
    for a in arrivals:
        if a.membership_status in ('expired', 'none') and a.member_id not in seen:
            seen.add(a.member_id)
            lapsed_visitors.append(a)

    return render_template(
        'dashboard/front_desk.html',
        arrivals=arrivals,
        denied=denied,
        expiring=expiring,
        unpaid=unpaid,
        lapsed_visitors=lapsed_visitors,
        visits_today=len(arrivals),
        unique_today=len({a.member_id for a in arrivals}),
        via_face_id=sum(1 for a in arrivals if a.source == 'face_id'),
        dashboard_view='frontdesk',
    )


@dashboard_bp.route('/desk/search')
@login_required
def desk_search():
    """Type-ahead behind the front-desk search box. Returns the verdict, not
    just the name — answering 'are they good to go?' is the whole point."""
    gid = current_user.gym_id
    q   = request.args.get('q', '').strip()
    if len(q) < 1:
        return jsonify([])

    like = f'%{q}%'
    members = (Member.query
               .filter(Member.gym_id == gid,
                       db.or_(Member.first_name.ilike(like),
                              Member.last_name.ilike(like),
                              Member.phone.ilike(like)))
               .order_by(Member.first_name)
               .limit(6).all())

    out = []
    for m in members:
        state, message = m.membership_state
        active = m.active_membership
        out.append({
            'id': m.id,
            'name': m.full_name,
            'initials': m.initials,
            'phone': m.phone or '',
            'state': state,
            'message': message,
            'membership_id': active.id if active else None,
            'detail_url': url_for('members.detail', member_id=m.id) + '#membership',
        })
    return jsonify(out)


def _business():
    gid        = current_user.gym_id
    today      = date.today()
    month_start = today.replace(day=1)

    # ── Stats ──────────────────────────────────────────────────────────────────
    total_members  = Member.query.filter_by(gym_id=gid).count()
    active_members = Member.query.filter_by(gym_id=gid, status='active').count()

    new_this_month = Member.query.filter(
        Member.gym_id == gid,
        Member.joining_date >= month_start,
    ).count()

    expiring_soon_count = MemberMembership.expiring_soon_query(gym_id=gid).count()

    monthly_revenue = db.session.query(func.sum(MemberMembership.amount)).filter(
        MemberMembership.gym_id        == gid,
        MemberMembership.payment_date  >= month_start,
        MemberMembership.payment_status == 'paid',
    ).scalar() or 0.0

    # ── Gym visits ────────────────────────────────────────────────────────────
    # Arrivals only — there's no "currently in gym" figure to show, since
    # nobody is at the door recording who leaves (see the Attendance model).
    today_start = datetime.combine(today, datetime.min.time())
    week_start  = datetime.combine(today - timedelta(days=6), datetime.min.time())

    visits_today = Attendance.query.filter(
        Attendance.gym_id     == gid,
        Attendance.visited_at >= today_start,
    ).count()

    visits_this_week = Attendance.query.filter(
        Attendance.gym_id     == gid,
        Attendance.visited_at >= week_start,
    ).count()

    stats = SimpleNamespace(
        total_members=total_members,
        active_members=active_members,
        new_this_month=new_this_month,
        expiring_soon=expiring_soon_count,
        revenue_this_month=monthly_revenue,
        visits_today=visits_today,
        visits_this_week=visits_this_week,
    )

    # ── Renewal alerts ────────────────────────────────────────────────────────
    renewal_alerts = (
        MemberMembership.expiring_soon_query(gym_id=gid)
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
        dashboard_view='business',
    )
