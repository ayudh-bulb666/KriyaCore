import csv
import io
from flask import Blueprint, render_template, redirect, url_for, request, flash, Response
from flask_login import login_required, current_user
from datetime import date, datetime

from .models import (db, Member, User, MemberMembership, Attendance,
                     Notification, WhatsAppMessage, DataRequest)
from .helpers import role_required, own_gym_id
from .plans import plan_within_member_limit, plan_has, PLANS

members_bp = Blueprint('members', __name__, url_prefix='/<string:gym_slug>/members')


# The tab bar across the top of the member list, mirroring Billing's.
# Keys map to Member.membership_state, except 'inactive', which is the
# member's own account status — a suspended member still has a membership
# state, and staff need one control that surfaces both kinds of problem.
MEMBER_TABS = [
    ('all',      'All'),
    ('active',   'Active'),
    ('expiring', 'Expiring'),
    ('unpaid',   'Unpaid'),
    ('expired',  'Expired'),
    ('none',     'No plan'),
    ('inactive', 'Inactive'),
]


def _matches_state(member, state, key):
    if key == 'all':
        return True
    if key == 'inactive':
        return member.status != 'active'
    return state == key


def _filtered_members(gid, search, state_filter, trainer_filter):
    """Shared by index() and export_csv() so the list on screen and the
    exported CSV are always the same set of people.

    Text and trainer narrow the query in SQL; membership state is decided in
    Python by Member.membership_state. That's deliberate — that property is
    the single source of truth the Face ID door and front desk also read, so
    filtering through it means a tab count can never disagree with the badge
    printed on the row. At a few hundred members a gym it costs nothing.
    """
    query = Member.query.filter_by(gym_id=gid)

    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(
                Member.first_name.ilike(like),
                Member.last_name.ilike(like),
                Member.email.ilike(like),
                Member.phone.ilike(like),
            )
        )
    if trainer_filter:
        if trainer_filter == 'none':
            query = query.filter(Member.assigned_trainer_id.is_(None))
        else:
            query = query.filter_by(assigned_trainer_id=int(trainer_filter))

    rows = query.order_by(Member.first_name.asc()).all()

    # One pass: decide each member's state once, then bucket for the counts
    # and filter for the visible list off that same answer.
    counts  = {key: 0 for key, _ in MEMBER_TABS}
    visible = []
    for m in rows:
        state, message = m.membership_state
        m.state         = state          # stashed for the template badge
        m.state_message = message
        for key, _ in MEMBER_TABS:
            if _matches_state(m, state, key):
                counts[key] += 1
        if _matches_state(m, state, state_filter):
            visible.append(m)

    return visible, counts


@members_bp.route('/')
@login_required
def index():
    gid            = current_user.gym_id
    search         = request.args.get('search', '').strip()
    state_filter   = request.args.get('state', 'all')
    trainer_filter = request.args.get('trainer', '')

    if state_filter not in dict(MEMBER_TABS):
        state_filter = 'all'

    members, counts = _filtered_members(gid, search, state_filter, trainer_filter)
    trainers = User.query.filter_by(gym_id=gid, role='staff').order_by(User.name).all()

    return render_template(
        'members/index.html',
        members=members,
        trainers=trainers,
        search=search,
        state_filter=state_filter,
        trainer_filter=trainer_filter,
        tabs=MEMBER_TABS,
        counts=counts,
    )


@members_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    gid      = current_user.gym_id
    trainers = User.query.filter_by(gym_id=gid, role='staff').order_by(User.name).all()

    # Plan limit check
    gym = current_user.gym
    if not plan_within_member_limit(gym):
        tier = gym.plan_tier or 'starter'
        limit = PLANS[tier]['max_members']
        flash(f'Member limit reached ({limit} on {PLANS[tier]["label"]} plan). Upgrade to add more members.', 'danger')
        return redirect(url_for('members.index'))

    if request.method == 'POST':
        first_name  = request.form.get('first_name', '').strip()
        last_name   = request.form.get('last_name', '').strip()
        email       = request.form.get('email', '').strip()
        phone       = request.form.get('phone', '').strip()
        dob_str     = request.form.get('date_of_birth', '')
        joining_str = request.form.get('joining_date', '')
        trainer_id  = request.form.get('assigned_trainer_id') or None
        status      = request.form.get('status', 'active')
        notes       = request.form.get('notes', '').strip()

        if not first_name or not last_name or not joining_str:
            flash('First name, last name, and joining date are required.', 'danger')
            return render_template('members/new.html', trainers=trainers)

        dob     = date.fromisoformat(dob_str) if dob_str else None
        joining = date.fromisoformat(joining_str)

        member = Member(
            gym_id=gid,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            date_of_birth=dob,
            joining_date=joining,
            # Validated, not trusted: an unchecked id here attaches another
            # gym's trainer to our member, leaking their name into this gym.
            assigned_trainer_id=own_gym_id(User, trainer_id, gid, role='staff'),
            status=status,
            notes=notes,
        )
        db.session.add(member)
        db.session.commit()
        flash(f'{member.full_name} has been added successfully.', 'success')
        return redirect(url_for('members.detail', member_id=member.id))

    return render_template('members/new.html', trainers=trainers, today=date.today().isoformat())


@members_bp.route('/<int:member_id>')
@login_required
def detail(member_id):
    member = Member.query.filter_by(id=member_id, gym_id=current_user.gym_id).first_or_404()
    return render_template('members/detail.html', member=member, today=date.today())


@members_bp.route('/<int:member_id>/face-id', methods=['POST'])
@login_required
def set_face_id(member_id):
    """Record the enrollment ID the gym's access-control terminal assigned
    this member, plus their consent to biometric processing. KriyaCore never
    handles the actual face data — only this opaque mapping."""
    gym = current_user.gym
    if not gym or not gym.face_id_enabled:
        flash('Face ID is not enabled for this gym.', 'danger')
        return redirect(url_for('members.index'))

    member = Member.query.filter_by(id=member_id, gym_id=gym.id).first_or_404()
    action = request.form.get('action', '')

    if action == 'unenroll':
        member.face_id_external_id = None
        member.face_id_consent_at  = None
        db.session.commit()
        flash(f'Face ID enrollment removed for {member.full_name}.', 'info')
        return redirect(url_for('members.detail', member_id=member.id))

    external_id = request.form.get('external_id', '').strip()
    consent     = request.form.get('consent') == 'on'

    if not external_id:
        flash('Enter the ID the terminal assigned this member after enrolling their face on it.', 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    if not consent:
        flash("Member consent is required before storing a Face ID enrollment.", 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    member.face_id_external_id = external_id
    member.face_id_consent_at  = datetime.utcnow()
    db.session.commit()
    flash(f'Face ID enrollment saved for {member.full_name}.', 'success')
    return redirect(url_for('members.detail', member_id=member.id))


@members_bp.route('/<int:member_id>/whatsapp-optin', methods=['POST'])
@login_required
def set_whatsapp_optin(member_id):
    """Toggle WhatsApp consent for a member. Required before the gym can
    message them — WhatsApp Business Platform policy, not just courtesy."""
    gym = current_user.gym
    if not gym or not plan_has(gym, 'whatsapp'):
        flash('WhatsApp messaging is not available on this plan.', 'danger')
        return redirect(url_for('members.index'))

    member = Member.query.filter_by(id=member_id, gym_id=gym.id).first_or_404()
    action = request.form.get('action', '')

    if action == 'opt_out':
        member.whatsapp_opt_in    = False
        member.whatsapp_opt_in_at = None
        db.session.commit()
        flash(f'{member.full_name} opted out of WhatsApp messages.', 'info')
        return redirect(url_for('members.detail', member_id=member.id))

    if not member.phone:
        flash('Add a phone number for this member before opting them in to WhatsApp.', 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    member.whatsapp_opt_in    = True
    member.whatsapp_opt_in_at = datetime.utcnow()
    db.session.commit()
    flash(f'{member.full_name} opted in to WhatsApp messages.', 'success')
    return redirect(url_for('members.detail', member_id=member.id))


@members_bp.route('/<int:member_id>/edit', methods=['GET', 'POST'])
@login_required
def edit(member_id):
    gid      = current_user.gym_id
    member   = Member.query.filter_by(id=member_id, gym_id=gid).first_or_404()
    trainers = User.query.filter_by(gym_id=gid, role='staff').order_by(User.name).all()

    if request.method == 'POST':
        member.first_name = request.form.get('first_name', '').strip()
        member.last_name  = request.form.get('last_name', '').strip()
        member.email      = request.form.get('email', '').strip()
        member.phone      = request.form.get('phone', '').strip()
        dob_str           = request.form.get('date_of_birth', '')
        joining_str       = request.form.get('joining_date', '')
        trainer_id        = request.form.get('assigned_trainer_id') or None
        member.status     = request.form.get('status', 'active')
        member.notes      = request.form.get('notes', '').strip()

        if not member.first_name or not member.last_name or not joining_str:
            flash('First name, last name, and joining date are required.', 'danger')
            return render_template('members/edit.html', member=member, trainers=trainers)

        member.date_of_birth        = date.fromisoformat(dob_str) if dob_str else None
        member.joining_date         = date.fromisoformat(joining_str)
        member.assigned_trainer_id  = own_gym_id(User, trainer_id, gid, role='staff')

        db.session.commit()
        flash(f'{member.full_name} has been updated.', 'success')
        return redirect(url_for('members.detail', member_id=member.id))

    return render_template('members/edit.html', member=member, trainers=trainers)


@members_bp.route('/export.csv')
@login_required
def export_csv():
    gid            = current_user.gym_id
    search         = request.args.get('search', '').strip()
    state_filter   = request.args.get('state', 'all')
    trainer_filter = request.args.get('trainer', '')

    if state_filter not in dict(MEMBER_TABS):
        state_filter = 'all'

    members, _ = _filtered_members(gid, search, state_filter, trainer_filter)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'ID', 'First Name', 'Last Name', 'Email', 'Phone',
        'Date of Birth', 'Joining Date', 'Trainer', 'Status',
        'Active Plan', 'Plan Expires', 'Amount', 'Membership State', 'Notes',
    ])
    for m in members:
        am = m.active_membership
        writer.writerow([
            m.id, m.first_name, m.last_name, m.email or '', m.phone or '',
            m.date_of_birth.isoformat() if m.date_of_birth else '',
            m.joining_date.isoformat(),
            m.trainer.name if m.trainer else '',
            m.status,
            am.plan.name if am else '',
            am.end_date.isoformat() if am else '',
            f'{am.amount:.2f}' if am else '',
            m.state_message,
            m.notes or '',
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=members.csv'},
    )


@members_bp.route('/<int:member_id>/delete', methods=['POST'])
@login_required
@role_required('super_admin')
def delete(member_id):
    """Hard delete — wipes the member and everything hanging off them.

    For a DPDP deletion request use privacy.erase instead: that scrubs the
    personal data but keeps the billing rows the gym is legally required to
    retain. This route is for genuine mistakes (duplicate record, test data).
    """
    member = Member.query.filter_by(id=member_id, gym_id=current_user.gym_id).first_or_404()

    # Typing the name is the guard against an accidental irreversible delete.
    # Enforced here, not just in the browser — a client-side confirm() is a
    # convenience, never a control.
    typed = request.form.get('confirm_name', '').strip()
    if typed.lower() != member.full_name.lower():
        flash('The name you typed didn\'t match. Nothing was deleted.', 'danger')
        return redirect(url_for('members.detail', member_id=member.id))

    name = member.full_name

    # Every table with an FK to members.id has to be cleared first. Missing
    # any of these doesn't orphan the rows — SQLAlchemy tries to NULL the FK
    # on delete, and those columns are NOT NULL, so the whole request 500s.
    Attendance.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    Notification.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    WhatsAppMessage.query.filter_by(member_id=member_id).delete(synchronize_session=False)
    MemberMembership.query.filter_by(member_id=member_id).delete(synchronize_session=False)

    # DataRequest is the DPDP compliance log — it carries member_name_snapshot
    # precisely so it outlives the member. Unlink rather than delete, so the
    # gym keeps its evidence that it responded to past requests.
    DataRequest.query.filter_by(member_id=member_id).update(
        {'member_id': None}, synchronize_session=False)

    db.session.delete(member)
    db.session.commit()

    flash(f'{name} and all their records have been permanently deleted.', 'info')
    return redirect(url_for('members.index'))


# ── CSV import ───────────────────────────────────────────────────────────────

IMPORT_COLUMNS = ['First Name', 'Last Name', 'Email', 'Phone',
                  'Date of Birth', 'Joining Date', 'Status', 'Notes']


def _parse_member_csv(text, existing_phones, existing_emails):
    """Turn uploaded CSV text into (rows_to_create, problems).

    Pure function so the rules can be tested without a request. Column names
    match members/export.csv, so a file exported from KriyaCore can be edited
    and sent straight back.

    Every row is checked before anything is written, and a bad row is
    reported with its line number rather than aborting the batch — one typo
    in row 40 should not cost the other 200.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], [(0, 'The file is empty.')]

    # Tolerate case and spacing differences in the header row.
    normalise = lambda s: (s or '').strip().lower().replace('_', ' ')
    headers = {normalise(h): h for h in reader.fieldnames}
    for required in ('first name', 'last name'):
        if required not in headers:
            return [], [(0, f'Missing required column "{required.title()}". '
                            f'Found: {", ".join(reader.fieldnames)}')]

    def cell(row, name):
        key = headers.get(normalise(name))
        return (row.get(key) or '').strip() if key else ''

    rows, problems = [], []
    seen_phones, seen_emails = set(), set()

    for line, row in enumerate(reader, start=2):      # line 1 is the header
        first = cell(row, 'First Name')
        last  = cell(row, 'Last Name')
        if not first and not last and not any((v or '').strip() for v in row.values()):
            continue                                  # blank line, not an error
        if not first or not last:
            problems.append((line, 'First and last name are both required.'))
            continue

        phone = cell(row, 'Phone')
        email = cell(row, 'Email').lower()

        # Duplicates, against the gym's existing members and within the file.
        if phone and (phone in existing_phones or phone in seen_phones):
            problems.append((line, f'{first} {last}: phone {phone} is already a member.'))
            continue
        if email and (email in existing_emails or email in seen_emails):
            problems.append((line, f'{first} {last}: email {email} is already a member.'))
            continue

        joining_raw = cell(row, 'Joining Date')
        try:
            joining = date.fromisoformat(joining_raw) if joining_raw else date.today()
        except ValueError:
            problems.append((line, f'{first} {last}: joining date "{joining_raw}" '
                                   f'is not YYYY-MM-DD.'))
            continue

        dob_raw = cell(row, 'Date of Birth')
        try:
            dob = date.fromisoformat(dob_raw) if dob_raw else None
        except ValueError:
            problems.append((line, f'{first} {last}: date of birth "{dob_raw}" '
                                   f'is not YYYY-MM-DD.'))
            continue

        status = cell(row, 'Status').lower() or 'active'
        if status not in ('active', 'inactive', 'suspended'):
            problems.append((line, f'{first} {last}: status "{status}" must be '
                                   f'active, inactive or suspended.'))
            continue

        if phone:
            seen_phones.add(phone)
        if email:
            seen_emails.add(email)

        rows.append({'first_name': first, 'last_name': last, 'email': email,
                     'phone': phone, 'date_of_birth': dob, 'joining_date': joining,
                     'status': status, 'notes': cell(row, 'Notes'), 'line': line})

    return rows, problems


MAX_IMPORT_BYTES = 2 * 1024 * 1024      # ~20k members; far past any real gym


@members_bp.route('/import', methods=['GET', 'POST'])
@login_required
@role_required('super_admin')
def import_csv():
    """Bulk-add members from a spreadsheet.

    Two passes over one upload: the first shows what would happen, the second
    writes it. The file's text rides back in a hidden field rather than being
    parked in the session — a 300-row CSV is small, and a browser refresh on
    a half-finished import should do nothing rather than something.
    """
    gid = current_user.gym_id
    gym = current_user.gym

    if request.method == 'GET':
        return render_template('members/import.html', columns=IMPORT_COLUMNS)

    # Second pass carries the text it already validated; first pass has a file.
    text = request.form.get('csv_text')
    if text is None:
        upload = request.files.get('file')
        if not upload or not upload.filename:
            flash('Choose a CSV file to import.', 'danger')
            return redirect(url_for('members.import_csv'))
        raw = upload.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            flash('That file is larger than 2 MB. Split it and import in parts.', 'danger')
            return redirect(url_for('members.import_csv'))
        try:
            # utf-8-sig drops the BOM Excel writes, which would otherwise
            # become part of the first column's name.
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            flash('That file is not UTF-8 text. Re-save it as CSV UTF-8.', 'danger')
            return redirect(url_for('members.import_csv'))

    existing = Member.query.filter_by(gym_id=gid).all()
    rows, problems = _parse_member_csv(
        text,
        {m.phone for m in existing if m.phone},
        {m.email.lower() for m in existing if m.email},
    )

    # Plan limit applies to the batch, not one at a time.
    tier  = gym.plan_tier or 'starter'
    limit = PLANS.get(tier, PLANS['starter'])['max_members']
    room  = None if limit is None else limit - len(existing)
    if room is not None and len(rows) > room:
        problems.append((0, f'This gym can hold {limit} members on the '
                            f'{PLANS[tier]["label"]} plan and has {len(existing)}. '
                            f'The file adds {len(rows)}. Upgrade or split the file.'))
        rows = []

    if request.form.get('confirm') != 'yes':
        return render_template('members/import.html', columns=IMPORT_COLUMNS,
                               rows=rows, problems=problems, csv_text=text,
                               preview=True)

    for r in rows:
        db.session.add(Member(gym_id=gid, **{k: v for k, v in r.items() if k != 'line'}))
    db.session.commit()

    flash(f'Imported {len(rows)} member{"" if len(rows) == 1 else "s"}.'
          + (f' {len(problems)} row(s) were skipped.' if problems else ''), 'success')
    return redirect(url_for('members.index'))


@members_bp.route('/import/template.csv')
@login_required
@role_required('super_admin')
def import_template():
    """A blank CSV with the right headers, plus one row showing the formats.

    Built from IMPORT_COLUMNS, the same list the parser reads, so the file a
    gym downloads cannot drift from the file the importer accepts.

    The example row is left in deliberately — it shows the date format, which
    is the thing people get wrong — and named so nobody mistakes it for real
    data. If it survives to the upload, the import preview lists every name
    before writing, so it gets caught there.
    """
    example = {
        'First Name':    'Example',
        'Last Name':     'Delete This Row',
        'Email':         'member@example.com',
        'Phone':         '9876500000',
        'Date of Birth': '1994-08-23',
        'Joining Date':  '2026-01-15',
        'Status':        'active',
        'Notes':         'Optional free text',
    }
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(IMPORT_COLUMNS)
    writer.writerow([example[c] for c in IMPORT_COLUMNS])

    return Response(
        output.getvalue(),
        content_type='text/csv; charset=utf-8',
        headers={'Content-Disposition':
                 'attachment; filename="kriyacore-member-import-template.csv"'},
    )
