"""
WhatsApp sending — vendor-agnostic on purpose.

WhatsApp Business Platform doesn't allow free-form outbound messages from a
business. Every gym-initiated message (not a reply to the customer) has to
use a pre-approved template — a fixed wrapper with a few fill-in variables,
submitted to Meta and approved before it can be sent. The four message
types KriyaCore's UI offers map onto three underlying templates: closure and
"other events" both use the same generic 'announcement' template with a
free-text variable, since that's realistically how a BSP/Meta approval
would be structured too.

No real WhatsApp Business Account is wired in yet — that requires an actual
BSP (Gupshup, Interakt, AiSensy, WATI, or Meta's Cloud API directly) and a
real business decision on which one. Until WHATSAPP_PROVIDER is set, every
send is logged to WhatsAppMessage with status='queued_no_provider' and
never actually leaves the server — safe to build and test the entire
pipeline (opt-in, triggers, compose UI, message log) without a live
account. Swap the body of the 'if configured' branch below for the real
API call once a BSP is chosen; nothing else in the app needs to change.
"""
import os

import requests

META_API_VERSION = 'v21.0'

TEMPLATES = {
    'expiry_reminder': {
        'label': 'Membership Expiring',
        'vars': ['member_name', 'gym_name', 'plan_name', 'expiry_date', 'days_left'],
        'preview': (
            'Hi {member_name}, your {plan_name} membership at {gym_name} '
            'expires on {expiry_date} ({days_left} days left). Renew soon '
            'to keep your access!'
        ),
    },
    'renewal_confirmation': {
        'label': 'Renewal Confirmed',
        'vars': ['member_name', 'gym_name', 'plan_name', 'new_expiry_date'],
        'preview': (
            'Hi {member_name}, your {plan_name} membership at {gym_name} '
            'has been renewed. New expiry: {new_expiry_date}. Thank you!'
        ),
    },
    'announcement': {
        'label': 'Gym Announcement',
        'vars': ['member_name', 'gym_name', 'message_body'],
        'preview': 'Hi {member_name} — {message_body} ({gym_name})',
    },
}


# Which WhatsAppSettings column holds a gym's custom wording for a given
# template — only the two automatic/recurring message types are admin-
# editable; announcements are composed fresh by staff each time.
_CUSTOM_TEMPLATE_FIELD = {
    'expiry_reminder':      'expiry_reminder_template',
    'renewal_confirmation': 'renewal_confirmation_template',
}


def is_configured():
    return bool(os.environ.get('WHATSAPP_PROVIDER', '').strip())


def get_template_text(gym, template_name):
    """The wording that will actually be used — the gym's own customized
    text if they've set one, otherwise the built-in default."""
    field = _CUSTOM_TEMPLATE_FIELD.get(template_name)
    if field and gym is not None:
        settings = getattr(gym, 'whatsapp_settings', None)
        custom = getattr(settings, field, None) if settings else None
        if custom and custom.strip():
            return custom
    tpl = TEMPLATES.get(template_name)
    return tpl['preview'] if tpl else ''


def render_preview(gym, template_name, variables):
    text = get_template_text(gym, template_name)
    if not text:
        return ''
    try:
        return text.format(**variables)
    except (KeyError, ValueError, IndexError):
        # A gym's custom wording referenced a variable that doesn't exist
        # for this message type, or used malformed {braces} — fall back to
        # showing the raw (unfilled) text rather than crashing the send.
        return text


def normalize_phone(phone):
    """WhatsApp's Cloud API wants E.164 digits with no leading '+'. KriyaCore's
    seed/staff-entered numbers are bare 10-digit Indian mobiles with no
    country code — assume +91 when a number looks like that. A gym entering
    numbers with a country code already (e.g. '91987...' or '+1987...')
    passes through unchanged."""
    digits = ''.join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10 and digits[0] in '6789':
        return '91' + digits
    return digits


def _send_via_meta(phone, template_name, variables):
    """Meta WhatsApp Cloud API — the direct route (no BSP middleman).
    Requires a Meta for Developers app with the WhatsApp product added; see
    deploy/SETUP.md's WhatsApp section for the exact click-path to get
    WHATSAPP_API_KEY (access token) and WHATSAPP_PHONE_NUMBER_ID.

    The template name sent to Meta must exactly match a template you've
    created and had approved in WhatsApp Manager — KriyaCore doesn't submit
    templates on your behalf. Variable order follows TEMPLATES[...]['vars'];
    if you've customized a template's wording in KriyaCore's Settings page,
    make sure the *approved* Meta template uses the same variables in the
    same order, or Meta will reject the mismatch.
    """
    phone_number_id = os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '').strip()
    access_token     = os.environ.get('WHATSAPP_API_KEY', '').strip()
    lang_code        = os.environ.get('WHATSAPP_TEMPLATE_LANG', 'en_US').strip()

    if not phone_number_id or not access_token:
        return 'failed', None, 'WHATSAPP_PHONE_NUMBER_ID and/or WHATSAPP_API_KEY are not set.'

    var_order = TEMPLATES.get(template_name, {}).get('vars', [])
    parameters = [{'type': 'text', 'text': str(variables.get(v, ''))} for v in var_order]

    payload = {
        'messaging_product': 'whatsapp',
        'to': normalize_phone(phone),
        'type': 'template',
        'template': {
            'name': template_name,
            'language': {'code': lang_code},
            'components': [{'type': 'body', 'parameters': parameters}] if parameters else [],
        },
    }

    try:
        resp = requests.post(
            f'https://graph.facebook.com/{META_API_VERSION}/{phone_number_id}/messages',
            headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        return 'failed', None, f'Network error calling Meta: {e}'

    if resp.status_code == 200:
        data = resp.json()
        message_id = data.get('messages', [{}])[0].get('id')
        return 'sent', message_id, None

    # Meta's error responses are JSON with an 'error' object — surface its
    # message directly rather than a generic HTTP status, since the reason
    # is almost always actionable (unapproved template, unverified test
    # recipient, expired token, etc.).
    try:
        error_detail = resp.json().get('error', {}).get('message', resp.text)
    except ValueError:
        error_detail = resp.text
    return 'failed', None, f'Meta API error ({resp.status_code}): {error_detail}'


def send_whatsapp_template(phone, template_name, variables):
    """Send one templated message. Returns (status, provider_message_id, error_message).

    status is one of: 'sent', 'failed', 'queued_no_provider'.
    """
    if template_name not in TEMPLATES:
        return 'failed', None, f'Unknown template "{template_name}"'

    if not is_configured():
        return 'queued_no_provider', None, None

    provider = os.environ.get('WHATSAPP_PROVIDER', '').strip().lower()

    if provider == 'meta':
        return _send_via_meta(phone, template_name, variables)

    # Any other WHATSAPP_PROVIDER value (a BSP name like 'gupshup',
    # 'interakt', etc.) isn't wired up yet — add a branch above matching
    # this pattern once one is chosen. Left as a loud failure rather than a
    # silent fake-success so a misconfigured value never looks like it's
    # working.
    return 'failed', None, (
        f'WHATSAPP_PROVIDER="{provider}" is set but no integration exists for it yet — '
        f'only "meta" is implemented. See app/whatsapp_provider.py.'
    )
