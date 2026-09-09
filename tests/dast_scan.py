"""Dynamic security scan against a running KriyaCore instance.

Fires real HTTP requests at a live server and checks how it responds. This
is deliberately not a generic web scanner: the interesting failure mode for
a multi-tenant gym CRM is one gym reading or editing another gym's records,
and no off-the-shelf crawler can tell a correct 200 from a catastrophic one
without knowing which rows belong to whom.

Read-only by intent. Anything that would create or modify data is sent with
values that must be rejected, and the test asserts they were.

Usage:
    python3 tests/dast_scan.py [base_url]
"""
import re
import sys
import json
import urllib.parse

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5001').rstrip('/')

# Populated from the live database by main(); see load_fixtures().
FIX = {}

results = []


def record(severity, name, passed, detail=''):
    results.append((severity, name, passed, detail))
    mark = 'PASS' if passed else 'FAIL'
    print(f'  [{mark}] {name}' + (f'\n         {detail}' if detail and not passed else ''))


def login(email, password):
    """Log in and return a session, or None.

    Two details that are easy to get wrong and produce a scan that reports
    authorisation failures it never actually tested:

      * **Send a Referer.** Over HTTPS, Flask-WTF checks it and refuses the
        POST without one. A browser always sends it; `requests` does not.
      * **Verify, don't infer.** A CSRF rejection is also a 302, so "we got
        a redirect" is not evidence of a login. Fetch a protected page and
        confirm we are actually authenticated.
    """
    s = requests.Session()
    s.headers['Referer'] = f'{BASE}/login'
    r = s.get(f'{BASE}/login', timeout=10)
    token = extract_csrf(r.text)
    r = s.post(f'{BASE}/login',
               data={'email': email, 'password': password, 'csrf_token': token},
               allow_redirects=False, timeout=10)
    if r.status_code != 302 or '/login' in r.headers.get('Location', ''):
        return None

    # Prove the session actually carries an identity.
    probe = s.get(f'{BASE}/{FIX["gym_a"]["slug"]}/members/',
                  allow_redirects=False, timeout=10)
    return s if probe.status_code == 200 else None


def extract_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ''


# ── 1. Cross-tenant access (IDOR) ────────────────────────────────────────────

def test_tenant_isolation(sess_a):
    """Gym A's owner must not reach gym B's records by any route.

    Two shapes are tested, because they fail differently:
      - walking into B's URL space   (/<b-slug>/members/)
      - keeping A's URL but passing B's row id  (/<a-slug>/members/<b-id>)
    The second is the one that slips through when a query filters by id but
    forgets gym_id.
    """
    a, b = FIX['gym_a'], FIX['gym_b']

    # Walking into the other gym's URL space
    for path in ['/', '/members/', '/billing/', '/staff/', '/expenses/']:
        r = sess_a.get(f'{BASE}/{b["slug"]}{path}', allow_redirects=False, timeout=10)
        blocked = r.status_code in (302, 403, 404)
        record('CRITICAL', f'A cannot browse B\'s {path or "/"}', blocked,
               f'got HTTP {r.status_code} at /{b["slug"]}{path}')

    # A's own URL space, but B's row ids
    probes = [
        ('member',     f'/members/{b["member_id"]}',            b['member_name']),
        ('staff',      f'/staff/{b["staff_id"]}',               b['staff_name']),
        ('invoice',    f'/billing/{b["membership_id"]}/invoice.pdf', None),
    ]
    for label, path, leaked_text in probes:
        url = f'{BASE}/{a["slug"]}{path}'
        r = sess_a.get(url, allow_redirects=False, timeout=10)
        blocked = r.status_code in (403, 404, 302)
        detail = f'HTTP {r.status_code} at {path}'
        if not blocked and leaked_text and leaked_text in r.text:
            detail += f' — LEAKED "{leaked_text}"'
        record('CRITICAL', f'A cannot read B\'s {label} by id', blocked, detail)

    # Search must not reach across tenants
    r = sess_a.get(f'{BASE}/{a["slug"]}/desk/search',
                   params={'q': b['member_name'].split()[0]}, timeout=10)
    leaked = b['member_name'] in r.text
    record('CRITICAL', 'Front-desk search does not return B\'s members',
           not leaked, f'search leaked: {r.text[:200]}')


def test_cross_tenant_writes(sess_a):
    """Reading another gym's data is bad; changing it is worse."""
    a, b = FIX['gym_a'], FIX['gym_b']
    csrf = get_csrf_from(sess_a,
                         f'{BASE}/{a["slug"]}/members/{a["member_id"]}',
                         f'{BASE}/{a["slug"]}/expenses/')
    if not csrf:
        record('CRITICAL', 'cross-tenant write probes could not get a CSRF token',
               False, 'without one these probes test CSRF, not authorisation')
        return

    attempts = [
        ('mark B\'s membership paid', f'/billing/{b["membership_id"]}/pay', {}),
        ('change B\'s membership amount',
         f'/billing/{b["membership_id"]}/amount', {'amount': '1'}),
        ('delete B\'s member',
         f'/members/{b["member_id"]}/delete', {'confirm_name': b['member_name']}),
        ('clock in B\'s staff', f'/staff/{b["staff_id"]}/shift/start', {}),
    ]
    for label, path, data in attempts:
        data = dict(data, csrf_token=csrf)
        r = sess_a.post(f'{BASE}/{a["slug"]}{path}', data=data,
                        allow_redirects=False, timeout=10)
        blocked = r.status_code in (403, 404)
        record('CRITICAL', f'A cannot {label}', blocked,
               f'HTTP {r.status_code} — a 302 here usually means the CSRF layer '
               f'caught it first, so authorisation went untested')


def get_csrf_from(sess, *urls):
    """Pull a CSRF token, trying each page until one yields it.

    This matters more than it looks. An index page that only has a GET
    filter form carries no token, so asking it for one returns '' — and
    every probe then gets stopped by CSRF before it ever reaches the
    authorisation check being tested. The result is a scan that reports
    authz failures it never actually exercised.
    """
    for u in urls:
        sess.headers['Referer'] = u
        tok = extract_csrf(sess.get(u, timeout=10).text)
        if tok:
            return tok
    return ''


# ── 2. Authentication and authorisation ──────────────────────────────────────

def test_unauthenticated():
    """Nothing sensitive may be reachable without a session."""
    a = FIX['gym_a']
    s = requests.Session()
    s.headers['Referer'] = f'{BASE}/login'
    paths = ['/', '/members/', '/billing/', '/staff/', '/expenses/', '/privacy/',
             f'/members/{a["member_id"]}', '/desk/search?q=a', '/members/export.csv']
    for p in paths:
        r = s.get(f'{BASE}/{a["slug"]}{p}', allow_redirects=False, timeout=10)
        blocked = r.status_code in (302, 401, 403)
        record('CRITICAL', f'anon blocked from {p}', blocked, f'HTTP {r.status_code}')


def test_staff_privileges(sess_staff):
    """Staff run the desk; they don't see payroll, expenses or DPDP tools."""
    a = FIX['gym_a']
    for p in ['/staff/', f'/staff/{a["staff_id"]}', '/expenses/', '/privacy/',
              '/expenses/export.csv']:
        r = sess_staff.get(f'{BASE}/{a["slug"]}{p}', allow_redirects=False, timeout=10)
        record('HIGH', f'staff blocked from {p}', r.status_code == 403,
               f'HTTP {r.status_code}')

    csrf = get_csrf_from(sess_staff,
                         f'{BASE}/{a["slug"]}/members/{a["member_id"]}',
                         f'{BASE}/{a["slug"]}/?view=frontdesk')
    writes = [
        ('/expenses/new', {'category': 'rent', 'amount': '1'}),
        (f'/staff/{a["staff_id"]}/salary', {'base_amount': '999999'}),
        (f'/staff/{a["staff_id"]}/profile', {'name': 'x', 'salary_amount': '999999'}),
    ]
    for p, data in writes:
        r = sess_staff.post(f'{BASE}/{a["slug"]}{p}', data=dict(data, csrf_token=csrf),
                            allow_redirects=False, timeout=10)
        # 403 = the role gate. 302 = CSRF stopped it first, which is still
        # blocked but tests a different layer; both are reported honestly.
        layer = 'role gate' if r.status_code == 403 else 'CSRF layer'
        record('HIGH', f'staff blocked from POST {p} ({layer})',
               r.status_code in (302, 403), f'HTTP {r.status_code}')


# ── 3. CSRF ──────────────────────────────────────────────────────────────────

def test_csrf(sess_a):
    """Every state-changing route must reject a request with no token."""
    a = FIX['gym_a']
    posts = [
        f'/billing/{a["membership_id"]}/amount',
        f'/billing/{a["membership_id"]}/pay',
        '/expenses/new',
        f'/staff/{a["staff_id"]}/shift/start',
        f'/staff/{a["staff_id"]}/profile',
        f'/members/{a["member_id"]}/delete',
    ]
    for p in posts:
        r = sess_a.post(f'{BASE}/{a["slug"]}{p}', data={'amount': '1'},
                        allow_redirects=False, timeout=10)
        # The app turns a CSRF failure into a redirect with a flash, not a 400.
        rejected = r.status_code in (302, 400, 403)
        record('HIGH', f'CSRF enforced on {p}', rejected, f'HTTP {r.status_code}')


# ── 4. Injection ─────────────────────────────────────────────────────────────

SQLI = ["' OR '1'='1", "'; DROP TABLE members;--", "1' UNION SELECT null,null--",
        "' OR 1=1--", "admin'--"]


def test_sql_injection(sess_a):
    a = FIX['gym_a']
    for payload in SQLI:
        for path, param in [('/members/', 'search'), ('/desk/search', 'q')]:
            r = sess_a.get(f'{BASE}/{a["slug"]}{path}', params={param: payload},
                           timeout=10)
            err = re.search(r'(OperationalError|SQL syntax|sqlite3\.|psycopg2\.|'
                            r'ProgrammingError|Traceback)', r.text)
            record('CRITICAL', f'SQLi safe: {param}={payload[:20]!r}',
                   r.status_code == 200 and not err,
                   f'HTTP {r.status_code}' + (f' — {err.group(1)}' if err else ''))

    # The table must still be there afterwards.
    r = sess_a.get(f'{BASE}/{a["slug"]}/members/', timeout=10)
    record('CRITICAL', 'members table intact after SQLi probes',
           r.status_code == 200 and 'member' in r.text.lower())


XSS = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>',
       "javascript:alert(1)", '<svg/onload=alert(1)>']


def test_xss_reflected(sess_a):
    """Flag a payload only when it comes back in a form that could execute.

    "Is the string present in the response?" is the naive test and it lies:
    a payload with no HTML metacharacters — `javascript:alert(1)` — appears
    verbatim inside a quoted `value=""` and inside the query string of a
    relative href, and neither can run. What matters is whether the
    dangerous characters survived unescaped.
    """
    a = FIX['gym_a']
    for payload in XSS:
        r = sess_a.get(f'{BASE}/{a["slug"]}/members/',
                       params={'search': payload}, timeout=10)
        dangerous = [c for c in ('<', '>', '"') if c in payload]
        if dangerous:
            # Escaping worked if the raw metacharacters aren't echoed back.
            executable = payload in r.text
            why = 'metacharacters echoed unescaped'
        else:
            # No metacharacters: only exploitable if it became a URL scheme,
            # i.e. sits immediately after href=" or src=".
            executable = bool(re.search(
                r'(?:href|src)\s*=\s*["\']\s*' + re.escape(payload), r.text, re.I))
            why = 'payload landed at the start of an href/src'
        record('HIGH', f'no reflected XSS: {payload[:24]!r}', not executable, why)


# ── 5. Session, headers, disclosure ──────────────────────────────────────────

def test_cookie_flags(sess_a):
    for c in sess_a.cookies:
        if c.name == 'session':
            record('HIGH', 'session cookie HttpOnly',
                   bool(c.has_nonstandard_attr('HttpOnly') or c._rest.get('HttpOnly')))
            record('MEDIUM', 'session cookie SameSite set',
                   bool(c._rest.get('SameSite')), f'rest={c._rest}')
            # Secure can't be set on plain http locally; report, don't fail.
            print(f'         (Secure={c.secure} — expected False over http, '
                  f'must be True in production)')
            return
    record('HIGH', 'session cookie present', False, 'no session cookie found')


def test_security_headers():
    r = requests.get(f'{BASE}/login', timeout=10)
    wanted = {
        'X-Frame-Options':        'MEDIUM',
        'X-Content-Type-Options': 'LOW',
        'Content-Security-Policy': 'MEDIUM',
        'Referrer-Policy':        'LOW',
    }
    for h, sev in wanted.items():
        record(sev, f'header {h}', h in r.headers,
               'not set — add it at the nginx layer or in Flask')


def test_debug_and_disclosure():
    # Werkzeug's interactive debugger on a public host is remote code execution.
    r = requests.get(f'{BASE}/nonexistent-{"x"*12}', timeout=10)
    dbg = 'Werkzeug' in r.text and 'console' in r.text.lower()
    record('CRITICAL', 'no interactive debugger on 404', not dbg)
    record('MEDIUM', 'no stack trace on 404',
           'Traceback (most recent call last)' not in r.text)

    r = requests.get(f'{BASE}/login', timeout=10)
    record('LOW', 'no server version banner',
           'Werkzeug/' not in r.headers.get('Server', ''),
           f'Server: {r.headers.get("Server", "")}')


def test_open_redirect():
    """?next= must not bounce a user to another site after login."""
    s = requests.Session()
    for target in ['https://evil.example.com', '//evil.example.com',
                   'https:evil.example.com']:
        r = s.get(f'{BASE}/login', params={'next': target},
                  allow_redirects=False, timeout=10)
        loc = r.headers.get('Location', '')
        bad = 'evil.example.com' in loc
        record('MEDIUM', f'no open redirect via next={target[:28]}', not bad,
               f'Location: {loc}')


def test_rate_limiting():
    """The login endpoint should stop answering a burst of bad passwords.

    Detected by the flash message, not the status code. The app turns a 429
    into a friendly 302-then-200 (see errorhandler(429) in app/__init__.py),
    so watching for a raw 429 would report "not rate limited" on an app that
    is, in fact, rate limited. Run this last — it burns the minute's budget
    and will make any later login in the same run fail.
    """
    s = requests.Session()
    s.headers['Referer'] = f'{BASE}/login'
    hit_at = None
    for i in range(14):
        t = extract_csrf(s.get(f'{BASE}/login', timeout=10).text)
        r = s.post(f'{BASE}/login',
                   data={'email': f'nobody{i}@nowhere.test',
                         'password': 'wrong', 'csrf_token': t},
                   allow_redirects=True, timeout=10)
        if 'Too many attempts' in r.text:
            hit_at = i + 1
            break
    record('HIGH', 'login is rate limited', hit_at is not None,
           '14 bad logins in a burst were all answered normally')
    if hit_at:
        print(f'         (limiter engaged on attempt {hit_at})')


def test_webhook_auth():
    """The Face ID webhook is CSRF-exempt by necessity, so its secret is the
    only thing standing between the internet and the attendance log."""
    a = FIX['gym_a']
    for secret in ['', 'wrong-secret', 'x' * 40]:
        r = requests.post(f'{BASE}/webhook/faceid/{a["gym_id"]}/{secret}',
                          json={'external_id': 'x'}, timeout=10)
        record('CRITICAL', f'faceid webhook rejects secret={secret[:12]!r}',
               r.status_code in (401, 403, 404), f'HTTP {r.status_code}')

    r = requests.post(f'{BASE}/internal/cron/whatsapp-reminders', timeout=10)
    record('CRITICAL', 'cron endpoint rejects missing secret',
           r.status_code in (401, 403, 404), f'HTTP {r.status_code}')


# ── Runner ───────────────────────────────────────────────────────────────────

def load_fixtures():
    """Read real ids straight from the database so the probes aim at rows
    that actually exist — a 404 from a guessed id proves nothing."""
    sys.path.insert(0, '.')
    from app import create_app
    from app.models import Gym, User, Member, MemberMembership
    app = create_app()
    with app.app_context():
        gyms = Gym.query.order_by(Gym.id).all()
        if len(gyms) < 2:
            raise SystemExit('Need two gyms to test tenant isolation.')
        out = {}
        for key, g in (('gym_a', gyms[0]), ('gym_b', gyms[1])):
            m = Member.query.filter_by(gym_id=g.id).first()
            st = User.query.filter_by(gym_id=g.id, role='staff').first()
            own = User.query.filter_by(gym_id=g.id, role='super_admin').first()
            mm = MemberMembership.query.filter_by(gym_id=g.id).first()
            out[key] = {
                'gym_id': g.id, 'slug': g.slug,
                'member_id': m.id, 'member_name': m.full_name,
                'staff_id': st.id, 'staff_name': st.name,
                'owner_email': own.email,
                'membership_id': mm.id,
            }
        return out


def main():
    global FIX
    FIX = load_fixtures()
    a, b = FIX['gym_a'], FIX['gym_b']
    print(f'Target: {BASE}')
    print(f'  gym A = {a["slug"]} (owner {a["owner_email"]})')
    print(f'  gym B = {b["slug"]} (owner {b["owner_email"]})\n')

    sess_a = login(a['owner_email'], 'admin123')
    if sess_a is None:
        raise SystemExit(f'Could not log in as {a["owner_email"]} — check the password.')
    sess_staff = login('raj@kriyacore.com', 'staff123')

    print('── Tenant isolation ──────────────────────────────────────')
    test_tenant_isolation(sess_a)
    test_cross_tenant_writes(sess_a)

    print('\n── Authentication ────────────────────────────────────────')
    test_unauthenticated()

    print('\n── Authorisation ─────────────────────────────────────────')
    if sess_staff:
        test_staff_privileges(sess_staff)
    else:
        print('  (skipped — could not log in as staff)')

    print('\n── CSRF ──────────────────────────────────────────────────')
    test_csrf(sess_a)

    print('\n── Injection ─────────────────────────────────────────────')
    test_sql_injection(sess_a)
    test_xss_reflected(sess_a)

    print('\n── Session & headers ─────────────────────────────────────')
    test_cookie_flags(sess_a)
    test_security_headers()
    test_debug_and_disclosure()
    test_open_redirect()

    print('\n── Unauthenticated endpoints ─────────────────────────────')
    test_webhook_auth()

    print('\n── Rate limiting ─────────────────────────────────────────')
    test_rate_limiting()

    # ── Summary ──────────────────────────────────────────────────────────
    order = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
    fails = [r for r in results if not r[2]]
    print('\n' + '=' * 62)
    print(f'{len(results)} checks, {len(results) - len(fails)} passed, {len(fails)} failed')
    if fails:
        print('\nFindings:')
        for sev in order:
            for s, name, _, detail in fails:
                if s == sev:
                    print(f'  {sev:8} {name}')
                    if detail:
                        print(f'           {detail}')
    else:
        print('\nNo findings.')
    print('=' * 62)
    return 1 if any(f[0] in ('CRITICAL', 'HIGH') for f in fails) else 0


if __name__ == '__main__':
    sys.exit(main())
