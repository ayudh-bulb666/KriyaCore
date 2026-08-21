import base64
from flask import Blueprint, render_template, redirect, url_for, request, flash, session
from flask_login import login_required, current_user, login_user
from werkzeug.security import generate_password_hash
from datetime import date, timedelta
from sqlalchemy import func

from .models import db, Gym, User, Member, MemberMembership, MembershipPlan, AuditLog, Notification
from .helpers import role_required
from .plans import PLANS


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

    expiring_across = MemberMembership.query.filter(
        MemberMembership.status   == 'active',
        MemberMembership.end_date >= today,
        MemberMembership.end_date <= today + timedelta(days=7),
    ).count()

    # ── Per-gym stats ─────────────────────────────────────────────────────────
    for gym in gyms:
        gym.active_member_count = Member.query.filter_by(gym_id=gym.id, status='active').count()
        gym.total_member_count  = Member.query.filter_by(gym_id=gym.id).count()
        gym.active_memberships  = MemberMembership.query.filter(
            MemberMembership.gym_id   == gym.id,
            MemberMembership.status   == 'active',
            MemberMembership.end_date >= today,
        ).count()
        gym.revenue_month = db.session.query(
            func.sum(MemberMembership.amount)
        ).filter(
            MemberMembership.gym_id         == gym.id,
            MemberMembership.payment_status == 'paid',
            MemberMembership.payment_date   >= this_month,
        ).scalar() or 0
        gym.staff_count = User.query.filter_by(gym_id=gym.id).count()
        gym.admin = User.query.filter_by(gym_id=gym.id, role='super_admin').first()

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
        if not admin_pass:    errors.append('Admin password is required.')
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

    staff   = User.query.filter_by(gym_id=gym_id).all()
    members = Member.query.filter_by(gym_id=gym_id).all()
    active  = [m for m in members if m.status == 'active']
    gym.admin = User.query.filter_by(gym_id=gym_id, role='super_admin').first()

    return render_template('operator/gym_detail.html',
                           gym=gym, staff=staff,
                           members=members, active=active, today=today)


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
            allowed = {'image/png', 'image/jpeg', 'image/jpg', 'image/svg+xml', 'image/webp'}
            if logo.content_type not in allowed:
                flash('Logo must be a PNG, JPG, SVG, or WebP image.', 'danger')
                return render_template('operator/edit_gym.html', gym=gym)
            data    = logo.read()
            b64     = base64.b64encode(data).decode('utf-8')
            gym.logo_data = f'data:{logo.content_type};base64,{b64}'

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

    if len(new_password) < 8:
        flash('Password must be at least 8 characters.', 'danger')
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
