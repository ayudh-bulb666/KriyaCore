#!/usr/bin/env python3
"""Print the three secret values to paste into Render's Environment tab.

    python3 deploy/print_render_env.py

Reads DATABASE_URL out of your local .env and generates a fresh SECRET_KEY
and CRON_SECRET. Run it in your own terminal and copy from there — that keeps
the values on your machine instead of in a chat transcript or a shell history.

SECRET_KEY is generated once here and then must not change: it signs every
session cookie, so replacing it signs out every logged-in user. Keep the same
value if you later move off Render.
"""
import re
import secrets
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / '.env'


def database_url():
    if not ENV.exists():
        return None, f'{ENV} does not exist. Run deploy/set_database_url.py first.'
    for line in ENV.read_text().splitlines():
        if line.startswith('DATABASE_URL='):
            url = line.split('=', 1)[1].strip()
            if 'localhost' in url or 'CHANGE_ME' in url:
                return None, 'DATABASE_URL is still the template value.'
            if '@' not in url:
                return None, 'DATABASE_URL looks truncated — no host in it.'
            if 'pooler.supabase.com' not in url:
                return None, ('DATABASE_URL is not a Supabase pooler address. '
                              'Use the Session pooler string.')
            return url, None
    return None, 'No DATABASE_URL line found in .env.'


def main():
    url, problem = database_url()
    if problem:
        print(f'\n  Problem: {problem}\n')
        return 1

    print()
    print('=' * 72)
    print('  Paste these into Render → Environment → Add Environment Variable')
    print('  https://dashboard.render.com/web/srv-dah3mslbedkc739027n0/env')
    print('=' * 72)
    print()
    print('  KEY:   DATABASE_URL')
    print(f'  VALUE: {url}')
    print()
    print('  KEY:   SECRET_KEY')
    print(f'  VALUE: {secrets.token_hex(32)}')
    print()
    print('  KEY:   CRON_SECRET')
    print(f'  VALUE: {secrets.token_hex(32)}')
    print()
    print('=' * 72)
    print('  Save the SECRET_KEY somewhere safe. Changing it later signs out')
    print('  every user, and you will want the same one if you move hosts.')
    print()
    print('  These are live credentials. Clear your terminal when you are done:')
    print('      clear && printf "\\033[3J"')
    print('=' * 72)
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
