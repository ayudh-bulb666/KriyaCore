import os
import secrets

import click
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

from datetime import timedelta
from pathlib import Path

from flask import Flask, redirect, url_for, request, flash, session
from flask_login import LoginManager, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix

from .helpers import validate_password, format_inr
from .models import db, User, Gym, Member, MembershipPlan, MemberMembership
from .plans import PLANS, FEATURE_ROUTES, plan_has

login_manager = LoginManager()


def _rate_limit_key():
    """Rate-limit per user when we know who they are, per IP otherwise.

    Keying purely on IP is wrong for this app: a gym's staff all sit behind
    one router, so three people at the front desk would share a single
    bucket and throttle each other. Anonymous traffic — crucially the login
    form — still keys on IP, which is exactly where per-IP limiting belongs.
    """
    if current_user.is_authenticated:
        return f'user:{current_user.id}'
    return get_remote_address()


# A ceiling on every endpoint, not just the ones we remembered to decorate.
# With `default_limits=[]` the password change, 2FA disable, staff password
# reset and Face ID webhook were all completely unthrottled.
#
# The numbers are deliberately high. This is a backstop against scripted
# abuse, not a usage quota — a busy front desk must never meet it, and a
# first attempt at 60/minute throttled ordinary browsing. Endpoints that
# actually need a tight limit set their own.
limiter = Limiter(key_func=_rate_limit_key,
                  default_limits=['5000 per hour', '300 per minute'],
                  # Static assets are served by nginx in production; in dev
                  # they'd otherwise eat the allowance a page load at a time.
                  default_limits_exempt_when=lambda: request.endpoint == 'static')
csrf = CSRFProtect()
migrate = Migrate()


def _load_secret_key(is_production):
    """The key that signs session cookies. Never hardcoded.

    There used to be a placeholder default here. It lived in a public repo,
    which meant anyone who read the repo could forge a session cookie for any
    user of any instance that hadn't overridden it. There is no safe value to
    ship, so there is no default.

    Production must supply SECRET_KEY and the app refuses to start without
    it — a loud failure at boot beats finding out from an intrusion.

    Development generates one on first run and keeps it in .secret_key.local
    (git-ignored). Generating a fresh key each start would work but would
    sign you out on every reload, so it's written down — just never committed.
    """
    key = os.environ.get('SECRET_KEY', '').strip()
    if key:
        return key

    if is_production:
        raise RuntimeError(
            'SECRET_KEY is not set and FLASK_ENV=production.\n'
            'It signs every session cookie, so there is no safe default.\n'
            'Generate one and set it in the environment:\n'
            '    python3 -c "import secrets; print(secrets.token_hex(32))"')

    key_file = Path(__file__).resolve().parent.parent / '.secret_key.local'
    if key_file.exists():
        existing = key_file.read_text().strip()
        if existing:
            return existing

    key = secrets.token_hex(32)
    key_file.write_text(key)
    key_file.chmod(0o600)
    print(f'Generated a development SECRET_KEY in {key_file.name} (git-ignored).')
    return key


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

    is_production = os.environ.get('FLASK_ENV') == 'production'
    app.config['SECRET_KEY'] = _load_secret_key(is_production)

    # ── Session cookie hardening ─────────────────────────────────────────────
    # SECURE requires HTTPS, so it's only forced on in production, where TLS
    # is terminated in front of the app (nginx, or a Cloudflare tunnel).
    # Left off locally so http:// dev logins keep working.
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE']   = is_production
    # Exposed to templates so views can hide development-only affordances —
    # e.g. the seeded credentials printed on the login page.
    app.config['IS_PRODUCTION'] = is_production

    # ── Session lifetime ─────────────────────────────────────────────────────
    # Sessions used to last until the browser closed, which on a gym's shared
    # front-desk machine means "forever" — nobody closes that browser. Twelve
    # hours covers a full shift and expires overnight.
    #
    # SESSION_REFRESH_EACH_REQUEST makes it a sliding window: the clock resets
    # on activity, so this logs out idle machines, not busy ones.
    app.config['PERMANENT_SESSION_LIFETIME']  = timedelta(hours=12)
    app.config['SESSION_REFRESH_EACH_REQUEST'] = True
    # "Remember me" is a deliberate, longer-lived choice by the user, but it
    # still has to end — an indefinite cookie on a lost phone is a standing
    # key to the gym's data.
    app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=14)
    app.config['REMEMBER_COOKIE_HTTPONLY'] = True
    app.config['REMEMBER_COOKIE_SECURE']   = is_production

    @app.before_request
    def _enforce_session_lifetime():
        # Flask only applies PERMANENT_SESSION_LIFETIME to sessions marked
        # permanent. Without this the setting above is silently inert.
        session.permanent = True

    # Support Railway PostgreSQL (DATABASE_URL) or fall back to SQLite locally
    _db_url = os.environ.get('DATABASE_URL', 'sqlite:///kriyacore.db')
    if _db_url.startswith('postgres://'):          # Railway uses the old postgres:// scheme
        _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    if _db_url.startswith('postgresql'):
        # Managed Postgres (Supabase, and any pooler in front of it) closes
        # idle connections without telling the client. SQLAlchemy will then
        # hand a dead one to the next request, which surfaces as an
        # intermittent 500 that is miserable to reproduce.
        #
        # pool_pre_ping issues a cheap SELECT 1 before handing a connection
        # out and transparently reconnects if it's gone. pool_recycle drops
        # anything older than five minutes so we rarely get that far.
        #
        # The pool is deliberately small: Supabase's free tier has a modest
        # connection cap, and a two-gym app never needs more than a handful.
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'pool_pre_ping': True,
            'pool_recycle': 300,
            'pool_size': 5,
            'max_overflow': 2,
        }

    # ── Email (SMTP) ───────────────────────────────────────────────────────────
    app.config['MAIL_SERVER']         = os.environ.get('MAIL_SERVER', '')
    app.config['MAIL_PORT']           = int(os.environ.get('MAIL_PORT', 587))
    app.config['MAIL_USE_TLS']        = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
    app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME', '')
    app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD', '')
    app.config['MAIL_DEFAULT_SENDER'] = os.environ.get(
        'MAIL_DEFAULT_SENDER',
        f"KriyaCore <{os.environ.get('MAIL_USERNAME', 'noreply@kriyacore.com')}>"
    )

    db.init_app(app)
    # render_as_batch rebuilds a table to alter it, because SQLite can't ALTER
    # a column in place. Harmless on Postgres, and it keeps a migration
    # written against local SQLite from being one that only runs there.
    migrate.init_app(app, db, render_as_batch=True)
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
    from .expenses      import expenses_bp
    from .cron          import cron_bp
    from .health        import health_bp

    # Gym-facing blueprints are mounted under '/<gym_slug>/...' so each gym
    # gets its own branded URL (e.g. kriyacore.app/powerfit-mumbai/dashboard).
    # register_gym_scoping wires up the slug plumbing + tenant-URL guard —
    # see app/tenant.py for details. auth_bp and operator_bp stay unscoped.
    from .tenant import register_gym_scoping
    # privacy_bp is gym-scoped like the rest, but deliberately absent from
    # plans.py's FEATURE_ROUTES — DPDP data rights are a legal obligation,
    # not a paid feature, so they stay available on every tier.
    for _bp in (dashboard_bp, members_bp, billing_bp, staff_bp,
                attendance_bp, reminders_bp, notifications_bp, whatsapp_bp,
                privacy_bp, expenses_bp):
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
    app.register_blueprint(expenses_bp)

    # Called by a third-party device, not a logged-in browser — no session,
    # no CSRF token to give it. Auth is the gym-specific secret in the URL
    # itself instead (see app/faceid.py).
    app.register_blueprint(faceid_bp)
    csrf.exempt(faceid_bp)

    # Called by an external cron, not a logged-in browser — same reasoning
    # as faceid_bp above. Auth is CRON_SECRET via header (see app/cron.py).
    app.register_blueprint(cron_bp)
    csrf.exempt(cron_bp)

    # Polled by an uptime monitor every few minutes. Exempt from the global
    # rate limit so a short check interval can never produce a 429 and be
    # reported as an outage — see app/health.py.
    app.register_blueprint(health_bp)
    limiter.exempt(health_bp)

    # Expose Python builtins to all templates
    app.jinja_env.globals.update(zip=zip, enumerate=enumerate)
    app.jinja_env.filters['inr'] = format_inr

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

    # ── Security headers ──────────────────────────────────────────────────────
    @app.after_request
    def security_headers(response):
        """Browser-side defences Flask doesn't set on its own.

        Set here rather than in nginx so they hold wherever the app runs —
        behind nginx, on Render, or on a laptop — instead of silently
        disappearing the day the deployment target changes.
        """
        # Clickjacking: nothing in KriyaCore is meant to be framed.
        response.headers.setdefault('X-Frame-Options', 'DENY')
        # Stop the browser guessing a content type (e.g. sniffing an upload
        # into executable script).
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        # Don't leak a gym's URLs — which contain their slug — to third parties.
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        # Turn off browser features the app never uses.
        response.headers.setdefault(
            'Permissions-Policy', 'geolocation=(), microphone=(), camera=(), payment=()')

        # CSP scoped to what the app actually loads.
        #
        # script-src is 'self' only — Tailwind, Alpine and Chart.js are served
        # from static/vendor/ rather than a CDN, so no third party can change
        # the JavaScript running on these pages. That is strictly stronger
        # than subresource integrity, which only detects a swapped file.
        #
        # 'unsafe-inline' and 'unsafe-eval' remain and are not oversights:
        # Tailwind's runtime build compiles classes with eval, and Alpine
        # works through inline attributes and inline <script> blocks. They
        # weaken CSP against injected script, so CSP is the second line here —
        # Jinja's autoescaping is the first. Removing them needs a Tailwind
        # build step and every inline handler moved into a file.
        response.headers.setdefault('Content-Security-Policy', '; '.join([
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com data:",
            "img-src 'self' data:",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]))

        # HSTS only over a real HTTPS connection — sending it over plain http
        # is ignored by browsers, and setting it in local dev would pin
        # localhost to https and break the dev server.
        if request.is_secure:
            response.headers.setdefault(
                'Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        return response

    _register_cli(app)
    return app


def _register_cli(app):
    """CLI commands, so the two things that change a database — migrating it
    and filling it — are deliberate acts rather than side effects of booting.

    `db.create_all()` used to run here on every start. It creates missing
    tables but never alters an existing one, so the day a model gained a
    column, production would boot cleanly and then 500 on the first query
    that touched it. `flask db upgrade` is now the only thing that changes
    the schema.
    """

    def _tables_ready():
        from sqlalchemy import inspect
        if inspect(db.engine).has_table('users'):
            return True
        print('No tables yet — run "flask db upgrade" first.')
        return False

    @app.cli.command('seed')
    def seed_command():
        """Fill an empty database with the DEMO gym, staff and members.

        For demos and local work. On a real instance use create-admin
        instead — nobody wants ten fictional members in their live gym.
        """
        if not _tables_ready():
            return
        if User.query.first():
            print('Already seeded; nothing to do.')
            return
        creds = _seed_data()
        print('\nSeeded demo data. These passwords are generated fresh each')
        print('time and shown ONCE — nothing is stored in the repo.\n')
        width = max(len(e) for e in creds)
        for email, pw in creds.items():
            print(f'  {email:<{width}}  {pw}')
        print('\nDemo data only. On a real instance use "flask create-admin".\n')

    @app.cli.command('create-admin')
    @click.option('--email', prompt='Your email')
    @click.option('--name', prompt='Your name', default='Platform Admin',
                  help='Shown in the app and on audit-log entries.')
    @click.password_option(
        help='At least 10 characters; cannot contain your name or email.')
    def create_admin_command(email, name, password):
        """Create a platform admin on an otherwise empty database.

        This is how a live instance starts: one account that can reach the
        operator panel and create the real gyms from there. No demo data.
        """
        from werkzeug.security import generate_password_hash
        if not _tables_ready():
            return

        email = email.strip().lower()
        pw_error = validate_password(password, email=email, name=name)
        if pw_error:
            print(pw_error)
            return
        if User.query.filter_by(email=email).first():
            print(f'A user with {email} already exists.')
            return

        db.session.add(User(
            name=name.strip() or 'Platform Admin',
            email=email,
            password_hash=generate_password_hash(password, method='pbkdf2:sha256'),
            role='platform_admin',
            gym_id=None,
        ))
        db.session.commit()
        print(f'Platform admin {email} created. Sign in, then add your gyms '
              f'from the operator panel.')


def _seed_data():
    """Seed the database with demo data. Returns {email: password}.

    Passwords are generated fresh each time and returned to the caller to
    print once. They used to be literals — 'admin123' and friends — which
    meant the credentials for every KriyaCore instance were published in a
    public repo. Random ones cost nothing and remove the whole class of
    problem: even a demo instance someone leaves exposed isn't walk-in-able.
    """
    from werkzeug.security import generate_password_hash
    from datetime import date, timedelta

    if User.query.first():
        return {}

    _hash = lambda p: generate_password_hash(p, method='pbkdf2:sha256')

    def _demo_password():
        # Readable enough to retype from a terminal, random enough that
        # knowing this source tells you nothing about any instance.
        return secrets.token_urlsafe(12)

    creds = {}

    # ── Platform Admin (no gym) ────────────────────────────────────────────────
    platform_admin = User(
        name='Platform Admin',
        email='platform@kriyacore.com',
        password_hash=_hash(creds.setdefault('platform@kriyacore.com', _demo_password())),
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
        email='admin@kriyacore.com',
        password_hash=_hash(creds.setdefault('admin@kriyacore.com', _demo_password())),
        role='super_admin',
        gym_id=gym.id,
    )
    trainer1 = User(
        name='Raj Malhotra',
        email='raj@kriyacore.com',
        password_hash=_hash(creds.setdefault('raj@kriyacore.com', _demo_password())),
        role='staff',
        gym_id=gym.id,
    )
    trainer2 = User(
        name='Divya Krishnan',
        email='divya@kriyacore.com',
        password_hash=_hash(creds.setdefault('divya@kriyacore.com', _demo_password())),
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
    return creds
