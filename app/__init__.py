import os

from flask import Flask
from flask_login import LoginManager

from .models import db, User, Member, MembershipPlan, MemberMembership

login_manager = LoginManager()


def create_app():
    app = Flask(__name__, template_folder='../templates', static_folder='../static')

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'gympro-dev-secret-change-in-production')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///gympro.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # ── Email (SMTP) — set via environment variables ───────────────────────────
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
    from .auth import auth_bp
    from .dashboard import dashboard_bp
    from .members import members_bp
    from .billing import billing_bp
    from .staff import staff_bp
    from .attendance import attendance_bp
    from .reminders import reminders_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(staff_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(reminders_bp)

    # Expose Python builtins to all templates
    app.jinja_env.globals.update(zip=zip, enumerate=enumerate)

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

    # ── Users ──────────────────────────────────────────────────────────────────
    _hash = lambda p: generate_password_hash(p, method='pbkdf2:sha256')

    admin = User(
        name='Admin User',
        email='admin@gympro.com',
        password_hash=_hash('admin123'),
        role='super_admin',
    )
    trainer1 = User(
        name='Raj Malhotra',
        email='raj@gympro.com',
        password_hash=_hash('staff123'),
        role='staff',
    )
    trainer2 = User(
        name='Divya Krishnan',
        email='divya@gympro.com',
        password_hash=_hash('staff123'),
        role='staff',
    )
    db.session.add_all([admin, trainer1, trainer2])
    db.session.flush()

    # ── Plans ──────────────────────────────────────────────────────────────────
    monthly   = MembershipPlan(name='Monthly',   duration_days=30,  price=2500.00)
    quarterly = MembershipPlan(name='Quarterly', duration_days=90,  price=6500.00)
    annual    = MembershipPlan(name='Annual',    duration_days=365, price=24000.00)
    db.session.add_all([monthly, quarterly, annual])
    db.session.flush()

    # ── Members ────────────────────────────────────────────────────────────────
    today = date.today()

    raw_members = [
        # first, last, email, phone, joining_offset_days, trainer, status
        ('Aarav',   'Sharma',   'aarav.sharma@gmail.com',   '9876543201', 120, trainer1.id, 'active'),
        ('Priya',   'Patel',    'priya.patel@gmail.com',    '8765432102',  90, trainer2.id, 'active'),
        ('Rohit',   'Kumar',    'rohit.kumar@gmail.com',    '9654321203',  60, trainer1.id, 'active'),
        ('Neha',    'Singh',    'neha.singh@gmail.com',     '7543210904', 200, trainer2.id, 'active'),
        ('Arjun',   'Mehta',    'arjun.mehta@gmail.com',    '9812345605',  35, trainer1.id, 'active'),
        ('Kavya',   'Reddy',    'kavya.reddy@gmail.com',    '8901234506', 180,        None, 'inactive'),
        ('Vikram',  'Nair',     'vikram.nair@gmail.com',    '9723456807',  45, trainer2.id, 'active'),
        ('Anjali',  'Gupta',    'anjali.gupta@gmail.com',   '8634567008',  15, trainer1.id, 'active'),
        ('Rahul',   'Verma',    'rahul.verma@gmail.com',    '9545678109',  10, trainer2.id, 'active'),
        ('Sneha',   'Iyer',     'sneha.iyer@gmail.com',     '7456789010',   5, trainer1.id, 'active'),
    ]

    members = []
    for first, last, email, phone, offset, trainer_id, status in raw_members:
        m = Member(
            first_name=first, last_name=last, email=email, phone=phone,
            joining_date=today - timedelta(days=offset),
            assigned_trainer_id=trainer_id,
            status=status,
        )
        db.session.add(m)
        members.append(m)
    db.session.flush()

    # ── Memberships ────────────────────────────────────────────────────────────
    # today = current date; offsets are days from today
    # Memberships designed so 3 are expiring within 7 days (good dashboard demo)
    raw_memberships = [
        # (member_idx, plan, start_off, end_off, status, payment_status)
        (0, monthly,   -120, -90, 'expired', 'paid'),       # Emma – old expired
        (0, quarterly,  -89,   1, 'active',  'paid'),       # Emma – expires in 1 day ⚠️
        (1, quarterly,  -90,   4, 'active',  'paid'),       # Michael – expires in 4 days ⚠️
        (2, monthly,    -60, -30, 'expired', 'paid'),       # Sophia – expired
        (2, monthly,    -29,   6, 'active',  'pending'),    # Sophia – expires in 6 days, payment pending ⚠️
        (3, annual,    -200, 165, 'active',  'paid'),       # James – long-term active
        (4, monthly,    -35,  -5, 'expired', 'paid'),       # Olivia – recently expired
        (4, monthly,     -4,  26, 'active',  'pending'),    # Olivia – new cycle, payment pending
        (5, monthly,   -180,-150, 'expired', 'paid'),       # William – inactive member
        (6, quarterly,  -45,  45, 'active',  'paid'),       # Ava – active
        (7, monthly,    -15,  15, 'active',  'paid'),       # Benjamin – active
        (8, monthly,    -10,  20, 'active',  'paid'),       # Isabella – active
        (9, annual,      -5, 360, 'active',  'pending'),    # Liam – new annual, payment pending
    ]

    for idx, plan, start_off, end_off, status, payment_status in raw_memberships:
        start = today + timedelta(days=start_off)
        mem = MemberMembership(
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
