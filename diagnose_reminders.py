"""Read-only diagnostic: explain, per user/card/benefit, exactly why the daily
reminder check would or would not have emailed it. No emails are sent, nothing
is written. Run on the box that owns the live benefits.db:

    python diagnose_reminders.py

Mirrors the eligibility logic in app._run_reminder_check (force=False):
a benefit is emailed only when it is NOT fully used, its days_left equals one
of its configured reminder days exactly, and that (period_start, days_before)
is not already in sent_reminders.
"""
import os
import sqlite3

from periods import get_current_period, days_left

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'benefits.db')


def connect():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def share_pool(db, user_card_id):
    """Replicate effective_user_card_ids: pool redemptions across the share group."""
    row = db.execute('SELECT share_group_id FROM user_cards WHERE id = ?', (user_card_id,)).fetchone()
    if not row or row['share_group_id'] is None:
        return [user_card_id]
    ids = [r['id'] for r in db.execute(
        'SELECT id FROM user_cards WHERE share_group_id = ?', (row['share_group_id'],)).fetchall()]
    return ids or [user_card_id]


def main():
    db = connect()
    users = db.execute('''
        SELECT id, email
        FROM users ORDER BY id
    ''').fetchall()

    for u in users:
        recipient = u['email']
        print(f"\n=== user {u['id']}  {u['email']}  ->  {recipient} ===")

        cards = db.execute('''
            SELECT uc.id AS uc_id, uc.active AS uc_active, uc.nickname,
                   c.id AS card_id, c.name AS card_name, c.active AS card_active
            FROM user_cards uc JOIN cards c ON c.id = uc.card_id
            WHERE uc.user_id = ? ORDER BY c.name, uc.id
        ''', (u['id'],)).fetchall()

        for c in cards:
            label = c['nickname'] or c['card_name']
            tags = []
            if not c['uc_active']:
                tags.append('user_card INACTIVE')
            if not c['card_active']:
                tags.append('card template INACTIVE')
            print(f"\n  --- {label}  (user_card {c['uc_id']}, card {c['card_id']})"
                  + (f"  [{', '.join(tags)}]" if tags else '') + " ---")

            benefits = db.execute('''
                SELECT b.id, b.name, b.period_type, b.credit_amount, b.active AS b_active,
                       COALESCE(ub.active, 1) AS ub_active
                FROM benefits b
                LEFT JOIN user_benefits ub ON ub.benefit_id = b.id AND ub.user_card_id = ?
                WHERE b.card_id = ? ORDER BY b.id
            ''', (c['uc_id'], c['card_id'])).fetchall()

            pool = share_pool(db, c['uc_id'])
            ph = ','.join('?' * len(pool))

            for b in benefits:
                ps, pe = get_current_period(b['period_type'])
                dl = days_left(pe)
                rdays = [r['days_before'] for r in db.execute(
                    'SELECT days_before FROM reminders WHERE user_card_id = ? AND benefit_id = ? '
                    'ORDER BY days_before DESC', (c['uc_id'], b['id'])).fetchall()]

                used = db.execute(
                    f'SELECT COALESCE(SUM(amount),0) t, COUNT(*) n FROM redemptions '
                    f'WHERE user_card_id IN ({ph}) AND benefit_id = ? AND period_start = ?',
                    (*pool, b['id'], str(ps))).fetchone()
                if b['credit_amount']:
                    fully_used = (b['credit_amount'] - used['t']) <= 0
                else:
                    fully_used = used['n'] > 0

                match_day = next((d for d in rdays if dl == d), None)
                sent = None
                if match_day is not None:
                    sent = db.execute(
                        'SELECT sent_at FROM sent_reminders WHERE user_card_id=? AND benefit_id=? '
                        'AND period_start=? AND days_before=?',
                        (c['uc_id'], b['id'], str(ps), match_day)).fetchone()

                if not b['b_active']:
                    verdict = 'SKIP: benefit inactive'
                elif not b['ub_active']:
                    verdict = 'SKIP: turned off on this card (user_benefits.active=0)'
                elif not c['uc_active'] or not c['card_active']:
                    verdict = 'SKIP: card inactive'
                elif fully_used:
                    verdict = 'SKIP: fully used this period'
                elif not rdays:
                    verdict = 'SKIP: no reminder days configured'
                elif match_day is None:
                    verdict = f'no-op today: days_left={dl} matches none of {rdays}'
                elif sent:
                    verdict = f'SKIP: already sent for this period (day {match_day} at {sent["sent_at"]})'
                else:
                    verdict = f'WOULD EMAIL today (days_left={dl} == reminder day {match_day})'

                print(f"    b{b['id']:<3} {b['name'][:34]:<34} dl={dl:<4} "
                      f"rdays={rdays!s:<14} used={'Y' if fully_used else 'n'}  {verdict}")

    db.close()


if __name__ == '__main__':
    main()
