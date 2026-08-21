import os

from flask import Flask, g, redirect, url_for, request, flash
from flask_login import LoginManager, current_user

from .models import db, User, Gym, Member, MembershipPlan, MemberMembership
from .plans import PLANS, FEATURE_ROUTES, plan_has

login_manager = LoginManager()


def create_app():
    app = Flask(__name__, template_folder='../templates', static_folder='../static')

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'gympro-dev-secret-change-in-production')

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

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(reminders_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(operator_bp)

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
            from datetime import date, timedelta
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
            expiring = MemberMembership.query.filter(
                MemberMembership.gym_id   == gid,
                MemberMembership.status   == 'active',
                MemberMembership.end_date >= today,
                MemberMembership.end_date <= today + timedelta(days=7),
            ).count()
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
