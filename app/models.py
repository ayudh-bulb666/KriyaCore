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

    # ── Face ID access control ──────────────────────────────────────────────
    # Per-gym opt-in, independent of plan tier — some gyms have the hardware
    # installed, most won't for a long while. GYMPro never stores biometric
    # data itself; a third-party terminal does face capture/matching and
    # pushes scan events to face_id_webhook_secret's URL (see app/faceid.py).
    face_id_enabled       = db.Column(db.Boolean, default=False, nullable=False)
    face_id_webhook_secret = db.Column(db.String(64), nullable=True)
    # Whether the webhook tells the door to refuse someone whose membership
    # has run out. Defaults to False — fail open. Wrongly locking a paying
    # member out of the gym they've paid for is a worse failure than letting
    # a lapsed one in, and staff get told either way.
    face_id_deny_expired  = db.Column(db.Boolean, default=False, nullable=False)

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

    # ── Brute-force protection ──────────────────────────────────────────────
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until           = db.Column(db.DateTime, nullable=True)

    # ── Two-factor authentication (TOTP) ────────────────────────────────────
    # totp_secret is set the moment a user starts setup, but totp_enabled
    # only flips to True once they've confirmed a real code — so a half-
    # finished setup never accidentally locks someone out.
    totp_secret      = db.Column(db.String(32), nullable=True)
    totp_enabled     = db.Column(db.Boolean, default=False, nullable=False)
    totp_enrolled_at = db.Column(db.DateTime, nullable=True)

    backup_codes = db.relationship(
        'TOTPBackupCode', backref='user', lazy=True,
        cascade='all, delete-orphan'
    )

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

    @property
    def is_locked(self):
        return self.locked_until is not None and self.locked_until > datetime.utcnow()

    @property
    def unused_backup_codes_count(self):
        return sum(1 for c in self.backup_codes if c.used_at is None)

    def register_failed_login(self, max_attempts=5, lockout_minutes=15):
        """Call after a wrong password. Locks the account once max_attempts is hit."""
        from datetime import timedelta
        self.failed_login_attempts = (self.failed_login_attempts or 0) + 1
        if self.failed_login_attempts >= max_attempts:
            self.locked_until = datetime.utcnow() + timedelta(minutes=lockout_minutes)

    def reset_failed_logins(self):
        self.failed_login_attempts = 0
        self.locked_until = None

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

    # ── Face ID access control ──────────────────────────────────────────────
    # face_id_external_id is whatever opaque ID the gym's access-control
    # terminal assigns after enrolling this member's face on its own device —
    # GYMPro never receives or stores the face image/template itself.
    face_id_external_id = db.Column(db.String(80), nullable=True)
    face_id_consent_at  = db.Column(db.DateTime, nullable=True)

    # ── WhatsApp messaging ────────────────────────────────────────────────
    # Required opt-in before the gym can message this member — WhatsApp
    # Business Platform policy, not just good practice.
    whatsapp_opt_in    = db.Column(db.Boolean, default=False, nullable=False)
    whatsapp_opt_in_at = db.Column(db.DateTime, nullable=True)

    # ── DPDP erasure ────────────────────────────────────────────────────────
    # An erased member's PII is scrubbed in place rather than the row being
    # deleted — their MemberMembership rows carry financial history the gym
    # is legally required to retain, and those rows can't survive a hard
    # delete of the member they point at. See app/privacy.py:erase_member.
    is_erased = db.Column(db.Boolean, default=False, nullable=False)
    erased_at = db.Column(db.DateTime, nullable=True)

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

    @property
    def membership_state(self):
        """One place that decides where a member stands, so the Face ID door,
        the arrivals log and the front desk can never disagree.

        Returns (state, message):
          active   — good to go
          expiring — valid but inside the 7-day window; worth a nudge
          unpaid   — inside their dates but payment is pending/overdue
          expired  — had a membership, it has run out
          none     — never had one at all
        """
        active = self.active_membership
        if active is None:
            ever = bool(self.memberships)
            if not ever:
                return 'none', 'No membership on record'
            last = max(self.memberships, key=lambda m: m.end_date)
            days = (date.today() - last.end_date).days
            if days == 0:
                return 'expired', f'{last.plan.name} membership expired today'
            return 'expired', f'{last.plan.name} membership expired {days} day{"s" if days != 1 else ""} ago'

        if active.payment_status in ('pending', 'overdue'):
            return 'unpaid', f'{active.plan.name} payment is {active.payment_status}'

        if active.is_expiring_soon:
            d = active.days_remaining
            when = 'today' if d == 0 else ('tomorrow' if d == 1 else f'in {d} days')
            return 'expiring', f'{active.plan.name} expires {when}'

        return 'active', f'{active.plan.name} until {active.end_date.strftime("%d %b %Y")}'

    @property
    def is_face_id_enrolled(self):
        return bool(self.face_id_external_id and self.face_id_consent_at)

    @property
    def whatsapp_reachable(self):
        return bool(self.phone and self.whatsapp_opt_in and self.whatsapp_opt_in_at)

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


# A second scan within this many minutes of a member's last visit is treated
# as the same arrival, not a new one. Face readers at a door re-trigger
# easily — the member steps out to take a call, the door doesn't open first
# time, the camera catches them twice — and with no check-out there's
# nothing to absorb that, so every stray scan would otherwise become a
# phantom visit. A genuine second session later in the day still records.
VISIT_COOLDOWN_MINUTES = 60


class Attendance(db.Model):
    """One row = one gym visit. Arrival only, deliberately.

    There is no check-out: most gyms have nobody stationed at the door to
    record people leaving, so a check_out column would sit permanently
    empty and make every member look like they never left. Recording the
    arrival is the part that's actually true.
    """
    __tablename__ = 'attendance'

    id              = db.Column(db.Integer, primary_key=True)
    gym_id          = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=False)
    member_id       = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    # Server-local time, NOT UTC — unlike every other timestamp in this file.
    # Attendance is a local-clock concept ("who came in today"), and the log
    # is filtered and displayed by local date. Both write paths (Face ID
    # webhook, manual button) and the cooldown lookup must use the same
    # clock or the dedup silently stops matching. Assumes the server clock
    # is set to the gym's timezone — true for a single-region deployment;
    # a multi-region future would need a per-gym timezone column.
    visited_at      = db.Column(db.DateTime, nullable=False, default=datetime.now)
    notes           = db.Column(db.Text)
    recorded_by_id  = db.Column(db.Integer, db.ForeignKey('users.id'))
    source          = db.Column(db.String(20), default='manual', nullable=False)  # manual | face_id
    # Membership standing at the moment they walked in, snapshotted because it
    # changes later — a visit logged while someone was lapsed should still read
    # as lapsed after they renew.
    membership_status = db.Column(db.String(20), nullable=True)  # active|expiring|unpaid|expired|none

    member      = db.relationship('Member', backref=db.backref('attendance', lazy=True,
                                  order_by='Attendance.visited_at.desc()'))
    recorded_by = db.relationship('User', foreign_keys=[recorded_by_id])

    @staticmethod
    def recent_visit(gym_id, member_id, at=None, minutes=VISIT_COOLDOWN_MINUTES):
        """The member's last visit inside the cooldown window, if any.
        Shared by the Face ID webhook and the manual check-in button so both
        entry points dedupe identically."""
        from datetime import timedelta
        at = at or datetime.now()   # local clock — must match visited_at
        return (Attendance.query
                .filter(Attendance.gym_id    == gym_id,
                        Attendance.member_id == member_id,
                        Attendance.visited_at >= at - timedelta(minutes=minutes),
                        Attendance.visited_at <= at)
                .order_by(Attendance.visited_at.desc())
                .first())

    def __repr__(self):
        return f'<Attendance member={self.member_id} at={self.visited_at}>'


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

    # Set once an automatic WhatsApp expiry reminder has gone out for this
    # specific billing cycle — stops the daily cron from re-sending the same
    # reminder every day the membership stays inside the reminder window.
    reminder_sent_at = db.Column(db.DateTime, nullable=True)

    @property
    def days_remaining(self):
        today = date.today()
        return (self.end_date - today).days

    @property
    def is_expiring_soon(self):
        return 0 <= self.days_remaining <= 7

    @staticmethod
    def expiring_soon_query(gym_id=None, days=7):
        """Active memberships expiring within `days`. Pass gym_id to scope to
        one gym, or leave it None for a platform-wide query (operator panel).
        Returns an unexecuted query — chain .count() or .order_by(...).all()."""
        from datetime import timedelta
        today = date.today()
        q = MemberMembership.query.filter(
            MemberMembership.status   == 'active',
            MemberMembership.end_date >= today,
            MemberMembership.end_date <= today + timedelta(days=days),
        )
        if gym_id is not None:
            q = q.filter(MemberMembership.gym_id == gym_id)
        return q

    @staticmethod
    def unpaid_query(gym_id=None):
        """Active memberships with pending/overdue payment. Same gym_id
        convention as expiring_soon_query."""
        q = MemberMembership.query.filter(
            MemberMembership.status         == 'active',
            MemberMembership.payment_status.in_(['pending', 'overdue']),
            MemberMembership.end_date       >= date.today(),
        )
        if gym_id is not None:
            q = q.filter(MemberMembership.gym_id == gym_id)
        return q

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


class WhatsAppMessage(db.Model):
    """One row per member per send — a log/audit trail, not a queue worker.
    Sending is synchronous (see app/whatsapp_provider.py); at this volume
    (a couple hundred members) that's fine and keeps this simple."""
    __tablename__ = 'whatsapp_messages'

    id         = db.Column(db.Integer, primary_key=True)
    gym_id     = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=False)
    member_id  = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)

    # expiry_reminder | renewal_confirmation | closure | event
    message_type = db.Column(db.String(30), nullable=False)
    # The approved WhatsApp template this maps to once a real provider is
    # wired in — see app/whatsapp_provider.py's TEMPLATES.
    template_name = db.Column(db.String(60), nullable=False)
    # Rendered, human-readable text — for the log/audit view only, not what
    # gets sent (the provider sends template_name + variables).
    body_preview  = db.Column(db.Text, nullable=False)

    # queued_no_provider | sent | failed | skipped_no_optin
    status            = db.Column(db.String(30), nullable=False, default='queued_no_provider')
    provider_message_id = db.Column(db.String(120), nullable=True)
    error_message     = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sent_at    = db.Column(db.DateTime, nullable=True)

    gym    = db.relationship('Gym',    backref=db.backref('whatsapp_messages', lazy=True))
    member = db.relationship('Member', backref=db.backref('whatsapp_messages', lazy=True,
                              order_by='WhatsAppMessage.created_at.desc()'))

    def __repr__(self):
        return f'<WhatsAppMessage {self.message_type} to member={self.member_id} [{self.status}]>'


class WhatsAppSettings(db.Model):
    """One row per gym — admin-controlled automation timing and message
    wording. A blank template column means "use the built-in default text"
    (see app/whatsapp_provider.py's TEMPLATES); a gym only gets a row here
    once they've actually opened the Settings page."""
    __tablename__ = 'whatsapp_settings'

    id     = db.Column(db.Integer, primary_key=True)
    gym_id = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=False, unique=True)

    auto_expiry_reminders_enabled = db.Column(db.Boolean, default=False, nullable=False)
    remind_days_before            = db.Column(db.Integer, default=7, nullable=False)

    # NULL = fall back to the built-in default text for that message type.
    expiry_reminder_template      = db.Column(db.Text, nullable=True)
    renewal_confirmation_template = db.Column(db.Text, nullable=True)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    gym = db.relationship('Gym', backref=db.backref('whatsapp_settings', uselist=False))

    def __repr__(self):
        return f'<WhatsAppSettings gym={self.gym_id}>'


class TOTPBackupCode(db.Model):
    """One-time recovery codes generated at 2FA enrollment — the safety net
    for a lost authenticator device. Hashed like a password, never stored
    or displayed in plaintext after the moment they're first generated."""
    __tablename__ = 'totp_backup_codes'

    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    code_hash = db.Column(db.String(256), nullable=False)
    used_at   = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<TOTPBackupCode user={self.user_id} used={self.used_at is not None}>'


class DataRequest(db.Model):
    """A member's DPDP request (access/portability or erasure) and how the
    gym responded. This log is the gym's evidence that it honoured the
    request — so every field it needs is snapshotted at write time, letting
    the row stay meaningful even after the member it refers to is erased."""
    __tablename__ = 'data_requests'

    id        = db.Column(db.Integer, primary_key=True)
    gym_id    = db.Column(db.Integer, db.ForeignKey('gyms.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=True)

    # Snapshot — an erased member's name is scrubbed, but the gym still needs
    # to show *whose* request this was if a regulator ever asks.
    member_name_snapshot = db.Column(db.String(120), nullable=False)

    request_type = db.Column(db.String(20), nullable=False)   # export | erasure
    status       = db.Column(db.String(20), nullable=False, default='pending')  # pending | completed

    requested_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    handled_by_name = db.Column(db.String(120), nullable=True)
    notes           = db.Column(db.Text, nullable=True)

    gym    = db.relationship('Gym',    backref=db.backref('data_requests', lazy=True))
    member = db.relationship('Member', backref=db.backref('data_requests', lazy=True))

    def __repr__(self):
        return f'<DataRequest {self.request_type} for {self.member_name_snapshot} [{self.status}]>'
