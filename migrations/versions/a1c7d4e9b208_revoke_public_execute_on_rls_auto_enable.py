"""Close the Supabase REST surface: revoke anon/authenticated access

Revision ID: a1c7d4e9b208
Revises: fede0af10b2b
Create Date: 2026-09-14

KriyaCore talks to Postgres directly through psycopg2 as `postgres` — the
table owner, with rolbypassrls. It does not use PostgREST, the Supabase
client, or the anon key. Every grant Supabase hands to `anon` and
`authenticated` is therefore unused, and each one is a way into real member
data if anything ever turns RLS off.

Two things this migration does, both of which the Supabase linter asked for:

1. `public.rls_auto_enable()` — Supabase's event-trigger function that turns
   RLS on for new tables — had EXECUTE granted to PUBLIC, anon and
   authenticated, which their linter reports as 0028/0029. It was never
   actually callable: a function returning `event_trigger` is refused by
   Postgres on its return type ("trigger functions can only be called as
   triggers"), before privileges are consulted. Revoked anyway, so the
   warning stops coming back.

2. Table and sequence privileges for anon/authenticated across `public`,
   plus the default privileges that would hand the same grants to every
   future table. Before this, RLS-with-no-policies was the ONLY thing
   between the internet and the members table; measured from outside, reads
   returned `[]` and a DELETE returned 204-matched-nothing. After, both
   return 401 / 42501 insufficient_privilege — refused before RLS is even
   reached. RLS becomes the second lock rather than the only one.

Kept: `service_role` and `postgres`. service_role is Supabase's own
backend identity; postgres is what the app connects as.

Worth knowing: `supabase_admin` holds its own default-privilege entries
that this migration cannot change, and a platform update may re-grant.
If the linter flags 0008 again, re-running this migration is safe.

Guards, because this file also runs where none of the above exists:
  * Postgres only — local development is SQLite.
  * Role-existence checked — a plain VPS Postgres has no anon/authenticated
    role, and REVOKE on a missing role is an error, not a no-op.
"""
from alembic import op


revision = 'a1c7d4e9b208'
down_revision = 'fede0af10b2b'
branch_labels = None
depends_on = None


def _is_postgres():
    return op.get_bind().dialect.name == 'postgresql'


def upgrade():
    if not _is_postgres():
        return

    # The Supabase-only event trigger function.
    op.execute("""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_proc p
                     JOIN pg_namespace n ON n.oid = p.pronamespace
                     WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable') THEN
            REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM PUBLIC;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
              REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
              REVOKE EXECUTE ON FUNCTION public.rls_auto_enable() FROM authenticated;
            END IF;
          END IF;
        END $$;
    """)

    # The REST surface. Each role handled separately so a database with only
    # one of them still gets the other revoked.
    for role in ('anon', 'authenticated'):
        op.execute(f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM {role};
                REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                  REVOKE ALL ON TABLES    FROM {role};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                  REVOKE ALL ON SEQUENCES FROM {role};
              END IF;
            END $$;
        """)


def downgrade():
    """Put Supabase's defaults back.

    Deliberately restores a state the linter objects to — a downgrade should
    return the database to how it was, not to how it ought to be.
    """
    if not _is_postgres():
        return

    for role in ('anon', 'authenticated'):
        op.execute(f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                GRANT ALL ON ALL TABLES    IN SCHEMA public TO {role};
                GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {role};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES    TO {role};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {role};
              END IF;
            END $$;
        """)

    op.execute("""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_proc p
                     JOIN pg_namespace n ON n.oid = p.pronamespace
                     WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable') THEN
            GRANT EXECUTE ON FUNCTION public.rls_auto_enable() TO PUBLIC;
          END IF;
        END $$;
    """)
