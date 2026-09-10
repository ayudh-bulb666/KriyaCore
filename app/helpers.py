from functools import wraps
from flask import abort
from flask_login import current_user

# Minimum password length. Ten rather than eight, and no composition rules
# (no "must contain a symbol") — modern guidance is that length beats
# complexity, and forced symbols mostly produce "Password1!" written on a
# sticky note under the keyboard.
PASSWORD_MIN_LENGTH = 10

# The passwords people actually pick when a form says "at least 10
# characters". Not a substitute for a real breach corpus, but it catches the
# handful that a front-desk machine would otherwise end up using.
_WEAK_PASSWORDS = {
    'password', 'password1', 'password123', 'passw0rd', 'p@ssword',
    '1234567890', '12345678', '123456789', '0123456789', 'qwertyuiop',
    'qwerty123', 'iloveyou', 'admin12345', 'administrator', 'letmein123',
    'welcome123', 'abcd123456', 'gympassword', 'kriyacore', 'kriyacore123',
    'gympro123', 'changeme', 'changeme123', 'trustno1', 'football123',
}


def own_gym_id(model, raw_id, gym_id, **extra):
    """Resolve a form-supplied row id, but only within this gym.

    Returns the id as an int, or None if it's blank, unparseable, or points
    at another gym's row.

    This exists because a validated *page* is not a validated *request*. A
    dropdown that lists only this gym's members is a convenience for the
    person filling it in; the POST that follows can carry any id at all. Two
    routes trusted the id because the dropdown looked safe, and a gym could
    attach its own billing row to another gym's member — whose name then
    appeared on the first gym's billing page.

    Every foreign key that arrives from a form goes through here.
    """
    if raw_id in (None, '', 'none'):
        return None
    try:
        row_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    row = model.query.filter_by(id=row_id, gym_id=gym_id, **extra).first()
    return row.id if row else None


def validate_password(password, *, email=None, name=None):
    """Return an error message, or None if the password is acceptable.

    One place, so the rule can't drift between the login form, the admin
    reset and the staff-creation form — which is exactly what happened when
    each of the three checked `len(password) < 8` on its own.
    """
    if not password or len(password) < PASSWORD_MIN_LENGTH:
        return f'Password must be at least {PASSWORD_MIN_LENGTH} characters.'

    lowered = password.strip().lower()

    if lowered in _WEAK_PASSWORDS:
        return 'That password is too common. Pick something less guessable.'

    # A single repeated character passes any length check but is trivially
    # guessed: 'aaaaaaaaaa' is ten characters.
    if len(set(password)) < 4:
        return 'Password needs more variety than that.'

    # Reusing the account's own email or name is one of the first things
    # anyone targeting a specific gym would try. Compare against a
    # space-stripped copy too, or "Raj Malhotra" sails past as
    # "RajMalhotra2026" — the space in the name means a literal `in` test
    # never matches the way someone actually writes it.
    squashed = lowered.replace(' ', '').replace('-', '').replace('.', '')

    if email and lowered == email.strip().lower():
        return 'Password can\'t be the same as the email address.'
    if email and '@' in email:
        local = email.split('@')[0].strip().lower()
        if len(local) >= 4 and (local in lowered or local in squashed):
            return 'Password can\'t contain the email address.'

    if name:
        parts = [p.lower() for p in name.split() if len(p) >= 4]
        # The full name with spaces removed, plus each individual part —
        # first names and surnames get used on their own just as often.
        candidates = [''.join(name.lower().split())] + parts
        for cand in candidates:
            if len(cand) >= 4 and (cand in lowered or cand in squashed):
                return 'Password can\'t contain the account holder\'s name.'

    return None


def role_required(*roles):
    """Decorator that restricts a view to users with specific roles."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def format_inr(value):
    """Group digits the Indian way: 1,41,600 rather than 141,600.

    Python's own thousands separator groups in threes all the way up, which
    is not how anyone in India reads a rupee figure — 1,41,600 is one lakh
    forty-one thousand six hundred, and 141,600 makes a reader stop and
    count. The charts on the same pages already use toLocaleString('en-IN'),
    so without this the server-rendered totals disagreed with the axis
    labels sitting directly beneath them.

    Registered as the `inr` Jinja filter in create_app().
    """
    try:
        rupees = int(round(float(value or 0)))
    except (TypeError, ValueError):
        return '0'

    sign, digits = ('-' if rupees < 0 else ''), str(abs(rupees))
    if len(digits) <= 3:
        return sign + digits

    head, last_three = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}{','.join(groups)},{last_three}"
