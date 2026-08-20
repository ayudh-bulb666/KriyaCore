from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, date

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='staff')  # 'super_admin' or 'staff'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assigned_members = db.relationship(
        'Member', backref='trainer', lazy=True,
        foreign_keys='Member.assigned_trainer_id'
    )

    @property
    def is_super_admin(self):
        return self.role == 'super_admin'

    def __repr__(self):
        return f'<User {self.email}>'


class Member(db.Model):
    __tablename__ = 'members'

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    date_of_birth = db.Column(db.Date)
    joining_date = db.Column(db.Date, nullable=False)
    assigned_trainer_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    status = db.Column(db.String(20), default='active')  # active, inactive, suspended
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    duration_days = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)

    memberships = db.relationship('MemberMembership', backref='plan', lazy=True)

    def __repr__(self):
        return f'<Plan {self.name}>'


class Attendance(db.Model):
    __tablename__ = 'attendance'

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    check_in = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    check_out = db.Column(db.DateTime)
    notes = db.Column(db.Text)
    recorded_by_id = db.Column(db.Integer, db.ForeignKey('users.id'))

    member = db.relationship('Member', backref=db.backref('attendance', lazy=True,
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


class MemberMembership(db.Model):
    __tablename__ = 'member_memberships'

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey('membership_plans.id'), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), default='active')         # active, expired, pending
    payment_status = db.Column(db.String(20), default='pending') # paid, pending, overdue
    amount = db.Column(db.Float, nullable=False)
    payment_date = db.Column(db.Date)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def days_remaining(self):
        today = date.today()
        return (self.end_date - today).days

    @property
    def is_expiring_soon(self):
        return 0 <= self.days_remaining <= 7

    def __repr__(self):
        return f'<Membership member={self.member_id} plan={self.plan_id}>'
