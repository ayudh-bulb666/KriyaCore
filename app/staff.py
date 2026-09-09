from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash

from .models import (db, User, Member, StaffProfile, StaffShift, PTPackage,
                     PTSession, SalaryPayment, EMPLOYMENT_TYPES, PT_PAY_MODES)
from .helpers import role_required, validate_password
from .plans import plan_within_staff_limit, PLANS

staff_bp = Blueprint('staff', __name__, url_prefix='/<string:gym_slug>/staff')


# ── Small shared helpers ─────────────────────────────────────────────────────

def _month_bounds(ref=None):
    ref = ref or date.today()
    start = ref.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def _parse_month(raw):
    """'YYYY-MM' → (first, last) of that month. Anything unparseable falls
    back to the current month rather than erroring."""
    if raw:
        try:
            y, m = raw.split('-')
            return _month_bounds(date(int(y), int(m), 1))
        except (ValueError, TypeError):
            pass
    return _month_bounds()


def _recent_months(n=6):
    out, cur = [], date.today().replace(day=1)
    for _ in range(n):
        out.append(cur)
        cur = (cur - timedelta(days=1)).replace(day=1)
    return out


def _money(raw, default=None):
    """Parse a rupee figure. Returns (value, error); blank yields `default`.
    Rejected rather than coerced — a typo silently becoming ₹0 would put a
    wrong number into someone's pay."""
    raw = (raw or '').strip().replace(',', '')
    if not raw:
        return default, None
    try:
        v = float(raw)
    except ValueError:
        return None, 'That amount isn\'t a number.'
    if v < 0:
        return None, 'Amount cannot be negative.'
    return round(v, 2), None


def _get_staff(user_id):
    """A staff user in this gym, or 404. Never matches an admin account —
    admins are managed from the operator panel, not here."""
    return User.query.filter_by(
        id=user_id, gym_id=current_user.gym_id, role='staff').first_or_404()


def _profile_for(user):
    """Every staff page assumes a profile exists. Created empty on first
    access rather than at signup, so adding a staff member stays a
    two-field job."""
    if user.profile:
        return user.profile
    prof = StaffProfile(user_id=user.id, gym_id=user.gym_id)
    db.session.add(prof)
    db.session.commit()
    return prof


# ── List ─────────────────────────────────────────────────────────────────────

@staff_bp.route('/')
@login_required
@role_required('super_admin')
def index():
    gid   = current_user.gym_id
    staff = User.query.filter_by(gym_id=gid, role='staff').order_by(User.name).all()
    start, end = _month_bounds()

    rows = []
    for u in staff:
        prof = u.profile
        rows.append({
            'user':        u,
            'profile':     prof,
            'designation': (prof.designation if prof else None) or '—',
            'salary':      prof.salary_amount if prof else None,
            'open_shift':  StaffShift.open_shift_for(gid, u.id),
            'hours':       StaffShift.hours_between(gid, u.id, start, end),
            'pt_sessions': PTSession.count_between(gid, u.id, start, end),
            'pt_payout':   PTSession.payout_between(gid, u.id, start, end),
            'members':     len(u.assigned_members),
        })

    return render_template(
        'staff/index.html',
        rows=rows,
        month_label=start.strftime('%B'),
        on_shift=sum(1 for r in rows if r['open_shift']),
    )


@staff_bp.route('/new', methods=['GET', 'POST'])
@login_required
@role_required('super_admin')
def new():
    gym = current_user.gym
    if not plan_within_staff_limit(gym):
        tier  = gym.plan_tier or 'starter'
        limit = PLANS[tier]['max_staff']
        flash(f'Staff limit reached ({limit} on {PLANS[tier]["label"]} plan). '
              f'Upgrade to add more staff.', 'danger')
        return redirect(url_for('staff.index'))

    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not name or not email or not password:
            flash('Name, email, and password are required.', 'danger')
            return render_template('staff/new.html')
        pw_error = validate_password(password, email=email, name=name)
        if pw_error:
            flash(pw_error, 'danger')
            return render_template('staff/new.html')
        if User.query.filter_by(email=email).first():
            flash('A user with this email already exists.', 'danger')
            return render_template('staff/new.html')

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
            role='staff',
            gym_id=current_user.gym_id,
        )
        db.session.add(user)
        db.session.commit()

        # Straight to their page — the next thing anyone wants is to fill in
        # designation, salary and PT terms while they still have the details.
        flash(f'{name} added. Fill in their employment details below.', 'success')
        return redirect(url_for('staff.detail', user_id=user.id))

    return render_template('staff/new.html')


# ── Detail ───────────────────────────────────────────────────────────────────

@staff_bp.route('/<int:user_id>')
@login_required
@role_required('super_admin')
def detail(user_id):
    gid   = current_user.gym_id
    user  = _get_staff(user_id)
    prof  = _profile_for(user)
    start, end = _parse_month(request.args.get('month', ''))

    shifts = (StaffShift.query
              .filter(StaffShift.gym_id == gid, StaffShift.user_id == user.id,
                      StaffShift.started_at >= datetime.combine(start, datetime.min.time()),
                      StaffShift.started_at <= datetime.combine(end, datetime.max.time()))
              .order_by(StaffShift.started_at.desc()).all())

    sessions = (PTSession.query
                .filter(PTSession.gym_id == gid, PTSession.trainer_id == user.id,
                        PTSession.held_at >= datetime.combine(start, datetime.min.time()),
                        PTSession.held_at <= datetime.combine(end, datetime.max.time()))
                .order_by(PTSession.held_at.desc()).all())

    packages = (PTPackage.query
                .filter_by(gym_id=gid, trainer_id=user.id)
                .order_by(PTPackage.sold_on.desc()).all())

    pt_payout = round(sum(s.trainer_payout for s in sessions), 2)
    base      = float(prof.salary_amount or 0)
    payment   = SalaryPayment.query.filter_by(user_id=user.id, period_month=start).first()

    return render_template(
        'staff/detail.html',
        user=user,
        profile=prof,
        shifts=shifts,
        open_shift=StaffShift.open_shift_for(gid, user.id),
        sessions=sessions,
        packages=packages,
        hours=StaffShift.hours_between(gid, user.id, start, end),
        pt_payout=pt_payout,
        base_salary=base,
        due=base + pt_payout,
        payment=payment,
        employment_types=EMPLOYMENT_TYPES,
        pt_pay_modes=PT_PAY_MODES,
        months=_recent_months(),
        month_start=start,
        month_value=start.strftime('%Y-%m'),
        members=Member.query.filter_by(gym_id=gid, status='active')
                            .order_by(Member.first_name).all(),
        today=date.today().isoformat(),
        now=datetime.now(),
    )


@staff_bp.route('/<int:user_id>/profile', methods=['POST'])
@login_required
@role_required('super_admin')
def save_profile(user_id):
    user = _get_staff(user_id)
    prof = _profile_for(user)
    f    = request.form

    name = f.get('name', '').strip()
    if not name:
        flash('Name can\'t be blank.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))
    user.name = name

    salary, err = _money(f.get('salary_amount'), default=None)
    if err:
        flash(err, 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    pt_mode = f.get('pt_pay_mode', 'none')
    if pt_mode not in dict(PT_PAY_MODES):
        pt_mode = 'none'

    pct, err = _money(f.get('pt_commission_pct'), default=None)
    if err:
        flash('Commission must be a number.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))
    if pct is not None and pct > 100:
        flash('Commission can\'t be more than 100%.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    rate, err = _money(f.get('pt_session_rate'), default=None)
    if err:
        flash('Session rate must be a number.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    # Refuse a mode with no number behind it — a trainer on "commission" with
    # a blank percentage silently earns nothing on every session.
    if pt_mode == 'commission' and not pct:
        flash('Set a commission percentage, or choose a different PT pay mode.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))
    if pt_mode == 'per_session' and not rate:
        flash('Set a per-session rate, or choose a different PT pay mode.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    emp = f.get('employment_type', 'full_time')
    prof.employment_type = emp if emp in dict(EMPLOYMENT_TYPES) else 'full_time'

    prof.phone           = f.get('phone', '').strip() or None
    prof.address         = f.get('address', '').strip() or None
    prof.designation     = f.get('designation', '').strip() or None
    prof.salary_amount   = salary
    prof.pt_pay_mode     = pt_mode
    prof.pt_commission_pct = pct if pt_mode == 'commission' else None
    prof.pt_session_rate   = rate if pt_mode == 'per_session' else None
    prof.emergency_contact_name  = f.get('emergency_contact_name', '').strip() or None
    prof.emergency_contact_phone = f.get('emergency_contact_phone', '').strip() or None
    prof.notes = f.get('notes', '').strip() or None

    for field, attr in (('date_of_birth', 'date_of_birth'), ('joined_on', 'joined_on')):
        raw = f.get(field, '')
        try:
            setattr(prof, attr, date.fromisoformat(raw) if raw else None)
        except ValueError:
            flash('That date isn\'t valid.', 'danger')
            return redirect(url_for('staff.detail', user_id=user.id))

    db.session.commit()
    flash(f'{user.name}\'s details saved.', 'success')
    return redirect(url_for('staff.detail', user_id=user.id))


# ── Shifts ───────────────────────────────────────────────────────────────────

@staff_bp.route('/<int:user_id>/shift/start', methods=['POST'])
@login_required
@role_required('super_admin')
def shift_start(user_id):
    gid  = current_user.gym_id
    user = _get_staff(user_id)

    existing = StaffShift.open_shift_for(gid, user.id)
    if existing:
        flash(f'{user.name} is already clocked in since '
              f'{existing.started_at.strftime("%I:%M %p on %d %b")}.', 'warning')
        return redirect(request.referrer or url_for('staff.detail', user_id=user.id))

    db.session.add(StaffShift(gym_id=gid, user_id=user.id, started_at=datetime.now()))
    db.session.commit()
    flash(f'{user.name} clocked in at {datetime.now().strftime("%I:%M %p")}.', 'success')
    return redirect(request.referrer or url_for('staff.detail', user_id=user.id))


@staff_bp.route('/<int:user_id>/shift/end', methods=['POST'])
@login_required
@role_required('super_admin')
def shift_end(user_id):
    gid  = current_user.gym_id
    user = _get_staff(user_id)

    shift = StaffShift.open_shift_for(gid, user.id)
    if not shift:
        flash(f'{user.name} isn\'t clocked in.', 'warning')
        return redirect(request.referrer or url_for('staff.detail', user_id=user.id))

    shift.ended_at = datetime.now()
    if shift.ended_at < shift.started_at:
        shift.ended_at = shift.started_at
    db.session.commit()

    flash(f'{user.name} clocked out — {shift.hours or 0:g}h logged.', 'success')
    return redirect(request.referrer or url_for('staff.detail', user_id=user.id))


@staff_bp.route('/shift/<int:shift_id>/fix', methods=['POST'])
@login_required
@role_required('super_admin')
def shift_fix(shift_id):
    """Correct a forgotten clock-out by hand.

    The alternative — auto-closing stale shifts at some assumed time — puts
    a number nobody chose into payroll. Making the owner type the real end
    time keeps the hours defensible.
    """
    shift = StaffShift.query.filter_by(
        id=shift_id, gym_id=current_user.gym_id).first_or_404()

    raw = request.form.get('ended_at', '').strip()
    if not raw:
        flash('Enter when the shift actually ended.', 'danger')
        return redirect(request.referrer or url_for('staff.detail', user_id=shift.user_id))

    try:
        ended = datetime.fromisoformat(raw)
    except ValueError:
        flash('That end time isn\'t valid.', 'danger')
        return redirect(request.referrer or url_for('staff.detail', user_id=shift.user_id))

    if ended < shift.started_at:
        flash('A shift can\'t end before it started.', 'danger')
        return redirect(request.referrer or url_for('staff.detail', user_id=shift.user_id))
    if ended > datetime.now():
        flash('A shift can\'t end in the future.', 'danger')
        return redirect(request.referrer or url_for('staff.detail', user_id=shift.user_id))

    shift.ended_at = ended
    db.session.commit()
    flash(f'Shift corrected — {shift.hours:g}h logged.', 'success')
    return redirect(request.referrer or url_for('staff.detail', user_id=shift.user_id))


@staff_bp.route('/shift/<int:shift_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin')
def shift_delete(shift_id):
    shift = StaffShift.query.filter_by(
        id=shift_id, gym_id=current_user.gym_id).first_or_404()
    uid = shift.user_id
    db.session.delete(shift)
    db.session.commit()
    flash('Shift removed.', 'info')
    return redirect(request.referrer or url_for('staff.detail', user_id=uid))


# ── Personal training ────────────────────────────────────────────────────────

@staff_bp.route('/<int:user_id>/pt/package', methods=['POST'])
@login_required
@role_required('super_admin')
def pt_package_new(user_id):
    gid  = current_user.gym_id
    user = _get_staff(user_id)
    f    = request.form

    member = Member.query.filter_by(
        id=f.get('member_id', type=int) or 0, gym_id=gid).first()
    if member is None:
        flash('Pick a member for this package.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    try:
        sessions_total = int(f.get('sessions_total', '0'))
    except ValueError:
        sessions_total = 0
    if sessions_total < 1:
        flash('A package needs at least one session.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    price, err = _money(f.get('price'), default=0.0)
    if err:
        flash(err, 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    sold_raw = f.get('sold_on', '')
    try:
        sold_on = date.fromisoformat(sold_raw) if sold_raw else date.today()
    except ValueError:
        flash('That sale date isn\'t valid.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    expires_raw = f.get('expires_on', '')
    try:
        expires_on = date.fromisoformat(expires_raw) if expires_raw else None
    except ValueError:
        flash('That expiry date isn\'t valid.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))
    if expires_on and expires_on < sold_on:
        flash('A package can\'t expire before it was sold.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    paid = f.get('payment_status') == 'paid'
    db.session.add(PTPackage(
        gym_id=gid, member_id=member.id, trainer_id=user.id,
        sessions_total=sessions_total, price=price,
        sold_on=sold_on, expires_on=expires_on,
        payment_status='paid' if paid else 'pending',
        payment_date=sold_on if paid else None,
        notes=f.get('notes', '').strip() or None,
    ))
    db.session.commit()

    flash(f'{sessions_total}-session package sold to {member.full_name} '
          f'for ₹{price:,.0f}.', 'success')
    return redirect(url_for('staff.detail', user_id=user.id))


@staff_bp.route('/pt/package/<int:package_id>/pay', methods=['POST'])
@login_required
@role_required('super_admin')
def pt_package_pay(package_id):
    pkg = PTPackage.query.filter_by(
        id=package_id, gym_id=current_user.gym_id).first_or_404()
    pkg.payment_status = 'paid'
    pkg.payment_date   = date.today()
    db.session.commit()
    flash(f'Payment recorded for {pkg.member.full_name}\'s PT package.', 'success')
    return redirect(url_for('staff.detail', user_id=pkg.trainer_id))


@staff_bp.route('/pt/package/<int:package_id>/session', methods=['POST'])
@login_required
@role_required('super_admin')
def pt_session_log(package_id):
    """Log a delivered session and freeze the trainer's payout for it."""
    gid = current_user.gym_id
    pkg = PTPackage.query.filter_by(id=package_id, gym_id=gid).first_or_404()

    if pkg.sessions_left <= 0:
        flash(f'All {pkg.sessions_total} sessions on this package are already '
              f'logged. Sell a new package first.', 'warning')
        return redirect(url_for('staff.detail', user_id=pkg.trainer_id))

    raw = request.form.get('held_at', '').strip()
    try:
        held_at = datetime.fromisoformat(raw) if raw else datetime.now()
    except ValueError:
        flash('That session time isn\'t valid.', 'danger')
        return redirect(url_for('staff.detail', user_id=pkg.trainer_id))
    if held_at > datetime.now():
        flash('You can\'t log a session that hasn\'t happened yet.', 'danger')
        return redirect(url_for('staff.detail', user_id=pkg.trainer_id))

    prof   = _profile_for(pkg.trainer)
    payout = prof.payout_for(pkg.price, pkg.sessions_total)

    db.session.add(PTSession(
        gym_id=gid, package_id=pkg.id, member_id=pkg.member_id,
        trainer_id=pkg.trainer_id, held_at=held_at,
        note=request.form.get('note', '').strip() or None,
        trainer_payout=payout,
    ))
    db.session.commit()

    left = pkg.sessions_left
    earned = f' — {pkg.trainer.name} earned ₹{payout:,.0f}' if payout else ''
    flash(f'Session logged for {pkg.member.full_name}. {left} left{earned}.', 'success')
    return redirect(url_for('staff.detail', user_id=pkg.trainer_id))


@staff_bp.route('/pt/session/<int:session_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin')
def pt_session_delete(session_id):
    sess = PTSession.query.filter_by(
        id=session_id, gym_id=current_user.gym_id).first_or_404()
    tid = sess.trainer_id
    db.session.delete(sess)
    db.session.commit()
    flash('Session removed.', 'info')
    return redirect(url_for('staff.detail', user_id=tid))


# ── Salary ───────────────────────────────────────────────────────────────────

@staff_bp.route('/<int:user_id>/salary', methods=['POST'])
@login_required
@role_required('super_admin')
def salary_pay(user_id):
    gid  = current_user.gym_id
    user = _get_staff(user_id)

    start, end = _parse_month(request.form.get('period_month', ''))
    if SalaryPayment.query.filter_by(user_id=user.id, period_month=start).first():
        flash(f'{user.name} has already been paid for '
              f'{start.strftime("%B %Y")}.', 'warning')
        return redirect(url_for('staff.detail', user_id=user.id, month=start.strftime('%Y-%m')))

    prof = _profile_for(user)
    base, err = _money(request.form.get('base_amount'),
                       default=float(prof.salary_amount or 0))
    if err:
        flash(err, 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    pt_default = PTSession.payout_between(gid, user.id, start, end)
    pt_amt, err = _money(request.form.get('pt_amount'), default=pt_default)
    if err:
        flash(err, 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    # Adjustments are the one figure that may be negative — a deduction.
    raw_adj = (request.form.get('adjustment') or '').strip().replace(',', '')
    try:
        adjustment = round(float(raw_adj), 2) if raw_adj else 0.0
    except ValueError:
        flash('Adjustment must be a number.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    if base + pt_amt + adjustment < 0:
        flash('That deduction is larger than the pay. Nothing recorded.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    db.session.add(SalaryPayment(
        gym_id=gid, user_id=user.id, period_month=start,
        base_amount=base, pt_amount=pt_amt, adjustment=adjustment,
        paid_on=date.today(),
        note=request.form.get('note', '').strip() or None,
        recorded_by_name=current_user.name,
    ))
    db.session.commit()

    total = base + pt_amt + adjustment
    flash(f'₹{total:,.0f} recorded as paid to {user.name} for '
          f'{start.strftime("%B %Y")}.', 'success')
    return redirect(url_for('staff.detail', user_id=user.id, month=start.strftime('%Y-%m')))


@staff_bp.route('/salary/<int:payment_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin')
def salary_delete(payment_id):
    pay = SalaryPayment.query.filter_by(
        id=payment_id, gym_id=current_user.gym_id).first_or_404()
    uid, month = pay.user_id, pay.period_month
    db.session.delete(pay)
    db.session.commit()
    flash(f'Salary payment for {month.strftime("%B %Y")} removed.', 'info')
    return redirect(url_for('staff.detail', user_id=uid, month=month.strftime('%Y-%m')))


# ── Credentials ──────────────────────────────────────────────────────────────

@staff_bp.route('/<int:user_id>/reset-password', methods=['POST'])
@login_required
@role_required('super_admin')
def reset_password(user_id):
    user    = _get_staff(user_id)
    new_pw  = request.form.get('new_password', '').strip()
    confirm = request.form.get('confirm_password', '').strip()

    pw_error = validate_password(new_pw, email=user.email, name=user.name)
    if pw_error:
        flash(pw_error, 'danger')
        return redirect(request.referrer or url_for('staff.detail', user_id=user.id))
    if new_pw != confirm:
        flash('Passwords do not match.', 'danger')
        return redirect(request.referrer or url_for('staff.detail', user_id=user.id))

    user.password_hash = generate_password_hash(new_pw, method='pbkdf2:sha256')
    # A password reset is also how you rescue someone locked out by failed
    # attempts, so clear the lock at the same time.
    user.reset_failed_logins()
    db.session.commit()
    flash(f'Password reset for {user.name}.', 'success')
    return redirect(request.referrer or url_for('staff.detail', user_id=user.id))


@staff_bp.route('/<int:user_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin')
def delete(user_id):
    user = User.query.filter_by(id=user_id, gym_id=current_user.gym_id).first_or_404()
    if user.role in ('super_admin', 'platform_admin'):
        flash('Cannot delete an admin account.', 'danger')
        return redirect(url_for('staff.index'))

    # Typing the name is the guard, same as deleting a member.
    typed = request.form.get('confirm_name', '').strip()
    if typed.lower() != user.name.lower():
        flash('The name you typed didn\'t match. Nothing was deleted.', 'danger')
        return redirect(url_for('staff.detail', user_id=user.id))

    name = user.name

    # Everything with an FK to users.id has to be dealt with first, or
    # SQLAlchemy tries to NULL a NOT NULL column and the request 500s.
    # Members are unlinked, not deleted; their history stays.
    for member in user.assigned_members:
        member.assigned_trainer_id = None

    StaffShift.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    SalaryPayment.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    PTSession.query.filter_by(trainer_id=user.id).delete(synchronize_session=False)
    PTPackage.query.filter_by(trainer_id=user.id).delete(synchronize_session=False)
    StaffProfile.query.filter_by(user_id=user.id).delete(synchronize_session=False)

    db.session.delete(user)
    db.session.commit()
    flash(f'{name} and their employment records have been removed.', 'info')
    return redirect(url_for('staff.index'))
