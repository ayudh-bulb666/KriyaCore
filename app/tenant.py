"""
Path-based multi-tenancy.

Every gym-facing blueprint is mounted under '/<gym_slug>/...' so each gym
gets its own branded URL, e.g. gympro.app/powerfit-mumbai/dashboard.

register_gym_scoping(blueprint) wires up three hooks on that blueprint:

  1. url_value_preprocessor — strips `gym_slug` out of the matched URL args
     before it reaches the view function, so existing view signatures
     (member_id, etc.) don't need to change at all.
  2. url_defaults           — auto-fills `gym_slug` on every url_for() call
     for this blueprint from the logged-in user's own gym, so templates and
     redirect()/url_for() calls elsewhere in the app keep working unchanged.
  3. before_request         — the actual guard: the slug in the URL must
     match the logged-in user's own gym. This is NOT how tenant data
     isolation happens (that's still current_user.gym_id on every query,
     completely unchanged) — it only stops a logged-in staff member at
     Gym A from browsing to Gym B's URL and viewing Gym B's UI shell.
"""
from flask import g, redirect, url_for
from flask_login import current_user

# Slugs that would collide with a real top-level route (or just read badly
# as a gym's URL). Checked at gym-creation time in operator.py.
RESERVED_SLUGS = {
    'operator', 'login', 'logout', 'auth', 'static', 'account',
    'api', 'admin', 'app', 'www', 'favicon.ico', 'robots.txt',
}


def register_gym_scoping(blueprint):

    @blueprint.url_value_preprocessor
    def _pull_gym_slug(endpoint, values):
        if values is not None:
            g.url_gym_slug = values.pop('gym_slug', None)

    @blueprint.url_defaults
    def _add_gym_slug(endpoint, values):
        if 'gym_slug' in values:
            return
        if current_user.is_authenticated and getattr(current_user, 'gym', None):
            values['gym_slug'] = current_user.gym.slug

    @blueprint.before_request
    def _enforce_gym_scope():
        if not current_user.is_authenticated:
            return  # let @login_required on the view handle it
        if current_user.is_platform_admin:
            return  # platform admin shouldn't be here; app-level guard already redirects

        gym = current_user.gym
        if not gym or g.get('url_gym_slug') != gym.slug:
            return redirect(url_for('dashboard.index'))
