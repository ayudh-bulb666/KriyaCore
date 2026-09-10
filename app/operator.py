import base64
import csv
import io
import secrets
from flask import (Blueprint, render_template, redirect, url_for, request,
                   flash, session, Response)
from flask_login import login_required, current_user, login_user
from werkzeug.security import generate_password_hash
from datetime import date, timedelta
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from .helpers import validate_password
from .models import db, Gym, User, Member, MemberMembership, MembershipPlan, AuditLog, Notification
from .operator_stats import (attach_gym_stats, estate_rows, finance_rows,
                             net_by_month, revenue_by_month)
# Reused, not restated: the filter tabs must mean the same thing on the
# platform billing screen as they do inside a gym.
from .billing import _apply_status_filter
from .plans import PLANS
from .tenant import RESERVED_SLUGS

# The logo is base64'd into every page's HTML, so it costs bandwidth on every
# request. 512 KB is generous for a gym logo and stops a 10 MB PNG making the
# whole app feel slow.
MAX_LOGO_BYTES = 512 * 1024


def _sniff_image(data: bytes):
    """Return a MIME type based on the file's own magic bytes, or None.

    Deliberately does not consult the upload's Content-Type header: that is
    supplied by the client and can say 'image/png' about anything at all.
    Checking the bytes is what makes the allowlist mean something.

    SVG is intentionally absent — it is XML that can contain <script>, and a
    logo has no need to be a scriptable document.
    """
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def log_action(action: str, gym=None, detail: str = None):
    """Write one audit log entry. Call before db.session.commit()."""
    entry = AuditLog(
        actor_id   = current_user.id   if current_user.is_authenticated else None,
        actor_name = current_user.name if current_user.is_authenticated else 'System',
        action     = action,
        gym_id     = gym.id   if gym else None,
        gym_name   = gym.name if gym else None,
        detail     = detail,
    )
    db.session.add(entry)

operator_bp = Blueprint('operator', __name__, url_prefix='/operator')


def _platform_required(f):
    """Decorator: only platform_admin may access these routes."""
    from functools import wraps
    from flask import abort
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_platform_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


@operator_bp.route('/')
@login_required
@_platform_required
def index():
    gyms  = Gym.query.order_by(Gym.created_at.desc()).all()
    today = date.today()
    this_month = today.replace(day=1)

    # ── Platform-wide KPIs ────────────────────────────────────────────────────
    total_gyms      = Gym.query.count()
    active_gyms     = Gym.query.filter_by(is_active=True).count()
    suspended_gyms  = total_gyms - active_gyms
    total_members   = Member.query.filter_by(status='active').count()

    total_memberships = MemberMembership.query.filter(
        MemberMembership.status   == 'active',
        MemberMembership.end_date >= today,
    ).count()

    total_revenue_month = db.session.query(
        func.sum(MemberMembership.amount)
    ).filter(
        MemberMembership.payment_status == 'paid',
        MemberMembership.payment_date   >= this_month,
    ).scalar() or 0

    expiring_across = MemberMembership.expiring_soon_query().count()

    # ── Per-gym stats ─────────────────────────────────────────────────────────
    # Five grouped queries for the whole estate, not six per gym — see
    # app/operator_stats.py for why that mattered.
    attach_gym_stats(gyms, today)

    # Chart series — top gyms by members / revenue
    gyms_by_members = sorted(gyms, key=lambda g: g.active_member_count, reverse=True)[:10]
    gyms_by_revenue = sorted(gyms, key=lambda g: g.revenue_month, reverse=True)[:10]

    return render_template('operator/index.html',
        gyms=gyms,
        gyms_by_members=gyms_by_members,
        gyms_by_revenue=gyms_by_revenue,
        total_gyms=total_gyms,
        active_gyms=active_gyms,
        suspended_gyms=suspended_gyms,
        total_members=total_members,
        total_memberships=total_memberships,
        total_revenue_month=total_revenue_month,
        expiring_across=expiring_across,
        today=today,
    )


# Sortable columns on the insights table. An allow-list, not getattr on a
# query parameter: the sort key comes from the URL, and a dict lookup means a
# crafted ?sort= can only ever pick one of these.
INSIGHT_SORTS = {
    'name':           lambda r: r['gym'].name.lower(),
    'members_active': lambda r: r['members_active'],
    'new_this_month': lambda r: r['new_this_month'],
    'lapsed_pct':     lambda r: r['lapsed_pct'],
    'revenue_month':  lambda r: r['revenue_month'],
    'outstanding':    lambda r: r['outstanding'],
    'visits_7d':      lambda r: r['visits_7d'],
}


@operator_bp.route('/insights')
@login_required
@_platform_required
def insights():
    """Estate-wide analytics. Read-only, and aggregates only.

    Deliberately contains no member names, phone numbers or email addresses.
    Under the DPDP Act each gym is the data fiduciary for its own members and
    KriyaCore is the processor; a platform-wide screen that casually lists
    every member's contact details across every gym is not something anyone
    needs in order to see how the business is doing. Drilling into a named
    individual stays in the gym's own screens, reached by impersonation,
    where it is attributed in the audit log.
    """
    today  = date.today()
    months = 6
    gyms   = Gym.query.order_by(Gym.name).all()

    rows = estate_rows(gyms, today)
    revenue_buckets, month_list = revenue_by_month(today, months)

    # Sorted here rather than in the browser so the table is correct with
    # JavaScript off and stays correct once there are more gyms than fit on
    # one screen — at which point client-side sorting would only reorder the
    # page you happen to be looking at.
    sort_key = request.args.get('sort', 'name')
    if sort_key not in INSIGHT_SORTS:
        sort_key = 'name'
    # Default direction follows the column: names read A-Z, numbers read
    # biggest-first. Defaulting everything to descending sorted the gym list
    # Z-A, which nobody expects of a name column.
    default_dir = 'asc' if sort_key == 'name' else 'desc'
    descending  = request.args.get('dir', default_dir) != 'asc'
    rows.sort(key=INSIGHT_SORTS[sort_key], reverse=descending)

    # Chart series: one line per gym over the same month axis, plus the
    # estate total. Zero-filled so a gym that billed nothing in March draws a
    # point at zero rather than breaking the line.
    labels = [m.strftime('%b %Y') for m in month_list]
    keys   = [(m.year, m.month) for m in month_list]
    series = [{
        'name':   gym.name,
        'color':  gym.primary_color or '#166534',
        'values': [round(revenue_buckets.get(gym.id, {}).get(k, 0.0), 2) for k in keys],
    } for gym in gyms]
    estate_series = [round(sum(s['values'][i] for s in series), 2)
                     for i in range(len(keys))]

    totals = {
        'gyms':           len(gyms),
        'gyms_active':    sum(1 for g in gyms if g.is_active),
        'members_active': sum(r['members_active'] for r in rows),
        'members_total':  sum(r['members_total'] for r in rows),
        'new_this_month': sum(r['new_this_month'] for r in rows),
        'lapsed':         sum(r['lapsed'] for r in rows),
        'revenue_month':  sum(r['revenue_month'] for r in rows),
        'outstanding':    sum(r['outstanding'] for r in rows),
        'outstanding_n':  sum(r['outstanding_n'] for r in rows),
        'visits_7d':      sum(r['visits_7d'] for r in rows),
        'expiring':       MemberMembership.expiring_soon_query().count(),
    }

    return render_template('operator/insights.html',
                           rows=rows, totals=totals, today=today,
                           labels=labels, series=series,
                           estate_series=estate_series, months=months,
                           sort_key=sort_key,
                           sort_dir='desc' if descending else 'asc')


"""Cross-gym views that show individual members.

Both routes below differ from /insights in one important way: they show real
people — names, phone numbers, what they owe. That is the whole point of
them, but it also means a platform admin reading a gym's member list is an
event worth recording. Every request that returns member data writes an
AuditLog row naming the actor, the search term or filter, and how many rows
came back. The gyms are the data fiduciaries here; if one ever asks who
looked at their members, the answer should exist.
"""

PER_PAGE = 50
MIN_SEARCH = 3

# A cap on any single export. The screen is paginated at 50; a CSV is not,
# so without a ceiling one click turns a browsing session into a full extract
# of the estate. Nothing legitimate needs more than this in one file, and the
# audit row records when a request was truncated.
EXPORT_LIMIT = 5000


def _csv_safe(value):
    """Return a cell that a spreadsheet will treat as data, never as code.

    Excel, LibreOffice and Google Sheets all execute a cell beginning with
    =, +, - or @. Member names, plan names and notes are typed by gym staff
    and land in these exports unaltered, so a member saved as
    `=HYPERLINK("http://evil","click")` would become a live formula the
    moment someone opened the file. Prefixing an apostrophe forces it to
    text.

    Numbers are passed through as numbers: quoting them would protect
    nothing and would stop the column adding up, and a negative amount is
    the one legitimate cell that starts with '-'.
    """
    if value is None:
        return ''
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value)
    if text[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + text
    return text


def _csv_response(filename, header, rows):
    """Build a CSV download from already-computed rows.

    The leading BOM is deliberate: without it Excel on Windows reads the file
    as the local codepage and renders every ₹ as mojibake. Every other reader
    ignores it.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_safe(cell) for cell in row])

    return Response(
        '﻿' + buffer.getvalue(),
        # content_type, not mimetype: Flask appends its own charset to a
        # mimetype, so passing one here produced the header
        # "text/csv; charset=utf-8; charset=utf-8".
        content_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


def _date_arg(name):
    """Parse an ISO date from the query string, or None if absent/malformed."""
    raw = (request.args.get(name) or '').strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


# Both views below write their audit row and commit BEFORE fetching the page
# of results, never after. SQLAlchemy expires every loaded instance on
# commit, so committing afterwards marked all 50 fetched rows stale and the
# template then refreshed them one at a time — fifty extra SELECTs, defeating
# the joinedload that exists to prevent exactly that. Measured at 64 queries
# for a 50-row page; ordering the commit first brings it back down.

BILLING_TABS = [
    ('all',      'All'),
    ('active',   'Active'),
    ('expiring', 'Expiring'),
    ('pending',  'Pending'),
    ('overdue',  'Overdue'),
    ('expired',  'Expired'),
]


@operator_bp.route('/billing')
@login_required
@_platform_required
def billing():
    """Every membership across every gym, with the gym-level filter tabs."""
    today      = date.today()
    week_later = today + timedelta(days=7)

    status_filter = request.args.get('status', 'all')
    if status_filter not in dict(BILLING_TABS):
        status_filter = 'all'

    gym_filter = request.args.get('gym', type=int)
    page       = request.args.get('page', 1, type=int)

    date_from = _date_arg('from')
    date_to   = _date_arg('to')

    base = MemberMembership.query
    if gym_filter:
        base = base.filter(MemberMembership.gym_id == gym_filter)
    # The range narrows on end_date — the column the table shows and sorts by,
    # so "from 1 Oct to 31 Oct" means the memberships whose period ends in
    # October, which is what someone reading this screen is asking for.
    if date_from:
        base = base.filter(MemberMembership.end_date >= date_from)
    if date_to:
        base = base.filter(MemberMembership.end_date <= date_to)

    # Imported from the gym-level blueprint rather than restated, so the two
    # billing screens can never disagree about what "Expiring" means.
    query = _apply_status_filter(base, status_filter, today, week_later)

    # Counted off the same `base` the rows come from, so the tab badges agree
    # with the gym and date filters instead of always reporting the estate.
    counts = {key: _apply_status_filter(base, key, today, week_later).count()
              for key, _ in BILLING_TABS}

    gyms      = Gym.query.order_by(Gym.name).all()
    gym_names = {g.id: g.name for g in gyms}

    # Audit first, rows second — see the note above BILLING_TABS.
    # counts[status_filter] is already the number of matching rows, so
    # nothing is lost by logging before the fetch.
    log_action('platform_billing_viewed',
               detail=(f'status={status_filter} '
                       f'gym={gym_names.get(gym_filter, "all")} '
                       f'page={page} matches={counts[status_filter]}'))
    db.session.commit()

    # joinedload, or every row triggers extra queries for its member and
    # plan — the same N+1 Phase 0 removed from the dashboard.
    # Order depends on what you asked for. On a filter that is a to-do list —
    # expiring, unpaid — soonest first puts the most urgent row at the top.
    # On "All" and "Expired" that same order opens on the oldest memberships
    # in the database, which across an estate means a first page of long-dead
    # rows from years ago; newest first is what someone actually wants there.
    oldest_first = status_filter in ('active', 'expiring', 'pending', 'overdue')
    order_by = (MemberMembership.end_date.asc() if oldest_first
                else MemberMembership.end_date.desc())

    pagination = (query
                  .options(joinedload(MemberMembership.member),
                           joinedload(MemberMembership.plan))
                  .order_by(order_by)
                  .paginate(page=page, per_page=PER_PAGE, error_out=False))

    return render_template('operator/billing.html',
                           pagination=pagination,
                           memberships=pagination.items,
                           tabs=BILLING_TABS, counts=counts,
                           status_filter=status_filter,
                           gyms=gyms, gym_names=gym_names,
                           gym_filter=gym_filter, today=today,
                           date_from=date_from, date_to=date_to)


@operator_bp.route('/search')
@login_required
@_platform_required
def search():
    """Find a member by name, phone or email across every gym.

    Deliberately returns nothing until at least MIN_SEARCH characters are
    typed. A one-letter query would match most of the estate, which is a
    bulk export of member contact details dressed up as a search.
    """
    raw   = (request.args.get('q') or '').strip()
    page  = request.args.get('page', 1, type=int)
    today = date.today()

    pagination = None
    too_short  = bool(raw) and len(raw) < MIN_SEARCH

    if raw and not too_short:
        like = f'%{raw}%'
        # .ilike() renders as lower() LIKE lower() on SQLite and ILIKE on
        # Postgres, so this stays case-insensitive on both.
        query = (Member.query
                 .filter(Member.is_erased.is_(False))
                 .filter(or_(Member.first_name.ilike(like),
                             Member.last_name.ilike(like),
                             Member.email.ilike(like),
                             Member.phone.ilike(like),
                             (Member.first_name + ' ' + Member.last_name).ilike(like)))
                 .order_by(Member.first_name, Member.last_name))

        # Counted, logged and committed before the page is fetched, so the
        # commit has nothing loaded to expire. One extra COUNT beats fifty
        # extra row refreshes.
        total = query.count()
        log_action('platform_member_search',
                   detail=f'query="{raw}" page={page} matches={total}')
        db.session.commit()

        pagination = query.paginate(page=page, per_page=PER_PAGE, error_out=False)

    gym_names = {g.id: g.name for g in Gym.query.all()}

    return render_template('operator/search.html',
                           q=raw, too_short=too_short, min_search=MIN_SEARCH,
                           pagination=pagination,
                           members=pagination.items if pagination else [],
                           gym_names=gym_names, today=today)


@operator_bp.route('/finance')
@login_required
@_platform_required
def finance():
    """Per-gym profit and loss for one month, plus a six-month net trend.

    Aggregates only — no member appears on this page, so unlike /billing and
    /search it needs no access log entry.

    What the figures mean, because a P&L that quietly picks its own basis is
    worse than none: revenue is money RECEIVED in the month (membership and
    PT packages, both keyed on payment_date), salaries are money PAID OUT
    (keyed on paid_on), and expenses are keyed on the date they were
    INCURRED, because that is the only date the expense record carries. So
    an unpaid bill entered this month counts against this month. The template
    says so on the page rather than leaving it to be discovered.
    """
    today = date.today()

    # ?month=YYYY-MM, defaulting to the current one. Parsed strictly: a
    # malformed value falls back rather than raising, since it arrives
    # straight off the query string.
    raw_month = request.args.get('month', '')
    month_start = today.replace(day=1)
    if raw_month:
        try:
            year, month = (int(part) for part in raw_month.split('-', 1))
            month_start = date(year, month, 1)
        except (ValueError, TypeError):
            month_start = today.replace(day=1)

    gyms = Gym.query.order_by(Gym.name).all()
    rows, period_start, period_end = finance_rows(gyms, month_start)
    nets, month_list = net_by_month(today, months=6)

    totals = {
        'membership_rev': sum(r['membership_rev'] for r in rows),
        'pt_rev':         sum(r['pt_rev'] for r in rows),
        'revenue':        sum(r['revenue'] for r in rows),
        'expenses':       sum(r['expenses'] for r in rows),
        'salaries':       sum(r['salaries'] for r in rows),
        'costs':          sum(r['costs'] for r in rows),
        'net':            sum(r['net'] for r in rows),
    }
    totals['margin'] = (round(totals['net'] / totals['revenue'] * 100, 1)
                        if totals['revenue'] else None)

    keys   = [(m.year, m.month) for m in month_list]
    labels = [m.strftime('%b %Y') for m in month_list]
    series = [{
        'name':   gym.name,
        'color':  gym.primary_color or '#166534',
        'values': [nets.get(gym.id, {}).get(k, 0.0) for k in keys],
    } for gym in gyms]

    # Months offered in the picker — the same six the chart covers, newest
    # first so the current month is the first option.
    month_options = [{'value': m.strftime('%Y-%m'),
                      'label': m.strftime('%B %Y')}
                     for m in reversed(month_list)]

    return render_template('operator/finance.html',
                           rows=rows, totals=totals,
                           period_start=period_start, period_end=period_end,
                           selected_month=month_start.strftime('%Y-%m'),
                           month_options=month_options,
                           labels=labels, series=series, today=today)


@operator_bp.route('/billing/export.csv')
@login_required
@_platform_required
def billing_export():
    """The billing table as CSV, honouring whatever filters are in the URL."""
    today      = date.today()
    week_later = today + timedelta(days=7)

    status_filter = request.args.get('status', 'all')
    if status_filter not in dict(BILLING_TABS):
        status_filter = 'all'
    gym_filter = request.args.get('gym', type=int)
    date_from  = _date_arg('from')
    date_to    = _date_arg('to')

    base = MemberMembership.query
    if gym_filter:
        base = base.filter(MemberMembership.gym_id == gym_filter)
    if date_from:
        base = base.filter(MemberMembership.end_date >= date_from)
    if date_to:
        base = base.filter(MemberMembership.end_date <= date_to)

    query = _apply_status_filter(base, status_filter, today, week_later)
    total = query.count()

    memberships = (query
                   .options(joinedload(MemberMembership.member),
                            joinedload(MemberMembership.plan))
                   .order_by(MemberMembership.end_date.desc())
                   .limit(EXPORT_LIMIT).all())

    gym_names = {g.id: g.name for g in Gym.query.all()}

    log_action('platform_billing_exported',
               detail=(f'status={status_filter} '
                       f'gym={gym_names.get(gym_filter, "all")} '
                       f'from={date_from or "-"} to={date_to or "-"} '
                       f'rows={len(memberships)} of {total}'))
    db.session.commit()

    rows = [[
        m.id,
        gym_names.get(m.gym_id, ''),
        m.member.first_name if m.member else '',
        m.member.last_name if m.member else '',
        m.member.phone if m.member else '',
        m.member.email if m.member else '',
        m.plan.name if m.plan else '',
        m.start_date.isoformat(),
        m.end_date.isoformat(),
        (m.end_date - today).days,
        float(m.amount or 0),
        m.status,
        m.payment_status,
        m.payment_date.isoformat() if m.payment_date else '',
    ] for m in memberships]

    return _csv_response(
        f'kriyacore-billing-{status_filter}-{today.isoformat()}.csv',
        ['Membership ID', 'Gym', 'First Name', 'Last Name', 'Phone', 'Email',
         'Plan', 'Start Date', 'End Date', 'Days Remaining', 'Amount (INR)',
         'Membership Status', 'Payment Status', 'Payment Date'],
        rows)


@operator_bp.route('/insights/export.csv')
@login_required
@_platform_required
def insights_export():
    """The gym comparison table as CSV. Aggregates only, so no audit entry."""
    today = date.today()
    gyms  = Gym.query.order_by(Gym.name).all()
    rows  = estate_rows(gyms, today)

    return _csv_response(
        f'kriyacore-gyms-{today.isoformat()}.csv',
        ['Gym', 'Active', 'Suspended', 'Members Total', 'Members Active',
         'Live Memberships', 'New This Month', 'Lapsed', 'Lapsed %',
         'Revenue MTD (INR)', 'Outstanding (INR)', 'Unpaid Count',
         'Visits 7d', 'Staff'],
        [[
            r['gym'].name,
            'yes' if r['gym'].is_active else 'no',
            'no' if r['gym'].is_active else 'yes',
            r['members_total'], r['members_active'], r['memberships'],
            r['new_this_month'], r['lapsed'], r['lapsed_pct'],
            r['revenue_month'], r['outstanding'], r['outstanding_n'],
            r['visits_7d'], r['staff_count'],
        ] for r in rows])


@operator_bp.route('/finance/export.csv')
@login_required
@_platform_required
def finance_export():
    """The month's profit and loss as CSV, one row per gym plus a total."""
    today = date.today()

    raw_month = request.args.get('month', '')
    month_start = today.replace(day=1)
    if raw_month:
        try:
            year, month = (int(part) for part in raw_month.split('-', 1))
            month_start = date(year, month, 1)
        except (ValueError, TypeError):
            month_start = today.replace(day=1)

    gyms = Gym.query.order_by(Gym.name).all()
    rows, period_start, period_end = finance_rows(gyms, month_start)

    body = [[
        r['gym'].name, r['membership_rev'], r['pt_rev'], r['revenue'],
        r['expenses'], r['salaries'], r['costs'], r['net'],
        '' if r['margin'] is None else r['margin'],
    ] for r in rows]

    if len(rows) > 1:
        body.append([
            'All gyms',
            sum(r['membership_rev'] for r in rows),
            sum(r['pt_rev'] for r in rows),
            sum(r['revenue'] for r in rows),
            sum(r['expenses'] for r in rows),
            sum(r['salaries'] for r in rows),
            sum(r['costs'] for r in rows),
            sum(r['net'] for r in rows),
            '',
        ])

    return _csv_response(
        f'kriyacore-finance-{period_start.strftime("%Y-%m")}.csv',
        ['Gym', 'Membership Revenue (INR)', 'PT Revenue (INR)',
         'Total Revenue (INR)', 'Running Costs (INR)', 'Salaries (INR)',
         'Total Costs (INR)', 'Net (INR)', 'Margin %'],
        body)


@operator_bp.route('/sentry-test')
@login_required
@_platform_required
def sentry_test():
    """Deliberately raises so you can confirm an event lands in the Sentry
    dashboard after setting SENTRY_DSN. Left unguarded by try/except on
    purpose — Sentry's Flask integration only captures *unhandled*
    exceptions."""
    raise RuntimeError('KriyaCore Sentry test — if you see this in Sentry, monitoring is working.')


@operator_bp.route('/gyms/new', methods=['GET', 'POST'])
@login_required
@_platform_required
def new_gym():
    if request.method == 'POST':
        name         = request.form.get('name', '').strip()
        slug         = request.form.get('slug', '').strip().lower().replace(' ', '-')
        address      = request.form.get('address', '').strip()
        phone        = request.form.get('phone', '').strip()
        email        = request.form.get('email', '').strip().lower()
        admin_name   = request.form.get('admin_name', '').strip()
        admin_email  = request.form.get('admin_email', '').strip().lower()
        admin_pass   = request.form.get('admin_password', '')

        errors = []
        if not name:          errors.append('Gym name is required.')
        if not slug:          errors.append('Slug is required.')
        if not admin_name:    errors.append('Admin name is required.')
        if not admin_email:   errors.append('Admin email is required.')
        if not admin_pass:
            errors.append('Admin password is required.')
        else:
            # This account owns an entire gym's member and billing data —
            # it had no strength check at all before.
            pw_error = validate_password(admin_pass, email=admin_email, name=admin_name)
            if pw_error:
                errors.append(pw_error)
        if slug in RESERVED_SLUGS:
            errors.append(f'"{slug}" is a reserved word and can\'t be used as a gym URL.')
        if Gym.query.filter_by(slug=slug).first():
            errors.append(f'Slug "{slug}" is already taken.')
        if User.query.filter_by(email=admin_email).first():
            errors.append(f'Email "{admin_email}" is already in use.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('operator/new_gym.html',
                                   form=request.form)

        # Brand colour (optional)
        color = request.form.get('primary_color', '').strip()
        brand_color = color if (color and color.startswith('#') and len(color) == 7) else '#166534'

        # Create gym
        gym = Gym(name=name, slug=slug, address=address, phone=phone, email=email,
                  primary_color=brand_color)
        db.session.add(gym)
        db.session.flush()

        # Create gym admin
        admin = User(
            name=admin_name,
            email=admin_email,
            password_hash=generate_password_hash(admin_pass, method='pbkdf2:sha256'),
            role='super_admin',
            gym_id=gym.id,
        )
        db.session.add(admin)

        # Create default plans
        db.session.add_all([
            MembershipPlan(gym_id=gym.id, name='Monthly',   duration_days=30,  price=2500.0),
            MembershipPlan(gym_id=gym.id, name='Quarterly', duration_days=90,  price=6500.0),
            MembershipPlan(gym_id=gym.id, name='Annual',    duration_days=365, price=24000.0),
        ])
        log_action('gym_created', gym=gym,
                   detail=f'Admin: {admin_email} | Color: {brand_color}')
        db.session.commit()

        flash(f'"{name}" has been created. Admin login: {admin_email}', 'success')
        return redirect(url_for('operator.index'))

    return render_template('operator/new_gym.html', form={})


@operator_bp.route('/gyms/<int:gym_id>')
@login_required
@_platform_required
def gym_detail(gym_id):
    gym   = Gym.query.get_or_404(gym_id)
    today = date.today()

    staff = User.query.filter_by(gym_id=gym_id).all()

    # Counted, not fetched. This page shows the two totals and never lists the
    # members themselves, so pulling every row into Python to call len() on it
    # was buying nothing — and would have pulled thousands of rows, each with
    # the member's name, phone and notes, once a gym is real.
    member_count = Member.query.filter_by(gym_id=gym_id).count()
    active_count = Member.query.filter_by(gym_id=gym_id, status='active').count()

    gym.admin = User.query.filter_by(gym_id=gym_id, role='super_admin').first()

    return render_template('operator/gym_detail.html',
                           gym=gym, staff=staff,
                           member_count=member_count,
                           active_count=active_count, today=today)


@operator_bp.route('/gyms/<int:gym_id>/edit', methods=['GET', 'POST'])
@login_required
@_platform_required
def edit_gym(gym_id):
    gym = Gym.query.get_or_404(gym_id)

    if request.method == 'POST':
        gym.name    = request.form.get('name', gym.name).strip()
        gym.address = request.form.get('address', '').strip()
        gym.phone   = request.form.get('phone', '').strip()
        gym.email   = request.form.get('email', '').strip().lower()

        color = request.form.get('primary_color', '').strip()
        if color and color.startswith('#') and len(color) == 7:
            gym.primary_color = color

        logo = request.files.get('logo')
        if logo and logo.filename:
            data = logo.read(MAX_LOGO_BYTES + 1)
            if len(data) > MAX_LOGO_BYTES:
                flash(f'Logo must be under {MAX_LOGO_BYTES // 1024} KB. '
                      f'It is embedded in every page, so keep it small.', 'danger')
                return render_template('operator/edit_gym.html', gym=gym)

            # Sniff the actual bytes rather than trusting logo.content_type —
            # that header is set by the client and can claim anything. SVG is
            # deliberately not accepted: it is a document format that can
            # carry <script>, and there is no good reason for a logo to be one.
            mime = _sniff_image(data)
            if mime is None:
                flash('Logo must be a PNG, JPEG, GIF or WebP image.', 'danger')
                return render_template('operator/edit_gym.html', gym=gym)

            b64 = base64.b64encode(data).decode('utf-8')
            gym.logo_data = f'data:{mime};base64,{b64}'

        if request.form.get('remove_logo'):
            gym.logo_data = None

        changes = []
        if color and color.startswith('#'):
            changes.append(f'color → {color}')
        if logo and logo.filename:
            changes.append('logo uploaded')
        if request.form.get('remove_logo'):
            changes.append('logo removed')
        log_action('branding_updated', gym=gym,
                   detail=', '.join(changes) if changes else 'details only')
        db.session.commit()
        flash(f'"{gym.name}" branding updated.', 'success')
        return redirect(url_for('operator.gym_detail', gym_id=gym.id))

    return render_template('operator/edit_gym.html', gym=gym)


@operator_bp.route('/gyms/<int:gym_id>/set-plan', methods=['POST'])
@login_required
@_platform_required
def set_plan(gym_id):
    gym        = Gym.query.get_or_404(gym_id)
    tier       = request.form.get('plan_tier', '').strip()
    status     = request.form.get('plan_status', 'active').strip()
    expires_str = request.form.get('plan_expires_at', '').strip()

    if tier not in PLANS:
        flash('Invalid plan tier.', 'danger')
        return redirect(url_for('operator.gym_detail', gym_id=gym_id))

    old_tier = gym.plan_tier or 'starter'
    gym.plan_tier   = tier
    gym.plan_status = status if status in ('active', 'expired', 'trial') else 'active'
    gym.plan_expires_at = date.fromisoformat(expires_str) if expires_str else None
    log_action('plan_changed', gym=gym,
               detail=f'{PLANS[old_tier]["label"]} → {PLANS[tier]["label"]} ({status})')
    db.session.commit()

    flash(f'"{gym.name}" subscription updated to {PLANS[tier]["label"]}.', 'success')
    return redirect(url_for('operator.gym_detail', gym_id=gym_id))


@operator_bp.route('/gyms/<int:gym_id>/face-id', methods=['POST'])
@login_required
@_platform_required
def toggle_face_id(gym_id):
    """Enable/disable Face ID for a gym, and (re)generate its webhook
    secret. This is entirely independent of plan tier — a gym either has
    the hardware installed or it doesn't."""
    gym    = Gym.query.get_or_404(gym_id)
    action = request.form.get('action', '')

    if action == 'enable':
        gym.face_id_enabled = True
        if not gym.face_id_webhook_secret:
            gym.face_id_webhook_secret = secrets.token_hex(32)
        log_action('face_id_enabled', gym=gym, detail='Face ID access control enabled')
        flash(f'Face ID enabled for "{gym.name}". Hand the webhook URL below to whoever installs the hardware.', 'success')

    elif action == 'disable':
        gym.face_id_enabled = False
        log_action('face_id_disabled', gym=gym, detail='Face ID access control disabled')
        flash(f'Face ID disabled for "{gym.name}".', 'info')

    elif action == 'regenerate_secret':
        gym.face_id_webhook_secret = secrets.token_hex(32)
        log_action('face_id_secret_rotated', gym=gym, detail='Webhook secret regenerated')
        flash('Webhook secret regenerated — update it on the access-control device too, the old URL stops working immediately.', 'warning')

    elif action == 'set_deny_expired':
        gym.face_id_deny_expired = request.form.get('deny_expired') == 'on'
        state = 'refused' if gym.face_id_deny_expired else 'let in (and flagged)'
        log_action('face_id_policy_changed', gym=gym,
                   detail=f'Lapsed members are now {state} at the door')
        flash(f'Saved — members with an expired membership are {state}.', 'success')

    db.session.commit()
    return redirect(url_for('operator.gym_detail', gym_id=gym_id))


@operator_bp.route('/gyms/<int:gym_id>/reset-password', methods=['POST'])
@login_required
@_platform_required
def reset_password(gym_id):
    user_id      = request.form.get('user_id', type=int)
    new_password = request.form.get('new_password', '').strip()
    confirm      = request.form.get('confirm_password', '').strip()

    if not user_id or not new_password:
        flash('User and new password are required.', 'danger')
        return redirect(url_for('operator.gym_detail', gym_id=gym_id))

    if new_password != confirm:
        flash('Passwords do not match.', 'danger')
        return redirect(url_for('operator.gym_detail', gym_id=gym_id))

    pw_error = validate_password(new_password)
    if pw_error:
        flash(pw_error, 'danger')
        return redirect(url_for('operator.gym_detail', gym_id=gym_id))

    # Security: verify user belongs to this gym
    user = User.query.filter_by(id=user_id, gym_id=gym_id).first()
    if not user:
        flash('User not found in this gym.', 'danger')
        return redirect(url_for('operator.gym_detail', gym_id=gym_id))

    gym = Gym.query.get(gym_id)
    user.password_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
    log_action('password_reset', gym=gym,
               detail=f'Reset for {user.name} ({user.email}) [{user.role}]')
    db.session.commit()
    flash(f'Password reset for {user.name} ({user.email}).', 'success')
    return redirect(url_for('operator.gym_detail', gym_id=gym_id))


@operator_bp.route('/gyms/<int:gym_id>/impersonate', methods=['POST'])
@login_required
@_platform_required
def impersonate(gym_id):
    gym = Gym.query.get_or_404(gym_id)
    if not gym.is_active:
        flash(f'"{gym.name}" is suspended — cannot log in as a suspended gym.', 'danger')
        return redirect(url_for('operator.gym_detail', gym_id=gym_id))

    admin = User.query.filter_by(gym_id=gym_id, role='super_admin').first()
    if not admin:
        flash(f'"{gym.name}" has no super_admin account to impersonate.', 'danger')
        return redirect(url_for('operator.gym_detail', gym_id=gym_id))

    # Stash the real platform admin's ID so we can restore later
    real_name = current_user.name
    session['impersonator_id']  = current_user.id
    session['impersonated_gym'] = gym.name

    log_action('impersonation_started', gym=gym,
               detail=f'{real_name} logged in as {admin.name} ({admin.email})')
    db.session.commit()

    login_user(admin)
    flash(f'👁 Logged in as {admin.name} ({gym.name}). Click "Exit" in the banner to return.', 'info')
    return redirect(url_for('dashboard.index'))


@operator_bp.route('/exit-impersonation', methods=['POST'])
@login_required
def exit_impersonation():
    impersonator_id = session.pop('impersonator_id', None)
    session.pop('impersonated_gym', None)

    if not impersonator_id:
        flash('No active impersonation session.', 'warning')
        return redirect(url_for('dashboard.index'))

    original = User.query.get(impersonator_id)
    if not original or not original.is_platform_admin:
        flash('Invalid impersonation state — please log in again.', 'danger')
        return redirect(url_for('auth.logout'))

    gym_name = session.get('impersonated_gym', 'unknown')
    gym_obj  = Gym.query.filter_by(name=gym_name).first()
    # Log as the impersonated user before restoring original
    entry = AuditLog(
        actor_id=original.id, actor_name=original.name,
        action='impersonation_ended',
        gym_id=gym_obj.id if gym_obj else None,
        gym_name=gym_name,
        detail=f'Session ended for {current_user.name}',
    )
    db.session.add(entry)
    db.session.commit()

    login_user(original)
    flash('Impersonation ended. Welcome back to the platform panel.', 'success')
    return redirect(url_for('operator.index'))


@operator_bp.route('/gyms/<int:gym_id>/toggle', methods=['POST'])
@login_required
@_platform_required
def toggle_gym(gym_id):
    gym = Gym.query.get_or_404(gym_id)
    gym.is_active = not gym.is_active
    status = 'activated' if gym.is_active else 'suspended'
    log_action(f'gym_{status}', gym=gym)
    db.session.commit()
    flash(f'"{gym.name}" has been {status}.', 'info')
    return redirect(url_for('operator.index'))


@operator_bp.route('/audit-log')
@login_required
@_platform_required
def audit_log():
    action_filter = request.args.get('action', '')
    gym_filter    = request.args.get('gym_id', '', type=str)
    page          = request.args.get('page', 1, type=int)

    query = AuditLog.query.order_by(AuditLog.created_at.desc())

    if action_filter:
        query = query.filter(AuditLog.action == action_filter)
    if gym_filter:
        query = query.filter(AuditLog.gym_id == int(gym_filter))

    logs       = query.paginate(page=page, per_page=50, error_out=False)
    gyms       = Gym.query.order_by(Gym.name).all()
    all_actions = db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()
    all_actions = [a[0] for a in all_actions]

    return render_template('operator/audit_log.html',
                           logs=logs, gyms=gyms,
                           all_actions=all_actions,
                           action_filter=action_filter,
                           gym_filter=gym_filter)


@operator_bp.route('/broadcast', methods=['GET', 'POST'])
@login_required
@_platform_required
def broadcast():
    gyms = Gym.query.filter_by(is_active=True).order_by(Gym.name).all()

    if request.method == 'POST':
        title     = request.form.get('title', '').strip()
        message   = request.form.get('message', '').strip()
        target    = request.form.get('target', 'all')          # 'all' or 'select'
        gym_ids   = request.form.getlist('gym_ids', type=int)  # used when target='select'

        errors = []
        if not title:   errors.append('A subject line is required.')
        if not message: errors.append('Message body is required.')
        if target == 'select' and not gym_ids:
            errors.append('Select at least one gym when using "Selected gyms".')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('operator/broadcast.html', gyms=gyms,
                                   form=request.form)

        # Determine recipient gyms
        if target == 'all':
            targets = gyms
        else:
            targets = [g for g in gyms if g.id in gym_ids]

        full_msg = f'[Announcement] {title}: {message}'

        for gym in targets:
            db.session.add(Notification(
                gym_id  = gym.id,
                type    = 'broadcast',
                message = full_msg,
                member_id = None,
                is_read = False,
            ))

        log_action('broadcast_sent',
                   detail=f'"{title}" → {len(targets)} gym(s): {", ".join(g.name for g in targets[:5])}{"…" if len(targets) > 5 else ""}')
        db.session.commit()

        flash(f'Announcement sent to {len(targets)} gym{"s" if len(targets) != 1 else ""}.', 'success')
        return redirect(url_for('operator.broadcast'))

    # Recent broadcasts from audit log
    recent = (AuditLog.query
              .filter_by(action='broadcast_sent')
              .order_by(AuditLog.created_at.desc())
              .limit(10).all())

    return render_template('operator/broadcast.html', gyms=gyms,
                           recent=recent, form={})
