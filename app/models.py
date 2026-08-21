from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, date

db = SQLAlchemy()


class Gym(db.Model):
    __tablename__ = 'gyms'

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    slug          = db.Column(db.String(60), unique=True, nullable=False)
    address       = db.Column(db.String(255))
    phone         = db.Column(db.String(20))
    email         = db.Column(db.String(120))
    is_active     = db.Column(db.Boolean, default=True, nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    # ── White-label branding ──────────────────────────────────────────────────
    primary_color = db.Column(db.String(7), default='#166534')   # hex e.g. #1d4ed8
    logo_data     = db.Column(db.Text, nullable=True)             # base64 data URI

    # ── Subscription ─────────────────────────────────────────────────────────
    plan_tier      = db.Column(db.String(20), default='starter')  # starter / growth / pro
    plan_status    = db.Column(db.String(20), default='active')   # active / expired / trial
    plan_expires_at = db.Column(db.Date, nullable=True)           # None = no expiry (free)

    users       = db.relationship('User',             backref='gym', lazy=True,
                                  foreign_keys='User.gym_id')
    members     = db.relationship('Member',           backref='gym', lazy=True)
    plans       = db.relationship('MembershipPlan',   backref='gym', lazy=True)
    memberships = db.relationship('MemberMembership', backref='gym', lazy=True)
    attendances = db.relationship('Attendance',       backref='gym', lazy=True)
    notifs      = db.relationship('Notification',     backref='gym', lazy=True)

    def __repr__(self):
        return f'<Gym {self.name}>'


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    # roles: 'platform_admin' | 'super_admin' | 'staff'
    role          = db.Column(db.String(20), default='staff')
    gym_id        = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    assigned_members = db.relationship(
        'Member', backref='trainer', lazy=True,
        foreign_keys='Member.assigned_trainer_id'
    )

    @property
    def is_super_admin(self):
        return self.role == 'super_admin'

    @property
    def is_platform_admin(self):
        return self.role == 'platform_admin'

    def __repr__(self):
        return f'<User {self.email}>'


class Member(db.Model):
    __tablename__ = 'members'

    id                  = db.Column(db.Integer, primary_key=True)
    gym_id              = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=False)
    first_name          = db.Column(db.String(50), nullable=False)
    last_name           = db.Column(db.String(50), nullable=False)
    email               = db.Column(db.String(120))
    phone               = db.Column(db.String(20))
    date_of_birth       = db.Column(db.Date)
    joining_date        = db.Column(db.Date, nullable=False)
    assigned_trainer_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    status              = db.Column(db.String(20), default='active')  # active, inactive, suspended
    notes               = db.Column(db.Text)
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)

    memberships = db.relationship(
        'MemberMembership', backref='member', lazy=True,
        order_by='MemberMembership.start_date.desc()'
    )

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'

    @property
    def initials(self):
        return f'{self.first_name[0]}{self.last_name[0]}'.upper()

    @property
    def active_membership(self):
        today = date.today()
        for m in self.memberships:
            if m.end_date >= today and m.status == 'active':
                return m
        return None

    def __repr__(self):
        return f'<Member {self.full_name}>'


class MembershipPlan(db.Model):
    __tablename__ = 'membership_plans'

    id            = db.Column(db.Integer, primary_key=True)
    gym_id        = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=False)
    name          = db.Column(db.String(50), nullable=False)
    duration_days = db.Column(db.Integer, nullable=False)
    price         = db.Column(db.Float, nullable=False)

    memberships = db.relationship('MemberMembership', backref='plan', lazy=True)

    def __repr__(self):
        return f'<Plan {self.name}>'


class Attendance(db.Model):
    __tablename__ = 'attendance'

    id              = db.Column(db.Integer, primary_key=True)
    gym_id          = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=False)
    member_id       = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    check_in        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    check_out       = db.Column(db.DateTime)
    notes           = db.Column(db.Text)
    recorded_by_id  = db.Column(db.Integer, db.ForeignKey('users.id'))

    member      = db.relationship('Member', backref=db.backref('attendance', lazy=True,
                                  order_by='Attendance.check_in.desc()'))
    recorded_by = db.relationship('User', foreign_keys=[recorded_by_id])

    @property
    def duration_minutes(self):
        if self.check_out:
            return int((self.check_out - self.check_in).total_seconds() / 60)
        return None

    @property
    def is_checked_out(self):
        return self.check_out is not None

    def __repr__(self):
        return f'<Attendance member={self.member_id} in={self.check_in}>'


class Notification(db.Model):
    __tablename__ = 'notifications'

    id         = db.Column(db.Integer, primary_key=True)
    gym_id     = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=False)
    type       = db.Column(db.String(50), nullable=False, default='check_in')
    message    = db.Column(db.Text, nullable=False)
    member_id  = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=True)
    is_read    = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    notif_member = db.relationship('Member', foreign_keys=[member_id],
                                   backref=db.backref('notifs', lazy='dynamic'))

    def __repr__(self):
        return f'<Notification {self.type}: {self.message[:40]}>'


class MemberMembership(db.Model):
    __tablename__ = 'member_memberships'

    id             = db.Column(db.Integer, primary_key=True)
    gym_id         = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=False)
    member_id      = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    plan_id        = db.Column(db.Integer, db.ForeignKey('membership_plans.id'), nullable=False)
    start_date     = db.Column(db.Date, nullable=False)
    end_date       = db.Column(db.Date, nullable=False)
    status         = db.Column(db.String(20), default='active')          # active, expired, pending
    payment_status = db.Column(db.String(20), default='pending')         # paid, pending, overdue
    amount         = db.Column(db.Float, nullable=False)
    payment_date   = db.Column(db.Date)
    notes          = db.Column(db.Text)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def days_remaining(self):
        today = date.today()
        return (self.end_date - today).days

    @property
    def is_expiring_soon(self):
        return 0 <= self.days_remaining <= 7

    def __repr__(self):
        return f'<Membership member={self.member_id} plan={self.plan_id}>'


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id          = db.Column(db.Integer, primary_key=True)
    # Who did it — stored as snapshot so log survives user deletion
    actor_id    = db.Column(db.Integer, nullable=True)
    actor_name  = db.Column(db.String(120), nullable=False, default='System')
    # What happened
    action      = db.Column(db.String(60), nullable=False)   # e.g. 'gym_created'
    # Which gym — snapshot so log survives gym deletion
    gym_id      = db.Column(db.Integer, nullable=True)
    gym_name    = db.Column(db.String(120), nullable=True)
    # Human-readable detail
    detail      = db.Column(db.Text, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<AuditLog {self.action} by {self.actor_name}>'
