import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from itsdangerous import URLSafeTimedSerializer, BadData
from werkzeug.security import check_password_hash, generate_password_hash

from periods import get_current_period, days_left, PERIOD_LABELS
from email_sender import (send_reminder_email, send_invite_email,
                           send_reset_email, send_link_invite_email,
                           send_card_request_email)

app = Flask(__name__)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATABASE    = os.path.join(BASE_DIR, 'benefits.db')
SCHEMA      = os.path.join(BASE_DIR, 'schema.sql')
SECRET_FILE = os.path.join(BASE_DIR, '.secret_key')
CREDS_FILE  = os.path.join(BASE_DIR, '.credentials')



def _load_secret():
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE) as f:
            return f.read().strip()
    key = os.urandom(24).hex()
    with open(SECRET_FILE, 'w') as f:
        f.write(key)
    return key


app.secret_key = _load_secret()
app.permanent_session_lifetime = timedelta(days=90)
# Session-cookie hardening. The site is HTTPS-only (nginx redirects :80 → :443),
# so Secure is safe; SameSite=Lax blocks cross-site form POSTs (CSRF defence).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=True,
)


# ── CSRF protection ──────────────────────────────────────────────────────────
# A per-session token must accompany every state-changing (POST) request. All
# such requests in this app are HTML form posts (no AJAX), so the token rides in
# a hidden `csrf_token` field rendered via the csrf_token() template global.
def _ensure_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


@app.context_processor
def _inject_csrf_token():
    return {'csrf_token': _ensure_csrf_token}


# ── Lightweight rate limiting ────────────────────────────────────────────────
# Prod runs a single gunicorn worker, so an in-process store is enough. Keyed by
# real client IP (nginx forwards it as X-Real-IP). Used to throttle the
# unauthenticated POST endpoints (login, signup, password reset).
_rate_hits = {}


def _client_ip():
    return request.headers.get('X-Real-IP') or request.remote_addr or 'unknown'


def _rate_limited(bucket, max_hits, window_seconds):
    """Record a hit for (bucket, client-IP) and return True if it now exceeds
    max_hits within the trailing window."""
    key = (bucket, _client_ip())
    now = time.time()
    hits = [t for t in _rate_hits.get(key, []) if now - t < window_seconds]
    hits.append(now)
    _rate_hits[key] = hits
    return len(hits) > max_hits


@app.errorhandler(404)
def _handle_404(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def _handle_500(e):
    return render_template('500.html'), 500


@app.before_request
def _csrf_protect():
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return
    submitted = request.form.get('csrf_token', '')
    expected  = session.get('_csrf_token', '')
    if not expected or not hmac.compare_digest(str(submitted), str(expected)):
        flash('Your session expired or the form was invalid — please try again.', 'danger')
        return redirect(request.referrer or url_for('dashboard'))


# ── Signed "mark redeemed from email" links ──────────────────────────────────
# Reminder emails carry a signed, expiring link that lets the recipient record a
# redemption without logging in. The token (not a login) authorises the action
# and is scoped to one instance/benefit/period, so it can't be forged or aimed
# at another user's card. Clicking lands on a confirm page (a GET never mutates,
# so mail-client link prefetch can't create phantom redemptions); the confirm
# POST is CSRF-protected like every other form.
APP_BASE_URL = 'https://cardbenefits.trentschroeder.com'
REDEEM_TOKEN_MAX_AGE = 120 * 24 * 3600  # 120 days — covers the longest (annual) reminder lead


def _redeem_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt='redeem-link')


def _make_redeem_token(uc_id, benefit_id, period_start):
    return _redeem_serializer().dumps([int(uc_id), int(benefit_id), str(period_start)])


def _load_redeem_token(token):
    """Return (uc_id, benefit_id, period_start) or None if invalid/expired."""
    try:
        uc_id, bid, ps = _redeem_serializer().loads(token, max_age=REDEEM_TOKEN_MAX_AGE)
        return int(uc_id), int(bid), str(ps)
    except (BadData, ValueError, TypeError):
        return None


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.get('user'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Gate a route to admin users only. Assumes login_required already ran
    or is composed after this one — if not, will redirect to /login."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        u = g.get('user')
        if not u:
            return redirect(url_for('login', next=request.path))
        if not u['is_admin']:
            flash('Admin access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


# ── DB ─────────────────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db


def init_db():
    db = get_db()
    with open(SCHEMA) as f:
        db.executescript(f.read())
    for drop_col, drop_tbl in [('period_anchor', 'benefits'), ('color', 'cards'), ('issuer', 'cards')]:
        try:
            db.execute(f'ALTER TABLE {drop_tbl} DROP COLUMN {drop_col}')
            db.commit()
        except Exception:
            pass
    try:
        db.execute('ALTER TABLE benefits ADD COLUMN is_subscription INTEGER NOT NULL DEFAULT 0')
        db.commit()
    except Exception:
        pass
    try:
        db.execute('ALTER TABLE cards ADD COLUMN annual_fee REAL')
        db.commit()
    except Exception:
        pass
    try:
        db.execute('ALTER TABLE cards ADD COLUMN published INTEGER NOT NULL DEFAULT 0')
        # First-time backfill: every pre-existing card was the only template
        # library before the published flag existed, so consider them all published.
        db.execute('UPDATE cards SET published = 1')
        db.commit()
    except Exception:
        pass
    try:
        db.execute('ALTER TABLE cards DROP COLUMN last_four')
        db.commit()
    except Exception:
        pass
    # Phase 5: recipient becomes a per-user thing, so card.owner_email is dropped.
    try:
        db.execute('ALTER TABLE cards DROP COLUMN owner_email')
        db.commit()
    except Exception:
        pass
    for col, decl in [
        ('notification_email', 'TEXT'),
        ('reminders_enabled',  'INTEGER NOT NULL DEFAULT 1'),
        ('summary_enabled',    'INTEGER NOT NULL DEFAULT 1'),
        # Account linking. NULL default keeps the REFERENCES clause legal for
        # ALTER ADD COLUMN; account_link_groups was created by executescript above.
        ('link_group_id',      'INTEGER REFERENCES account_link_groups(id) ON DELETE SET NULL'),
    ]:
        try:
            db.execute(f'ALTER TABLE users ADD COLUMN {col} {decl}')
            db.commit()
        except Exception:
            pass
    try:
        db.execute("ALTER TABLE invitations ADD COLUMN purpose TEXT NOT NULL DEFAULT 'invite'")
        db.commit()
    except Exception:
        pass
    # Phase 9a: card sharing — user_cards gets a nullable share_group_id pointing
    # at card_share_groups (created by executescript above). NULL = solo card.
    try:
        db.execute('ALTER TABLE user_cards ADD COLUMN share_group_id INTEGER REFERENCES card_share_groups(id) ON DELETE SET NULL')
        db.commit()
    except Exception:
        pass
    # Phase 9b: invitations gain inviter_user_id + card_id for share invites
    for col, decl in [
        ('inviter_user_id', 'INTEGER REFERENCES users(id) ON DELETE CASCADE'),
        ('card_id',         'INTEGER REFERENCES cards(id) ON DELETE CASCADE'),
    ]:
        try:
            db.execute(f'ALTER TABLE invitations ADD COLUMN {col} {decl}')
            db.commit()
        except Exception:
            pass
    # Phase 10: share invites identify the specific user_cards instance being
    # shared, so a multi-instance inviter can pick which one to share.
    try:
        db.execute('ALTER TABLE invitations ADD COLUMN inviter_user_card_id '
                   'INTEGER REFERENCES user_cards(id) ON DELETE CASCADE')
        db.commit()
    except Exception:
        pass
    _migrate_subscriptions_to_redemptions(db)
    _drop_last_subscription_period_if_exists(db)
    _migrate_credentials_file_to_users(db)
    _migrate_scope_data_to_users(db)
    _migrate_to_per_user_card(db)
    _ensure_user_scoped_indexes(db)
    _backfill_benefit_default_reminders(db)
    db.close()


def _backfill_benefit_default_reminders(db):
    """One-time: give every catalog benefit that has no default reminders a
    sensible schedule based on its period. Guarded by a settings flag so it
    runs once and never overwrites an admin who later clears the defaults."""
    try:
        done = db.execute(
            "SELECT value FROM settings WHERE key = 'benefit_default_reminders_backfilled'"
        ).fetchone()
        if done:
            return
        for b in db.execute('SELECT id, period_type FROM benefits').fetchall():
            has = db.execute(
                'SELECT 1 FROM benefit_default_reminders WHERE benefit_id = ? LIMIT 1',
                (b['id'],)).fetchone()
            if has:
                continue
            for d in _DEFAULT_REMINDER_DAYS.get(b['period_type'], []):
                db.execute(
                    'INSERT OR IGNORE INTO benefit_default_reminders (benefit_id, days_before) '
                    'VALUES (?, ?)', (b['id'], d))
        db.execute(
            "INSERT OR REPLACE INTO settings (key, value) "
            "VALUES ('benefit_default_reminders_backfilled', '1')")
        db.commit()
    except Exception:
        pass


def _migrate_card_shares_to_account_links(db):
    """ONE-TIME, MANUAL migration — run over SSH after deploy, NOT from init_db
    (which has a documented startup write race). Converts the retired card-level
    sharing into account-level links:

      1. Backfill the new per-recipient dedup tables (reminder_sends,
         offer_reminder_sends) from the legacy sent_reminders/offer_sent_reminders
         so a freshly deployed reminder run doesn't re-send already-sent items.
      2. For each card_share_group: link its distinct users into one
         account_link_group (merging any pre-existing link groups), and collapse
         their duplicate user_cards instances of that card into a single
         surviving instance — reassigning redemptions, reminders, user_benefits,
         and reminder_sends to the survivor — so the shared card shows once.
      3. Empty the retired card_share_* tables and null user_cards.share_group_id.

    Idempotent via the 'card_shares_to_links_done' settings flag. Returns a short
    status string. Run with:  python app.py migrate-links
    """
    if db.execute("SELECT 1 FROM settings WHERE key = 'card_shares_to_links_done'").fetchone():
        return 'already done — no-op'

    # 1. Backfill per-recipient dedup (recipient = current owner of card/offer).
    db.execute('''
        INSERT OR IGNORE INTO reminder_sends
            (recipient_user_id, user_card_id, benefit_id, period_start, days_before, sent_at)
        SELECT uc.user_id, sr.user_card_id, sr.benefit_id, sr.period_start, sr.days_before, sr.sent_at
        FROM sent_reminders sr JOIN user_cards uc ON uc.id = sr.user_card_id
    ''')
    db.execute('''
        INSERT OR IGNORE INTO offer_reminder_sends
            (recipient_user_id, offer_id, days_before, sent_at)
        SELECT o.user_id, osr.offer_id, osr.days_before, osr.sent_at
        FROM offer_sent_reminders osr JOIN offers o ON o.id = osr.offer_id
    ''')

    groups = db.execute('SELECT id FROM card_share_groups').fetchall()
    linked = 0
    for grp in groups:
        members = db.execute('''
            SELECT csm.user_card_id, uc.user_id
            FROM card_share_members csm JOIN user_cards uc ON uc.id = csm.user_card_id
            WHERE csm.group_id = ?
        ''', (grp['id'],)).fetchall()
        if not members:
            continue
        user_ids = sorted({m['user_id'] for m in members})
        uc_ids   = sorted({m['user_card_id'] for m in members})
        if len(user_ids) < 2:
            continue  # not actually shared between two people — leave it alone

        # 2a. Link the users into one account_link_group, merging any existing.
        uph = ','.join('?' * len(user_ids))
        existing = sorted({r['link_group_id'] for r in db.execute(
            f'SELECT link_group_id FROM users WHERE id IN ({uph}) AND link_group_id IS NOT NULL',
            (*user_ids,)).fetchall()})
        if existing:
            link_gid = existing[0]
            for other in existing[1:]:
                db.execute('UPDATE users SET link_group_id = ? WHERE link_group_id = ?', (link_gid, other))
                db.execute('DELETE FROM account_link_groups WHERE id = ?', (other,))
        else:
            link_gid = db.execute('INSERT INTO account_link_groups DEFAULT VALUES').lastrowid
        db.execute(f'UPDATE users SET link_group_id = ? WHERE id IN ({uph})', (link_gid, *user_ids))

        # 2b. Collapse duplicate instances of this card into the lowest-id survivor.
        survivor = uc_ids[0]
        for dup in uc_ids[1:]:
            db.execute('UPDATE redemptions SET user_card_id = ? WHERE user_card_id = ?', (survivor, dup))
            for tbl in ('reminders', 'user_benefits', 'reminder_sends'):
                db.execute(f'UPDATE OR IGNORE {tbl} SET user_card_id = ? WHERE user_card_id = ?', (survivor, dup))
                db.execute(f'DELETE FROM {tbl} WHERE user_card_id = ?', (dup,))
            db.execute('DELETE FROM card_share_members WHERE user_card_id = ?', (dup,))
            db.execute('DELETE FROM user_cards WHERE id = ?', (dup,))
        db.execute('UPDATE user_cards SET share_group_id = NULL WHERE id = ?', (survivor,))
        linked += 1

    # 3. Retire the card-level sharing tables (kept in schema; just emptied).
    db.execute('DELETE FROM card_share_members')
    db.execute('DELETE FROM card_share_groups')
    db.execute('UPDATE user_cards SET share_group_id = NULL WHERE share_group_id IS NOT NULL')

    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('card_shares_to_links_done', '1')")
    db.commit()
    return f'converted {linked} share group(s) to account links'


def _ensure_user_scoped_indexes(db):
    """Create the per-instance indexes. Must run AFTER the Phase 10 migration
    has swapped user_id for user_card_id on the affected tables."""
    # Drop legacy indexes (from before the user_card_id migration) if present
    for legacy in ('idx_redemptions_lookup', 'idx_reminders_benefit', 'idx_sent_reminders'):
        try:
            db.execute(f'DROP INDEX IF EXISTS {legacy}')
        except Exception:
            pass
    db.execute('CREATE INDEX IF NOT EXISTS idx_redemptions_lookup ON redemptions(user_card_id, benefit_id, period_start)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_reminders_benefit  ON reminders(user_card_id, benefit_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_sent_reminders     ON sent_reminders(user_card_id, benefit_id, period_start)')
    db.commit()


def _migrate_scope_data_to_users(db):
    """Phase 2: thread user_id through redemptions/reminders/sent_reminders,
    and populate user_cards from the existing cards. All operations are
    idempotent and skip if no admin user exists yet (fresh install)."""
    admin_row = db.execute(
        'SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1'
    ).fetchone()
    if not admin_row:
        return
    admin_id = admin_row['id']

    _migrate_redemptions_add_user_id(db, admin_id)
    _migrate_reminders_to_per_user(db, admin_id)
    _migrate_sent_reminders_to_per_user(db, admin_id)
    _populate_user_cards_for_admin(db, admin_id)


def _migrate_redemptions_add_user_id(db, admin_id):
    cols = {r[1] for r in db.execute('PRAGMA table_info(redemptions)').fetchall()}
    if 'user_id' in cols or 'user_card_id' in cols:
        # Already at Phase 2 (user_id) or advanced to Phase 10 (user_card_id);
        # re-running would collapse per-instance rows and collide.
        return
    # SQLite can't add a NOT NULL column without a default; add nullable, backfill,
    # then rebuild the table to enforce NOT NULL + FK.
    db.execute('ALTER TABLE redemptions ADD COLUMN user_id INTEGER')
    db.execute('UPDATE redemptions SET user_id = ? WHERE user_id IS NULL', (admin_id,))
    db.commit()
    db.execute('PRAGMA foreign_keys = OFF')
    try:
        db.execute('''
            CREATE TABLE redemptions_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                benefit_id   INTEGER NOT NULL,
                period_start DATE    NOT NULL,
                amount       REAL,
                notes        TEXT,
                redeemed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
                FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
            )
        ''')
        db.execute('''
            INSERT INTO redemptions_new (id, user_id, benefit_id, period_start, amount, notes, redeemed_at)
            SELECT id, user_id, benefit_id, period_start, amount, notes, redeemed_at FROM redemptions
        ''')
        db.execute('DROP TABLE redemptions')
        db.execute('ALTER TABLE redemptions_new RENAME TO redemptions')
        db.commit()
    finally:
        db.execute('PRAGMA foreign_keys = ON')


def _migrate_reminders_to_per_user(db, admin_id):
    cols = {r[1] for r in db.execute('PRAGMA table_info(reminders)').fetchall()}
    if 'user_id' in cols or 'user_card_id' in cols:
        # Already at Phase 2 (user_id) or advanced to Phase 10 (user_card_id);
        # re-running would collapse per-instance rows and collide.
        return
    db.commit()
    db.execute('PRAGMA foreign_keys = OFF')
    try:
        db.execute('''
            CREATE TABLE reminders_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                benefit_id  INTEGER NOT NULL,
                days_before INTEGER NOT NULL,
                UNIQUE(user_id, benefit_id, days_before),
                FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
                FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
            )
        ''')
        db.execute('''
            INSERT INTO reminders_new (id, user_id, benefit_id, days_before)
            SELECT id, ?, benefit_id, days_before FROM reminders
        ''', (admin_id,))
        db.execute('DROP TABLE reminders')
        db.execute('ALTER TABLE reminders_new RENAME TO reminders')
        db.commit()
    finally:
        db.execute('PRAGMA foreign_keys = ON')


def _migrate_sent_reminders_to_per_user(db, admin_id):
    cols = {r[1] for r in db.execute('PRAGMA table_info(sent_reminders)').fetchall()}
    if 'user_id' in cols or 'user_card_id' in cols:
        # Already at Phase 2 (user_id) or advanced to Phase 10 (user_card_id);
        # re-running would collapse per-instance rows and collide.
        return
    db.commit()
    db.execute('PRAGMA foreign_keys = OFF')
    try:
        db.execute('''
            CREATE TABLE sent_reminders_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                benefit_id   INTEGER NOT NULL,
                period_start DATE    NOT NULL,
                days_before  INTEGER NOT NULL,
                sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, benefit_id, period_start, days_before),
                FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
                FOREIGN KEY (benefit_id) REFERENCES benefits(id) ON DELETE CASCADE
            )
        ''')
        db.execute('''
            INSERT INTO sent_reminders_new (id, user_id, benefit_id, period_start, days_before, sent_at)
            SELECT id, ?, benefit_id, period_start, days_before, sent_at FROM sent_reminders
        ''', (admin_id,))
        db.execute('DROP TABLE sent_reminders')
        db.execute('ALTER TABLE sent_reminders_new RENAME TO sent_reminders')
        db.commit()
    finally:
        db.execute('PRAGMA foreign_keys = ON')


def _populate_user_cards_for_admin(db, admin_id):
    """Make every existing card show up on the admin's dashboard. Idempotent."""
    db.execute('''
        INSERT INTO user_cards (user_id, card_id, active)
        SELECT ?, id, active FROM cards
        WHERE NOT EXISTS (
            SELECT 1 FROM user_cards uc
            WHERE uc.user_id = ? AND uc.card_id = cards.id
        )
    ''', (admin_id, admin_id))
    db.commit()


def _table_cols(db, table):
    return {r[1] for r in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _migrate_one_table_to_user_card_id(db, table, new_table_sql, copy_sql,
                                        backfill_via_benefit=True,
                                        backfill_via_group=False):
    """Generic per-table migration: add user_card_id (nullable), backfill from
    the existing user_id + a sibling column, then rebuild the table to enforce
    NOT NULL + the new UNIQUE constraints. Skips if user_card_id is already
    present, so partial migrations can resume from where they stopped."""
    cols = _table_cols(db, table)
    if 'user_card_id' in cols:
        return
    db.execute(f'ALTER TABLE {table} ADD COLUMN user_card_id INTEGER')
    if backfill_via_benefit:
        db.execute(f'''
            UPDATE {table} SET user_card_id = (
                SELECT uc.id FROM user_cards uc
                JOIN benefits b ON b.card_id = uc.card_id
                WHERE uc.user_id = {table}.user_id AND b.id = {table}.benefit_id
            )
        ''')
    elif backfill_via_group:
        db.execute(f'''
            UPDATE {table} SET user_card_id = (
                SELECT uc.id FROM user_cards uc
                JOIN card_share_groups csg ON csg.card_id = uc.card_id
                WHERE uc.user_id = {table}.user_id
                  AND csg.id    = {table}.group_id
            )
        ''')
    db.execute(new_table_sql)
    db.execute(copy_sql)
    db.execute(f'DROP TABLE {table}')
    db.execute(f'ALTER TABLE {table}_new RENAME TO {table}')


def _migrate_to_per_user_card(db):
    """Phase 10: thread user_card_id through every per-instance table
    (redemptions, reminders, sent_reminders, user_benefits,
    card_share_members), drop UNIQUE(user_id, card_id) on user_cards,
    and add user_cards.nickname.

    Each per-table step is independently idempotent — re-running on a
    partial state advances the laggards rather than short-circuiting,
    so a half-completed prior run heals on the next start."""
    db.commit()
    db.execute('PRAGMA foreign_keys = OFF')
    try:
        _migrate_one_table_to_user_card_id(db, 'redemptions',
            '''CREATE TABLE redemptions_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_card_id INTEGER NOT NULL,
                benefit_id   INTEGER NOT NULL,
                period_start DATE    NOT NULL,
                amount       REAL,
                notes        TEXT,
                redeemed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_card_id) REFERENCES user_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (benefit_id)   REFERENCES benefits(id)   ON DELETE CASCADE
            )''',
            '''INSERT INTO redemptions_new (id, user_card_id, benefit_id, period_start, amount, notes, redeemed_at)
               SELECT id, user_card_id, benefit_id, period_start, amount, notes, redeemed_at FROM redemptions
               WHERE user_card_id IS NOT NULL''')

        _migrate_one_table_to_user_card_id(db, 'reminders',
            '''CREATE TABLE reminders_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_card_id INTEGER NOT NULL,
                benefit_id   INTEGER NOT NULL,
                days_before  INTEGER NOT NULL,
                UNIQUE(user_card_id, benefit_id, days_before),
                FOREIGN KEY (user_card_id) REFERENCES user_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (benefit_id)   REFERENCES benefits(id)   ON DELETE CASCADE
            )''',
            '''INSERT INTO reminders_new (id, user_card_id, benefit_id, days_before)
               SELECT id, user_card_id, benefit_id, days_before FROM reminders
               WHERE user_card_id IS NOT NULL''')

        _migrate_one_table_to_user_card_id(db, 'sent_reminders',
            '''CREATE TABLE sent_reminders_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_card_id INTEGER NOT NULL,
                benefit_id   INTEGER NOT NULL,
                period_start DATE    NOT NULL,
                days_before  INTEGER NOT NULL,
                sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_card_id, benefit_id, period_start, days_before),
                FOREIGN KEY (user_card_id) REFERENCES user_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (benefit_id)   REFERENCES benefits(id)   ON DELETE CASCADE
            )''',
            '''INSERT INTO sent_reminders_new (id, user_card_id, benefit_id, period_start, days_before, sent_at)
               SELECT id, user_card_id, benefit_id, period_start, days_before, sent_at FROM sent_reminders
               WHERE user_card_id IS NOT NULL''')

        _migrate_one_table_to_user_card_id(db, 'user_benefits',
            '''CREATE TABLE user_benefits_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_card_id INTEGER NOT NULL,
                benefit_id   INTEGER NOT NULL,
                active       INTEGER NOT NULL DEFAULT 1,
                UNIQUE(user_card_id, benefit_id),
                FOREIGN KEY (user_card_id) REFERENCES user_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (benefit_id)   REFERENCES benefits(id)   ON DELETE CASCADE
            )''',
            '''INSERT INTO user_benefits_new (id, user_card_id, benefit_id, active)
               SELECT id, user_card_id, benefit_id, active FROM user_benefits
               WHERE user_card_id IS NOT NULL''')

        _migrate_one_table_to_user_card_id(db, 'card_share_members',
            '''CREATE TABLE card_share_members_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id     INTEGER NOT NULL,
                user_card_id INTEGER NOT NULL,
                joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(group_id, user_card_id),
                FOREIGN KEY (group_id)     REFERENCES card_share_groups(id) ON DELETE CASCADE,
                FOREIGN KEY (user_card_id) REFERENCES user_cards(id)        ON DELETE CASCADE
            )''',
            '''INSERT INTO card_share_members_new (id, group_id, user_card_id, joined_at)
               SELECT id, group_id, user_card_id, joined_at FROM card_share_members
               WHERE user_card_id IS NOT NULL''',
            backfill_via_benefit=False, backfill_via_group=True)

        # === user_cards: drop UNIQUE(user_id, card_id), add nickname ===
        # Detect completion by the absence of a UNIQUE index from sqlite_master.
        # SQLite names anonymous UNIQUE constraints sqlite_autoindex_user_cards_N.
        # Easier: try to insert a duplicate test row in a savepoint; if it
        # fails on UNIQUE, recreate the table. Simpler still: just check
        # whether nickname exists — if so, this step already ran.
        uc_cols = _table_cols(db, 'user_cards')
        if 'nickname' not in uc_cols:
            db.execute('''
                CREATE TABLE user_cards_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER NOT NULL,
                    card_id         INTEGER NOT NULL,
                    active          INTEGER NOT NULL DEFAULT 1,
                    share_group_id  INTEGER,
                    nickname        TEXT,
                    assigned_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id)        REFERENCES users(id)             ON DELETE CASCADE,
                    FOREIGN KEY (card_id)        REFERENCES cards(id)             ON DELETE CASCADE,
                    FOREIGN KEY (share_group_id) REFERENCES card_share_groups(id) ON DELETE SET NULL
                )
            ''')
            db.execute('''
                INSERT INTO user_cards_new (id, user_id, card_id, active, share_group_id, assigned_at)
                SELECT id, user_id, card_id, active, share_group_id, assigned_at FROM user_cards
            ''')
            db.execute('DROP TABLE user_cards')
            db.execute('ALTER TABLE user_cards_new RENAME TO user_cards')
        db.commit()
    finally:
        db.execute('PRAGMA foreign_keys = ON')


def _migrate_credentials_file_to_users(db):
    """One-time backfill: if a legacy .credentials file exists and no admin
    user is in the users table yet, insert an admin row using the file's
    username as the email and its password hash verbatim. Idempotent."""
    if not os.path.exists(CREDS_FILE):
        return
    existing_admin = db.execute(
        'SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1'
    ).fetchone()
    if existing_admin:
        return
    try:
        with open(CREDS_FILE) as f:
            raw = f.read().strip()
        username, pw_hash = raw.split(':', 1)
    except (OSError, ValueError):
        return
    try:
        db.execute(
            'INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, 1)',
            (username, pw_hash)
        )
        db.commit()
    except sqlite3.IntegrityError:
        pass


def _drop_last_subscription_period_if_exists(db):
    """Cleanup: the auto-create-redemption-on-render behavior was reverted,
    so the marker column is no longer needed. Drop it if a prior version
    of this app added it."""
    cols = {r[1] for r in db.execute('PRAGMA table_info(benefits)').fetchall()}
    if 'last_subscription_period' not in cols:
        return
    try:
        db.execute('ALTER TABLE benefits DROP COLUMN last_subscription_period')
        db.commit()
    except Exception:
        pass


def _migrate_subscriptions_to_redemptions(db):
    """One-time backfill: convert subscription_start/end columns into per-period
    redemption rows, then drop the columns. Idempotent — does nothing once the
    columns are gone."""
    cols = {r[1] for r in db.execute('PRAGMA table_info(benefits)').fetchall()}
    if 'subscription_start' not in cols and 'subscription_end' not in cols:
        return

    rows = db.execute(
        'SELECT id, credit_amount, period_type, subscription_start, subscription_end '
        'FROM benefits WHERE is_subscription = 1 AND subscription_start IS NOT NULL'
    ).fetchall()
    today = date.today()
    for r in rows:
        try:
            sub_start = date.fromisoformat(r['subscription_start'])
        except (TypeError, ValueError):
            continue
        sub_end = None
        if r['subscription_end']:
            try:
                sub_end = date.fromisoformat(r['subscription_end'])
            except ValueError:
                pass
        through = min(sub_end, today) if sub_end else today
        cursor = sub_start
        while cursor <= through:
            p_start, p_end = get_current_period(r['period_type'], for_date=cursor)
            existing = db.execute(
                'SELECT 1 FROM redemptions WHERE benefit_id = ? AND period_start = ?',
                (r['id'], str(p_start))
            ).fetchone()
            if not existing:
                db.execute(
                    'INSERT INTO redemptions (benefit_id, period_start, amount, notes) '
                    'VALUES (?, ?, ?, ?)',
                    (r['id'], str(p_start), r['credit_amount'], 'subscription')
                )
            cursor = p_end + timedelta(days=1)
        # If the subscription has already ended in the past, turn off the flag
        # so the scheduler doesn't keep auto-renewing it.
        if sub_end and sub_end < today:
            db.execute('UPDATE benefits SET is_subscription = 0 WHERE id = ?', (r['id'],))

    for col in ('subscription_start', 'subscription_end'):
        try:
            db.execute(f'ALTER TABLE benefits DROP COLUMN {col}')
        except Exception:
            pass
    db.commit()


init_db()


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_setting(db, key, default=None):
    row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(db, key, value):
    db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))


def effective_user_card_ids(db, user_card_id):
    """Return the list of user_cards.id whose redemptions pool together with
    this one. Solo → [user_card_id]. Shared → every share-group member's
    user_cards.id."""
    row = db.execute(
        'SELECT share_group_id FROM user_cards WHERE id = ?',
        (user_card_id,)
    ).fetchone()
    if not row or row['share_group_id'] is None:
        return [user_card_id]
    members = [r['user_card_id'] for r in db.execute(
        'SELECT user_card_id FROM card_share_members WHERE group_id = ?',
        (row['share_group_id'],)
    ).fetchall()]
    return members or [user_card_id]


def linked_user_ids(db, user_id):
    """Return the user ids that share one wallet with user_id, INCLUDING
    user_id itself. Solo account → [user_id]. Linked → both members of the
    account_link_group. Always non-empty and always includes self, so callers
    can build an `IN (...)` clause unconditionally."""
    row = db.execute('SELECT link_group_id FROM users WHERE id = ?', (user_id,)).fetchone()
    if not row or row['link_group_id'] is None:
        return [user_id]
    ids = [r['id'] for r in db.execute(
        'SELECT id FROM users WHERE link_group_id = ? ORDER BY id',
        (row['link_group_id'],)).fetchall()]
    return ids or [user_id]


def _link_partner_id(db, user_id):
    """The other member of a pairwise link, or None if the user isn't linked."""
    others = [i for i in linked_user_ids(db, user_id) if i != user_id]
    return others[0] if others else None


def _dissolve_link(db, user_id):
    """Before deleting `user_id`, dissolve any account link without data loss:
    reassign the departing user's still-shared cards and offers to the partner
    (so the survivor keeps the shared wallet), then clear the link group. No-op
    for a solo account. Caller commits."""
    row = db.execute('SELECT link_group_id FROM users WHERE id = ?', (user_id,)).fetchone()
    if not row or row['link_group_id'] is None:
        return
    gid = row['link_group_id']
    partner_id = _link_partner_id(db, user_id)
    if partner_id is not None:
        # Hand the shared wallet to the surviving partner so the cascade from
        # DELETE FROM users doesn't take it with the departing account.
        db.execute('UPDATE user_cards SET user_id = ? WHERE user_id = ?', (partner_id, user_id))
        db.execute('UPDATE offers     SET user_id = ? WHERE user_id = ?', (partner_id, user_id))
    db.execute('UPDATE users SET link_group_id = NULL WHERE link_group_id = ?', (gid,))
    db.execute('DELETE FROM account_link_groups WHERE id = ?', (gid,))


def today_in_tz(db):
    """Current date in the app's configured reminder timezone. The reminder
    scheduler fires in this zone, so all 'days left' / current-period math must
    use it too — otherwise a UTC server disagrees by a day near boundaries."""
    return datetime.now(ZoneInfo(valid_tz(get_setting(db, 'reminder_tz', DEFAULT_TZ)))).date()


def _safe_hour(value, default=8):
    """Coerce a stored reminder_hour to an int in 0–23, falling back to default
    so a bad value can never wedge the scheduler at startup."""
    try:
        h = int(value)
    except (TypeError, ValueError):
        return default
    return h if 0 <= h <= 23 else default


def enrich_benefit(db, benefit, user_card_id, today=None):
    """Add period info and usage totals to a benefit row dict, scoped to a
    single user_cards instance. Redemption sums pool across share-group
    members when the instance is shared; reminder days remain per-instance.

    `today` is the reference date for period/days-left math; it defaults to the
    app's configured timezone so 'days left' matches when reminders actually
    fire (the server clock may be UTC). Callers in loops can pass it to avoid a
    per-benefit settings read."""
    if today is None:
        today = today_in_tz(db)
    b = dict(benefit)
    period_start, period_end = get_current_period(b['period_type'], for_date=today)
    b['period_start'] = period_start
    b['period_end']   = period_end
    b['days_left']    = days_left(period_end, today=today)
    b['period_label'] = PERIOD_LABELS[b['period_type']]

    pool = effective_user_card_ids(db, user_card_id)
    placeholders = ','.join('?' * len(pool))

    rows = db.execute(
        f'SELECT COALESCE(SUM(amount), 0) AS total FROM redemptions '
        f'WHERE user_card_id IN ({placeholders}) AND benefit_id = ? AND period_start = ?',
        (*pool, b['id'], str(period_start))
    ).fetchone()
    b['amount_used'] = rows['total']

    if b['credit_amount']:
        b['remaining'] = max(0.0, b['credit_amount'] - b['amount_used'])
        b['pct_used']  = min(100, int((b['amount_used'] / b['credit_amount']) * 100))
        b['fully_used'] = b['remaining'] <= 0
    else:
        count = db.execute(
            f'SELECT COUNT(*) FROM redemptions '
            f'WHERE user_card_id IN ({placeholders}) AND benefit_id = ? AND period_start = ?',
            (*pool, b['id'], str(period_start))
        ).fetchone()[0]
        b['remaining']  = 0 if count > 0 else 1
        b['pct_used']   = 100 if count > 0 else 0
        b['fully_used'] = count > 0

    reminders = db.execute(
        'SELECT days_before FROM reminders WHERE user_card_id = ? AND benefit_id = ? '
        'ORDER BY days_before DESC',
        (user_card_id, b['id'])
    ).fetchall()
    b['reminder_days'] = [r['days_before'] for r in reminders]

    return b


_PERIODS_PER_YEAR = {'monthly': 12, 'quarterly': 4, 'semi-annual': 2, 'annual': 1}

# Reminder-day options offered per period type, plus a sane max for the custom
# field. Kept shorter than the period so a chosen reminder can actually fire
# within it (a 30-day reminder on a monthly benefit is meaningless).
_REMINDER_DAY_CHOICES = {
    'monthly':     [1, 3, 7],
    'quarterly':   [1, 3, 7, 14, 30],
    'semi-annual': [1, 3, 7, 14, 30, 60],
    'annual':      [1, 3, 7, 14, 30, 60, 90],
}
_REMINDER_CUSTOM_MAX = {'monthly': 27, 'quarterly': 88, 'semi-annual': 178, 'annual': 364}

# Logical default reminder schedule per period, pre-configured on catalog
# benefits and copied to a user's instance when they add the card. Two nudges
# for longer periods (an early heads-up plus a final reminder), one for monthly.
_DEFAULT_REMINDER_DAYS = {
    'monthly':     [3],
    'quarterly':   [14, 3],
    'semi-annual': [30, 7],
    'annual':      [60, 14],
}
# Full union of offered reminder-day checkboxes; the catalog form's JS narrows
# this to the valid set for the selected period.
_ALL_REMINDER_DAYS = [1, 3, 7, 14, 30, 60, 90]

# ── Offers (gift cards / coupons / promotions) ──────────────────────────────────
# Personal, one-shot items with a fixed expiration date (not a recurring period),
# so reminders are a simple "N days before expiration". A new offer starts with a
# sensible two-nudge schedule the user can tweak.
_OFFER_REMINDER_DAY_CHOICES   = [1, 3, 7, 14, 30]
_OFFER_DEFAULT_REMINDER_DAYS  = [14, 3]


def _parse_offer_reminder_days(form):
    """Read reminder-day checkboxes from an offer form, keeping only the offered
    choices."""
    days = set()
    for raw in form.getlist('reminder_days'):
        try:
            n = int((raw or '').strip())
        except ValueError:
            continue
        if n in _OFFER_REMINDER_DAY_CHOICES:
            days.add(n)
    return sorted(days)


def enrich_offer(db, offer, today=None):
    """Add display fields (remaining balance, days-left, reminder schedule) to an
    offer row dict."""
    if today is None:
        today = today_in_tz(db)
    o = dict(offer)
    if o.get('amount') is not None:
        used = o.get('amount_used') or 0
        o['remaining'] = max(0.0, o['amount'] - used)
        o['pct_used']  = min(100, int((used / o['amount']) * 100)) if o['amount'] else 0
        o['fully_used'] = o['remaining'] <= 0
    else:
        o['remaining'] = None
        o['pct_used']  = 0
        o['fully_used'] = False

    exp = o.get('expiration_date')
    o['expiration_display'] = None
    if exp:
        try:
            exp_date = date.fromisoformat(str(exp))
            o['days_left'] = (exp_date - today).days
            o['expired']   = o['days_left'] < 0
            o['expiration_display'] = exp_date.strftime('%b %-d, %Y') if os.name != 'nt' \
                else exp_date.strftime('%b %#d, %Y')
        except (ValueError, TypeError):
            o['days_left'] = None
            o['expired']   = False
    else:
        o['days_left'] = None
        o['expired']   = False

    rows = db.execute(
        'SELECT days_before FROM offer_reminders WHERE offer_id = ? ORDER BY days_before DESC',
        (o['id'],)).fetchall()
    o['reminder_days'] = [r['days_before'] for r in rows]
    return o


def _offer_email_dict(o):
    """Shape an enriched offer for the reminder-email renderer."""
    detail = None
    if o.get('amount') is not None:
        detail = f"${o['remaining']:,.0f} of ${o['amount']:,.0f} left"
    exp_str = None
    if o.get('expiration_date'):
        try:
            exp_str = date.fromisoformat(str(o['expiration_date'])).strftime('%b %d, %Y')
        except (ValueError, TypeError):
            exp_str = None
    return {
        'name':       o['name'],
        'detail':     detail,
        'expiration': exp_str,
        'days_left':  o.get('days_left'),
    }


def _gather_user_offers(db, uid, today, force=False):
    """For recipient `uid`'s reminder run, return (offers_for_email, due_keys):
      offers_for_email: every active, non-expired offer in the recipient's shared
                        wallet (their own + any linked partner's), shaped for the
                        email footer (awareness on every benefit email);
      due_keys: list of (offer_id, days_before) whose lead-time threshold is
                reached and not yet sent TO THIS RECIPIENT — these drive a
                standalone offers email and get logged to offer_reminder_sends
                (keyed by recipient) once the email goes out."""
    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    rows = db.execute(
        f'SELECT * FROM offers WHERE user_id IN ({ph}) AND archived = 0', (*ids,)).fetchall()
    offers_for_email = []
    due_keys = []
    for r in rows:
        o = enrich_offer(db, r, today)
        if o['expired']:
            continue  # don't keep nagging about offers that already lapsed
        offers_for_email.append(_offer_email_dict(o))
        if o['days_left'] is None:
            continue
        # reminder_days is sorted DESC; fire the first (earliest) threshold that
        # is reached and hasn't been sent to this recipient, mirroring the benefit
        # catch-up logic.
        for d in o['reminder_days']:
            if o['days_left'] <= d:
                already = db.execute(
                    'SELECT 1 FROM offer_reminder_sends WHERE recipient_user_id = ? AND offer_id = ? AND days_before = ?',
                    (uid, o['id'], d)).fetchone()
                if not already or force:
                    due_keys.append((o['id'], d))
                break
    return offers_for_email, due_keys


def _parse_reminder_days(form, period_type):
    """Read the reminder_days checkboxes + custom field from a submitted form,
    keeping only positive ints that fit inside the period."""
    max_d = _REMINDER_CUSTOM_MAX.get(period_type, 364)
    days = set()
    for raw in list(form.getlist('reminder_days')) + [form.get('custom_reminder_day', '')]:
        raw = (raw or '').strip()
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            continue
        if 1 <= n <= max_d:
            days.add(n)
    return sorted(days)


def _benefit_reminder_ctx(selected_days):
    """Template context for the default-reminder controls on the catalog
    benefit form: which days are checked plus the JSON the form's JS needs to
    gate options/defaults by period."""
    return dict(
        selected_days=selected_days,
        all_reminder_days=_ALL_REMINDER_DAYS,
        reminder_day_choices_json=json.dumps(_REMINDER_DAY_CHOICES),
        default_reminder_days_json=json.dumps(_DEFAULT_REMINDER_DAYS),
        reminder_custom_max_json=json.dumps(_REMINDER_CUSTOM_MAX),
    )


def compute_card_roi(db, enriched_benefits, user_card_id):
    """Return (captured, max_possible) for a user_cards instance's benefits
    this calendar year. Captured pools across share-group members."""
    year = str(date.today().year)
    captured = 0.0
    max_possible = 0.0
    pool = effective_user_card_ids(db, user_card_id)
    placeholders = ','.join('?' * len(pool))
    for b in enriched_benefits:
        if not b.get('credit_amount'):
            continue
        ca = b['credit_amount']
        ppy = _PERIODS_PER_YEAR.get(b['period_type'], 1)
        max_possible += ca * ppy
        row = db.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS total FROM redemptions "
            f"WHERE user_card_id IN ({placeholders}) AND benefit_id = ? AND strftime('%Y', period_start) = ?",
            (*pool, b['id'], year)
        ).fetchone()
        captured += row['total']
    return captured, max_possible


# ── Icon shims ────────────────────────────────────────────────────────────────
# iOS Safari probes these root paths in addition to honoring the <link rel="apple-touch-icon">
# tags. Serving them avoids 404s and lets older iOS pick up the home-screen icon.

@app.route('/apple-touch-icon.png')
@app.route('/apple-touch-icon-precomposed.png')
@app.route('/apple-touch-icon-180x180.png')
@app.route('/apple-touch-icon-180x180-precomposed.png')
def apple_touch_icon():
    return send_from_directory(os.path.join(BASE_DIR, 'static'),
                               'icon-180.png', mimetype='image/png')


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(BASE_DIR, 'static'),
                               'icon-192.png', mimetype='image/png')


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.before_request
def _load_current_user():
    """Resolve the effective user for this request.

    Normal case: g.user is whoever's logged in. g.impersonator is None.

    Impersonation case: if the actual session user is admin AND
    session['impersonating_user_id'] is set, g.user becomes that target
    user and g.impersonator points to the admin. Every existing scoped
    query that reads g.user['id'] then automatically operates on the
    impersonated user's data without further code changes.
    """
    g.user         = None
    g.impersonator = None
    uid = session.get('user_id')
    if not uid:
        return
    db = get_db()
    actual = db.execute('SELECT * FROM users WHERE id = ?', (uid,)).fetchone()
    if not actual:
        db.close()
        session.clear()
        return
    imp_id = session.get('impersonating_user_id')
    if imp_id and actual['is_admin']:
        target = db.execute('SELECT * FROM users WHERE id = ?', (imp_id,)).fetchone()
        if target:
            g.user         = target
            g.impersonator = actual
            db.close()
            return
        # target gone — drop the impersonation
        session.pop('impersonating_user_id', None)
    g.user = actual
    db.close()


@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.get('user'):
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        if _rate_limited('login', 10, 300):
            flash('Too many sign-in attempts — please wait a few minutes and try again.', 'danger')
            return redirect(url_for('login'))
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        if email and password:
            db  = get_db()
            row = db.execute(
                'SELECT * FROM users WHERE email = ?', (email,)
            ).fetchone()
            db.close()
            if row and check_password_hash(row['password_hash'], password):
                session.clear()
                session['user_id'] = row['id']
                session.permanent = True
                # Only honour local relative paths in next= to avoid open redirect.
                nxt = request.args.get('next', '')
                if nxt.startswith('/') and not nxt.startswith('//'):
                    return redirect(nxt)
                return redirect(url_for('dashboard'))
        error = 'Invalid email or password.'
    db = get_db()
    signup_enabled = get_setting(db, 'signup_open', '0') == '1'
    db.close()
    return render_template('login.html', error=error, signup_enabled=signup_enabled)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """Open self-service sign-up, toggled by the admin setting 'signup_open'
    ('1' = open; default off). Open registration. Creates a non-admin user and
    logs them in."""
    if g.get('user'):
        return redirect(url_for('dashboard'))
    db = get_db()
    if get_setting(db, 'signup_open', '0') != '1':
        db.close()
        flash("Sign-up isn't open right now — ask the administrator for an invite.", 'info')
        return redirect(url_for('login'))

    error = None
    email = ''
    if request.method == 'POST':
        if _rate_limited('signup', 5, 900):
            db.close()
            flash('Too many attempts — please wait a few minutes and try again.', 'danger')
            return redirect(url_for('signup'))
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm_password', '')
        if not (email and '@' in email):
            error = 'A valid email is required.'
        elif len(password) < 8:
            error = 'Password must be at least 8 characters.'
        elif password != confirm:
            error = "Passwords don't match."
        elif db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone():
            error = 'An account with that email already exists — try signing in.'
        if error is None:
            try:
                cur = db.execute(
                    'INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, 0)',
                    (email, generate_password_hash(password)))
                uid = cur.lastrowid
                db.commit()
            except sqlite3.IntegrityError:
                # Email is UNIQUE COLLATE NOCASE — catch the race / case variant
                # the pre-check can miss, instead of 500-ing.
                db.rollback()
                error = 'An account with that email already exists — try signing in.'
            else:
                db.close()
                session.clear()
                session['user_id']  = uid
                session.permanent   = True
                flash('Welcome! Add your first card to get started.', 'success')
                return redirect(url_for('dashboard'))

    db.close()
    return render_template('signup.html', error=error, email=email)


# ── Invitations / user management ─────────────────────────────────────────────

INVITE_TTL_DAYS  = 7
RESET_TTL_HOURS  = 24


def _now_utc():
    return datetime.now(timezone.utc)


def _hash_invite_token(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _ttl_for_purpose(purpose):
    if purpose in ('reset', 'share', 'link'):
        return timedelta(hours=RESET_TTL_HOURS)
    return timedelta(days=INVITE_TTL_DAYS)


def _create_token(db, user_id, purpose='invite', inviter_user_id=None, card_id=None):
    """Generate a new one-time token for this user + purpose. Revokes prior
    unused tokens of the SAME purpose so only the latest is valid; for
    share invites the revoke is scoped to (invitee, card) so a share for
    Card A doesn't clobber a pending share for Card B.

    Token TTL varies by purpose (7 days invite, 24 hours reset/share)."""
    if purpose == 'share' and card_id is not None:
        db.execute(
            "DELETE FROM invitations WHERE user_id = ? AND used_at IS NULL "
            "AND purpose = 'share' AND card_id = ?",
            (user_id, card_id))
    else:
        db.execute(
            'DELETE FROM invitations WHERE user_id = ? AND used_at IS NULL AND purpose = ?',
            (user_id, purpose))
    raw_token  = secrets.token_urlsafe(32)
    token_hash = _hash_invite_token(raw_token)
    expires_at = _now_utc() + _ttl_for_purpose(purpose)
    db.execute(
        'INSERT INTO invitations (user_id, token_hash, purpose, expires_at, inviter_user_id, card_id) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (user_id, token_hash, purpose, expires_at.isoformat(timespec='seconds'),
         inviter_user_id, card_id))
    return raw_token


def _parse_iso_utc(s):
    """Parse an ISO datetime string. Treat naive strings as UTC."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _user_status(db, user):
    """active | pending | expired — derived from the latest *invite* row.
    Reset-password tokens are ignored for status display."""
    inv = db.execute(
        "SELECT used_at, expires_at FROM invitations "
        "WHERE user_id = ? AND purpose = 'invite' ORDER BY id DESC LIMIT 1",
        (user['id'],)
    ).fetchone()
    if not inv or inv['used_at']:
        return 'active'
    try:
        exp = _parse_iso_utc(inv['expires_at'])
    except Exception:
        return 'pending'
    return 'expired' if exp < _now_utc() else 'pending'


def _send_invite_or_flash(db, user_email, raw_token):
    """Pull SMTP creds from settings and send the invite. Flashes a message
    on failure rather than raising. Returns True on success."""
    gmail_user = get_setting(db, 'gmail_user')
    gmail_pass = get_setting(db, 'gmail_app_password')
    if not all([gmail_user, gmail_pass]):
        flash('Cannot send invite — SMTP credentials are not configured in Settings.', 'danger')
        return False
    accept_url = url_for('accept_invite', token=raw_token, _external=True)
    try:
        send_invite_email(gmail_user, gmail_pass, user_email, accept_url, g.user['email'])
        return True
    except Exception as e:
        flash(f'Failed to send invite email to {user_email}: {e}', 'danger')
        return False


@app.route('/users')
@admin_required
def users_list():
    db = get_db()
    raw_users = db.execute(
        'SELECT * FROM users ORDER BY is_admin DESC, email COLLATE NOCASE'
    ).fetchall()
    users = []
    for u in raw_users:
        users.append({**dict(u), 'status': _user_status(db, u)})
    db.close()
    return render_template('users.html', users=users)


@app.route('/users/new', methods=['POST'])
@admin_required
def user_new():
    email = request.form.get('email', '').strip()
    is_admin = 1 if request.form.get('is_admin') else 0
    if not email or '@' not in email:
        flash('A valid email is required.', 'danger')
        return redirect(url_for('users_list'))
    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
    if existing:
        db.close()
        flash(f'A user with email {email} already exists.', 'danger')
        return redirect(url_for('users_list'))
    placeholder_hash = generate_password_hash(secrets.token_hex(32))
    try:
        cur = db.execute(
            'INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, ?)',
            (email, placeholder_hash, is_admin))
    except sqlite3.IntegrityError:
        db.close()
        flash(f'A user with email {email} already exists.', 'danger')
        return redirect(url_for('users_list'))
    new_user_id = cur.lastrowid
    raw_token = _create_token(db, new_user_id, purpose='invite')
    db.commit()
    sent = _send_invite_or_flash(db, email, raw_token)
    db.close()
    if sent:
        flash(f'Invitation sent to {email}. Expires in {INVITE_TTL_DAYS} days.', 'success')
    else:
        flash(f'Account created for {email}, but the invitation email failed to send. '
              f'Use "Resend invite" once SMTP is configured.', 'warning')
    return redirect(url_for('users_list'))


@app.route('/users/<int:id>/resend-invite', methods=['POST'])
@admin_required
def user_resend_invite(id):
    db = get_db()
    user = db.execute('SELECT id, email FROM users WHERE id = ?', (id,)).fetchone()
    if not user:
        db.close()
        flash('User not found.', 'danger')
        return redirect(url_for('users_list'))
    raw_token = _create_token(db, user['id'], purpose='invite')
    db.commit()
    sent = _send_invite_or_flash(db, user['email'], raw_token)
    db.close()
    if sent:
        flash(f'New invite sent to {user["email"]}.', 'success')
    return redirect(url_for('users_list'))


@app.route('/users/<int:id>/impersonate', methods=['POST'])
@admin_required
def user_impersonate(id):
    if session.get('impersonating_user_id'):
        flash('Already impersonating. Exit first to switch to a different user.', 'danger')
        return redirect(url_for('users_list'))
    if id == g.user['id']:
        flash("Can't impersonate yourself.", 'danger')
        return redirect(url_for('users_list'))
    db = get_db()
    target = db.execute('SELECT id, email FROM users WHERE id = ?', (id,)).fetchone()
    if not target:
        db.close()
        flash('User not found.', 'danger')
        return redirect(url_for('users_list'))
    db.execute(
        'INSERT INTO impersonation_log (admin_id, impersonated_id, started_at) VALUES (?, ?, ?)',
        (g.user['id'], target['id'], _now_utc().isoformat(timespec='seconds')))
    db.commit()
    db.close()
    session['impersonating_user_id'] = target['id']
    flash(f'Now acting as {target["email"]}. Admin actions are disabled until you exit.', 'info')
    return redirect(url_for('dashboard'))


@app.route('/impersonate/stop', methods=['POST'])
@login_required
def impersonate_stop():
    imp_id   = session.pop('impersonating_user_id', None)
    admin_id = g.impersonator['id'] if g.impersonator else None
    if imp_id and admin_id:
        db = get_db()
        db.execute(
            'UPDATE impersonation_log SET stopped_at = ? '
            'WHERE admin_id = ? AND impersonated_id = ? AND stopped_at IS NULL',
            (_now_utc().isoformat(timespec='seconds'), admin_id, imp_id))
        db.commit()
        db.close()
        flash('Exited impersonation.', 'info')
    return redirect(url_for('users_list'))


@app.route('/users/<int:id>/delete', methods=['POST'])
@admin_required
def user_delete(id):
    if id == g.user['id']:
        flash('You cannot delete your own admin account.', 'danger')
        return redirect(url_for('users_list'))
    db = get_db()
    user = db.execute('SELECT email FROM users WHERE id = ?', (id,)).fetchone()
    if not user:
        db.close()
        flash('User not found.', 'danger')
        return redirect(url_for('users_list'))
    _dissolve_link(db, id)
    db.execute('DELETE FROM users WHERE id = ?', (id,))
    db.commit()
    db.close()
    flash(f'Deleted user {user["email"]} and all their data.', 'success')
    return redirect(url_for('users_list'))


def _consume_valid_token(db, token, purpose):
    """Return a row with (invite_id, user_id, email) if the token is valid,
    matches the given purpose, and is not used or expired. Returns None
    otherwise. Caller is responsible for marking used_at after applying."""
    if not token:
        return None
    token_hash = _hash_invite_token(token)
    row = db.execute('''
        SELECT i.id AS invite_id, i.expires_at, i.used_at,
               u.id AS user_id, u.email
        FROM invitations i
        JOIN users u ON u.id = i.user_id
        WHERE i.token_hash = ? AND i.purpose = ?
    ''', (token_hash, purpose)).fetchone()
    if not row or row['used_at']:
        return None
    try:
        exp = _parse_iso_utc(row['expires_at'])
    except Exception:
        return None
    if exp < _now_utc():
        return None
    return row


@app.route('/accept-invite/<token>', methods=['GET', 'POST'])
def accept_invite(token):
    db  = get_db()
    inv = _consume_valid_token(db, token, purpose='invite')
    if not inv:
        db.close()
        return render_template('accept_invite.html', invalid=True), 410

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm_password', '')
        if len(password) < 8:
            db.close()
            return render_template('accept_invite.html', invalid=False, email=inv['email'],
                                    token=token, error='Password must be at least 8 characters.')
        if password != confirm:
            db.close()
            return render_template('accept_invite.html', invalid=False, email=inv['email'],
                                    token=token, error='Passwords do not match.')
        db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                   (generate_password_hash(password), inv['user_id']))
        db.execute('UPDATE invitations SET used_at = ? WHERE id = ?',
                   (_now_utc().isoformat(timespec='seconds'), inv['invite_id']))
        db.commit()
        db.close()
        session.clear()
        session['user_id'] = inv['user_id']
        session.permanent = True
        flash('Account set up. Welcome!', 'success')
        return redirect(url_for('dashboard'))

    db.close()
    return render_template('accept_invite.html', invalid=False, email=inv['email'], token=token)


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if g.get('user'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        if _rate_limited('forgot', 5, 900):
            flash('Too many requests — please wait a few minutes and try again.', 'danger')
            return redirect(url_for('login'))
        email = request.form.get('email', '').strip()
        # Same flash regardless of whether the email exists, to prevent
        # account enumeration.
        generic_msg = ('If an account with that email exists, a reset link has been sent. '
                       f'It expires in {RESET_TTL_HOURS} hours.')
        if not email:
            flash(generic_msg, 'info')
            return redirect(url_for('login'))
        db   = get_db()
        user = db.execute('SELECT id, email FROM users WHERE email = ?', (email,)).fetchone()
        if user:
            raw_token  = _create_token(db, user['id'], purpose='reset')
            db.commit()
            gmail_user = get_setting(db, 'gmail_user')
            gmail_pass = get_setting(db, 'gmail_app_password')
            if gmail_user and gmail_pass:
                reset_url = url_for('reset_password', token=raw_token, _external=True)
                try:
                    send_reset_email(gmail_user, gmail_pass, user['email'], reset_url)
                except Exception as e:
                    app.logger.error(f'Failed to send reset email to {user["email"]}: {e}')
        db.close()
        flash(generic_msg, 'info')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    db  = get_db()
    inv = _consume_valid_token(db, token, purpose='reset')
    if not inv:
        db.close()
        return render_template('reset_password.html', invalid=True), 410

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm_password', '')
        if len(password) < 8:
            db.close()
            return render_template('reset_password.html', invalid=False, email=inv['email'],
                                    token=token, error='Password must be at least 8 characters.')
        if password != confirm:
            db.close()
            return render_template('reset_password.html', invalid=False, email=inv['email'],
                                    token=token, error='Passwords do not match.')
        db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                   (generate_password_hash(password), inv['user_id']))
        db.execute('UPDATE invitations SET used_at = ? WHERE id = ?',
                   (_now_utc().isoformat(timespec='seconds'), inv['invite_id']))
        db.commit()
        db.close()
        session.clear()
        session['user_id'] = inv['user_id']
        session.permanent = True
        flash('Password reset. You are now signed in.', 'success')
        return redirect(url_for('dashboard'))

    db.close()
    return render_template('reset_password.html', invalid=False, email=inv['email'], token=token)


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    db = get_db()
    uid = g.user['id']
    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    user_cards = db.execute(f'''
        SELECT uc.id           AS user_card_id,
               uc.nickname     AS nickname,
               uc.share_group_id,
               c.id            AS card_id,
               c.name          AS card_name,
               c.annual_fee    AS annual_fee,
               c.active        AS card_active,
               c.published     AS card_published
        FROM user_cards uc
        JOIN cards c ON c.id = uc.card_id
        WHERE uc.user_id IN ({ph}) AND uc.active = 1 AND c.active = 1
        ORDER BY c.name, uc.id
    ''', (*ids,)).fetchall()

    dashboard_cards = []
    total_benefits = 0
    total_used = 0

    for ucr in user_cards:
        card_id = ucr['card_id']
        uc_id   = ucr['user_card_id']
        raw_benefits = db.execute('''
            SELECT b.* FROM benefits b
            LEFT JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_card_id = ?
            WHERE b.card_id = ? AND b.active = 1 AND COALESCE(ub.active, 1) = 1
            ORDER BY b.name
        ''', (uc_id, card_id)).fetchall()
        enriched = [enrich_benefit(db, b, uc_id) for b in raw_benefits]
        enriched.sort(key=lambda b: (1 if b['fully_used'] else 0, b['days_left']))
        total_benefits += len(enriched)
        total_used += sum(1 for b in enriched if b['fully_used'])

        archived = db.execute(
            'SELECT * FROM benefits WHERE card_id = ? AND active = 0 ORDER BY name',
            (card_id,)
        ).fetchall()
        not_pursuing = db.execute('''
            SELECT b.* FROM benefits b
            JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_card_id = ?
            WHERE b.card_id = ? AND b.active = 1 AND ub.active = 0
            ORDER BY b.name
        ''', (uc_id, card_id)).fetchall()

        captured, max_possible = compute_card_roi(db, enriched, uc_id)
        annual_fee = ucr['annual_fee'] or 0
        roi = {
            'captured':      captured,
            'max_possible':  max_possible,
            'fee_pct':       min(100, int(captured / annual_fee * 100)) if annual_fee > 0 else None,
            'max_pct':       min(100, int(captured / max_possible * 100)) if max_possible > 0 else 0,
            'fee_tick_pct':  min(100, int(annual_fee / max_possible * 100)) if (annual_fee > 0 and max_possible > 0) else None,
        }
        # Render-friendly card dict: catalog id + name + fee, plus per-instance nickname
        card_view = {
            'id':         card_id,
            'name':       ucr['card_name'],
            'annual_fee': ucr['annual_fee'],
            'active':     ucr['card_active'],
            'published':  ucr['card_published'],
        }
        dashboard_cards.append({
            'user_card_id':   uc_id,
            'card':           card_view,
            'nickname':       ucr['nickname'],
            'display_name':   ucr['nickname'] or ucr['card_name'],
            'benefits':       enriched,
            'archived':       [dict(b) for b in archived],
            'not_pursuing':   [dict(b) for b in not_pursuing],
            'roi':            roi,
        })

    total_captured = sum(dc['roi']['captured'] for dc in dashboard_cards)
    total_fees     = sum(dc['card']['annual_fee'] or 0 for dc in dashboard_cards)
    due_in_30      = sum(
        1 for dc in dashboard_cards
        for b in dc['benefits']
        if not b['fully_used'] and not b['is_subscription'] and b['days_left'] <= 30
    )

    db.close()
    return render_template('dashboard.html',
                           dashboard_cards=dashboard_cards,
                           total_benefits=total_benefits,
                           total_used=total_used,
                           total_captured=total_captured,
                           total_fees=total_fees,
                           due_in_30=due_in_30)


# ── Cards ──────────────────────────────────────────────────────────────────────

@app.route('/add-card')
@login_required
def add_card():
    """User-facing 'browse and add' page. Shows published + active catalog
    cards as tiles with per-tile Add buttons. Distinct from /card-templates,
    which is admin's catalog-management surface."""
    db = get_db()
    ids = linked_user_ids(db, g.user['id'])
    ph  = ','.join('?' * len(ids))
    rows = db.execute(f'''
        SELECT c.id, c.name, c.annual_fee, c.active,
               (SELECT COUNT(*) FROM benefits b WHERE b.card_id = c.id AND b.active = 1) AS benefit_count,
               (SELECT COUNT(*) FROM user_cards uc WHERE uc.card_id = c.id AND uc.user_id IN ({ph}) AND uc.active = 1) AS my_count
        FROM cards c
        WHERE c.published = 1 AND c.active = 1
        ORDER BY c.name
    ''', (*ids,)).fetchall()
    db.close()
    return render_template('add_card.html', cards=rows)


@app.route('/add-card/request', methods=['POST'])
@login_required
def card_request():
    """A user asks for a card that's not in the catalog yet. Stored for admins
    to action on the Card Templates page, and emailed to them (best-effort)."""
    name  = request.form.get('card_name', '').strip()
    notes = request.form.get('notes', '').strip() or None
    if not name:
        flash('Enter the name of the card you want.', 'danger')
        return redirect(url_for('add_card'))
    db = get_db()
    db.execute('INSERT INTO card_requests (user_id, card_name, notes) VALUES (?, ?, ?)',
               (g.user['id'], name, notes))
    db.commit()
    gmail_user = get_setting(db, 'gmail_user')
    gmail_pass = get_setting(db, 'gmail_app_password')
    base = (get_setting(db, 'app_base_url', APP_BASE_URL) or APP_BASE_URL).rstrip('/')
    admins = [r['email'] for r in db.execute(
        'SELECT COALESCE(notification_email, email) AS email FROM users WHERE is_admin = 1'
    ).fetchall() if r['email']]
    db.close()
    if gmail_user and gmail_pass and admins:
        try:
            send_card_request_email(gmail_user, gmail_pass, admins, g.user['email'],
                                    name, notes, f'{base}/card-templates')
        except Exception as e:
            app.logger.error(f'Card request email failed: {e}')
    flash('Thanks! Your request was sent to the admin.', 'success')
    return redirect(url_for('add_card'))


@app.route('/card-requests/<int:req_id>/resolve', methods=['POST'])
@admin_required
def card_request_resolve(req_id):
    status = request.form.get('status', 'done')
    if status not in ('done', 'dismissed'):
        status = 'done'
    db = get_db()
    db.execute('UPDATE card_requests SET status = ? WHERE id = ?', (status, req_id))
    db.commit()
    db.close()
    flash('Request updated.', 'success')
    return redirect(url_for('card_templates'))


@app.route('/card-templates')
@admin_required
def card_templates():
    """Admin's catalog-management page. Lists every catalog card (published
    or not, active or not) with instance counts + edit/delete actions."""
    db = get_db()
    rows = db.execute('''
        SELECT c.id, c.name, c.annual_fee, c.active, c.published,
               (SELECT COUNT(*) FROM benefits b WHERE b.card_id = c.id) AS benefit_count,
               (SELECT COUNT(*) FROM benefits b WHERE b.card_id = c.id AND b.active = 1) AS active_benefit_count,
               (SELECT COUNT(*) FROM user_cards uc WHERE uc.card_id = c.id AND uc.active = 1) AS instance_count
        FROM cards c
        ORDER BY c.active DESC, c.published DESC, c.name
    ''').fetchall()
    requests = db.execute('''
        SELECT cr.id, cr.card_name, cr.notes, cr.created_at, u.email AS requester
        FROM card_requests cr JOIN users u ON u.id = cr.user_id
        WHERE cr.status = 'open'
        ORDER BY cr.created_at DESC
    ''').fetchall()
    db.close()
    return render_template('card_templates.html', cards=rows, requests=requests)


@app.route('/card-templates/<int:card_id>/add', methods=['POST'])
@login_required
def card_templates_add(card_id):
    """Add a new instance of this catalog card to the current user's dashboard.
    Multi-instance: each call creates a brand-new user_cards row, so a user can
    hold several of the same card with independent tracking."""
    db   = get_db()
    uid  = g.user['id']
    card = db.execute(
        'SELECT id, name FROM cards WHERE id = ? AND published = 1 AND active = 1', (card_id,)
    ).fetchone()
    if not card:
        db.close()
        flash('That card is not available.', 'danger')
        return redirect(url_for('add_card'))
    nickname = request.form.get('nickname', '').strip() or None
    cur = db.execute(
        'INSERT INTO user_cards (user_id, card_id, active, nickname) VALUES (?, ?, 1, ?)',
        (uid, card_id, nickname))
    new_uc_id = cur.lastrowid
    # Seed the new instance with each active benefit's default reminders so the
    # user starts with a sensible schedule instead of a blank slate.
    seeded = db.execute('''
        SELECT bdr.benefit_id, bdr.days_before
        FROM benefit_default_reminders bdr
        JOIN benefits b ON b.id = bdr.benefit_id
        WHERE b.card_id = ? AND b.active = 1
    ''', (card_id,)).fetchall()
    for r in seeded:
        db.execute(
            'INSERT OR IGNORE INTO reminders (user_card_id, benefit_id, days_before) '
            'VALUES (?, ?, ?)', (new_uc_id, r['benefit_id'], r['days_before']))
    db.commit()
    db.close()
    label = nickname or card['name']
    if seeded:
        flash(f'Added "{label}" to your wallet with default reminders — '
              'adjust them on each benefit.', 'success')
    else:
        flash(f'Added "{label}" to your wallet.', 'success')
    return redirect(url_for('card_detail', id=new_uc_id))


@app.route('/link-account', methods=['POST'])
@login_required
def link_account():
    """Invite an existing user to LINK accounts. Once accepted, the two accounts
    share one wallet — all cards, benefits, redemptions, and offers. Pairwise:
    neither party may already be linked."""
    db        = get_db()
    uid       = g.user['id']
    inviter   = g.user
    raw_email = request.form.get('email', '').strip()
    if not raw_email:
        db.close()
        flash('Enter the email of the account you want to link with.', 'danger')
        return redirect(url_for('settings'))

    if _link_partner_id(db, uid) is not None:
        db.close()
        flash('Your account is already linked. Unlinking isn\'t available yet.', 'danger')
        return redirect(url_for('settings'))

    invitee = db.execute('SELECT id, email FROM users WHERE email = ?', (raw_email,)).fetchone()
    if not invitee:
        db.close()
        flash(f'No user found with email {raw_email}. Ask the administrator to invite them first.', 'danger')
        return redirect(url_for('settings'))
    if invitee['id'] == uid:
        db.close()
        flash("You can't link an account with itself.", 'danger')
        return redirect(url_for('settings'))
    if _link_partner_id(db, invitee['id']) is not None:
        db.close()
        flash(f'{invitee["email"]} is already linked with another account.', 'danger')
        return redirect(url_for('settings'))

    raw_token = _create_token(db, invitee['id'], purpose='link', inviter_user_id=uid)
    db.commit()

    gmail_user = get_setting(db, 'gmail_user')
    gmail_pass = get_setting(db, 'gmail_app_password')
    if not all([gmail_user, gmail_pass]):
        db.close()
        flash('Link invitation created but the email could not be sent — SMTP is not configured.', 'warning')
        return redirect(url_for('settings'))
    accept_url = url_for('accept_link', token=raw_token, _external=True)
    try:
        send_link_invite_email(gmail_user, gmail_pass, invitee['email'],
                               accept_url, inviter['email'])
    except Exception as e:
        db.close()
        flash(f'Failed to send link invitation to {invitee["email"]}: {e}', 'danger')
        return redirect(url_for('settings'))
    db.close()
    flash(f'Link invitation sent to {invitee["email"]}. Expires in {RESET_TTL_HOURS} hours.', 'success')
    return redirect(url_for('settings'))


@app.route('/accept-link/<token>', methods=['GET', 'POST'])
@login_required
def accept_link(token):
    db  = get_db()
    inv = _consume_valid_token(db, token, purpose='link')
    if not inv:
        db.close()
        return render_template('accept_link.html', invalid=True), 410
    # The token's user_id is the invitee. Reject if the wrong user is logged in.
    if inv['user_id'] != g.user['id']:
        db.close()
        return render_template('accept_link.html', invalid=True,
                                wrong_user_msg=True), 403

    extra = db.execute('''
        SELECT i.inviter_user_id, u.email AS inviter_email
        FROM invitations i
        JOIN users u ON u.id = i.inviter_user_id
        WHERE i.id = ?
    ''', (inv['invite_id'],)).fetchone()
    if not extra:
        db.close()
        return render_template('accept_link.html', invalid=True), 410

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'decline':
            db.execute('UPDATE invitations SET used_at = ? WHERE id = ?',
                       (_now_utc().isoformat(timespec='seconds'), inv['invite_id']))
            db.commit()
            db.close()
            flash(f'Declined the link invitation from {extra["inviter_email"]}.', 'info')
            return redirect(url_for('dashboard'))
        if action != 'accept':
            db.close()
            flash('Unknown action.', 'danger')
            return redirect(url_for('accept_link', token=token))

        inviter_id = extra['inviter_user_id']
        invitee_id = inv['user_id']

        # Re-validate pairwise at accept time (state may have changed since invite).
        if _link_partner_id(db, inviter_id) is not None:
            db.close()
            flash(f'{extra["inviter_email"]} has since linked with another account.', 'danger')
            return redirect(url_for('dashboard'))
        if _link_partner_id(db, invitee_id) is not None:
            db.close()
            flash('Your account is already linked.', 'danger')
            return redirect(url_for('dashboard'))

        cur = db.execute('INSERT INTO account_link_groups DEFAULT VALUES')
        group_id = cur.lastrowid
        db.execute('UPDATE users SET link_group_id = ? WHERE id IN (?, ?)',
                   (group_id, inviter_id, invitee_id))
        db.execute('UPDATE invitations SET used_at = ? WHERE id = ?',
                   (_now_utc().isoformat(timespec='seconds'), inv['invite_id']))
        db.commit()
        db.close()
        flash(f'Your account is now linked with {extra["inviter_email"]} — you share everything.', 'success')
        return redirect(url_for('dashboard'))

    db.close()
    return render_template('accept_link.html',
                            invalid=False,
                            inviter_email=extra['inviter_email'],
                            token=token)


@app.route('/cards/<int:id>/rename', methods=['POST'])
@login_required
def user_card_rename(id):
    """Set or clear the nickname on a user_cards instance the current user owns."""
    nickname = request.form.get('nickname', '').strip() or None
    db = get_db()
    ids = linked_user_ids(db, g.user['id'])
    ph  = ','.join('?' * len(ids))
    row = db.execute(
        f'SELECT id FROM user_cards WHERE id = ? AND user_id IN ({ph})',
        (id, *ids)).fetchone()
    if not row:
        db.close()
        flash('Card not found.', 'danger')
        return redirect(url_for('dashboard'))
    db.execute('UPDATE user_cards SET nickname = ? WHERE id = ?', (nickname, id))
    db.commit()
    db.close()
    flash('Card renamed.', 'success')
    return redirect(url_for('card_detail', id=id))


@app.route('/cards/<int:id>/remove', methods=['POST'])
@login_required
def card_remove(id):
    """Soft-remove this card instance from the current user's dashboard.
    id is a user_cards row id. Redemption history is preserved; re-adding
    is via Card Templates (which creates a fresh instance)."""
    db  = get_db()
    ids = linked_user_ids(db, g.user['id'])
    ph  = ','.join('?' * len(ids))
    row = db.execute(f'''
        SELECT uc.id, COALESCE(uc.nickname, c.name) AS label
        FROM user_cards uc
        JOIN cards c ON c.id = uc.card_id
        WHERE uc.id = ? AND uc.user_id IN ({ph})
    ''', (id, *ids)).fetchone()
    if not row:
        db.close()
        flash('Card not found in your wallet.', 'danger')
        return redirect(url_for('dashboard'))
    db.execute('UPDATE user_cards SET active = 0 WHERE id = ?', (row['id'],))
    db.commit()
    db.close()
    flash(f'"{row["label"]}" removed from your wallet.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/cards')
@login_required
def cards_list():
    """Deprecated: the Cards list page was removed. Per-card management now
    lives on card_detail, reached from the dashboard's "Open card" link. Kept
    as a redirect so old bookmarks/links land on the dashboard instead of 404ing."""
    return redirect(url_for('dashboard'))


@app.route('/cards/new', methods=['GET', 'POST'])
@admin_required
def card_new():
    if request.method == 'POST':
        name        = request.form.get('name', '').strip()
        annual_fee  = request.form.get('annual_fee', '').strip() or None
        published   = 1 if request.form.get('published') else 0
        if annual_fee:
            try:
                annual_fee = float(annual_fee)
            except ValueError:
                annual_fee = None
        if not name:
            flash('Card name is required.', 'danger')
            return render_template('cards/form.html', form=request.form)
        db = get_db()
        cur = db.execute(
            'INSERT INTO cards (name, annual_fee, published) VALUES (?, ?, ?)',
            (name, annual_fee, published))
        cid = cur.lastrowid
        cur = db.execute(
            'INSERT INTO user_cards (user_id, card_id, active) VALUES (?, ?, 1)',
            (g.user['id'], cid))
        new_uc_id = cur.lastrowid
        db.commit()
        db.close()
        flash(f'Card "{name}" added.', 'success')
        return redirect(url_for('card_detail', id=new_uc_id))
    return render_template('cards/form.html', form={})


@app.route('/cards/<int:id>', methods=['GET'])
@login_required
def card_detail(id):
    """Render a single user_cards instance: its nickname, the underlying
    catalog card, and per-instance benefit usage."""
    db  = get_db()
    uid = g.user['id']
    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    row = db.execute(f'''
        SELECT uc.id            AS user_card_id,
               uc.card_id       AS card_id,
               uc.nickname      AS nickname,
               uc.share_group_id,
               uc.active        AS uc_active,
               c.name           AS card_name,
               c.annual_fee     AS annual_fee,
               c.active         AS card_active,
               c.published      AS published
        FROM user_cards uc
        JOIN cards c ON c.id = uc.card_id
        WHERE uc.id = ? AND uc.user_id IN ({ph})
    ''', (id, *ids)).fetchone()
    if not row:
        db.close()
        flash('Card not found.', 'danger')
        return redirect(url_for('dashboard'))

    raw_benefits = db.execute('''
        SELECT b.*, COALESCE(ub.active, 1) AS user_active
        FROM benefits b
        LEFT JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_card_id = ?
        WHERE b.card_id = ?
        ORDER BY b.active DESC, b.name
    ''', (id, row['card_id'])).fetchall()
    benefits = []
    for b in raw_benefits:
        eb = enrich_benefit(db, b, id)
        eb['user_active'] = b['user_active']
        benefits.append(eb)

    card = {
        'id':             row['card_id'],
        'user_card_id':   row['user_card_id'],
        'name':           row['card_name'],
        'annual_fee':     row['annual_fee'],
        'active':         row['card_active'],
        'published':      row['published'],
        'nickname':       row['nickname'],
        'display_name':   row['nickname'] or row['card_name'],
    }
    db.close()
    return render_template('cards/detail.html', card=card, benefits=benefits)


# ── Admin catalog routes (operate on cards.id, not user_cards.id) ─────────

@app.route('/admin-cards/<int:card_id>', methods=['GET', 'POST'])
@admin_required
def catalog_card_edit(card_id):
    db   = get_db()
    card = db.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
    if not card:
        db.close()
        flash('Card not found.', 'danger')
        return redirect(url_for('card_templates'))

    if request.method == 'POST':
        name        = request.form.get('name', '').strip()
        active      = 1 if request.form.get('active') else 0
        published   = 1 if request.form.get('published') else 0
        annual_fee  = request.form.get('annual_fee', '').strip() or None
        if annual_fee:
            try:
                annual_fee = float(annual_fee)
            except ValueError:
                annual_fee = None
        if not name:
            flash('Card name is required.', 'danger')
        else:
            db.execute(
                'UPDATE cards SET name=?, active=?, annual_fee=?, published=? WHERE id=?',
                (name, active, annual_fee, published, card_id))
            db.commit()
            flash('Card updated.', 'success')
        db.close()
        return redirect(url_for('catalog_card_edit', card_id=card_id))

    benefits = db.execute(
        'SELECT * FROM benefits WHERE card_id = ? ORDER BY active DESC, name',
        (card_id,)
    ).fetchall()
    n_instances = db.execute(
        'SELECT COUNT(*) FROM user_cards WHERE card_id = ?', (card_id,)
    ).fetchone()[0]
    db.close()
    return render_template('admin_catalog_card.html', card=card,
                            benefits=benefits, n_instances=n_instances)


@app.route('/admin-cards/<int:card_id>/delete', methods=['POST'])
@admin_required
def catalog_card_delete(card_id):
    db  = get_db()
    row = db.execute('SELECT name FROM cards WHERE id = ?', (card_id,)).fetchone()
    if row:
        db.execute('DELETE FROM cards WHERE id = ?', (card_id,))
        db.commit()
        flash(f'Catalog card "{row["name"]}" deleted (cascaded to every instance and its data).', 'success')
    db.close()
    return redirect(url_for('card_templates'))


# ── Benefits ───────────────────────────────────────────────────────────────────

@app.route('/admin-cards/<int:card_id>/benefits/new', methods=['GET', 'POST'])
@admin_required
def benefit_new(card_id):
    db   = get_db()
    card = db.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
    if not card:
        db.close()
        flash('Card not found.', 'danger')
        return redirect(url_for('card_templates'))

    if request.method == 'POST':
        name               = request.form.get('name', '').strip()
        description        = request.form.get('description', '').strip() or None
        credit_amount      = request.form.get('credit_amount', '').strip() or None
        period_type        = request.form.get('period_type', 'monthly')
        is_subscription    = 1 if request.form.get('is_subscription') else 0
        default_days       = _parse_reminder_days(request.form, period_type)

        if not name:
            flash('Name is required.', 'danger')
            db.close()
            return render_template('admin_catalog_benefit.html', card=card, form=request.form,
                                   benefit=None, period_labels=PERIOD_LABELS,
                                   **_benefit_reminder_ctx(default_days))

        if credit_amount:
            try:
                credit_amount = float(credit_amount)
            except ValueError:
                flash('Credit amount must be a number.', 'danger')
                db.close()
                return render_template('admin_catalog_benefit.html', card=card, form=request.form,
                                       benefit=None, period_labels=PERIOD_LABELS,
                                       **_benefit_reminder_ctx(default_days))

        cur = db.execute(
            'INSERT INTO benefits (card_id, name, description, credit_amount, period_type, is_subscription) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (card_id, name, description, credit_amount, period_type, is_subscription))
        bid = cur.lastrowid
        # Persist the template-level default reminders for this benefit; these
        # are copied to each user's instance when they add the card.
        for d in default_days:
            db.execute(
                'INSERT OR IGNORE INTO benefit_default_reminders (benefit_id, days_before) '
                'VALUES (?, ?)', (bid, d))
        # Also apply them to any instances the admin (or their linked partner)
        # already holds of this card.
        _ids = linked_user_ids(db, g.user['id'])
        _ph  = ','.join('?' * len(_ids))
        my_ucs = [r['id'] for r in db.execute(
            f'SELECT id FROM user_cards WHERE user_id IN ({_ph}) AND card_id = ?',
            (*_ids, card_id)).fetchall()]
        for my_uc_id in my_ucs:
            for d in default_days:
                db.execute(
                    'INSERT OR IGNORE INTO reminders (user_card_id, benefit_id, days_before) '
                    'VALUES (?, ?, ?)', (my_uc_id, bid, d))

        db.commit()
        db.close()
        flash(f'Benefit "{name}" added.', 'success')
        return redirect(url_for('catalog_card_edit', card_id=card_id))

    db.close()
    return render_template('admin_catalog_benefit.html', card=card, form={},
                           benefit=None, period_labels=PERIOD_LABELS,
                           **_benefit_reminder_ctx(_DEFAULT_REMINDER_DAYS['monthly']))


@app.route('/admin-cards/<int:card_id>/benefits/<int:bid>/edit', methods=['GET', 'POST'])
@admin_required
def catalog_benefit_edit(card_id, bid):
    """Admin-only catalog edit for a benefit, including its template-level
    default reminders. Editing the defaults only affects future adds — it does
    not rewrite reminders users have already set on their own instances."""
    db   = get_db()
    card = db.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
    b    = db.execute('SELECT * FROM benefits WHERE id = ? AND card_id = ?',
                       (bid, card_id)).fetchone()
    if not card or not b:
        db.close()
        flash('Benefit not found.', 'danger')
        return redirect(url_for('card_templates'))

    if request.method == 'POST':
        name            = request.form.get('name', '').strip()
        description     = request.form.get('description', '').strip() or None
        credit_amount   = request.form.get('credit_amount', '').strip() or None
        period_type     = request.form.get('period_type', 'monthly')
        is_subscription = 1 if request.form.get('is_subscription') else 0
        active          = 1 if request.form.get('active') else 0
        default_days    = _parse_reminder_days(request.form, period_type)
        if not name:
            flash('Name is required.', 'danger')
            db.close()
            return render_template('admin_catalog_benefit.html', card=card, form=request.form,
                                   benefit=b, period_labels=PERIOD_LABELS,
                                   **_benefit_reminder_ctx(default_days))
        if credit_amount:
            try:
                credit_amount = float(credit_amount)
            except ValueError:
                flash('Credit amount must be a number.', 'danger')
                db.close()
                return render_template('admin_catalog_benefit.html', card=card, form=request.form,
                                       benefit=b, period_labels=PERIOD_LABELS,
                                       **_benefit_reminder_ctx(default_days))
        db.execute(
            'UPDATE benefits SET name=?, description=?, credit_amount=?, period_type=?, is_subscription=?, '
            'active=? WHERE id=?',
            (name, description, credit_amount, period_type, is_subscription, active, bid))
        # Replace this benefit's default reminders with the submitted set.
        db.execute('DELETE FROM benefit_default_reminders WHERE benefit_id = ?', (bid,))
        for d in default_days:
            db.execute(
                'INSERT OR IGNORE INTO benefit_default_reminders (benefit_id, days_before) '
                'VALUES (?, ?)', (bid, d))
        db.commit()
        db.close()
        flash(f'Benefit "{name}" updated.', 'success')
        return redirect(url_for('catalog_card_edit', card_id=card_id))

    saved_days = [r['days_before'] for r in db.execute(
        'SELECT days_before FROM benefit_default_reminders WHERE benefit_id = ? ORDER BY days_before',
        (bid,)).fetchall()]
    db.close()
    return render_template('admin_catalog_benefit.html', card=card, form=dict(b),
                           benefit=b, period_labels=PERIOD_LABELS,
                           **_benefit_reminder_ctx(saved_days))


@app.route('/admin-cards/<int:card_id>/benefits/<int:bid>/delete', methods=['POST'])
@admin_required
def catalog_benefit_delete(card_id, bid):
    db  = get_db()
    row = db.execute('SELECT name FROM benefits WHERE id = ? AND card_id = ?', (bid, card_id)).fetchone()
    if row:
        db.execute('DELETE FROM benefits WHERE id = ?', (bid,))
        db.commit()
        flash(f'Benefit "{row["name"]}" deleted from catalog.', 'success')
    db.close()
    return redirect(url_for('catalog_card_edit', card_id=card_id))


@app.route('/cards/<int:uc_id>/benefits/<int:bid>/edit', methods=['GET', 'POST'])
@login_required
def instance_benefit_edit(uc_id, bid):
    """Per-instance reminder + ignore save handler. The editor UI now lives
    inline on the benefit page (benefit_redemptions); this route persists a
    POST from there and redirects back. A GET (old bookmark/link) is bounced
    to the benefit page, which carries the same controls."""
    db  = get_db()
    uid = g.user['id']
    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    uc_row = db.execute(
        f'SELECT id, card_id, nickname FROM user_cards WHERE id = ? AND user_id IN ({ph})',
        (uc_id, *ids)).fetchone()
    if not uc_row:
        db.close()
        flash('Card instance not found.', 'danger')
        return redirect(url_for('dashboard'))
    b = db.execute('SELECT * FROM benefits WHERE id = ? AND card_id = ?',
                    (bid, uc_row['card_id'])).fetchone()
    if not b:
        db.close()
        flash('Benefit not found on this card.', 'danger')
        return redirect(url_for('card_detail', id=uc_id))
    card = db.execute('SELECT * FROM cards WHERE id = ?', (b['card_id'],)).fetchone()

    if request.method == 'POST':
        reminder_days = request.form.getlist('reminder_days')
        custom_day    = request.form.get('custom_reminder_day', '').strip()
        db.execute('DELETE FROM reminders WHERE user_card_id = ? AND benefit_id = ?',
                   (uc_id, bid))
        for d in reminder_days:
            try:
                db.execute(
                    'INSERT OR IGNORE INTO reminders (user_card_id, benefit_id, days_before) VALUES (?, ?, ?)',
                    (uc_id, bid, int(d)))
            except ValueError:
                pass
        if custom_day:
            try:
                db.execute(
                    'INSERT OR IGNORE INTO reminders (user_card_id, benefit_id, days_before) VALUES (?, ?, ?)',
                    (uc_id, bid, int(custom_day)))
            except ValueError:
                pass
        # Ignore flag: per-user opt-out for this benefit on this card instance.
        ignored = 1 if request.form.get('ignored') else 0
        db.execute(
            'INSERT INTO user_benefits (user_card_id, benefit_id, active) VALUES (?, ?, ?) '
            'ON CONFLICT(user_card_id, benefit_id) DO UPDATE SET active = excluded.active',
            (uc_id, bid, 0 if ignored else 1))
        db.commit()
        db.close()
        flash(f'Settings for "{b["name"]}" updated.', 'success')
        # Honor an explicit return target (the benefit page that posted this
        # form); fall back to the card detail page. Only allow local paths.
        nxt = request.form.get('next', '')
        if nxt.startswith('/') and not nxt.startswith('//'):
            return redirect(nxt)
        return redirect(url_for('card_detail', id=uc_id))

    # GET: the editor is now inline on the benefit page — send the user there.
    db.close()
    return redirect(url_for('benefit_redemptions', id=bid, uc=uc_id))


@app.route('/benefits/<int:id>/pursue-toggle', methods=['POST'])
@login_required
def benefit_pursue_toggle(id):
    """Flip the pursuit flag for this benefit on a specific user_cards
    instance (?uc=<user_card_id>). Per-instance — has no effect on the
    catalog, on other instances, or on other users."""
    db  = get_db()
    uid = g.user['id']
    uc_id_raw = request.values.get('uc')
    try:
        target_uc = int(uc_id_raw) if uc_id_raw else None
    except ValueError:
        target_uc = None
    if not target_uc:
        db.close()
        flash('Missing card instance for this action.', 'danger')
        return redirect(request.referrer or url_for('dashboard'))

    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    row = db.execute(f'''
        SELECT b.name AS bname, uc.id AS uc_id
        FROM benefits b
        JOIN user_cards uc ON uc.card_id = b.card_id
        WHERE b.id = ? AND uc.id = ? AND uc.user_id IN ({ph})
    ''', (id, target_uc, *ids)).fetchone()
    if not row:
        db.close()
        flash('Benefit not found.', 'danger')
        return redirect(url_for('dashboard'))

    existing = db.execute(
        'SELECT id, active FROM user_benefits WHERE user_card_id = ? AND benefit_id = ?',
        (target_uc, id)).fetchone()
    if existing:
        new_state = 0 if existing['active'] else 1
        db.execute('UPDATE user_benefits SET active = ? WHERE id = ?',
                   (new_state, existing['id']))
    else:
        new_state = 0
        db.execute(
            'INSERT INTO user_benefits (user_card_id, benefit_id, active) VALUES (?, ?, 0)',
            (target_uc, id))
    db.commit()
    db.close()
    if new_state:
        flash(f'No longer ignoring "{row["bname"]}".', 'success')
    else:
        flash(f'Ignored "{row["bname"]}" on this card.', 'info')
    return redirect(request.referrer or url_for('dashboard'))




# ── Redemptions ────────────────────────────────────────────────────────────────

def _resolve_target_uc_for_benefit(db, benefit_id, viewer_id, requested_uc=None):
    """Find which user_cards instance to scope a benefit action to.
    If requested_uc is given, validate it. Otherwise fall back to the first
    user_cards row for the benefit's card owned by the viewer or their linked
    partner (the wallet is shared, so either's instance is fair game)."""
    ids = linked_user_ids(db, viewer_id)
    ph  = ','.join('?' * len(ids))
    if requested_uc:
        row = db.execute(f'''
            SELECT uc.id FROM user_cards uc
            JOIN benefits b ON b.card_id = uc.card_id
            WHERE uc.id = ? AND uc.user_id IN ({ph}) AND b.id = ?
        ''', (requested_uc, *ids, benefit_id)).fetchone()
        if row:
            return row['id']
    fb = db.execute(f'''
        SELECT uc.id FROM user_cards uc
        JOIN benefits b ON b.card_id = uc.card_id
        WHERE uc.user_id IN ({ph}) AND b.id = ?
        ORDER BY uc.id LIMIT 1
    ''', (*ids, benefit_id)).fetchone()
    return fb['id'] if fb else None


def _record_period_redemption(db, target_uc, benefit, period_start, amount, notes):
    """Insert a single-period redemption unless the pool already has one for
    that period. Returns True if a row was added, False if it already existed."""
    pool = effective_user_card_ids(db, target_uc)
    ph   = ','.join('?' * len(pool))
    existing = db.execute(
        f'SELECT id FROM redemptions WHERE user_card_id IN ({ph}) AND benefit_id = ? AND period_start = ?',
        (*pool, benefit['id'], str(period_start))).fetchone()
    if existing:
        return False
    db.execute(
        'INSERT INTO redemptions (user_card_id, benefit_id, period_start, amount, notes) '
        'VALUES (?, ?, ?, ?, ?)',
        (target_uc, benefit['id'], str(period_start), amount, notes))
    return True


def _redeem_context(db, uc_id, bid, period_start):
    """Resolve a redeem token's target to display fields + the benefit row, or
    (None, None) if the instance/benefit is gone or inactive. Also reports
    whether the period is already fully recorded (pool-aware)."""
    uc = db.execute(
        'SELECT uc.id, uc.nickname, uc.card_id, uc.active AS uc_active, '
        'c.name AS card_name, c.active AS card_active '
        'FROM user_cards uc JOIN cards c ON c.id = uc.card_id WHERE uc.id = ?',
        (uc_id,)).fetchone()
    if not uc or not uc['uc_active'] or not uc['card_active']:
        return None, None
    b = db.execute('SELECT * FROM benefits WHERE id = ? AND card_id = ? AND active = 1',
                   (bid, uc['card_id'])).fetchone()
    if not b:
        return None, None

    pool = effective_user_card_ids(db, uc_id)
    ph   = ','.join('?' * len(pool))
    if b['credit_amount']:
        used = db.execute(
            f'SELECT COALESCE(SUM(amount), 0) FROM redemptions WHERE user_card_id IN ({ph}) '
            f'AND benefit_id = ? AND period_start = ?', (*pool, bid, period_start)).fetchone()[0]
        already = used >= b['credit_amount']
    else:
        cnt = db.execute(
            f'SELECT COUNT(*) FROM redemptions WHERE user_card_id IN ({ph}) '
            f'AND benefit_id = ? AND period_start = ?', (*pool, bid, period_start)).fetchone()[0]
        already = cnt > 0

    period_end = None
    try:
        _, pe = get_current_period(b['period_type'], for_date=date.fromisoformat(period_start))
        period_end = pe.strftime('%b %d, %Y')
    except ValueError:
        pass

    info = {
        'card_name':     uc['nickname'] or uc['card_name'],
        'benefit_name':  b['name'],
        'credit_amount': b['credit_amount'],
        'period_label':  PERIOD_LABELS.get(b['period_type'], ''),
        'period_end':    period_end,
        'already':       already,
    }
    return info, b


@app.route('/r/<token>', methods=['GET'])
def redeem_link(token):
    """Landing page for the signed redeem link in reminder emails. GET only
    shows a confirmation — it never records anything (prefetch-safe)."""
    data = _load_redeem_token(token)
    if not data:
        return render_template('redeem.html', state='invalid'), 400
    db = get_db()
    info, _ = _redeem_context(db, *data)
    db.close()
    if not info:
        return render_template('redeem.html', state='invalid'), 400
    return render_template('redeem.html',
                           state='already' if info['already'] else 'confirm',
                           token=token, **info)


@app.route('/r/<token>', methods=['POST'])
def redeem_link_confirm(token):
    """Confirm POST from the redeem landing page. Authorised by the signed
    token (no login) and CSRF-checked like every other form."""
    data = _load_redeem_token(token)
    if not data:
        return render_template('redeem.html', state='invalid'), 400
    uc_id, bid, period_start = data
    db = get_db()
    info, benefit = _redeem_context(db, uc_id, bid, period_start)
    if not info:
        db.close()
        return render_template('redeem.html', state='invalid'), 400
    if info['already']:
        db.close()
        return render_template('redeem.html', state='already', token=token, **info)
    _record_period_redemption(db, uc_id, benefit, period_start,
                              benefit['credit_amount'], 'Marked redeemed via email reminder')
    db.commit()
    db.close()
    return render_template('redeem.html', state='done', token=token, **info)


@app.route('/benefits/<int:id>/redeem', methods=['POST'])
@login_required
def benefit_redeem(id):
    db  = get_db()
    uid = g.user['id']
    uc_requested = request.values.get('uc')
    try:
        uc_requested = int(uc_requested) if uc_requested else None
    except ValueError:
        uc_requested = None
    target_uc = _resolve_target_uc_for_benefit(db, id, uid, uc_requested)
    if not target_uc:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    b = db.execute('SELECT * FROM benefits WHERE id = ?', (id,)).fetchone()
    if not b:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    notes = request.form.get('notes', '').strip() or None

    if b['is_subscription']:
        today = today_in_tz(db)
        def _ym(month_key, year_key):
            m = request.form.get(month_key, '').strip()
            y = request.form.get(year_key, '').strip()
            if not m or not y:
                return None
            try:
                return date(int(y), int(m), 1)
            except (ValueError, TypeError):
                return None
        start_date = _ym('redemption_start_month', 'redemption_start_year') or today
        end_date   = _ym('redemption_end_month',   'redemption_end_year')   or today
        if start_date > end_date:
            db.close()
            flash('Start month must be on or before end month.', 'danger')
            return redirect(request.referrer or url_for('dashboard'))

        amount_str = request.form.get('amount', '').strip()
        if amount_str:
            try:
                row_amount = float(amount_str)
            except ValueError:
                row_amount = b['credit_amount']
        else:
            row_amount = b['credit_amount']

        pool = effective_user_card_ids(db, target_uc)
        ph   = ','.join('?' * len(pool))
        cursor = start_date
        count = 0
        while cursor <= end_date:
            p_start, p_end = get_current_period(b['period_type'], for_date=cursor)
            ps_str = str(p_start)
            # On shared cards, a row from ANY pool member covers this period.
            existing = db.execute(
                f'SELECT id FROM redemptions WHERE user_card_id IN ({ph}) AND benefit_id=? AND period_start=?',
                (*pool, id, ps_str)
            ).fetchone()
            if existing:
                db.execute('UPDATE redemptions SET amount = ? WHERE id = ?',
                           (row_amount, existing['id']))
            else:
                db.execute(
                    'INSERT INTO redemptions (user_card_id, benefit_id, period_start, amount, notes) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (target_uc, id, ps_str, row_amount, notes or 'subscription'))
            count += 1
            cursor = p_end + timedelta(days=1)
        db.commit()
        db.close()
        flash(f'Recorded redemption for {count} period{"s" if count != 1 else ""}.', 'success')
        return redirect(request.referrer or url_for('dashboard'))

    redemption_date = None
    date_str = request.form.get('redemption_date', '').strip()
    if date_str:
        try:
            redemption_date = date.fromisoformat(date_str)
        except ValueError:
            pass

    period_start, _ = get_current_period(b['period_type'], for_date=redemption_date or today_in_tz(db))

    amount = request.form.get('amount', '').strip() or None
    if amount:
        try:
            amount = float(amount)
        except ValueError:
            amount = None

    db.execute(
        'INSERT INTO redemptions (user_card_id, benefit_id, period_start, amount, notes) VALUES (?, ?, ?, ?, ?)',
        (target_uc, id, str(period_start), amount, notes))
    db.commit()
    db.close()
    flash('Redemption recorded.', 'success')
    return redirect(request.referrer or url_for('dashboard'))


def _viewer_authorized_for_redemption(db, redemption_row, viewer_id):
    """A user can edit/delete a redemption iff the card it belongs to is owned
    by the viewer or their linked partner (linked accounts share the wallet)."""
    uc_owner = db.execute(
        'SELECT user_id FROM user_cards WHERE id = ?',
        (redemption_row['user_card_id'],)
    ).fetchone()
    if not uc_owner:
        return False
    return uc_owner['user_id'] in linked_user_ids(db, viewer_id)


@app.route('/redemptions/<int:id>/edit', methods=['POST'])
@login_required
def redemption_edit(id):
    db  = get_db()
    row = db.execute(
        'SELECT r.*, b.period_type FROM redemptions r '
        'JOIN benefits b ON b.id = r.benefit_id '
        'WHERE r.id = ?',
        (id,)
    ).fetchone()
    if not row:
        db.close()
        return redirect(url_for('dashboard'))
    if not _viewer_authorized_for_redemption(db, row, g.user['id']):
        db.close()
        return redirect(url_for('dashboard'))
    amount   = request.form.get('amount', '').strip() or None
    notes    = request.form.get('notes', '').strip() or None
    date_str = request.form.get('redemption_date', '').strip()
    if amount:
        try:    amount = float(amount)
        except ValueError: amount = None
    redemption_date = None
    if date_str:
        try:    redemption_date = date.fromisoformat(date_str)
        except ValueError: pass
    period_start, _ = get_current_period(row['period_type'], for_date=redemption_date or today_in_tz(db))
    db.execute('UPDATE redemptions SET amount=?, notes=?, period_start=? WHERE id=?',
               (amount, notes, str(period_start), id))
    db.commit()
    db.close()
    flash('Redemption updated.', 'success')
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/redemptions/<int:id>/delete', methods=['POST'])
@login_required
def redemption_delete(id):
    db  = get_db()
    row = db.execute(
        'SELECT user_card_id FROM redemptions WHERE id = ?',
        (id,)
    ).fetchone()
    if row and _viewer_authorized_for_redemption(db, row, g.user['id']):
        db.execute('DELETE FROM redemptions WHERE id = ?', (id,))
        db.commit()
        flash('Redemption removed.', 'success')
    db.close()
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/benefits/<int:id>/redemptions')
@login_required
def benefit_redemptions(id):
    db  = get_db()
    uid = g.user['id']
    uc_requested = request.values.get('uc')
    try:
        uc_requested = int(uc_requested) if uc_requested else None
    except ValueError:
        uc_requested = None
    target_uc = _resolve_target_uc_for_benefit(db, id, uid, uc_requested)
    if not target_uc:
        db.close()
        flash('Benefit not found.', 'danger')
        return redirect(url_for('dashboard'))
    b = db.execute('''
        SELECT b.*, c.name AS card_name FROM benefits b
        JOIN cards c ON c.id = b.card_id
        WHERE b.id = ?
    ''', (id,)).fetchone()
    if not b:
        db.close()
        flash('Benefit not found.', 'danger')
        return redirect(url_for('dashboard'))
    enriched = enrich_benefit(db, b, target_uc)
    pool = effective_user_card_ids(db, target_uc)
    ph   = ','.join('?' * len(pool))

    # Build last-year period history
    _n_map = {'monthly': 12, 'quarterly': 4, 'semi-annual': 2, 'annual': 1}
    period_history = []
    period_states  = {}
    check_date = today_in_tz(db)
    for _ in range(_n_map.get(enriched['period_type'], 1)):
        p_start, p_end = get_current_period(enriched['period_type'], for_date=check_date)
        pr = db.execute(
            f'SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt '
            f'FROM redemptions WHERE user_card_id IN ({ph}) AND benefit_id=? AND period_start=?',
            (*pool, enriched['id'], str(p_start))
        ).fetchone()
        amount_used = float(pr['total'])
        if enriched['credit_amount']:
            if amount_used >= enriched['credit_amount']:   state = 'full'
            elif amount_used > 0:                          state = 'partial'
            else:                                          state = 'none'
        else:
            state = 'full' if pr['cnt'] > 0 else 'none'
        period_history.append({'period_start': p_start, 'period_end': p_end,
                                'amount_used': amount_used, 'state': state})
        period_states[str(p_start)] = state
        check_date = p_start - timedelta(days=1)
    period_history.reverse()

    # Group all redemptions by period_start (pooled across share members)
    from collections import defaultdict
    all_redemptions = db.execute(
        f'SELECT * FROM redemptions WHERE user_card_id IN ({ph}) AND benefit_id=? '
        f'ORDER BY period_start DESC, redeemed_at DESC',
        (*pool, id)
    ).fetchall()
    redemptions_by_period = defaultdict(list)
    for r in all_redemptions:
        redemptions_by_period[r['period_start']].append(r)

    # Older periods (beyond last year) that have redemptions
    oldest_in_range = str(period_history[0]['period_start']) if period_history else None
    has_older = bool(oldest_in_range and db.execute(
        f'SELECT 1 FROM redemptions WHERE user_card_id IN ({ph}) AND benefit_id=? AND period_start<? LIMIT 1',
        (*pool, id, oldest_in_range)
    ).fetchone())

    show_all = request.args.get('all') == '1'
    older_periods = []
    if show_all and oldest_in_range:
        old_starts = db.execute(
            f'SELECT DISTINCT period_start FROM redemptions '
            f'WHERE user_card_id IN ({ph}) AND benefit_id=? AND period_start<? ORDER BY period_start DESC',
            (*pool, id, oldest_in_range)
        ).fetchall()
        for row in old_starts:
            ps_str  = row['period_start']
            ps_date = date.fromisoformat(ps_str)
            p_start, p_end = get_current_period(enriched['period_type'], for_date=ps_date)
            rds         = redemptions_by_period.get(ps_str, [])
            amount_used = float(sum(r['amount'] or 0 for r in rds))
            if enriched['credit_amount']:
                if amount_used >= enriched['credit_amount']:   state = 'full'
                elif amount_used > 0:                          state = 'partial'
                else:                                          state = 'none'
            else:
                state = 'full' if rds else 'none'
            older_periods.append({'period_start': p_start, 'period_end': p_end,
                                   'amount_used': amount_used, 'state': state})
            period_states[ps_str] = state

    ub = db.execute(
        'SELECT active FROM user_benefits WHERE user_card_id = ? AND benefit_id = ?',
        (target_uc, id)).fetchone()
    is_ignored = ub is not None and ub['active'] == 0

    db.close()
    return render_template('benefits/redemptions.html', benefit=enriched,
                           period_history=period_history, period_states=period_states,
                           redemptions=list(all_redemptions),
                           older_periods=older_periods, has_older=has_older, show_all=show_all,
                           user_card_id=target_uc, is_ignored=is_ignored,
                           reminder_options=_REMINDER_DAY_CHOICES.get(enriched['period_type'], [1, 3, 7, 14, 30]),
                           reminder_custom_max=_REMINDER_CUSTOM_MAX.get(enriched['period_type'], 365))


# ── Settings ───────────────────────────────────────────────────────────────────

@app.route('/preferences')
@login_required
def preferences():
    """Merged into the unified Settings page; kept as a redirect for old links."""
    return redirect(url_for('settings'))


@app.route('/preferences/password', methods=['POST'])
@login_required
def change_password():
    current = request.form.get('current_password', '')
    new     = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if not check_password_hash(g.user['password_hash'], current):
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('settings'))
    if len(new) < 8:
        flash('New password must be at least 8 characters.', 'danger')
        return redirect(url_for('settings'))
    if new != confirm:
        flash('New passwords do not match.', 'danger')
        return redirect(url_for('settings'))
    db = get_db()
    db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
               (generate_password_hash(new), g.user['id']))
    db.commit()
    db.close()
    flash('Password updated.', 'success')
    return redirect(url_for('settings'))


@app.route('/account/close', methods=['POST'])
@login_required
def account_close():
    """Self-service account deletion. Removes the user row, which cascades to
    their cards, redemptions, reminders, sent_reminders, user_benefits, share
    memberships, and invitations (ON DELETE CASCADE, with foreign_keys ON in
    get_db). The email is freed, so they can sign up again later."""
    if g.impersonator:
        flash('Exit impersonation before closing an account.', 'danger')
        return redirect(url_for('settings'))
    if not check_password_hash(g.user['password_hash'], request.form.get('current_password', '')):
        flash('Password is incorrect — your account was not closed.', 'danger')
        return redirect(url_for('settings'))

    db = get_db()
    if g.user['is_admin']:
        admin_count = db.execute('SELECT COUNT(*) FROM users WHERE is_admin = 1').fetchone()[0]
        if admin_count <= 1:
            db.close()
            flash('You are the only admin — promote another user to admin before closing your account.', 'danger')
            return redirect(url_for('settings'))

    _dissolve_link(db, g.user['id'])
    db.execute('DELETE FROM users WHERE id = ?', (g.user['id'],))
    db.commit()
    db.close()
    session.clear()
    flash('Your account and all of its data have been deleted. You can sign up again any time.', 'success')
    return redirect(url_for('login'))


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """Unified settings page. Everyone manages their own notification email and
    password here; admins additionally manage the system-wide SMTP sender, the
    daily reminder hour, and its timezone. Admin fields are gated both in the
    template (is_admin) and here (section == 'email_config' requires is_admin)."""
    db = get_db()
    if request.method == 'POST':
        section = request.form.get('section', 'profile')

        if section == 'email_config':
            if not g.user['is_admin']:
                db.close()
                flash('Admin access required.', 'danger')
                return redirect(url_for('settings'))
            gmail_user     = request.form.get('gmail_user', '').strip()
            gmail_password = request.form.get('gmail_password', '').strip()
            reminder_hour  = request.form.get('reminder_hour', '8').strip()
            reminder_tz    = request.form.get('reminder_tz', '').strip()
            if gmail_user:
                set_setting(db, 'gmail_user', gmail_user)
            if gmail_password:
                set_setting(db, 'gmail_app_password', gmail_password)
            # Reminder hour must be an integer 0–23. A bad value is rejected
            # (the previous valid value stays) so it can't wedge the scheduler.
            hour_bad = False
            if reminder_hour:
                if reminder_hour.isdigit() and 0 <= int(reminder_hour) <= 23:
                    set_setting(db, 'reminder_hour', str(int(reminder_hour)))
                else:
                    hour_bad = True
            tz_bad = bool(reminder_tz) and valid_tz(reminder_tz, None) is None
            if reminder_tz and not tz_bad:
                set_setting(db, 'reminder_tz', reminder_tz)
            db.commit()
            _reschedule_reminder(_safe_hour(get_setting(db, 'reminder_hour', '8')),
                                 get_setting(db, 'reminder_tz', DEFAULT_TZ))
            db.close()
            warnings = []
            if tz_bad:
                warnings.append("that timezone wasn't recognized")
            if hour_bad:
                warnings.append('the reminder hour must be a whole number from 0 to 23')
            if warnings:
                flash('Saved, but ' + ' and '.join(warnings) + ' — left unchanged.', 'warning')
            else:
                flash('Email settings saved.', 'success')
            return redirect(url_for('settings'))

        if section == 'signup':
            if not g.user['is_admin']:
                db.close()
                flash('Admin access required.', 'danger')
                return redirect(url_for('settings'))
            set_setting(db, 'signup_open', '1' if request.form.get('signup_open') else '0')
            db.commit()
            db.close()
            flash('Sign-up settings saved.', 'success')
            return redirect(url_for('settings'))

        # Per-user profile (every logged-in user)
        notification_email = request.form.get('notification_email', '').strip() or None
        db.execute('UPDATE users SET notification_email = ? WHERE id = ?',
                   (notification_email, g.user['id']))
        db.commit()
        db.close()
        flash('Preferences saved.', 'success')
        return redirect(url_for('settings'))

    cfg = {}
    if g.user['is_admin']:
        cfg = {
            'gmail_user':    get_setting(db, 'gmail_user', ''),
            'reminder_hour': get_setting(db, 'reminder_hour', '8'),
            'reminder_tz':   get_setting(db, 'reminder_tz', DEFAULT_TZ),
            'signup_open':   get_setting(db, 'signup_open', '0') == '1',
        }
    partner_id = _link_partner_id(db, g.user['id'])
    linked_partner_email = None
    if partner_id is not None:
        prow = db.execute('SELECT email FROM users WHERE id = ?', (partner_id,)).fetchone()
        linked_partner_email = prow['email'] if prow else None
    db.close()
    return render_template('settings.html', cfg=cfg,
                           linked_partner_email=linked_partner_email)


@app.route('/email/test-reminder', methods=['POST'])
@login_required
def send_test_reminder():
    """Email the current user a reminder built from their outstanding benefits,
    so they can preview the reminder layout. Falls back to a sample row if
    nothing is currently due. Admin may override the recipient for SMTP tests."""
    db = get_db()
    gmail_user = get_setting(db, 'gmail_user')
    gmail_pass = get_setting(db, 'gmail_app_password')
    if not all([gmail_user, gmail_pass]):
        db.close()
        flash('Gmail credentials are not configured. Ask the administrator to set them up.', 'danger')
        return redirect(url_for('settings'))

    uid       = g.user['id']
    recipient = g.user['notification_email'] or g.user['email']
    test_recipient = request.form.get('test_recipient', '').strip() or None
    if test_recipient and g.user['is_admin']:
        recipient = test_recipient

    base_url = (get_setting(db, 'app_base_url', APP_BASE_URL) or APP_BASE_URL).rstrip('/')
    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    rows = db.execute(f'''
        SELECT b.*, c.name AS card_name, uc.id AS user_card_id, uc.nickname AS nickname
        FROM benefits b
        JOIN cards c       ON c.id      = b.card_id
        JOIN user_cards uc ON uc.card_id = c.id
        LEFT JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_card_id = uc.id
        WHERE uc.user_id IN ({ph}) AND uc.active = 1 AND b.active = 1 AND c.active = 1
              AND COALESCE(ub.active, 1) = 1
    ''', (*ids,)).fetchall()

    due = []
    for row in rows:
        uc_id = row['user_card_id']
        b = enrich_benefit(db, row, uc_id)
        if b['fully_used'] or b['is_subscription']:
            continue
        due.append({
            'card_name':     row['nickname'] or row['card_name'],
            'benefit_name':  b['name'],
            'credit_amount': b['credit_amount'],
            'amount_used':   b['amount_used'],
            'period_end':    b['period_end'].strftime('%b %d, %Y'),
            'days_left':     b['days_left'],
            'redeem_url':    f"{base_url}/r/{_make_redeem_token(uc_id, b['id'], str(b['period_start']))}",
        })
    # Preview the awareness footer too, using the user's real active offers.
    offers_email, _ = _gather_user_offers(db, uid, today_in_tz(db))
    db.close()

    due.sort(key=lambda d: d['days_left'])
    if not due:
        due = [{
            'card_name': 'Sample Card', 'benefit_name': 'Example $50 Credit',
            'credit_amount': 50, 'amount_used': 0,
            'period_end': date.today().strftime('%b %d, %Y'), 'days_left': 7,
            'redeem_url': None,
        }]

    try:
        send_reminder_email(gmail_user, gmail_pass, recipient, due, offers=offers_email)
        flash(f'Test reminder sent to {recipient} — {len(due)} benefit(s).', 'success')
    except Exception as e:
        flash(f'Failed to send email: {e}', 'danger')
    return redirect(url_for('settings'))


# ── Offers (gift cards / coupons / promotions) ──────────────────────────────────

def _offer_form_ctx(selected_days):
    """Template context for the offer create/edit form's reminder controls."""
    return dict(
        offer_reminder_choices=_OFFER_REMINDER_DAY_CHOICES,
        selected_days=selected_days,
    )


def _read_offer_form(form):
    """Parse + validate an offer form. Returns (values_dict, error_msg). On
    success error_msg is None; the dict holds cleaned fields plus reminder_days."""
    name        = form.get('name', '').strip()
    description = form.get('description', '').strip() or None
    amount_raw  = form.get('amount', '').strip()
    exp_raw     = form.get('expiration_date', '').strip()
    days        = _parse_offer_reminder_days(form)

    if not name:
        return None, 'Name is required.'

    amount = None
    if amount_raw:
        try:
            amount = float(amount_raw)
        except ValueError:
            return None, 'Amount must be a number.'
        if amount <= 0:
            return None, 'Amount must be greater than zero.'

    expiration_date = None
    if exp_raw:
        try:
            expiration_date = date.fromisoformat(exp_raw).isoformat()
        except ValueError:
            return None, 'Expiration date is invalid.'

    return {
        'name': name, 'description': description,
        'amount': amount, 'expiration_date': expiration_date,
        'reminder_days': days,
    }, None


@app.route('/offers')
@login_required
def offers_list():
    db  = get_db()
    uid = g.user['id']
    today = today_in_tz(db)
    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    rows = db.execute(
        f'SELECT * FROM offers WHERE user_id IN ({ph}) AND archived = 0 ORDER BY '
        # Soonest expiration first; undated offers sink to the bottom.
        'CASE WHEN expiration_date IS NULL THEN 1 ELSE 0 END, expiration_date, created_at DESC',
        (*ids,)).fetchall()
    offers = [enrich_offer(db, r, today) for r in rows]
    db.close()
    return render_template('offers/list.html', offers=offers)


@app.route('/offers/new', methods=['GET', 'POST'])
@login_required
def offer_new():
    if request.method == 'POST':
        values, err = _read_offer_form(request.form)
        if err:
            flash(err, 'danger')
            return render_template('offers/form.html', offer=None, form=request.form,
                                   **_offer_form_ctx(_parse_offer_reminder_days(request.form)))
        db  = get_db()
        cur = db.execute(
            'INSERT INTO offers (user_id, name, description, amount, expiration_date) '
            'VALUES (?, ?, ?, ?, ?)',
            (g.user['id'], values['name'], values['description'],
             values['amount'], values['expiration_date']))
        oid = cur.lastrowid
        for d in values['reminder_days']:
            db.execute('INSERT OR IGNORE INTO offer_reminders (offer_id, days_before) VALUES (?, ?)',
                       (oid, d))
        db.commit()
        db.close()
        flash(f'Offer "{values["name"]}" added.', 'success')
        return redirect(url_for('offers_list'))

    return render_template('offers/form.html', offer=None, form={},
                           **_offer_form_ctx(_OFFER_DEFAULT_REMINDER_DAYS))


@app.route('/offers/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def offer_edit(id):
    db  = get_db()
    uid = g.user['id']
    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    offer = db.execute(f'SELECT * FROM offers WHERE id = ? AND user_id IN ({ph})', (id, *ids)).fetchone()
    if not offer:
        db.close()
        flash('Offer not found.', 'danger')
        return redirect(url_for('offers_list'))

    if request.method == 'POST':
        values, err = _read_offer_form(request.form)
        if err:
            db.close()
            flash(err, 'danger')
            return render_template('offers/form.html', offer=offer, form=request.form,
                                   **_offer_form_ctx(_parse_offer_reminder_days(request.form)))
        # Don't let an edit drop the recorded balance below what's already used.
        amount_used = offer['amount_used'] or 0
        if values['amount'] is not None and values['amount'] < amount_used:
            db.close()
            flash(f'Amount can\'t be less than the ${amount_used:,.2f} already redeemed.', 'danger')
            return render_template('offers/form.html', offer=offer, form=request.form,
                                   **_offer_form_ctx(values['reminder_days']))
        db.execute(
            'UPDATE offers SET name=?, description=?, amount=?, expiration_date=? WHERE id=?',
            (values['name'], values['description'],
             values['amount'], values['expiration_date'], id))
        # Replace the reminder schedule with the submitted set; reset the dedup
        # log so a newly added threshold can fire again this cycle.
        db.execute('DELETE FROM offer_reminders WHERE offer_id = ?', (id,))
        for d in values['reminder_days']:
            db.execute('INSERT OR IGNORE INTO offer_reminders (offer_id, days_before) VALUES (?, ?)',
                       (id, d))
        db.execute('DELETE FROM offer_sent_reminders WHERE offer_id = ?', (id,))
        db.commit()
        db.close()
        flash(f'Offer "{values["name"]}" updated.', 'success')
        return redirect(url_for('offers_list'))

    saved_days = [r['days_before'] for r in db.execute(
        'SELECT days_before FROM offer_reminders WHERE offer_id = ? ORDER BY days_before', (id,)).fetchall()]
    db.close()
    return render_template('offers/form.html', offer=offer, form=dict(offer),
                           **_offer_form_ctx(saved_days))


@app.route('/offers/<int:id>/redeem', methods=['POST'])
@login_required
def offer_redeem(id):
    """Record a redemption against an offer. For dollar offers, add to the used
    total (or 'mark fully used' to zero out the balance); when the balance hits
    zero the offer is archived. For non-dollar offers, this just marks it used
    and archives it. Per the spec we keep no per-redemption history."""
    db  = get_db()
    uid = g.user['id']
    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    offer = db.execute(f'SELECT * FROM offers WHERE id = ? AND user_id IN ({ph})', (id, *ids)).fetchone()
    if not offer:
        db.close()
        flash('Offer not found.', 'danger')
        return redirect(url_for('offers_list'))

    if offer['amount'] is not None:
        used = offer['amount_used'] or 0
        if request.form.get('full'):
            used = offer['amount']
        else:
            amount_raw = request.form.get('amount', '').strip()
            try:
                add = float(amount_raw)
            except ValueError:
                db.close()
                flash('Enter a valid redemption amount.', 'danger')
                return redirect(url_for('offers_list'))
            if add <= 0:
                db.close()
                flash('Redemption amount must be greater than zero.', 'danger')
                return redirect(url_for('offers_list'))
            used = min(offer['amount'], used + add)
        archived = 1 if used >= offer['amount'] else 0
        db.execute('UPDATE offers SET amount_used = ?, archived = ? WHERE id = ?',
                   (used, archived, id))
        msg = (f'"{offer["name"]}" fully redeemed — moved out of your list.' if archived
               else f'Recorded — ${max(0.0, offer["amount"] - used):,.2f} left on "{offer["name"]}".')
    else:
        db.execute('UPDATE offers SET archived = 1 WHERE id = ?', (id,))
        msg = f'"{offer["name"]}" marked used.'

    db.commit()
    db.close()
    flash(msg, 'success')
    return redirect(url_for('offers_list'))


@app.route('/offers/<int:id>/delete', methods=['POST'])
@login_required
def offer_delete(id):
    db  = get_db()
    uid = g.user['id']
    ids = linked_user_ids(db, uid)
    ph  = ','.join('?' * len(ids))
    row = db.execute(f'SELECT name FROM offers WHERE id = ? AND user_id IN ({ph})', (id, *ids)).fetchone()
    if row:
        db.execute(f'DELETE FROM offers WHERE id = ? AND user_id IN ({ph})', (id, *ids))
        db.commit()
        flash(f'Offer "{row["name"]}" deleted.', 'success')
    db.close()
    return redirect(url_for('offers_list'))


# ── Reminder logic ─────────────────────────────────────────────────────────────

def _run_reminder_check(force=False):
    """
    Iterate over every user with reminders_enabled = 1. For each, find their
    active-benefits-due-today and email them at notification_email (falling
    back to their login email). Returns the total count of benefits emailed
    across all users. force=True bypasses the "already sent" dedup check.
    """
    db = get_db()
    gmail_user = get_setting(db, 'gmail_user')
    gmail_pass = get_setting(db, 'gmail_app_password')
    if not all([gmail_user, gmail_pass]):
        db.close()
        return 0

    # Honour the per-user reminders_enabled flag: a user who has turned reminder
    # emails off gets neither benefit reminders nor offer reminders/footers.
    users = db.execute('''
        SELECT id, email, notification_email
        FROM users
        WHERE reminders_enabled = 1
    ''').fetchall()

    base_url = (get_setting(db, 'app_base_url', APP_BASE_URL) or APP_BASE_URL).rstrip('/')
    total_sent = 0

    for user in users:
        uid       = user['id']
        recipient = user['notification_email'] or user['email']
        if not recipient:
            continue

        # Shared wallet: a linked partner's cards are in this recipient's wallet
        # too, so both partners are reminded about every shared card.
        link_ids = linked_user_ids(db, uid)
        link_ph  = ','.join('?' * len(link_ids))
        raw = db.execute(f'''
            SELECT b.*, c.name AS card_name, uc.id AS user_card_id, uc.nickname AS nickname
            FROM benefits b
            JOIN cards c       ON c.id      = b.card_id
            JOIN user_cards uc ON uc.card_id = c.id
            LEFT JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_card_id = uc.id
            WHERE uc.user_id IN ({link_ph}) AND uc.active = 1
                  AND b.active = 1 AND c.active = 1
                  AND COALESCE(ub.active, 1) = 1
        ''', (*link_ids,)).fetchall()

        benefits_due = []
        for row in raw:
            uc_id = row['user_card_id']
            b = enrich_benefit(db, row, uc_id)
            if b['fully_used']:
                continue
            period_start_str = str(b['period_start'])
            dl = b['days_left']
            for days_before in b['reminder_days']:
                # Catch-up: fire when at or past the threshold and this
                # (period, days_before) slot hasn't been sent TO THIS RECIPIENT —
                # so a missed day still goes out instead of being skipped forever,
                # and both linked partners each get their own copy. The per-slot
                # dedup below keeps the normal one-email-per-threshold cadence.
                if dl <= days_before:
                    already_sent = db.execute(
                        'SELECT 1 FROM reminder_sends WHERE recipient_user_id=? AND user_card_id=? AND benefit_id=? AND period_start=? AND days_before=?',
                        (uid, uc_id, b['id'], period_start_str, days_before)
                    ).fetchone()
                    if not already_sent or force:
                        display_card = row['nickname'] or row['card_name']
                        benefits_due.append({
                            'card_name':     display_card,
                            'benefit_name':  b['name'],
                            'credit_amount': b['credit_amount'],
                            'amount_used':   b['amount_used'],
                            'period_end':    b['period_end'].strftime('%b %d, %Y'),
                            'days_left':     dl,
                            'redeem_url':    f"{base_url}/r/{_make_redeem_token(uc_id, b['id'], period_start_str)}",
                        })
                        if not already_sent:
                            db.execute(
                                'INSERT OR IGNORE INTO reminder_sends (recipient_user_id, user_card_id, benefit_id, period_start, days_before) '
                                'VALUES (?, ?, ?, ?, ?)',
                                (uid, uc_id, b['id'], period_start_str, days_before))
                        break  # only include a benefit once per email

        # Offers ride along every benefit email as an awareness footer; if none
        # of the user's benefits are due but an offer's lead-time has arrived,
        # send a standalone offers email instead.
        offers_email, due_offer_keys = _gather_user_offers(db, uid, today_in_tz(db), force)

        if benefits_due or due_offer_keys:
            try:
                send_reminder_email(gmail_user, gmail_pass, recipient, benefits_due, offers=offers_email)
                for oid, d in due_offer_keys:
                    db.execute(
                        'INSERT OR IGNORE INTO offer_reminder_sends (recipient_user_id, offer_id, days_before) VALUES (?, ?, ?)',
                        (uid, oid, d))
                db.commit()
                total_sent += len(benefits_due)
            except Exception as e:
                app.logger.error(f'Failed to send reminder email to user {uid} ({recipient}): {e}')
                db.rollback()

    db.close()
    return total_sent


# ── Scheduler ──────────────────────────────────────────────────────────────────

_scheduler = None


DEFAULT_TZ = 'America/Chicago'


def valid_tz(tz, default=DEFAULT_TZ):
    """Return tz if it's a valid IANA zone, else default. APScheduler accepts
    the zone as a string and resolves it itself."""
    try:
        ZoneInfo(tz)
        return tz
    except Exception:
        return default


def _reschedule_reminder(hour, tz=DEFAULT_TZ):
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.reschedule_job('daily_reminder', trigger='cron',
                                  hour=hour, minute=0, timezone=valid_tz(tz))
    except Exception:
        pass


def start_scheduler(hour=8, tz=DEFAULT_TZ):
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    # Pass the timezone as a string so APScheduler resolves it via pytz (it
    # rejects bare zoneinfo tzinfos). reminder_hour is interpreted in this zone.
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_run_reminder_check, 'cron', id='daily_reminder',
                       hour=hour, minute=0, timezone=valid_tz(tz),
                       misfire_grace_time=3600)
    _scheduler.start()


# ── Jinja helpers ──────────────────────────────────────────────────────────────

def _jinja_today():
    """Timezone-aware 'today' for templates (e.g. default date pickers), so it
    matches the rest of the period math rather than the server's UTC clock."""
    db = get_db()
    try:
        return today_in_tz(db)
    finally:
        db.close()


app.jinja_env.globals['today'] = _jinja_today
app.jinja_env.globals['period_labels'] = PERIOD_LABELS


# ── Scheduler startup ──────────────────────────────────────────────────────────
# Start here so the scheduler runs under gunicorn too (not just __main__).
# Skip in the Werkzeug reloader parent process to avoid double-starting.
if os.environ.get('WERKZEUG_RUN_MAIN') != 'false_sentinel':  # always true, just a hook point
    _is_werkzeug_parent = (app.debug and os.environ.get('WERKZEUG_RUN_MAIN') != 'true')
    if not _is_werkzeug_parent:
        _db = get_db()
        _hour = _safe_hour(get_setting(_db, 'reminder_hour', '8'))
        _tz   = get_setting(_db, 'reminder_tz', DEFAULT_TZ)
        _db.close()
        start_scheduler(_hour, _tz)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    # Manual one-time migration entrypoint (run over SSH after deploy):
    #   python app.py migrate-links
    if len(sys.argv) > 1 and sys.argv[1] == 'migrate-links':
        _db = get_db()
        try:
            print(_migrate_card_shares_to_account_links(_db))
        finally:
            _db.close()
        sys.exit(0)
    # The local dev server is plain http, so a Secure-only session cookie would
    # never round-trip. Production runs via gunicorn (app:app) and never reaches
    # this block, so it keeps Secure cookies.
    app.config['SESSION_COOKIE_SECURE'] = False
    app.run(debug=True, host='0.0.0.0', port=5001)
