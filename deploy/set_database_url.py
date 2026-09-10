#!/usr/bin/env python3
"""Paste your Supabase connection string into .env without hunting for it.

    python3 deploy/set_database_url.py

Prompts for the URL with the input hidden, checks it looks like the right
kind of connection string, and writes it into .env — creating the file from
deploy/.env.example if it doesn't exist yet.

Hidden input on purpose: the string contains your database password, and a
normal prompt would leave it in your terminal scrollback. It never goes into
your shell history either, which typing it as a command argument would.
"""
import re
import shutil
import sys
from getpass import getpass
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT     = Path(__file__).resolve().parent.parent
ENV      = ROOT / '.env'
TEMPLATE = ROOT / 'deploy' / '.env.example'


def check(url):
    """Return a list of problems with this URL. Empty list means it's good."""
    problems = []
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return [f'Could not parse that as a URL: {exc}']

    if parts.scheme not in ('postgres', 'postgresql'):
        problems.append(f'Scheme is "{parts.scheme}", expected postgres:// or postgresql://')

    host = parts.hostname or ''
    port = parts.port

    if 'localhost' in host or host in ('127.0.0.1', ''):
        problems.append('This still points at localhost — it is the template value, '
                        'not your Supabase string.')
    elif host.startswith('db.') and 'supabase.co' in host:
        problems.append('This is the Supabase DIRECT connection. It is IPv6-only, so '
                        'Render cannot reach it. Use the Session pooler string instead '
                        '(the Connect modal, "Session pooler").')
    elif 'pooler.supabase.com' not in host:
        problems.append(f'Host "{host}" is not a Supabase pooler address. Expected '
                        'something like aws-0-ap-southeast-1.pooler.supabase.com')

    if port == 6543:
        problems.append('Port 6543 is transaction mode. Change it to 5432 for session '
                        'mode — same host, just the port.')
    elif port not in (5432, None):
        problems.append(f'Port is {port}; expected 5432.')

    if '[YOUR-PASSWORD]' in url or 'CHANGE_ME' in url:
        problems.append('The password placeholder is still in there — replace '
                        '[YOUR-PASSWORD] with your actual database password.')

    if not parts.password:
        problems.append('No password found in the URL.')

    return problems


def needs_encoding(password):
    """Characters that break a URL if they appear raw in the password."""
    return [c for c in '@/:?#[]' if c in password]


def write(url):
    if not ENV.exists():
        shutil.copy(TEMPLATE, ENV)
        print(f'Created {ENV.name} from the template.')

    lines = ENV.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith('DATABASE_URL='):
            lines[i] = f'DATABASE_URL={url}'
            break
    else:
        lines.append(f'DATABASE_URL={url}')

    ENV.write_text('\n'.join(lines) + '\n')
    ENV.chmod(0o600)


def split_on_last_at(url):
    """Separate a URL into (before_password, password, after) by the LAST '@'.

    Done by hand rather than with urlsplit because a raw '#', '?' or '@' in
    the password breaks every URL parser — '#' in particular truncates the
    string at that point, so the parser reports a nonsense host and you chase
    the wrong problem. Splitting on the last '@' finds the password even when
    it contains characters that would otherwise confuse things.
    """
    if '://' not in url or '@' not in url:
        return None
    scheme, rest = url.split('://', 1)
    creds, host = rest.rsplit('@', 1)
    if ':' not in creds:
        return None
    user, password = creds.split(':', 1)
    return scheme, user, password, host


def main():
    print('\nPaste the Supabase **Session pooler** string.')
    print('Leave [YOUR-PASSWORD] in it — you will be asked for the password')
    print('separately, so special characters cannot break the URL.\n')

    url = getpass('Connection string: ').strip().strip('"').strip("'")
    if not url:
        print('Nothing entered. No changes made.')
        return 1

    # Password supplied separately, then encoded. This is the reliable path:
    # pasting a URL with a raw '#' or '@' already in the password silently
    # mangles it before any parser sees it.
    if '[YOUR-PASSWORD]' in url:
        pw = getpass('Database password:  ')
        if not pw:
            print('No password entered. No changes made.')
            return 1
        url = url.replace('[YOUR-PASSWORD]', quote(pw, safe=''))
    else:
        parsed = split_on_last_at(url)
        if parsed:
            scheme, user, password, host = parsed
            if password and password != quote(password, safe=''):
                url = f'{scheme}://{user}:{quote(password, safe="")}@{host}'
                print('\nNote: your password contained characters that break a URL '
                      '(such as # or @). Percent-encoded them.')

    problems = check(url)
    if problems:
        print('\nThat does not look right:\n')
        for p in problems:
            print(f'  - {p}')
        print('\nNothing was written. Fix it and run this again.')
        return 1

    parts = urlsplit(url)
    raw = needs_encoding(parts.password or '')
    if raw:
        # A raw '@' or '/' in the password silently truncates the URL and you
        # get a confusing "could not translate host name" instead of "bad
        # password". Encode it rather than let that happen.
        encoded = quote(parts.password, safe='')
        url = url.replace(f':{parts.password}@', f':{encoded}@', 1)
        print(f'\nNote: your password contains {" ".join(raw)} — percent-encoded it '
              f'so the URL parses correctly.')

    write(url)
    print(f'\nSaved to {ENV.name}.')
    print(f'  host: {parts.hostname}')
    print(f'  port: {parts.port or 5432}')
    print(f'  user: {parts.username}')
    print('\nTell Claude it is saved and it will run the migration.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
