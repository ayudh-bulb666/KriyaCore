from types import SimpleNamespace

from flask import Blueprint, render_template, request, session, jsonify, url_for
from flask_login import login_required, current_user
from datetime import date, datetime, timedelta
from sqlalchemy import func

from .models import (db, Member, MemberMembership, MembershipPlan, Attendance,
                     Notification, Expense)

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


# A member is "lapsed" once their last membership has been over for this
# long with no renewal. Short enough to still be worth a phone call.
LAPSED_AFTER_DAYS = 30

# A paid-up member who hasn't shown up in this long is drifting. This is the
# number that predicts next month's non-renewals.
QUIET_AFTER_DAYS = 14


def _drop_off(gid, today):
    """Two different kinds of losing a member, counted separately.

    'Lapsed' is a fact — they stopped paying. 'Going quiet' is a warning —
    they're still paid up but have stopped coming, and they're the ones you
    can still save. Rolling them into one "churn" number would hide that,
    because only one of the two is actionable.
    """
    lapsed, quiet = [], []

    members = Member.query.filter_by(gym_id=gid).all()

    # Going quiet only means anything once a door reader is logging arrivals.
    # Without one, every member looks absent and the tile would cry wolf.
    tracks_visits = db.session.query(Attendance.id).filter(
        Attendance.gym_id == gid).first() is not None

    quiet_cutoff = datetime.combine(today - timedelta(days=QUIET_AFTER_DAYS),
                                    datetime.min.time())

    for m in members:
        if m.is_erased:
            continue

        state, _ = m.membership_state

        if state in ('expired', 'none'):
            if not m.memberships:
                continue
            last = max(m.memberships, key=lambda x: x.end_date)
            days = (today - last.end_date).days
            if days >= LAPSED_AFTER_DAYS:
                m.days_lapsed  = days
                m.last_plan    = last.plan.name
                lapsed.append(m)
            continue

        # Still paid up — are they actually turning up?
        if tracks_visits and state in ('active', 'expiring'):
            last_visit = (Attendance.query
                          .filter(Attendance.gym_id == gid,
                                  Attendance.member_id == m.id)
                          .order_by(Attendance.visited_at.desc())
                          .first())
            if last_visit is None:
                # Never seen. Only a warning once they've had time to start.
                if (today - m.joining_date).days >= QUIET_AFTER_DAYS:
                    m.days_quiet = (today - m.joining_date).days
                    m.last_seen  = None
                    quiet.append(m)
            elif last_visit.visited_at < quiet_cutoff:
                m.days_quiet = (today - last_visit.visited_at.date()).days
                m.last_seen  = last_visit.visited_at
                quiet.append(m)

    lapsed.sort(key=lambda m: m.days_lapsed)
    quiet.sort(key=lambda m: m.days_quiet, reverse=True)
    return lapsed, quiet, tracks_visits


def _last_n_months(today, n=6):
    """The first of each of the last n months, oldest first."""
    out = []
    for i in range(n - 1, -1, -1):
        m, y = today.month - i, today.year
        while m <= 0:
            m += 12
            y -= 1
        out.append(date(y, m, 1))
    return out


def _month_end(first_of_month):
    return (first_of_month + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def _business():
    gid        = current_user.gym_id
    today      = date.today()
    month_start = today.replace(day=1)
    month_end   = _month_end(month_start)

    # ── 1. Expiring in the next 7 days ────────────────────────────────────────
    expiring_soon_count = MemberMembership.expiring_soon_query(gym_id=gid).count()
    renewal_alerts = (
        MemberMembership.expiring_soon_query(gym_id=gid)
        .order_by(MemberMembership.end_date.asc())
        .all()
    )
    for alert in renewal_alerts:
        alert.days_left = (alert.end_date - today).days

    # ── 2. Who came in today ──────────────────────────────────────────────────
    # Arrivals only — there's no "currently in gym" figure, since nobody is at
    # the door recording who leaves (see the Attendance model).
    today_start = datetime.combine(today, datetime.min.time())
    week_start  = datetime.combine(today - timedelta(days=6), datetime.min.time())

    arrivals_today = (Attendance.query
                      .filter(Attendance.gym_id == gid,
                              Attendance.visited_at >= today_start)
                      .order_by(Attendance.visited_at.desc())
                      .all())

    visits_this_week = Attendance.query.filter(
        Attendance.gym_id     == gid,
        Attendance.visited_at >= week_start,
    ).count()

    # ── 3. Membership health ──────────────────────────────────────────────────
    total_members  = Member.query.filter_by(gym_id=gid).count()
    active_members = Member.query.filter_by(gym_id=gid, status='active').count()
    new_this_month = Member.query.filter(
        Member.gym_id == gid,
        Member.joining_date >= month_start,
    ).count()

    lapsed, quiet, tracks_visits = _drop_off(gid, today)

    # ── 4. Money ──────────────────────────────────────────────────────────────
    monthly_revenue = db.session.query(func.sum(MemberMembership.amount)).filter(
        MemberMembership.gym_id         == gid,
        MemberMembership.payment_date   >= month_start,
        MemberMembership.payment_status == 'paid',
    ).scalar() or 0.0

    # Money already owed on live memberships — earned but not collected.
    pending_rows    = MemberMembership.unpaid_query(gym_id=gid).all()
    pending_revenue = sum(r.amount for r in pending_rows)

    expenses_this_month = Expense.total_between(gid, month_start, month_end)

    stats = SimpleNamespace(
        total_members=total_members,
        active_members=active_members,
        new_this_month=new_this_month,
        expiring_soon=expiring_soon_count,
        revenue_this_month=monthly_revenue,
        pending_revenue=pending_revenue,
        pending_count=len(pending_rows),
        expenses_this_month=expenses_this_month,
        net_this_month=monthly_revenue - expenses_this_month,
        visits_today=len(arrivals_today),
        unique_today=len({a.member_id for a in arrivals_today}),
        visits_this_week=visits_this_week,
        lapsed=len(lapsed),
        quiet=len(quiet),
        tracks_visits=tracks_visits,
    )

    # ── Charts — last 6 months ────────────────────────────────────────────────
    chart_months   = _last_n_months(today)
    revenue_labels = [m.strftime('%b %Y') for m in chart_months]

    # Bucketed in Python rather than with a GROUP BY on a date function.
    # SQLite's strftime() and Postgres's to_char() aren't interchangeable, and
    # this ran fine on SQLite locally while being a guaranteed 500 in
    # production. At a few hundred paid memberships the loop costs nothing.
    paid_rows = db.session.query(
        MemberMembership.payment_date, MemberMembership.amount,
    ).filter(
        MemberMembership.gym_id         == gid,
        MemberMembership.payment_status == 'paid',
        MemberMembership.payment_date.isnot(None),
    ).all()

    rev_by_month = {}
    for pay_date, amount in paid_rows:
        key = (pay_date.year, pay_date.month)
        rev_by_month[key] = rev_by_month.get(key, 0.0) + float(amount or 0)
    revenue_data = [rev_by_month.get((m.year, m.month), 0) for m in chart_months]

    exp_rows = db.session.query(
        Expense.incurred_on, Expense.amount,
    ).filter(Expense.gym_id == gid).all()

    exp_by_month = {}
    for inc_date, amount in exp_rows:
        key = (inc_date.year, inc_date.month)
        exp_by_month[key] = exp_by_month.get(key, 0.0) + float(amount or 0)
    expense_data = [exp_by_month.get((m.year, m.month), 0) for m in chart_months]

    # Member growth: total on the books at each month end, so the line shows
    # the size of the gym over time rather than just sign-up spikes.
    join_dates = [d for (d,) in db.session.query(Member.joining_date)
                  .filter(Member.gym_id == gid).all()]
    member_data = [sum(1 for d in join_dates if d <= _month_end(m))
                   for m in chart_months]

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
        arrivals_today=arrivals_today,
        lapsed=lapsed[:5],
        quiet=quiet[:5],
        recent_members=recent_members,
        revenue_labels=revenue_labels,
        revenue_data=revenue_data,
        expense_data=expense_data,
        member_data=member_data,
        plan_labels=plan_labels,
        plan_data=plan_data,
        lapsed_after_days=LAPSED_AFTER_DAYS,
        quiet_after_days=QUIET_AFTER_DAYS,
        now=today,
        dashboard_view='business',
    )
