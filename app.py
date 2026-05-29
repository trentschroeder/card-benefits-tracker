import hashlib
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone

from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from periods import get_current_period, days_left, PERIOD_LABELS
from email_sender import (send_reminder_email, send_summary_email,
                           send_invite_email, send_reset_email,
                           send_share_invite_email)

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
    _migrate_subscriptions_to_redemptions(db)
    _drop_last_subscription_period_if_exists(db)
    _migrate_credentials_file_to_users(db)
    _migrate_scope_data_to_users(db)
    _ensure_user_scoped_indexes(db)
    db.close()


def _ensure_user_scoped_indexes(db):
    """Create indexes that reference user_id. Must run AFTER the Phase 2
    migration has added the user_id column on existing dbs."""
    db.execute('CREATE INDEX IF NOT EXISTS idx_redemptions_lookup ON redemptions(user_id, benefit_id, period_start)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_reminders_benefit  ON reminders(user_id, benefit_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_sent_reminders     ON sent_reminders(user_id, benefit_id, period_start)')
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
    if 'user_id' in cols:
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
    if 'user_id' in cols:
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
    if 'user_id' in cols:
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


def effective_user_ids_for_card(db, card_id, viewer_id):
    """Return the list of user_ids whose redemptions pool together for this card,
    from the viewer's perspective. Solo card → [viewer_id]. Shared card →
    every share group member's user_id.

    The viewer is expected to have a user_cards row for the card; if they
    don't (e.g., admin viewing a benefit they don't personally own), the
    function falls back to [viewer_id] so existing solo-scoped behavior holds.
    """
    row = db.execute(
        'SELECT share_group_id FROM user_cards WHERE user_id = ? AND card_id = ?',
        (viewer_id, card_id)
    ).fetchone()
    if not row or row['share_group_id'] is None:
        return [viewer_id]
    members = [r['user_id'] for r in db.execute(
        'SELECT user_id FROM card_share_members WHERE group_id = ?',
        (row['share_group_id'],)
    ).fetchall()]
    return members or [viewer_id]


def enrich_benefit(db, benefit, user_id):
    """Add period info and usage totals to a benefit row dict, scoped to one
    user. Redemption sums pool across share-group members when the card is
    shared; reminder days remain strictly per-user."""
    b = dict(benefit)
    period_start, period_end = get_current_period(b['period_type'])
    b['period_start'] = period_start
    b['period_end']   = period_end
    b['days_left']    = days_left(period_end)
    b['period_label'] = PERIOD_LABELS[b['period_type']]

    pool = effective_user_ids_for_card(db, b['card_id'], user_id)
    placeholders = ','.join('?' * len(pool))

    rows = db.execute(
        f'SELECT COALESCE(SUM(amount), 0) AS total FROM redemptions '
        f'WHERE user_id IN ({placeholders}) AND benefit_id = ? AND period_start = ?',
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
            f'WHERE user_id IN ({placeholders}) AND benefit_id = ? AND period_start = ?',
            (*pool, b['id'], str(period_start))
        ).fetchone()[0]
        b['remaining']  = 0 if count > 0 else 1
        b['pct_used']   = 100 if count > 0 else 0
        b['fully_used'] = count > 0

    reminders = db.execute(
        'SELECT days_before FROM reminders WHERE user_id = ? AND benefit_id = ? ORDER BY days_before DESC',
        (user_id, b['id'])
    ).fetchall()
    b['reminder_days'] = [r['days_before'] for r in reminders]

    return b


_PERIODS_PER_YEAR = {'monthly': 12, 'quarterly': 4, 'semi-annual': 2, 'annual': 1}


def compute_card_roi(db, enriched_benefits, user_id):
    """Return (captured, max_possible) for a card's benefits this calendar year.
    Captured pools across share-group members when the card is shared."""
    year = str(date.today().year)
    captured = 0.0
    max_possible = 0.0
    for b in enriched_benefits:
        if not b.get('credit_amount'):
            continue
        ca = b['credit_amount']
        ppy = _PERIODS_PER_YEAR.get(b['period_type'], 1)
        max_possible += ca * ppy
        pool = effective_user_ids_for_card(db, b['card_id'], user_id)
        placeholders = ','.join('?' * len(pool))
        row = db.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS total FROM redemptions "
            f"WHERE user_id IN ({placeholders}) AND benefit_id = ? AND strftime('%Y', period_start) = ?",
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
                return redirect(request.args.get('next') or url_for('dashboard'))
        error = 'Invalid email or password.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Invitations / user management ─────────────────────────────────────────────

INVITE_TTL_DAYS  = 7
RESET_TTL_HOURS  = 24


def _now_utc():
    return datetime.now(timezone.utc)


def _hash_invite_token(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _ttl_for_purpose(purpose):
    if purpose in ('reset', 'share'):
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
    cur = db.execute(
        'INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, ?)',
        (email, placeholder_hash, is_admin))
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
    cards = db.execute('''
        SELECT c.* FROM cards c
        JOIN user_cards uc ON uc.card_id = c.id
        WHERE uc.user_id = ? AND uc.active = 1 AND c.active = 1
        ORDER BY c.name
    ''', (uid,)).fetchall()

    dashboard_cards = []
    total_benefits = 0
    total_used = 0

    for card in cards:
        raw_benefits = db.execute('''
            SELECT b.* FROM benefits b
            LEFT JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_id = ?
            WHERE b.card_id = ? AND b.active = 1 AND COALESCE(ub.active, 1) = 1
            ORDER BY b.name
        ''', (uid, card['id'])).fetchall()
        enriched = [enrich_benefit(db, b, uid) for b in raw_benefits]
        enriched.sort(key=lambda b: (1 if b['fully_used'] else 0, b['days_left']))
        total_benefits += len(enriched)
        total_used += sum(1 for b in enriched if b['fully_used'])

        # Split set-aside benefits into two distinct lists: catalog-archived
        # (admin marked inactive) vs. user-not-pursuing (per-user opt-out).
        archived = db.execute(
            'SELECT * FROM benefits WHERE card_id = ? AND active = 0 ORDER BY name',
            (card['id'],)
        ).fetchall()
        not_pursuing = db.execute('''
            SELECT b.* FROM benefits b
            JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_id = ?
            WHERE b.card_id = ? AND b.active = 1 AND ub.active = 0
            ORDER BY b.name
        ''', (uid, card['id'])).fetchall()

        captured, max_possible = compute_card_roi(db, enriched, uid)
        annual_fee = card['annual_fee'] or 0
        roi = {
            'captured':      captured,
            'max_possible':  max_possible,
            'fee_pct':       min(100, int(captured / annual_fee * 100)) if annual_fee > 0 else None,
            'max_pct':       min(100, int(captured / max_possible * 100)) if max_possible > 0 else 0,
            'fee_tick_pct':  min(100, int(annual_fee / max_possible * 100)) if (annual_fee > 0 and max_possible > 0) else None,
        }
        dashboard_cards.append({'card': dict(card), 'benefits': enriched,
                                'archived':     [dict(b) for b in archived],
                                'not_pursuing': [dict(b) for b in not_pursuing],
                                'roi': roi})

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

@app.route('/card-templates')
@login_required
def card_templates():
    db = get_db()
    rows = db.execute('''
        SELECT c.id, c.name, c.annual_fee, c.active,
               COUNT(b.id) AS benefit_count,
               uc.active   AS user_active
        FROM cards c
        LEFT JOIN benefits b   ON b.card_id  = c.id AND b.active = 1
        LEFT JOIN user_cards uc ON uc.card_id = c.id AND uc.user_id = ?
        WHERE c.published = 1 AND c.active = 1
        GROUP BY c.id
        ORDER BY c.name
    ''', (g.user['id'],)).fetchall()
    db.close()
    return render_template('card_templates.html', cards=rows)


@app.route('/card-templates/<int:card_id>/add', methods=['POST'])
@login_required
def card_templates_add(card_id):
    db   = get_db()
    uid  = g.user['id']
    card = db.execute(
        'SELECT id, name FROM cards WHERE id = ? AND published = 1 AND active = 1', (card_id,)
    ).fetchone()
    if not card:
        db.close()
        flash('That card is not available in the templates.', 'danger')
        return redirect(url_for('card_templates'))
    existing = db.execute(
        'SELECT id, active FROM user_cards WHERE user_id = ? AND card_id = ?',
        (uid, card_id)
    ).fetchone()
    if existing:
        db.execute('UPDATE user_cards SET active = 1 WHERE id = ?', (existing['id'],))
    else:
        db.execute(
            'INSERT INTO user_cards (user_id, card_id, active) VALUES (?, ?, 1)',
            (uid, card_id))
    db.commit()
    db.close()
    flash(f'"{card["name"]}" added to your dashboard.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/cards/<int:id>/share', methods=['POST'])
@login_required
def card_share(id):
    """Invite an existing user to pool redemptions on a card the current
    user already has on their dashboard."""
    db        = get_db()
    uid       = g.user['id']
    inviter   = g.user
    raw_email = request.form.get('email', '').strip()
    if not raw_email:
        db.close()
        flash('Enter the email of the user you want to share with.', 'danger')
        return redirect(url_for('card_detail', id=id))

    my_uc = db.execute(
        'SELECT id, share_group_id FROM user_cards WHERE user_id = ? AND card_id = ?',
        (uid, id)).fetchone()
    if not my_uc:
        db.close()
        flash('You can only share a card that is on your own dashboard.', 'danger')
        return redirect(url_for('cards_list'))

    invitee = db.execute('SELECT id, email FROM users WHERE email = ?', (raw_email,)).fetchone()
    if not invitee:
        db.close()
        flash(f'No user found with email {raw_email}. Ask the administrator to invite them first.', 'danger')
        return redirect(url_for('card_detail', id=id))
    if invitee['id'] == uid:
        db.close()
        flash("You can't share a card with yourself.", 'danger')
        return redirect(url_for('card_detail', id=id))

    # If invitee already shares this card with the current user, no-op
    if my_uc['share_group_id'] is not None:
        already = db.execute(
            'SELECT 1 FROM card_share_members WHERE group_id = ? AND user_id = ?',
            (my_uc['share_group_id'], invitee['id'])).fetchone()
        if already:
            db.close()
            flash(f'{invitee["email"]} already shares this card with you.', 'info')
            return redirect(url_for('card_detail', id=id))

    card = db.execute('SELECT id, name FROM cards WHERE id = ?', (id,)).fetchone()
    raw_token = _create_token(db, invitee['id'], purpose='share',
                              inviter_user_id=uid, card_id=id)
    db.commit()

    gmail_user = get_setting(db, 'gmail_user')
    gmail_pass = get_setting(db, 'gmail_app_password')
    if not all([gmail_user, gmail_pass]):
        db.close()
        flash('Share invitation created but the email could not be sent — SMTP is not configured.', 'warning')
        return redirect(url_for('card_detail', id=id))
    accept_url = url_for('accept_share', token=raw_token, _external=True)
    try:
        send_share_invite_email(gmail_user, gmail_pass, invitee['email'],
                                accept_url, inviter['email'], card['name'])
    except Exception as e:
        db.close()
        flash(f'Failed to send share invitation to {invitee["email"]}: {e}', 'danger')
        return redirect(url_for('card_detail', id=id))
    db.close()
    flash(f'Share invitation sent to {invitee["email"]}. Expires in {RESET_TTL_HOURS} hours.', 'success')
    return redirect(url_for('card_detail', id=id))


@app.route('/accept-share/<token>', methods=['GET', 'POST'])
@login_required
def accept_share(token):
    db  = get_db()
    inv = _consume_valid_token(db, token, purpose='share')
    if not inv:
        db.close()
        return render_template('accept_share.html', invalid=True), 410
    # The token's user_id is the invitee. Reject if the wrong user is logged in.
    if inv['user_id'] != g.user['id']:
        db.close()
        return render_template('accept_share.html', invalid=True,
                                wrong_user_msg=True), 403

    # Load context for display + action
    extra = db.execute('''
        SELECT i.card_id, i.inviter_user_id,
               c.name AS card_name,
               u.email AS inviter_email
        FROM invitations i
        JOIN cards c ON c.id = i.card_id
        JOIN users u ON u.id = i.inviter_user_id
        WHERE i.id = ?
    ''', (inv['invite_id'],)).fetchone()
    if not extra:
        db.close()
        return render_template('accept_share.html', invalid=True), 410

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'decline':
            db.execute('UPDATE invitations SET used_at = ? WHERE id = ?',
                       (_now_utc().isoformat(timespec='seconds'), inv['invite_id']))
            db.commit()
            db.close()
            flash(f'Declined share invitation for {extra["card_name"]}.', 'info')
            return redirect(url_for('dashboard'))
        if action != 'accept':
            db.close()
            flash('Unknown action.', 'danger')
            return redirect(url_for('accept_share', token=token))

        card_id     = extra['card_id']
        inviter_id  = extra['inviter_user_id']
        invitee_id  = inv['user_id']

        # Find the inviter's user_cards row — they must have the card.
        inviter_uc = db.execute(
            'SELECT id, share_group_id FROM user_cards WHERE user_id = ? AND card_id = ?',
            (inviter_id, card_id)).fetchone()
        if not inviter_uc:
            db.close()
            flash("The inviter no longer has this card. Ask them to share again.", 'danger')
            return redirect(url_for('dashboard'))

        # Resolve / create the share group
        if inviter_uc['share_group_id'] is not None:
            group_id = inviter_uc['share_group_id']
        else:
            cur = db.execute('INSERT INTO card_share_groups (card_id) VALUES (?)', (card_id,))
            group_id = cur.lastrowid
            db.execute(
                'INSERT OR IGNORE INTO card_share_members (group_id, user_id) VALUES (?, ?)',
                (group_id, inviter_id))
            db.execute(
                'UPDATE user_cards SET share_group_id = ? WHERE id = ?',
                (group_id, inviter_uc['id']))

        # Make sure the invitee has a user_cards row pointing at this group
        invitee_uc = db.execute(
            'SELECT id, share_group_id FROM user_cards WHERE user_id = ? AND card_id = ?',
            (invitee_id, card_id)).fetchone()
        if invitee_uc:
            if invitee_uc['share_group_id'] not in (None, group_id):
                db.close()
                flash("You're already in a different share for this card. Leave that one first, then re-accept.", 'danger')
                return redirect(url_for('dashboard'))
            db.execute(
                'UPDATE user_cards SET share_group_id = ?, active = 1 WHERE id = ?',
                (group_id, invitee_uc['id']))
        else:
            db.execute(
                'INSERT INTO user_cards (user_id, card_id, active, share_group_id) VALUES (?, ?, 1, ?)',
                (invitee_id, card_id, group_id))

        db.execute(
            'INSERT OR IGNORE INTO card_share_members (group_id, user_id) VALUES (?, ?)',
            (group_id, invitee_id))
        db.execute('UPDATE invitations SET used_at = ? WHERE id = ?',
                   (_now_utc().isoformat(timespec='seconds'), inv['invite_id']))
        db.commit()
        db.close()
        flash(f'You now share {extra["card_name"]} with {extra["inviter_email"]}.', 'success')
        return redirect(url_for('dashboard'))

    db.close()
    return render_template('accept_share.html',
                            invalid=False,
                            card_name=extra['card_name'],
                            inviter_email=extra['inviter_email'],
                            token=token)


@app.route('/cards/<int:id>/remove', methods=['POST'])
@login_required
def card_remove(id):
    """Soft-remove the card from the current user's dashboard. Redemption
    history stays in the db so re-adding restores everything."""
    db = get_db()
    row = db.execute(
        'SELECT uc.id, c.name FROM user_cards uc JOIN cards c ON c.id = uc.card_id '
        'WHERE uc.user_id = ? AND uc.card_id = ?',
        (g.user['id'], id)
    ).fetchone()
    if not row:
        db.close()
        flash('Card not found on your dashboard.', 'danger')
        return redirect(url_for('cards_list'))
    db.execute('UPDATE user_cards SET active = 0 WHERE id = ?', (row['id'],))
    db.commit()
    db.close()
    flash(f'"{row["name"]}" removed from your dashboard. Re-add anytime from Card Templates.', 'success')
    return redirect(url_for('cards_list'))


@app.route('/cards')
@login_required
def cards_list():
    db = get_db()
    cards = db.execute('''
        SELECT c.*, COUNT(b.id) AS benefit_count
        FROM cards c
        JOIN user_cards uc ON uc.card_id = c.id AND uc.user_id = ?
        LEFT JOIN benefits b ON b.card_id = c.id AND b.active = 1
        GROUP BY c.id
        ORDER BY c.active DESC, c.name
    ''', (g.user['id'],)).fetchall()
    db.close()
    return render_template('cards/list.html', cards=cards)


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
        db.execute(
            'INSERT INTO user_cards (user_id, card_id, active) VALUES (?, ?, 1)',
            (g.user['id'], cid))
        db.commit()
        db.close()
        flash(f'Card "{name}" added.', 'success')
        return redirect(url_for('card_detail', id=cid))
    return render_template('cards/form.html', form={})


@app.route('/cards/<int:id>', methods=['GET', 'POST'])
@login_required
def card_detail(id):
    db   = get_db()
    card = db.execute('''
        SELECT c.* FROM cards c
        JOIN user_cards uc ON uc.card_id = c.id
        WHERE c.id = ? AND uc.user_id = ?
    ''', (id, g.user['id'])).fetchone()
    if not card:
        db.close()
        flash('Card not found.', 'danger')
        return redirect(url_for('cards_list'))

    if request.method == 'POST':
        if not g.user['is_admin']:
            db.close()
            flash('Admin access required.', 'danger')
            return redirect(url_for('card_detail', id=id))
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
                (name, active, annual_fee, published, id))
            db.commit()
            flash('Card updated.', 'success')
        db.close()
        return redirect(url_for('card_detail', id=id))

    raw_benefits = db.execute('''
        SELECT b.*, COALESCE(ub.active, 1) AS user_active
        FROM benefits b
        LEFT JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_id = ?
        WHERE b.card_id = ?
        ORDER BY b.active DESC, b.name
    ''', (g.user['id'], id)).fetchall()
    benefits = []
    for b in raw_benefits:
        eb = enrich_benefit(db, b, g.user['id'])
        eb['user_active'] = b['user_active']
        benefits.append(eb)
    db.close()
    return render_template('cards/detail.html', card=card, benefits=benefits)


@app.route('/cards/<int:id>/delete', methods=['POST'])
@admin_required
def card_delete(id):
    db  = get_db()
    row = db.execute('SELECT name FROM cards WHERE id = ?', (id,)).fetchone()
    if row:
        db.execute('DELETE FROM cards WHERE id = ?', (id,))
        db.commit()
        flash(f'Card "{row["name"]}" deleted.', 'success')
    db.close()
    return redirect(url_for('cards_list'))


# ── Benefits ───────────────────────────────────────────────────────────────────

@app.route('/cards/<int:card_id>/benefits/new', methods=['GET', 'POST'])
@admin_required
def benefit_new(card_id):
    db   = get_db()
    card = db.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
    if not card:
        db.close()
        flash('Card not found.', 'danger')
        return redirect(url_for('cards_list'))

    if request.method == 'POST':
        name               = request.form.get('name', '').strip()
        description        = request.form.get('description', '').strip() or None
        credit_amount      = request.form.get('credit_amount', '').strip() or None
        period_type        = request.form.get('period_type', 'monthly')
        is_subscription    = 1 if request.form.get('is_subscription') else 0
        reminder_days      = request.form.getlist('reminder_days')

        if not name:
            flash('Name is required.', 'danger')
            db.close()
            return render_template('benefits/form.html', card=card, form=request.form,
                                   period_labels=PERIOD_LABELS)

        if credit_amount:
            try:
                credit_amount = float(credit_amount)
            except ValueError:
                flash('Credit amount must be a number.', 'danger')
                db.close()
                return render_template('benefits/form.html', card=card, form=request.form,
                                       period_labels=PERIOD_LABELS)

        cur = db.execute(
            'INSERT INTO benefits (card_id, name, description, credit_amount, period_type, is_subscription) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (card_id, name, description, credit_amount, period_type, is_subscription))
        bid = cur.lastrowid
        uid = g.user['id']

        for d in reminder_days:
            try:
                db.execute(
                    'INSERT OR IGNORE INTO reminders (user_id, benefit_id, days_before) VALUES (?, ?, ?)',
                    (uid, bid, int(d)))
            except ValueError:
                pass

        # Handle custom reminder day
        custom_day = request.form.get('custom_reminder_day', '').strip()
        if custom_day:
            try:
                db.execute(
                    'INSERT OR IGNORE INTO reminders (user_id, benefit_id, days_before) VALUES (?, ?, ?)',
                    (uid, bid, int(custom_day)))
            except ValueError:
                pass

        db.commit()
        db.close()
        flash(f'Benefit "{name}" added.', 'success')
        return redirect(url_for('card_detail', id=card_id))

    db.close()
    return render_template('benefits/form.html', card=card, form={},
                           period_labels=PERIOD_LABELS)


@app.route('/benefits/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def benefit_edit(id):
    db  = get_db()
    uid = g.user['id']
    # Only let the user touch a benefit on a card they actually have
    b = db.execute('''
        SELECT b.* FROM benefits b
        JOIN user_cards uc ON uc.card_id = b.card_id
        WHERE b.id = ? AND uc.user_id = ?
    ''', (id, uid)).fetchone()
    if not b:
        db.close()
        flash('Benefit not found.', 'danger')
        return redirect(url_for('dashboard'))
    card = db.execute('SELECT * FROM cards WHERE id = ?', (b['card_id'],)).fetchone()
    is_admin = bool(g.user['is_admin'])

    def _save_reminders(reminder_days, custom_day):
        db.execute('DELETE FROM reminders WHERE user_id = ? AND benefit_id = ?', (uid, id))
        for d in reminder_days:
            try:
                db.execute(
                    'INSERT OR IGNORE INTO reminders (user_id, benefit_id, days_before) VALUES (?, ?, ?)',
                    (uid, id, int(d)))
            except ValueError:
                pass
        if custom_day:
            try:
                db.execute(
                    'INSERT OR IGNORE INTO reminders (user_id, benefit_id, days_before) VALUES (?, ?, ?)',
                    (uid, id, int(custom_day)))
            except ValueError:
                pass

    if request.method == 'POST':
        reminder_days = request.form.getlist('reminder_days')
        custom_day    = request.form.get('custom_reminder_day', '').strip()

        if is_admin:
            name            = request.form.get('name', '').strip()
            description     = request.form.get('description', '').strip() or None
            credit_amount   = request.form.get('credit_amount', '').strip() or None
            period_type     = request.form.get('period_type', 'monthly')
            is_subscription = 1 if request.form.get('is_subscription') else 0
            active          = 1 if request.form.get('active') else 0

            if not name:
                flash('Name is required.', 'danger')
                db.close()
                return render_template('benefits/form.html', card=card, form=request.form,
                                       benefit=b, period_labels=PERIOD_LABELS)

            if credit_amount:
                try:
                    credit_amount = float(credit_amount)
                except ValueError:
                    flash('Credit amount must be a number.', 'danger')
                    db.close()
                    return render_template('benefits/form.html', card=card, form=request.form,
                                           benefit=b, period_labels=PERIOD_LABELS)

            db.execute(
                'UPDATE benefits SET name=?, description=?, credit_amount=?, period_type=?, is_subscription=?, '
                'active=? WHERE id=?',
                (name, description, credit_amount, period_type, is_subscription, active, id))

        # "Pursuing" is no longer part of the benefit edit form. It moves to
        # a dedicated per-user toggle on the card detail row (benefit_pursue_toggle).
        _save_reminders(reminder_days, custom_day)
        db.commit()
        card_id      = b['card_id']
        display_name = name if is_admin else b['name']
        db.close()
        flash(f'Benefit "{display_name}" updated.', 'success')
        next_url = request.form.get('_next') or url_for('card_detail', id=card_id)
        return redirect(next_url)

    existing_days = [r['days_before'] for r in db.execute(
        'SELECT days_before FROM reminders WHERE user_id = ? AND benefit_id = ?',
        (uid, id)
    ).fetchall()]
    form_data = dict(b)
    db.close()
    return render_template('benefits/form.html', card=card, form=form_data,
                           benefit=b, existing_reminder_days=existing_days,
                           period_labels=PERIOD_LABELS)


@app.route('/benefits/<int:id>/pursue-toggle', methods=['POST'])
@login_required
def benefit_pursue_toggle(id):
    """Flip the current user's pursuing flag for this benefit. Per-user only;
    has no effect on the catalog or on other users."""
    db  = get_db()
    uid = g.user['id']
    b = db.execute('''
        SELECT b.id, b.name FROM benefits b
        JOIN user_cards uc ON uc.card_id = b.card_id
        WHERE b.id = ? AND uc.user_id = ?
    ''', (id, uid)).fetchone()
    if not b:
        db.close()
        flash('Benefit not found.', 'danger')
        return redirect(url_for('dashboard'))
    row = db.execute(
        'SELECT id, active FROM user_benefits WHERE user_id = ? AND benefit_id = ?',
        (uid, id)).fetchone()
    if row:
        new_state = 0 if row['active'] else 1
        db.execute('UPDATE user_benefits SET active = ? WHERE id = ?',
                   (new_state, row['id']))
    else:
        # No override yet — default state is pursuing, so toggle to not pursuing
        new_state = 0
        db.execute('INSERT INTO user_benefits (user_id, benefit_id, active) VALUES (?, ?, 0)',
                   (uid, id))
    db.commit()
    db.close()
    if new_state:
        flash(f'Resumed pursuing "{b["name"]}".', 'success')
    else:
        flash(f'Marked "{b["name"]}" as not pursuing. Hidden from your dashboard and emails.', 'info')
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/benefits/<int:id>/delete', methods=['POST'])
@admin_required
def benefit_delete(id):
    db  = get_db()
    row = db.execute('SELECT card_id, name FROM benefits WHERE id = ?', (id,)).fetchone()
    if row:
        cid = row['card_id']
        db.execute('DELETE FROM benefits WHERE id = ?', (id,))
        db.commit()
        flash(f'Benefit "{row["name"]}" deleted.', 'success')
        db.close()
        return redirect(url_for('card_detail', id=cid))
    db.close()
    return redirect(url_for('dashboard'))


# ── Redemptions ────────────────────────────────────────────────────────────────

@app.route('/benefits/<int:id>/redeem', methods=['POST'])
@login_required
def benefit_redeem(id):
    db  = get_db()
    uid = g.user['id']
    b   = db.execute('''
        SELECT b.* FROM benefits b
        JOIN user_cards uc ON uc.card_id = b.card_id
        WHERE b.id = ? AND uc.user_id = ?
    ''', (id, uid)).fetchone()
    if not b:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    notes = request.form.get('notes', '').strip() or None

    if b['is_subscription']:
        today = date.today()
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

        pool = effective_user_ids_for_card(db, b['card_id'], uid)
        ph   = ','.join('?' * len(pool))
        cursor = start_date
        count = 0
        while cursor <= end_date:
            p_start, p_end = get_current_period(b['period_type'], for_date=cursor)
            ps_str = str(p_start)
            # On shared cards, a row from ANY pool member covers this period.
            existing = db.execute(
                f'SELECT id FROM redemptions WHERE user_id IN ({ph}) AND benefit_id=? AND period_start=?',
                (*pool, id, ps_str)
            ).fetchone()
            if existing:
                db.execute('UPDATE redemptions SET amount = ? WHERE id = ?',
                           (row_amount, existing['id']))
            else:
                db.execute(
                    'INSERT INTO redemptions (user_id, benefit_id, period_start, amount, notes) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (uid, id, ps_str, row_amount, notes or 'subscription'))
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

    period_start, _ = get_current_period(b['period_type'], for_date=redemption_date)

    amount = request.form.get('amount', '').strip() or None
    if amount:
        try:
            amount = float(amount)
        except ValueError:
            amount = None

    db.execute(
        'INSERT INTO redemptions (user_id, benefit_id, period_start, amount, notes) VALUES (?, ?, ?, ?, ?)',
        (uid, id, str(period_start), amount, notes))
    db.commit()
    db.close()
    flash('Redemption recorded.', 'success')
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/redemptions/<int:id>/edit', methods=['POST'])
@login_required
def redemption_edit(id):
    db  = get_db()
    row = db.execute(
        'SELECT r.*, b.period_type, b.card_id FROM redemptions r '
        'JOIN benefits b ON b.id = r.benefit_id '
        'WHERE r.id = ?',
        (id,)
    ).fetchone()
    if not row:
        db.close()
        return redirect(url_for('dashboard'))
    pool = effective_user_ids_for_card(db, row['card_id'], g.user['id'])
    if row['user_id'] not in pool:
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
    period_start, _ = get_current_period(row['period_type'], for_date=redemption_date)
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
        'SELECT r.user_id, b.card_id FROM redemptions r '
        'JOIN benefits b ON b.id = r.benefit_id WHERE r.id = ?',
        (id,)
    ).fetchone()
    if row:
        pool = effective_user_ids_for_card(db, row['card_id'], g.user['id'])
        if row['user_id'] in pool:
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
    b   = db.execute('''
        SELECT b.*, c.name AS card_name FROM benefits b
        JOIN cards c ON c.id = b.card_id
        JOIN user_cards uc ON uc.card_id = c.id
        WHERE b.id = ? AND uc.user_id = ?
    ''', (id, uid)).fetchone()
    if not b:
        db.close()
        flash('Benefit not found.', 'danger')
        return redirect(url_for('dashboard'))
    enriched = enrich_benefit(db, b, uid)
    pool = effective_user_ids_for_card(db, b['card_id'], uid)
    ph   = ','.join('?' * len(pool))

    # Build last-year period history
    _n_map = {'monthly': 12, 'quarterly': 4, 'semi-annual': 2, 'annual': 1}
    period_history = []
    period_states  = {}
    check_date = date.today()
    for _ in range(_n_map.get(enriched['period_type'], 1)):
        p_start, p_end = get_current_period(enriched['period_type'], for_date=check_date)
        pr = db.execute(
            f'SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt '
            f'FROM redemptions WHERE user_id IN ({ph}) AND benefit_id=? AND period_start=?',
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
        f'SELECT * FROM redemptions WHERE user_id IN ({ph}) AND benefit_id=? '
        f'ORDER BY period_start DESC, redeemed_at DESC',
        (*pool, id)
    ).fetchall()
    redemptions_by_period = defaultdict(list)
    for r in all_redemptions:
        redemptions_by_period[r['period_start']].append(r)

    # Older periods (beyond last year) that have redemptions
    oldest_in_range = str(period_history[0]['period_start']) if period_history else None
    has_older = bool(oldest_in_range and db.execute(
        f'SELECT 1 FROM redemptions WHERE user_id IN ({ph}) AND benefit_id=? AND period_start<? LIMIT 1',
        (*pool, id, oldest_in_range)
    ).fetchone())

    show_all = request.args.get('all') == '1'
    older_periods = []
    if show_all and oldest_in_range:
        old_starts = db.execute(
            f'SELECT DISTINCT period_start FROM redemptions '
            f'WHERE user_id IN ({ph}) AND benefit_id=? AND period_start<? ORDER BY period_start DESC',
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

    db.close()
    return render_template('benefits/redemptions.html', benefit=enriched,
                           period_history=period_history, period_states=period_states,
                           redemptions=list(all_redemptions),
                           older_periods=older_periods, has_older=has_older, show_all=show_all)


# ── Settings ───────────────────────────────────────────────────────────────────

@app.route('/preferences', methods=['GET', 'POST'])
@login_required
def preferences():
    db = get_db()
    if request.method == 'POST':
        notification_email = request.form.get('notification_email', '').strip() or None
        reminders_enabled  = 1 if request.form.get('reminders_enabled') else 0
        summary_enabled    = 1 if request.form.get('summary_enabled') else 0
        db.execute(
            'UPDATE users SET notification_email = ?, reminders_enabled = ?, summary_enabled = ? WHERE id = ?',
            (notification_email, reminders_enabled, summary_enabled, g.user['id']))
        db.commit()
        db.close()
        flash('Preferences saved.', 'success')
        return redirect(url_for('preferences'))
    db.close()
    return render_template('preferences.html')


@app.route('/preferences/password', methods=['POST'])
@login_required
def change_password():
    current = request.form.get('current_password', '')
    new     = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if not check_password_hash(g.user['password_hash'], current):
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('preferences'))
    if len(new) < 8:
        flash('New password must be at least 8 characters.', 'danger')
        return redirect(url_for('preferences'))
    if new != confirm:
        flash('New passwords do not match.', 'danger')
        return redirect(url_for('preferences'))
    db = get_db()
    db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
               (generate_password_hash(new), g.user['id']))
    db.commit()
    db.close()
    flash('Password updated.', 'success')
    return redirect(url_for('preferences'))


@app.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    db = get_db()
    if request.method == 'POST':
        gmail_user     = request.form.get('gmail_user', '').strip()
        gmail_password = request.form.get('gmail_password', '').strip()
        reminder_hour  = request.form.get('reminder_hour', '8').strip()

        if gmail_user:
            set_setting(db, 'gmail_user', gmail_user)
        if gmail_password:
            set_setting(db, 'gmail_app_password', gmail_password)
        if reminder_hour:
            set_setting(db, 'reminder_hour', reminder_hour)

        db.commit()
        flash('Settings saved.', 'success')

        # Reschedule with new hour if scheduler is running
        _reschedule_reminder(int(reminder_hour))

        db.close()
        return redirect(url_for('settings'))

    cfg = {
        'gmail_user':    get_setting(db, 'gmail_user', ''),
        'reminder_hour': get_setting(db, 'reminder_hour', '8'),
    }
    db.close()
    return render_template('settings.html', cfg=cfg)


# ── Summary email ──────────────────────────────────────────────────────────────

@app.route('/email/summary', methods=['POST'])
@login_required
def send_summary():
    """Send a summary email of the current user's cards. Default recipient
    is the user's notification_email (falling back to their login email).
    Admin can override with a test_recipient form field for testing."""
    db = get_db()
    gmail_user = get_setting(db, 'gmail_user')
    gmail_pass = get_setting(db, 'gmail_app_password')

    if not all([gmail_user, gmail_pass]):
        db.close()
        flash('Gmail credentials are not configured. Ask the administrator to set them up.', 'danger')
        return redirect(url_for('preferences'))

    uid       = g.user['id']
    recipient = (g.user['notification_email'] or g.user['email'])
    # Admin can override the destination for testing
    test_recipient = request.form.get('test_recipient', '').strip() or None
    if test_recipient and g.user['is_admin']:
        recipient = test_recipient

    cards = db.execute('''
        SELECT c.* FROM cards c
        JOIN user_cards uc ON uc.card_id = c.id
        WHERE uc.user_id = ? AND uc.active = 1 AND c.active = 1
        ORDER BY c.name
    ''', (uid,)).fetchall()

    cards_data = []
    for card in cards:
        raw = db.execute('''
            SELECT b.* FROM benefits b
            LEFT JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_id = ?
            WHERE b.card_id = ? AND b.active = 1 AND COALESCE(ub.active, 1) = 1
        ''', (uid, card['id'])).fetchall()
        enriched = [enrich_benefit(db, b, uid) for b in raw]
        pending  = [b for b in enriched if not b['fully_used'] and not b['is_subscription']]
        pending.sort(key=lambda b: b['days_left'])
        if pending:
            cards_data.append({'card_name': card['name'], 'benefits': pending})

    db.close()

    back = url_for('settings') if test_recipient and g.user['is_admin'] else url_for('preferences')

    if not cards_data:
        flash('Nothing to send — all benefits are fully used or handled by subscription.', 'info')
        return redirect(back)

    try:
        send_summary_email(gmail_user, gmail_pass, recipient, cards_data)
        total = sum(len(c['benefits']) for c in cards_data)
        label = 'Test summary' if (test_recipient and g.user['is_admin']) else 'Summary'
        flash(f'{label} sent to {recipient} — {total} benefit(s) across {len(cards_data)} card(s).', 'success')
    except Exception as e:
        flash(f'Failed to send email: {e}', 'danger')

    return redirect(back)


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

    users = db.execute('''
        SELECT id, email, notification_email
        FROM users
        WHERE reminders_enabled = 1
    ''').fetchall()

    total_sent = 0

    for user in users:
        uid       = user['id']
        recipient = user['notification_email'] or user['email']
        if not recipient:
            continue

        raw = db.execute('''
            SELECT b.*, c.name AS card_name
            FROM benefits b
            JOIN cards c       ON c.id      = b.card_id
            JOIN user_cards uc ON uc.card_id = c.id
            LEFT JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_id = ?
            WHERE uc.user_id = ? AND uc.active = 1
                  AND b.active = 1 AND c.active = 1
                  AND COALESCE(ub.active, 1) = 1
        ''', (uid, uid)).fetchall()

        benefits_due = []
        for row in raw:
            b = enrich_benefit(db, row, uid)
            if b['fully_used']:
                continue
            period_start_str = str(b['period_start'])
            dl = b['days_left']
            for days_before in b['reminder_days']:
                if dl == days_before or (force and dl <= days_before):
                    already_sent = db.execute(
                        'SELECT 1 FROM sent_reminders WHERE user_id=? AND benefit_id=? AND period_start=? AND days_before=?',
                        (uid, b['id'], period_start_str, days_before)
                    ).fetchone()
                    if not already_sent or force:
                        benefits_due.append({
                            'card_name':     row['card_name'],
                            'benefit_name':  b['name'],
                            'credit_amount': b['credit_amount'],
                            'amount_used':   b['amount_used'],
                            'period_end':    b['period_end'].strftime('%b %d, %Y'),
                            'days_left':     dl,
                        })
                        if not already_sent:
                            db.execute(
                                'INSERT OR IGNORE INTO sent_reminders (user_id, benefit_id, period_start, days_before) '
                                'VALUES (?, ?, ?, ?)',
                                (uid, b['id'], period_start_str, days_before))
                        break  # only include a benefit once per email

        if benefits_due:
            try:
                send_reminder_email(gmail_user, gmail_pass, recipient, benefits_due)
                db.commit()
                total_sent += len(benefits_due)
            except Exception as e:
                app.logger.error(f'Failed to send reminder email to user {uid} ({recipient}): {e}')
                db.rollback()

    db.close()
    return total_sent


# ── Scheduler ──────────────────────────────────────────────────────────────────

_scheduler = None


def _reschedule_reminder(hour):
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.reschedule_job('daily_reminder', trigger='cron', hour=hour, minute=0)
    except Exception:
        pass


def start_scheduler(hour=8):
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_run_reminder_check, 'cron', id='daily_reminder',
                       hour=hour, minute=0, misfire_grace_time=3600)
    _scheduler.start()


# ── Jinja helpers ──────────────────────────────────────────────────────────────

app.jinja_env.globals['today'] = date.today
app.jinja_env.globals['period_labels'] = PERIOD_LABELS


# ── Scheduler startup ──────────────────────────────────────────────────────────
# Start here so the scheduler runs under gunicorn too (not just __main__).
# Skip in the Werkzeug reloader parent process to avoid double-starting.
if os.environ.get('WERKZEUG_RUN_MAIN') != 'false_sentinel':  # always true, just a hook point
    _is_werkzeug_parent = (app.debug and os.environ.get('WERKZEUG_RUN_MAIN') != 'true')
    if not _is_werkzeug_parent:
        _db = get_db()
        _hour = int(get_setting(_db, 'reminder_hour', '8'))
        _db.close()
        start_scheduler(_hour)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
