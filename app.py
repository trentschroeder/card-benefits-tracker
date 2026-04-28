import os
import sqlite3
from datetime import date, timedelta

from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash

from periods import get_current_period, days_left, PERIOD_LABELS
from email_sender import send_reminder_email, send_summary_email

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


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.path))
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
        db.execute('ALTER TABLE cards ADD COLUMN owner_email TEXT')
        db.commit()
    except Exception:
        pass
    try:
        db.execute('ALTER TABLE cards DROP COLUMN last_four')
        db.commit()
    except Exception:
        pass
    db.close()


init_db()


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_setting(db, key, default=None):
    row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(db, key, value):
    db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))


def enrich_benefit(db, benefit):
    """Add period info and usage totals to a benefit row dict."""
    b = dict(benefit)
    period_start, period_end = get_current_period(b['period_type'])
    b['period_start'] = period_start
    b['period_end']   = period_end
    b['days_left']    = days_left(period_end)
    b['period_label'] = PERIOD_LABELS[b['period_type']]

    if b.get('is_subscription'):
        b['amount_used'] = b['credit_amount'] or 0
        b['remaining']   = 0
        b['pct_used']    = 100
        b['fully_used']  = True
    else:
        rows = db.execute(
            'SELECT COALESCE(SUM(amount), 0) AS total FROM redemptions WHERE benefit_id = ? AND period_start = ?',
            (b['id'], str(period_start))
        ).fetchone()
        b['amount_used'] = rows['total']

        if b['credit_amount']:
            b['remaining'] = max(0.0, b['credit_amount'] - b['amount_used'])
            b['pct_used']  = min(100, int((b['amount_used'] / b['credit_amount']) * 100))
            b['fully_used'] = b['remaining'] <= 0
        else:
            count = db.execute(
                'SELECT COUNT(*) FROM redemptions WHERE benefit_id = ? AND period_start = ?',
                (b['id'], str(period_start))
            ).fetchone()[0]
            b['remaining']  = 0 if count > 0 else 1
            b['pct_used']   = 100 if count > 0 else 0
            b['fully_used'] = count > 0

    reminders = db.execute(
        'SELECT days_before FROM reminders WHERE benefit_id = ? ORDER BY days_before DESC',
        (b['id'],)
    ).fetchall()
    b['reminder_days'] = [r['days_before'] for r in reminders]

    return b


_PERIODS_PER_YEAR = {'monthly': 12, 'quarterly': 4, 'semi-annual': 2, 'annual': 1}


def _periods_elapsed_this_year(period_type):
    today = date.today()
    if period_type == 'monthly':
        return today.month
    elif period_type == 'quarterly':
        return (today.month - 1) // 3 + 1
    elif period_type == 'semi-annual':
        return (today.month - 1) // 6 + 1
    else:
        return 1


def compute_card_roi(db, enriched_benefits):
    """Return (captured, max_possible) for a card's benefits this calendar year."""
    year = str(date.today().year)
    captured = 0.0
    max_possible = 0.0
    for b in enriched_benefits:
        if not b.get('credit_amount'):
            continue
        ca = b['credit_amount']
        ppy = _PERIODS_PER_YEAR.get(b['period_type'], 1)
        max_possible += ca * ppy
        if b.get('is_subscription'):
            captured += ca * _periods_elapsed_this_year(b['period_type'])
        else:
            row = db.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM redemptions "
                "WHERE benefit_id = ? AND strftime('%Y', period_start) = ?",
                (b['id'], year)
            ).fetchone()
            captured += row['total']
    return captured, max_possible


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if os.path.exists(CREDS_FILE):
            with open(CREDS_FILE) as f:
                stored_user, stored_hash = f.read().strip().split(':', 1)
            if username == stored_user and check_password_hash(stored_hash, password):
                session['logged_in'] = True
                return redirect(request.args.get('next') or url_for('dashboard'))
        error = 'Invalid username or password.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    db = get_db()
    cards = db.execute('SELECT * FROM cards WHERE active = 1 ORDER BY name').fetchall()

    dashboard_cards = []
    total_benefits = 0
    total_used = 0

    for card in cards:
        raw_benefits = db.execute(
            'SELECT * FROM benefits WHERE card_id = ? AND active = 1 ORDER BY name',
            (card['id'],)
        ).fetchall()
        enriched = [enrich_benefit(db, b) for b in raw_benefits]
        enriched.sort(key=lambda b: (1 if b['fully_used'] else 0, b['days_left']))
        total_benefits += len(enriched)
        total_used += sum(1 for b in enriched if b['fully_used'])

        captured, max_possible = compute_card_roi(db, enriched)
        annual_fee = card['annual_fee'] or 0
        roi = {
            'captured':      captured,
            'max_possible':  max_possible,
            'fee_pct':       min(100, int(captured / annual_fee * 100)) if annual_fee > 0 else None,
            'max_pct':       min(100, int(captured / max_possible * 100)) if max_possible > 0 else 0,
            'fee_tick_pct':  min(100, int(annual_fee / max_possible * 100)) if (annual_fee > 0 and max_possible > 0) else None,
        }
        dashboard_cards.append({'card': dict(card), 'benefits': enriched, 'roi': roi})

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

@app.route('/cards')
@login_required
def cards_list():
    db = get_db()
    cards = db.execute('''
        SELECT c.*, COUNT(b.id) AS benefit_count
        FROM cards c
        LEFT JOIN benefits b ON b.card_id = c.id AND b.active = 1
        GROUP BY c.id
        ORDER BY c.active DESC, c.name
    ''').fetchall()
    db.close()
    return render_template('cards/list.html', cards=cards)


@app.route('/cards/new', methods=['GET', 'POST'])
@login_required
def card_new():
    if request.method == 'POST':
        name        = request.form.get('name', '').strip()
        owner_email = request.form.get('owner_email', '').strip() or None
        annual_fee  = request.form.get('annual_fee', '').strip() or None
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
            'INSERT INTO cards (name, annual_fee, owner_email) VALUES (?, ?, ?)',
            (name, annual_fee, owner_email))
        cid = cur.lastrowid
        db.commit()
        db.close()
        flash(f'Card "{name}" added.', 'success')
        return redirect(url_for('card_detail', id=cid))
    return render_template('cards/form.html', form={})


@app.route('/cards/<int:id>', methods=['GET', 'POST'])
@login_required
def card_detail(id):
    db   = get_db()
    card = db.execute('SELECT * FROM cards WHERE id = ?', (id,)).fetchone()
    if not card:
        db.close()
        flash('Card not found.', 'danger')
        return redirect(url_for('cards_list'))

    if request.method == 'POST':
        name        = request.form.get('name', '').strip()
        active      = 1 if request.form.get('active') else 0
        owner_email = request.form.get('owner_email', '').strip() or None
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
                'UPDATE cards SET name=?, active=?, annual_fee=?, owner_email=? WHERE id=?',
                (name, active, annual_fee, owner_email, id))
            db.commit()
            flash('Card updated.', 'success')
        db.close()
        return redirect(url_for('card_detail', id=id))

    raw_benefits = db.execute(
        'SELECT * FROM benefits WHERE card_id = ? ORDER BY active DESC, name',
        (id,)
    ).fetchall()
    benefits = [enrich_benefit(db, b) for b in raw_benefits]
    db.close()
    return render_template('cards/detail.html', card=card, benefits=benefits)


@app.route('/cards/<int:id>/delete', methods=['POST'])
@login_required
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
@login_required
def benefit_new(card_id):
    db   = get_db()
    card = db.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
    if not card:
        db.close()
        flash('Card not found.', 'danger')
        return redirect(url_for('cards_list'))

    if request.method == 'POST':
        name            = request.form.get('name', '').strip()
        description     = request.form.get('description', '').strip() or None
        credit_amount   = request.form.get('credit_amount', '').strip() or None
        period_type     = request.form.get('period_type', 'monthly')
        is_subscription = 1 if request.form.get('is_subscription') else 0
        reminder_days   = request.form.getlist('reminder_days')

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
            'INSERT INTO benefits (card_id, name, description, credit_amount, period_type, is_subscription) VALUES (?, ?, ?, ?, ?, ?)',
            (card_id, name, description, credit_amount, period_type, is_subscription))
        bid = cur.lastrowid

        for d in reminder_days:
            try:
                db.execute('INSERT OR IGNORE INTO reminders (benefit_id, days_before) VALUES (?, ?)',
                           (bid, int(d)))
            except ValueError:
                pass

        # Handle custom reminder day
        custom_day = request.form.get('custom_reminder_day', '').strip()
        if custom_day:
            try:
                db.execute('INSERT OR IGNORE INTO reminders (benefit_id, days_before) VALUES (?, ?)',
                           (bid, int(custom_day)))
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
    b   = db.execute('SELECT * FROM benefits WHERE id = ?', (id,)).fetchone()
    if not b:
        db.close()
        flash('Benefit not found.', 'danger')
        return redirect(url_for('dashboard'))
    card = db.execute('SELECT * FROM cards WHERE id = ?', (b['card_id'],)).fetchone()

    if request.method == 'POST':
        name            = request.form.get('name', '').strip()
        description     = request.form.get('description', '').strip() or None
        credit_amount   = request.form.get('credit_amount', '').strip() or None
        period_type     = request.form.get('period_type', 'monthly')
        is_subscription = 1 if request.form.get('is_subscription') else 0
        active          = 1 if request.form.get('active') else 0
        reminder_days   = request.form.getlist('reminder_days')

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
            'UPDATE benefits SET name=?, description=?, credit_amount=?, period_type=?, is_subscription=?, active=? WHERE id=?',
            (name, description, credit_amount, period_type, is_subscription, active, id))

        db.execute('DELETE FROM reminders WHERE benefit_id = ?', (id,))
        for d in reminder_days:
            try:
                db.execute('INSERT OR IGNORE INTO reminders (benefit_id, days_before) VALUES (?, ?)',
                           (id, int(d)))
            except ValueError:
                pass

        custom_day = request.form.get('custom_reminder_day', '').strip()
        if custom_day:
            try:
                db.execute('INSERT OR IGNORE INTO reminders (benefit_id, days_before) VALUES (?, ?)',
                           (id, int(custom_day)))
            except ValueError:
                pass

        db.commit()
        card_id = b['card_id']
        db.close()
        flash(f'Benefit "{name}" updated.', 'success')
        next_url = request.form.get('_next') or url_for('card_detail', id=card_id)
        return redirect(next_url)

    existing_days = [r['days_before'] for r in
                     db.execute('SELECT days_before FROM reminders WHERE benefit_id = ?', (id,)).fetchall()]
    db.close()
    return render_template('benefits/form.html', card=card, form=dict(b),
                           benefit=b, existing_reminder_days=existing_days,
                           period_labels=PERIOD_LABELS)


@app.route('/benefits/<int:id>/delete', methods=['POST'])
@login_required
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
    db = get_db()
    b  = db.execute('SELECT * FROM benefits WHERE id = ?', (id,)).fetchone()
    if not b:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    redemption_date = None
    date_str = request.form.get('redemption_date', '').strip()
    if date_str:
        try:
            redemption_date = date.fromisoformat(date_str)
        except ValueError:
            pass

    period_start, _ = get_current_period(b['period_type'], for_date=redemption_date)

    amount = request.form.get('amount', '').strip() or None
    notes  = request.form.get('notes', '').strip() or None
    if amount:
        try:
            amount = float(amount)
        except ValueError:
            amount = None

    db.execute(
        'INSERT INTO redemptions (benefit_id, period_start, amount, notes) VALUES (?, ?, ?, ?)',
        (id, str(period_start), amount, notes))
    db.commit()
    db.close()
    flash('Redemption recorded.', 'success')
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/redemptions/<int:id>/delete', methods=['POST'])
@login_required
def redemption_delete(id):
    db  = get_db()
    row = db.execute('SELECT benefit_id FROM redemptions WHERE id = ?', (id,)).fetchone()
    if row:
        db.execute('DELETE FROM redemptions WHERE id = ?', (id,))
        db.commit()
        flash('Redemption removed.', 'success')
        bid = row['benefit_id']
        db.close()
        return redirect(request.referrer or url_for('dashboard'))
    db.close()
    return redirect(url_for('dashboard'))


@app.route('/benefits/<int:id>/redemptions')
@login_required
def benefit_redemptions(id):
    db = get_db()
    b  = db.execute('SELECT b.*, c.name AS card_name FROM benefits b JOIN cards c ON c.id = b.card_id WHERE b.id = ?', (id,)).fetchone()
    if not b:
        db.close()
        flash('Benefit not found.', 'danger')
        return redirect(url_for('dashboard'))
    enriched = enrich_benefit(db, b)

    # Build last-year period history
    _n_map = {'monthly': 12, 'quarterly': 4, 'semi-annual': 2, 'annual': 1}
    period_history = []
    period_states  = {}
    check_date = date.today()
    for _ in range(_n_map.get(enriched['period_type'], 1)):
        p_start, p_end = get_current_period(enriched['period_type'], for_date=check_date)
        if enriched['is_subscription']:
            state       = 'full'
            amount_used = enriched['credit_amount'] or 0.0
        else:
            pr = db.execute(
                'SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS cnt '
                'FROM redemptions WHERE benefit_id=? AND period_start=?',
                (enriched['id'], str(p_start))
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

    # Group all redemptions by period_start
    from collections import defaultdict
    all_redemptions = db.execute(
        'SELECT * FROM redemptions WHERE benefit_id=? ORDER BY period_start DESC, redeemed_at DESC', (id,)
    ).fetchall()
    redemptions_by_period = defaultdict(list)
    for r in all_redemptions:
        redemptions_by_period[r['period_start']].append(r)

    # Older periods (beyond last year) that have redemptions
    oldest_in_range = str(period_history[0]['period_start']) if period_history else None
    has_older = bool(oldest_in_range and db.execute(
        'SELECT 1 FROM redemptions WHERE benefit_id=? AND period_start<? LIMIT 1',
        (id, oldest_in_range)
    ).fetchone())

    show_all = request.args.get('all') == '1'
    older_periods = []
    if show_all and oldest_in_range:
        old_starts = db.execute(
            'SELECT DISTINCT period_start FROM redemptions WHERE benefit_id=? AND period_start<? ORDER BY period_start DESC',
            (id, oldest_in_range)
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

@app.route('/settings', methods=['GET', 'POST'])
@login_required
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
    db = get_db()
    gmail_user     = get_setting(db, 'gmail_user')
    gmail_pass     = get_setting(db, 'gmail_app_password')
    test_recipient = request.form.get('test_recipient', '').strip() or None

    if not all([gmail_user, gmail_pass]):
        flash('Gmail credentials are not configured. Fill in Settings first.', 'danger')
        db.close()
        return redirect(url_for('settings'))

    cards = db.execute('SELECT * FROM cards WHERE active = 1 ORDER BY name').fetchall()

    def _pending_for_card(card):
        raw = db.execute(
            'SELECT * FROM benefits WHERE card_id = ? AND active = 1', (card['id'],)
        ).fetchall()
        enriched = [enrich_benefit(db, b) for b in raw]
        pending = [b for b in enriched if not b['fully_used'] and not b['is_subscription']]
        pending.sort(key=lambda b: b['days_left'])
        return pending

    if test_recipient:
        # Combine all cards into one email to the override address
        cards_data = []
        for card in cards:
            pending = _pending_for_card(card)
            if pending:
                cards_data.append({'card_name': card['name'], 'benefits': pending})
        db.close()
        if not cards_data:
            flash('Nothing to send — all benefits are fully used or set to auto.', 'info')
            return redirect(url_for('settings'))
        try:
            send_summary_email(gmail_user, gmail_pass, test_recipient, cards_data)
            total = sum(len(c['benefits']) for c in cards_data)
            flash(f'Test summary sent to {test_recipient} — {total} benefit(s) across {len(cards_data)} card(s).', 'success')
        except Exception as e:
            flash(f'Failed to send email: {e}', 'danger')
    else:
        # Route each card to its owner email; skip cards with no address set
        by_recipient = {}
        skipped = 0
        for card in cards:
            if not card['owner_email']:
                skipped += 1
                continue
            pending = _pending_for_card(card)
            if pending:
                by_recipient.setdefault(card['owner_email'], []).append(
                    {'card_name': card['name'], 'benefits': pending}
                )
        db.close()
        if not by_recipient:
            msg = 'Nothing to send — all benefits are handled.'
            if skipped:
                msg += f' ({skipped} card(s) have no owner email set.)'
            flash(msg, 'info')
            return redirect(url_for('settings'))
        errors = []
        sent_msgs = []
        for to, cards_data in by_recipient.items():
            try:
                send_summary_email(gmail_user, gmail_pass, to, cards_data)
                total = sum(len(c['benefits']) for c in cards_data)
                sent_msgs.append(f'{to} ({total} benefit(s))')
            except Exception as e:
                errors.append(f'{to}: {e}')
        if sent_msgs:
            suffix = f' — {skipped} card(s) skipped (no owner email).' if skipped else '.'
            flash(f'Summary sent — {"; ".join(sent_msgs)}{suffix}', 'success')
        for err in errors:
            flash(f'Failed to send to {err}', 'danger')

    return redirect(url_for('settings'))


# ── Reminder logic ─────────────────────────────────────────────────────────────

def _run_reminder_check(force=False):
    """
    Check all active benefits. For each, if there is remaining credit
    and today is N days before period_end (matching a configured reminder),
    send an email. Returns count of benefits included in the email.
    force=True bypasses the "already sent" dedup check.
    """
    db = get_db()
    gmail_user = get_setting(db, 'gmail_user')
    gmail_pass = get_setting(db, 'gmail_app_password')

    if not all([gmail_user, gmail_pass]):
        db.close()
        return 0

    today = date.today()
    benefits_due = []

    raw = db.execute('''
        SELECT b.*, c.name AS card_name, c.owner_email
        FROM benefits b
        JOIN cards c ON c.id = b.card_id
        WHERE b.active = 1 AND c.active = 1
    ''').fetchall()

    for row in raw:
        b = enrich_benefit(db, row)
        if b['fully_used']:
            continue

        period_start_str = str(b['period_start'])
        dl = b['days_left']

        for days_before in b['reminder_days']:
            if dl == days_before or (force and dl <= days_before):
                already_sent = db.execute(
                    'SELECT 1 FROM sent_reminders WHERE benefit_id=? AND period_start=? AND days_before=?',
                    (b['id'], period_start_str, days_before)
                ).fetchone()

                if not already_sent or force:
                    card_recipient = row['owner_email']
                    if not card_recipient:
                        break  # no address configured for this card — skip
                    benefits_due.append({
                        'to':            card_recipient,
                        'card_name':     row['card_name'],
                        'benefit_name':  b['name'],
                        'credit_amount': b['credit_amount'],
                        'amount_used':   b['amount_used'],
                        'period_end':    b['period_end'].strftime('%b %d, %Y'),
                        'days_left':     dl,
                    })
                    if not already_sent:
                        db.execute(
                            'INSERT OR IGNORE INTO sent_reminders (benefit_id, period_start, days_before) VALUES (?, ?, ?)',
                            (b['id'], period_start_str, days_before))
                    break  # only include a benefit once per email

    if benefits_due:
        from collections import defaultdict
        by_recipient = defaultdict(list)
        for bd in benefits_due:
            to = bd.pop('to')
            by_recipient[to].append(bd)
        try:
            for to, items in by_recipient.items():
                send_reminder_email(gmail_user, gmail_pass, to, items)
            db.commit()
        except Exception as e:
            app.logger.error(f'Failed to send reminder email: {e}')
            db.rollback()
            benefits_due = []

    db.close()
    return len(benefits_due)


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
