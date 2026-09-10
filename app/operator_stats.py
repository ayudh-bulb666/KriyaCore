"""Per-gym aggregates for the operator panel.

Every operator view wants the same handful of numbers for each gym. Asking
for them one gym at a time costs a query per metric per gym — the dashboard
was running six, so twelve queries at two gyms, sixty at ten, three hundred
at fifty, all on a single worker. Each function here answers the same
question with ONE grouped query and returns a {gym_id: value} map.

Two rules this module keeps to, both learned the hard way in this codebase:

  * No dialect-specific date functions. `func.strftime` worked locally on
    SQLite and broke on Postgres in production; `date_trunc` would break the
    other way. Anything that buckets by period does it in Python, over rows
    the database has already narrowed with a plain comparison.

  * Missing means zero, not missing. A gym with no members simply has no row
    in the GROUP BY result, so every lookup goes through a default rather
    than a KeyError at render time.
"""
from datetime import date, datetime, time, timedelta

from sqlalchemy import case, func

from .models import (db, Attendance, Expense, Member, MemberMembership,
                     PTPackage, SalaryPayment, User)

# Imported rather than restated so the operator panel and a gym's own
# dashboard can never drift into disagreeing about what "lapsed" means.
from .dashboard import LAPSED_AFTER_DAYS


def member_counts():
    """{gym_id: {'total': int, 'active': int}} for every gym with members."""
    rows = (db.session.query(
                Member.gym_id,
                func.count(Member.id),
                func.count(case((Member.status == 'active', 1))),
            )
            .group_by(Member.gym_id)
            .all())
    return {gym_id: {'total': total, 'active': active}
            for gym_id, total, active in rows}


def active_membership_counts(today=None):
    """{gym_id: int} — memberships currently live, not merely marked active.

    Mirrors the dashboard's definition: status 'active' AND not past its end
    date. A row whose end_date has passed but whose status was never updated
    is expired in every sense that matters to a person looking at the number.
    """
    today = today or date.today()
    rows = (db.session.query(
                MemberMembership.gym_id,
                func.count(MemberMembership.id),
            )
            .filter(MemberMembership.status == 'active',
                    MemberMembership.end_date >= today)
            .group_by(MemberMembership.gym_id)
            .all())
    return dict(rows)


def revenue_since(start, end=None):
    """{gym_id: float} — payments received in [start, end].

    Keyed on payment_date, not start_date: this answers "what came in during
    the period", which is what a revenue figure means. A membership sold in
    March and paid in April belongs to April here.
    """
    q = (db.session.query(
             MemberMembership.gym_id,
             func.coalesce(func.sum(MemberMembership.amount), 0),
         )
         .filter(MemberMembership.payment_status == 'paid',
                 MemberMembership.payment_date >= start))
    if end is not None:
        q = q.filter(MemberMembership.payment_date <= end)
    return {gym_id: float(total or 0)
            for gym_id, total in q.group_by(MemberMembership.gym_id).all()}


def staff_counts():
    """{gym_id: int} — every user attached to the gym, staff and owner alike."""
    rows = (db.session.query(User.gym_id, func.count(User.id))
            .filter(User.gym_id.isnot(None))
            .group_by(User.gym_id)
            .all())
    return dict(rows)


def gym_admins():
    """{gym_id: User} — each gym's owner account.

    One query for all of them instead of a `.first()` per gym. Where a gym
    somehow has two super_admins, the lowest id wins; ordering makes that
    deterministic rather than left to the database.
    """
    rows = (User.query
            .filter(User.role == 'super_admin', User.gym_id.isnot(None))
            .order_by(User.gym_id, User.id)
            .all())
    admins = {}
    for user in rows:
        admins.setdefault(user.gym_id, user)
    return admins


def new_members_since(start):
    """{gym_id: int} — members who joined on or after `start`."""
    rows = (db.session.query(Member.gym_id, func.count(Member.id))
            .filter(Member.joining_date >= start,
                    Member.is_erased.is_(False))
            .group_by(Member.gym_id)
            .all())
    return dict(rows)


def lapsed_counts(today=None, days=LAPSED_AFTER_DAYS):
    """{gym_id: int} — members whose last membership ended `days` ago or more.

    The same definition the gym dashboard uses for "lapsed": they stopped
    paying, and enough time has passed that it looks deliberate rather than
    late. Its sibling metric, "gone quiet" — still paid up but no longer
    turning up — is deliberately NOT here: it needs each member's last visit,
    and doing that per member across every gym is the shape of query this
    module exists to avoid.

    Expressed as a grouped subquery rather than a Python loop over members:
    take each member's latest end_date, keep the ones already past the
    cutoff. A member whose latest membership is still running cannot appear,
    so no separate "has an active membership" exclusion is needed.
    """
    today  = today or date.today()
    cutoff = today - timedelta(days=days)

    last_end = (db.session.query(
                    MemberMembership.gym_id.label('gym_id'),
                    MemberMembership.member_id.label('member_id'),
                    func.max(MemberMembership.end_date).label('last_end'))
                .group_by(MemberMembership.gym_id, MemberMembership.member_id)
                .subquery())

    rows = (db.session.query(last_end.c.gym_id, func.count())
            .join(Member, Member.id == last_end.c.member_id)
            .filter(last_end.c.last_end <= cutoff,
                    Member.is_erased.is_(False))
            .group_by(last_end.c.gym_id)
            .all())
    return dict(rows)


def outstanding_amounts(today=None):
    """{gym_id: {'amount': float, 'count': int}} — money owed but not paid.

    Matches MemberMembership.unpaid_query: live memberships whose payment is
    pending or overdue. Summed in the database rather than by adding up rows
    in Python, which is what the gym-level page does.
    """
    today = today or date.today()
    rows = (db.session.query(
                MemberMembership.gym_id,
                func.coalesce(func.sum(MemberMembership.amount), 0),
                func.count(MemberMembership.id))
            .filter(MemberMembership.status == 'active',
                    MemberMembership.payment_status.in_(['pending', 'overdue']),
                    MemberMembership.end_date >= today)
            .group_by(MemberMembership.gym_id)
            .all())
    return {gym_id: {'amount': float(amount or 0), 'count': count}
            for gym_id, amount, count in rows}


def visits_since(start_dt):
    """{gym_id: int} — door check-ins at or after `start_dt`.

    `start_dt` is a naive datetime in the SERVER's local time, because that
    is what Attendance.visited_at stores (`default=datetime.now`, with
    TZ=Asia/Kolkata set on the host). Passing a UTC datetime here would
    silently shift every gym's day boundary by five and a half hours.
    """
    rows = (db.session.query(Attendance.gym_id, func.count(Attendance.id))
            .filter(Attendance.visited_at >= start_dt)
            .group_by(Attendance.gym_id)
            .all())
    return dict(rows)


def month_starts(today=None, months=6):
    """The first of each month, oldest first, ending with `today`'s month."""
    today = today or date.today()
    cursor = today.replace(day=1)
    out = [cursor]
    for _ in range(months - 1):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        out.append(cursor)
    return list(reversed(out))


def revenue_by_month(today=None, months=6):
    """({gym_id: {(year, month): float}}, [month_start, ...]).

    Bucketed in Python on purpose. Grouping by month in SQL means date_trunc
    on Postgres and strftime on SQLite — this codebase already shipped that
    bug once, where a dashboard query worked locally and broke in production.
    One plain range comparison fetches the rows; Python does the arithmetic.
    """
    today  = today or date.today()
    starts = month_starts(today, months)

    rows = (db.session.query(
                MemberMembership.gym_id,
                MemberMembership.payment_date,
                MemberMembership.amount)
            .filter(MemberMembership.payment_status == 'paid',
                    MemberMembership.payment_date.isnot(None),
                    MemberMembership.payment_date >= starts[0])
            .all())

    buckets = {}
    for gym_id, paid_on, amount in rows:
        key = (paid_on.year, paid_on.month)
        buckets.setdefault(gym_id, {})[key] = \
            buckets.setdefault(gym_id, {}).get(key, 0.0) + float(amount or 0)

    return buckets, starts


def attach_gym_stats(gyms, today=None):
    """Hang the per-gym numbers the operator templates read onto each gym.

    Five grouped queries total, whatever the number of gyms. The attribute
    names match what `operator/index.html` already expects, so templates
    don't change.
    """
    today = today or date.today()
    month_start = today.replace(day=1)

    members     = member_counts()
    memberships = active_membership_counts(today)
    revenue     = revenue_since(month_start)
    staff       = staff_counts()
    admins      = gym_admins()

    for gym in gyms:
        counts = members.get(gym.id) or {'total': 0, 'active': 0}
        gym.total_member_count  = counts['total']
        gym.active_member_count = counts['active']
        gym.active_memberships  = memberships.get(gym.id, 0)
        gym.revenue_month       = revenue.get(gym.id, 0.0)
        gym.staff_count         = staff.get(gym.id, 0)
        gym.admin               = admins.get(gym.id)

    return gyms


def month_bounds(month_start):
    """(first day, last day) of the month `month_start` falls in."""
    first = month_start.replace(day=1)
    # Day 28 is in every month; adding 4 days always lands in the next one.
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, following - timedelta(days=1)


def pt_revenue_between(start, end):
    """{gym_id: float} — personal-training packages PAID in the period.

    Keyed on payment_date like membership revenue, not sold_on: a package
    sold in March and paid in April is April's money.
    """
    rows = (db.session.query(
                PTPackage.gym_id,
                func.coalesce(func.sum(PTPackage.price), 0))
            .filter(PTPackage.payment_status == 'paid',
                    PTPackage.payment_date.isnot(None),
                    PTPackage.payment_date >= start,
                    PTPackage.payment_date <= end)
            .group_by(PTPackage.gym_id)
            .all())
    return {gym_id: float(total or 0) for gym_id, total in rows}


def expenses_between(start, end):
    """{gym_id: float} — running costs, keyed on the date incurred."""
    rows = (db.session.query(
                Expense.gym_id,
                func.coalesce(func.sum(Expense.amount), 0))
            .filter(Expense.incurred_on >= start,
                    Expense.incurred_on <= end)
            .group_by(Expense.gym_id)
            .all())
    return {gym_id: float(total or 0) for gym_id, total in rows}


def salaries_between(start, end):
    """{gym_id: float} — staff pay, keyed on the date it went out.

    Sums base + PT commission + adjustment, mirroring SalaryPayment.total().

    Note for anyone extending this: do NOT also add PTSession.trainer_payout
    as a cost. A salary run's pt_amount is populated straight from
    PTSession.payout_between(), so the trainer's commission is already inside
    this figure — counting the sessions again would charge every PT payout to
    the gym twice.
    """
    rows = (db.session.query(
                SalaryPayment.gym_id,
                func.coalesce(func.sum(SalaryPayment.base_amount), 0)
                + func.coalesce(func.sum(SalaryPayment.pt_amount), 0)
                + func.coalesce(func.sum(SalaryPayment.adjustment), 0))
            .filter(SalaryPayment.paid_on >= start,
                    SalaryPayment.paid_on <= end)
            .group_by(SalaryPayment.gym_id)
            .all())
    return {gym_id: float(total or 0) for gym_id, total in rows}


def finance_rows(gyms, month_start):
    """One profit-and-loss row per gym for the month containing `month_start`.

    Four grouped queries for the estate. Every figure is what actually moved
    in the period — see the caveat the template prints: expenses are dated
    when they were incurred while everything else is dated when it was paid,
    which is a property of the underlying records, not of this calculation.
    """
    start, end = month_bounds(month_start)

    membership = revenue_since(start, end)
    pt         = pt_revenue_between(start, end)
    costs      = expenses_between(start, end)
    salaries   = salaries_between(start, end)

    rows = []
    for gym in gyms:
        m_rev = membership.get(gym.id, 0.0)
        p_rev = pt.get(gym.id, 0.0)
        spend = costs.get(gym.id, 0.0)
        pay   = salaries.get(gym.id, 0.0)
        revenue = m_rev + p_rev
        outgo   = spend + pay
        net     = revenue - outgo

        rows.append({
            'gym':             gym,
            'membership_rev':  m_rev,
            'pt_rev':          p_rev,
            'revenue':         revenue,
            'expenses':        spend,
            'salaries':        pay,
            'costs':           outgo,
            'net':             net,
            # Margin is meaningless without revenue to divide by — a gym that
            # billed nothing but paid rent is not "-100% margin", it simply
            # has no margin to report.
            'margin':          round(net / revenue * 100, 1) if revenue else None,
        })
    return rows, start, end


def net_by_month(today=None, months=6):
    """({gym_id: {(year, month): net}}, [month_start, ...]) over `months`.

    Runs the same four aggregates once per month rather than once per gym per
    month: six months across any number of gyms costs 24 queries, flat.
    """
    today  = today or date.today()
    starts = month_starts(today, months)

    nets = {}
    for month_start in starts:
        start, end = month_bounds(month_start)
        membership = revenue_since(start, end)
        pt         = pt_revenue_between(start, end)
        costs      = expenses_between(start, end)
        salaries   = salaries_between(start, end)

        gym_ids = set(membership) | set(pt) | set(costs) | set(salaries)
        key = (month_start.year, month_start.month)
        for gym_id in gym_ids:
            net = (membership.get(gym_id, 0.0) + pt.get(gym_id, 0.0)
                   - costs.get(gym_id, 0.0) - salaries.get(gym_id, 0.0))
            nets.setdefault(gym_id, {})[key] = round(net, 2)

    return nets, starts


def estate_rows(gyms, today=None):
    """One row per gym holding every column the comparison table shows.

    Nine grouped queries for the whole estate, however many gyms there are.
    Returned as plain dicts rather than attributes hung on the Gym objects,
    so the template can sort on them without reaching into the model.
    """
    today       = today or date.today()
    month_start = today.replace(day=1)
    week_start  = datetime.combine(today - timedelta(days=7), time.min)

    members     = member_counts()
    memberships = active_membership_counts(today)
    revenue     = revenue_since(month_start)
    staff       = staff_counts()
    joined      = new_members_since(month_start)
    lapsed      = lapsed_counts(today)
    owed        = outstanding_amounts(today)
    visits      = visits_since(week_start)

    rows = []
    for gym in gyms:
        counts = members.get(gym.id) or {'total': 0, 'active': 0}
        due    = owed.get(gym.id) or {'amount': 0.0, 'count': 0}
        active = counts['active']

        rows.append({
            'gym':             gym,
            'members_total':   counts['total'],
            'members_active':  active,
            'memberships':     memberships.get(gym.id, 0),
            'new_this_month':  joined.get(gym.id, 0),
            'lapsed':          lapsed.get(gym.id, 0),
            # Share of everyone who ever joined that has since lapsed. Against
            # total, not active: dividing by the members who stayed would make
            # a gym look better the more people it lost.
            'lapsed_pct':      round(lapsed.get(gym.id, 0) / counts['total'] * 100, 1)
                               if counts['total'] else 0.0,
            'revenue_month':   revenue.get(gym.id, 0.0),
            'outstanding':     due['amount'],
            'outstanding_n':   due['count'],
            'visits_7d':       visits.get(gym.id, 0),
            'staff_count':     staff.get(gym.id, 0),
        })
    return rows
