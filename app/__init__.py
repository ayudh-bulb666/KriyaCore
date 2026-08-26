import os

import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

from flask import Flask, redirect, url_for, request, flash
from flask_login import LoginManager, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

from .models import db, User, Gym, Member, MembershipPlan, MemberMembership
from .plans import PLANS, FEATURE_ROUTES, plan_has

login_manager = LoginManager()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
csrf = CSRFProtect()


def create_app():
    # ── Error monitoring (Sentry) ────────────────────────────────────────────
    # Fully optional: with no SENTRY_DSN set, this block is skipped and the
    # app behaves exactly as before. Set SENTRY_DSN on Railway whenever the
    # account is ready — no code changes needed at that point.
    sentry_dsn = os.environ.get('SENTRY_DSN', '').strip()
    if sentry_dsn:
        sentry_sdk.init(
            dsn=sentry_dsn,
            integrations=[FlaskIntegration()],
            environment=os.environ.get('FLASK_ENV', 'development'),
            traces_sample_rate=0.1,
            send_default_pii=False,
            # This app has login, change-password, and staff/admin
            # password-reset forms everywhere — never let Sentry capture
            # request bodies, or a crash report could contain a plaintext
            # password.
            max_request_body_size='never',
        )

    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    app.config['SENTRY_ENABLED'] = bool(sentry_dsn)

    # Trust exactly one reverse-proxy hop for client IP / scheme / host.
    # The app is always deployed behind one — Railway's edge, or Nginx on a
    # self-hosted VPS — never exposed directly. Without this, every request
    # reports the proxy's own IP: rate limiting collapses into one shared
    # bucket for all users, and secure-cookie / HTTPS detection breaks.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'gympro-dev-secret-change-in-production')

    # ── Session cookie hardening ─────────────────────────────────────────────
    # SECURE requires HTTPS, so it's only forced on in production (Railway
    # terminates TLS in front of the app). Left off locally so http://
    # dev logins keep working.
    is_production = os.environ.get('FLASK_ENV') == 'production'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE']   = is_production

    # Support Railway PostgreSQL (DATABASE_URL) or fall back to SQLite locally
    _db_url = os.environ.get('DATABASE_URL', 'sqlite:///gympro.db')
    if _db_url.startswith('postgres://'):          # Railway uses the old postgres:// scheme
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # ── Email (SMTP) ───────────────────────────────────────────────────────────
    app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER', '')
    app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
    app.config['MAIL_USE_TLS']        = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
    app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME', '')
    app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD', '')
    app.config['MAIL_DEFAULT_SENDER'] = os.environ.get(
        'MAIL_DEFAULT_SENDER',
        f"GYMPro <{os.environ.get('MAIL_USERNAME', 'noreply@gympro.com')}>"
    )

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'warning'

    # In-memory limiter storage is fine for a single-dyno deploy (Railway).
    # If you scale to multiple workers/instances, point storage_uri at Redis.
    limiter.init_app(app)

    # Protects every POST/PUT/PATCH/DELETE app-wide. Forms carry the token via
    # a hidden csrf_token input; the one AJAX POST (mark-all-read) sends it as
    # an X-CSRFToken header — see templates/base.html.
    csrf.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Register blueprints
    from .auth          import auth_bp
    from .dashboard     import dashboard_bp
    from .members       import members_bp
    from .billing       import billing_bp
    from .staff         import staff_bp
    from .attendance    import attendance_bp
    from .reminders     import reminders_bp
    from .notifications import notifications_bp
    from .operator      import operator_bp
    from .faceid        import faceid_bp
    from .whatsapp      import whatsapp_bp
    from .privacy       import privacy_bp
    from .cron          import cron_bp

    # Gym-facing blueprints are mounted under '/<gym_slug>/...' so each gym
    # gets its own branded URL (e.g. gympro.app/powerfit-mumbai/dashboard).
    # register_gym_scoping wires up the slug plumbing + tenant-URL guard —
    # see app/tenant.py for details. auth_bp and operator_bp stay unscoped.
    from .tenant import register_gym_scoping
    # privacy_bp is gym-scoped like the rest, but deliberately absent from
    # plans.py's FEATURE_ROUTES — DPDP data rights are a legal obligation,
    # not a paid feature, so they stay available on every tier.
    for _bp in (dashboard_bp, members_bp, billing_bp, staff_bp,
                attendance_bp, reminders_bp, notifications_bp, whatsapp_bp,
                privacy_bp):
        register_gym_scoping(_bp)

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(reminders_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(operator_bp)
    app.register_blueprint(whatsapp_bp)
    app.register_blueprint(privacy_bp)

    # Called by a third-party device, not a logged-in browser — no session,
    # no CSRF token to give it. Auth is the gym-specific secret in the URL
    # itself instead (see app/faceid.py).
    app.register_blueprint(faceid_bp)
    csrf.exempt(faceid_bp)

    # Called by an external cron, not a logged-in browser — same reasoning
    # as faceid_bp above. Auth is CRON_SECRET via header (see app/cron.py).
    app.register_blueprint(cron_bp)
    csrf.exempt(cron_bp)

    # Expose Python builtins to all templates
    app.jinja_env.globals.update(zip=zip, enumerate=enumerate)

    # ── Route guard: enforce gym/platform-admin separation ─────────────────────
    @app.before_request
    def enforce_role_routing():
        if not current_user.is_authenticated:
            return
        # Platform admin must stay in /operator or /static
        if current_user.is_platform_admin:
            if not (request.endpoint or '').startswith(('operator.', 'auth.', 'static')):
                return redirect(url_for('operator.index'))
        else:
            # Gym users must not access /operator — except the exit-impersonation route
            if (request.endpoint or '').startswith('operator.') \
                    and request.endpoint != 'operator.exit_impersonation':
                return redirect(url_for('dashboard.index'))

            # Feature gating: block routes the gym's plan doesn't include
            gym = getattr(current_user, 'gym', None)
            if gym:
                endpoint = request.endpoint or ''
                for prefix, feature in FEATURE_ROUTES.items():
                    if endpoint.startswith(prefix) and not plan_has(gym, feature):
                        plan_label = PLANS.get(gym.plan_tier or 'starter', {}).get('label', 'your plan')
                        flash(f'This feature is not available on the {plan_label} plan. Please upgrade.', 'warning')
                        return redirect(url_for('dashboard.index'))

    # ── Inject gym context + notif badge into every template ──────────────────
    @app.context_processor
    def inject_globals():
        ctx = {'notif_badge': 0, 'current_gym': None, 'is_impersonating': False,
               'plan': None, 'plan_features': {}}
        try:
            if not current_user.is_authenticated:
                return ctx
            from flask import session as _s
            ctx['is_impersonating'] = 'impersonator_id' in _s
            if current_user.is_platform_admin:
                return ctx
            from datetime import date
            from .models import Notification, MemberMembership
            gid   = current_user.gym_id
            today = date.today()

            ctx['current_gym'] = current_user.gym
            gym = current_user.gym
            if gym:
                tier = gym.plan_tier or 'starter'
                ctx['plan'] = PLANS.get(tier, PLANS['starter'])
                ctx['plan_features'] = ctx['plan']['features']

            unread   = Notification.query.filter_by(gym_id=gid, is_read=False).count()
            expiring = MemberMembership.expiring_soon_query(gym_id=gid).count()
            unpaid = MemberMembership.query.filter(
                MemberMembership.gym_id        == gid,
                MemberMembership.status        == 'active',
                MemberMembership.payment_status.in_(['pending', 'overdue']),
                MemberMembership.end_date      >= today,
            ).count()
            ctx['notif_badge'] = unread + expiring + unpaid
        except Exception:
            pass
        return ctx

    @app.context_processor
    def inject_plans():
        return {'PLANS': PLANS}

    @app.context_processor
    def inject_sentry_flag():
        return {'SENTRY_ENABLED': app.config['SENTRY_ENABLED']}

    # ── Sentry: tag every event with who + which gym, once a user is known ────
    # No-ops safely if SENTRY_DSN isn't set (sentry_sdk calls are inert until
    # sentry_sdk.init() has actually run).
    @app.before_request
    def _tag_sentry_context():
        if not current_user.is_authenticated:
            return
        sentry_sdk.set_user({'id': current_user.id, 'email': current_user.email})
        sentry_sdk.set_tag('role', current_user.role)
        gym = getattr(current_user, 'gym', None)
        if gym:
            sentry_sdk.set_tag('gym_id', gym.id)
            sentry_sdk.set_tag('gym_name', gym.name)

    # ── Bare '/' — dashboards now live at '/<gym_slug>/...', so send visitors
    #    (PWA start_url, stale bookmarks, a bare domain hit) to the right place.
    @app.route('/')
    def root():
        if current_user.is_authenticated:
            if current_user.is_platform_admin:
                return redirect(url_for('operator.index'))
            if current_user.gym:
                return redirect(url_for('dashboard.index'))
        return redirect(url_for('auth.login'))

    # ── Rate-limit exceeded → friendly message instead of a bare 429 ──────────
    @app.errorhandler(429)
    def rate_limit_exceeded(e):
        flash('Too many attempts. Please wait a minute and try again.', 'danger')
        return redirect(url_for('auth.login'))

    # ── CSRF token missing/expired → bounce back instead of a bare 400 ────────
    @app.errorhandler(CSRFError)
    def csrf_error(e):
        flash('Your session expired — please try that again.', 'warning')
        return redirect(request.referrer or url_for('root'))

    with app.app_context():
        db.create_all()
        _seed_data()

    return app


def _seed_data():
    """Seed the database with initial data on first run."""
    from werkzeug.security import generate_password_hash
    from datetime import date, timedelta

    if User.query.first():
        return  # Already seeded

    _hash = lambda p: generate_password_hash(p, method='pbkdf2:sha256')

    # ── Platform Admin (no gym) ────────────────────────────────────────────────
    platform_admin = User(
        name='Platform Admin',
        email='platform@gympro.com',
        password_hash=_hash('platform123'),
        role='platform_admin',
        gym_id=None,
    )
    db.session.add(platform_admin)
    db.session.flush()

    # ── Demo Gym ───────────────────────────────────────────────────────────────
    gym = Gym(
        name='PowerFit Mumbai',
        slug='powerfit-mumbai',
        address='Andheri West, Mumbai, Maharashtra',
        phone='9820000001',
        email='admin@powerfitmumbai.com',
    )
    db.session.add(gym)
    db.session.flush()

    # ── Gym Users ──────────────────────────────────────────────────────────────
    admin = User(
        name='Admin User',
        email='admin@gympro.com',
        password_hash=_hash('admin123'),
        role='super_admin',
        gym_id=gym.id,
    )
    trainer1 = User(
        name='Raj Malhotra',
        email='raj@gympro.com',
        password_hash=_hash('staff123'),
        role='staff',
        gym_id=gym.id,
    )
    trainer2 = User(
        name='Divya Krishnan',
        email='divya@gympro.com',
        password_hash=_hash('staff123'),
        role='staff',
        gym_id=gym.id,
    )
    db.session.add_all([admin, trainer1, trainer2])
    db.session.flush()

    # ── Plans ──────────────────────────────────────────────────────────────────
    monthly   = MembershipPlan(gym_id=gym.id, name='Monthly',   duration_days=30,  price=2500.00)
    quarterly = MembershipPlan(gym_id=gym.id, name='Quarterly', duration_days=90,  price=6500.00)
    annual    = MembershipPlan(gym_id=gym.id, name='Annual',    duration_days=365, price=24000.00)
    db.session.add_all([monthly, quarterly, annual])
    db.session.flush()

    # ── Members ────────────────────────────────────────────────────────────────
    today = date.today()
    raw_members = [
        ('Aarav',  'Sharma',  'aarav.sharma@gmail.com',  '9876543201', 120, trainer1.id, 'active'),
        ('Priya',  'Patel',   'priya.patel@gmail.com',   '8765432102',  90, trainer2.id, 'active'),
        ('Rohit',  'Kumar',   'rohit.kumar@gmail.com',   '9654321203',  60, trainer1.id, 'active'),
        ('Neha',   'Singh',   'neha.singh@gmail.com',    '7543210904', 200, trainer2.id, 'active'),
        ('Arjun',  'Mehta',   'arjun.mehta@gmail.com',   '9812345605',  35, trainer1.id, 'active'),
        ('Kavya',  'Reddy',   'kavya.reddy@gmail.com',   '8901234506', 180,        None, 'inactive'),
        ('Vikram', 'Nair',    'vikram.nair@gmail.com',   '9723456807',  45, trainer2.id, 'active'),
        ('Anjali', 'Gupta',   'anjali.gupta@gmail.com',  '8634567008',  15, trainer1.id, 'active'),
        ('Rahul',  'Verma',   'rahul.verma@gmail.com',   '9545678109',  10, trainer2.id, 'active'),
        ('Sneha',  'Iyer',    'sneha.iyer@gmail.com',    '7456789010',   5, trainer1.id, 'active'),
    ]

    members = []
    for first, last, email, phone, offset, trainer_id, status in raw_members:
        m = Member(
            gym_id=gym.id,
            first_name=first, last_name=last, email=email, phone=phone,
            joining_date=today - timedelta(days=offset),
            assigned_trainer_id=trainer_id,
            status=status,
        )
        db.session.add(m)
        members.append(m)
    db.session.flush()

    # ── Memberships ────────────────────────────────────────────────────────────
    raw_memberships = [
        (0, monthly,   -120, -90, 'expired', 'paid'),
        (0, quarterly,  -89,   1, 'active',  'paid'),
        (1, quarterly,  -90,   4, 'active',  'paid'),
        (2, monthly,    -60, -30, 'expired', 'paid'),
        (2, monthly,    -29,   6, 'active',  'pending'),
        (3, annual,    -200, 165, 'active',  'paid'),
        (4, monthly,    -35,  -5, 'expired', 'paid'),
        (4, monthly,     -4,  26, 'active',  'pending'),
        (5, monthly,   -180,-150, 'expired', 'paid'),
        (6, quarterly,  -45,  45, 'active',  'paid'),
        (7, monthly,    -15,  15, 'active',  'paid'),
        (8, monthly,    -10,  20, 'active',  'paid'),
        (9, annual,      -5, 360, 'active',  'pending'),
    ]

    for idx, plan, start_off, end_off, status, payment_status in raw_memberships:
        start = today + timedelta(days=start_off)
        mem = MemberMembership(
            gym_id=gym.id,
            member_id=members[idx].id,
            plan_id=plan.id,
            start_date=start,
            end_date=today + timedelta(days=end_off),
            status=status,
            payment_status=payment_status,
            amount=plan.price,
            payment_date=start if payment_status == 'paid' else None,
        )
        db.session.add(mem)

    db.session.commit()
