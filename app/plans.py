# ── KriyaCore Subscription Plans ────────────────────────────────────────────────
# Single source of truth. Import PLANS wherever plan logic is needed.

PLANS = {
    'starter': {
        'label':       'Starter',
        'price':       0,
        'price_label': 'Free',
        'color':       'gray',          # used for badge styling
        'max_members': 50,
        'max_staff':   2,              # staff users (super_admin + staff)
        'features': {
            'attendance':    False,
            'notifications': False,
            'reminders':     False,
            'whatsapp':      False,
            'export_csv':    False,
            'brand_color':   False,    # can set custom brand color
            'brand_logo':    False,    # can upload logo
        },
        'feature_labels': [
            '50 members',
            '2 staff accounts',
            'Dashboard & billing',
        ],
        'missing_labels': [
            'Attendance tracking',
            'Notifications',
            'Automated reminders',
            'WhatsApp messaging',
            'CSV export',
            'Custom branding',
        ],
    },
    'growth': {
        'label':       'Growth',
        'price':       1400,
        'price_label': '₹1,400 / mo',
        'color':       'blue',
        'max_members': 200,
        'max_staff':   10,
        'features': {
            'attendance':    True,
            'notifications': True,
            'reminders':     False,
            'whatsapp':      False,
            'export_csv':    True,
            'brand_color':   True,
            'brand_logo':    False,
        },
        'feature_labels': [
            '200 members',
            '10 staff accounts',
            'Dashboard & billing',
            'Attendance tracking',
            'Notifications',
            'CSV export',
            'Custom brand colour',
        ],
        'missing_labels': [
            'Automated reminders',
            'WhatsApp messaging',
            'Logo upload',
        ],
    },
    'pro': {
        'label':       'Pro',
        'price':       2500,
        'price_label': '₹2,500 / mo',
        'color':       'violet',
        'max_members': None,           # unlimited
        'max_staff':   None,
        'features': {
            'attendance':    True,
            'notifications': True,
            'reminders':     True,
            'whatsapp':      True,
            'export_csv':    True,
            'brand_color':   True,
            'brand_logo':    True,
        },
        'feature_labels': [
            'Unlimited members',
            'Unlimited staff',
            'Dashboard & billing',
            'Attendance tracking',
            'Notifications',
            'Automated reminders',
            'WhatsApp messaging',
            'CSV export',
            'Custom brand colour',
            'Logo upload',
        ],
        'missing_labels': [],
    },
}

# Map blueprint prefix → feature key
FEATURE_ROUTES = {
    'attendance.': 'attendance',
    'notifications.': 'notifications',
    'reminders.': 'reminders',
    'whatsapp.': 'whatsapp',
}


def plan_has(gym, feature: str) -> bool:
    """Return True if the gym's plan includes the given feature."""
    tier = getattr(gym, 'plan_tier', 'starter') or 'starter'
    return PLANS.get(tier, PLANS['starter'])['features'].get(feature, False)


def plan_within_member_limit(gym) -> bool:
    """Return True if the gym can add more members."""
    from .models import Member
    tier = getattr(gym, 'plan_tier', 'starter') or 'starter'
    limit = PLANS.get(tier, PLANS['starter'])['max_members']
    if limit is None:
        return True
    count = Member.query.filter_by(gym_id=gym.id).count()
    return count < limit


def plan_within_staff_limit(gym) -> bool:
    """Return True if the gym can add more staff."""
    from .models import User
    tier = getattr(gym, 'plan_tier', 'starter') or 'starter'
    limit = PLANS.get(tier, PLANS['starter'])['max_staff']
    if limit is None:
        return True
    count = User.query.filter_by(gym_id=gym.id).count()
    return count < limit
