"""Seed Kevin's and Trent's American Express Platinum cards with all known recurring benefits."""
import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'benefits.db')

BENEFITS = [
    # (name, description, credit_amount, period_type, reminder_days)
    ('Uber Cash',
     '$15/month toward Uber rides & Eats in the U.S. ($35 in December)',
     15.00, 'monthly', [3]),

    ('Digital Entertainment Credit',
     'Disney+, Hulu, ESPN+, Paramount+, Peacock, NYT, WSJ, YouTube Premium/TV — requires enrollment',
     25.00, 'monthly', [3]),

    ('Lululemon Credit',
     'U.S. Lululemon retail stores (excl. outlets) and online — requires enrollment',
     75.00, 'quarterly', [7]),

    ('Resy Dining Credit',
     'U.S. Resy-affiliated restaurants — requires enrollment',
     100.00, 'quarterly', [7]),

    ('Fine Hotels + Resorts / Hotel Collection Credit',
     'Bookings through Amex Travel at Fine Hotels + Resorts or The Hotel Collection',
     300.00, 'semi-annual', [14, 7]),

    ('Saks Fifth Avenue Credit',
     'Saks U.S. stores or online (excl. gift cards) — NOTE: benefit ends July 1, 2026',
     50.00, 'semi-annual', [14, 7]),

    ('Airline Incidental Fees Credit',
     'Checked bags, seat fees, lounge day passes, in-flight food/drinks on selected airline — select airline by Jan 31',
     200.00, 'annual', [30, 14]),

    ('Walmart+ Credit',
     'Full reimbursement of Walmart+ annual membership auto-renewal — requires enrollment',
     155.00, 'annual', [14]),

    ('Equinox / Equinox+ Credit',
     'Equinox gym membership or Equinox+ app subscription — requires enrollment',
     300.00, 'annual', [30, 14]),

    ('CLEAR+ Membership Credit',
     'CLEAR Plus biometric airport security membership at 50+ airports — requires enrollment',
     209.00, 'annual', [30, 14]),

    ('Oura Ring Credit',
     'Oura Ring hardware purchase only (not subscription fees) — shared credit, not per authorized user',
     200.00, 'annual', [30]),
]

CARDS = ['Kevin', 'Trent']

db = sqlite3.connect(DATABASE)
db.row_factory = sqlite3.Row
db.execute('PRAGMA foreign_keys = ON')

for name_prefix in CARDS:
    card_name = f"{name_prefix}'s Amex Platinum"

    existing = db.execute('SELECT id FROM cards WHERE name = ?', (card_name,)).fetchone()
    if existing:
        print(f'Card "{card_name}" already exists, skipping.')
        continue

    cur = db.execute(
        'INSERT INTO cards (name) VALUES (?)',
        (card_name,))
    card_id = cur.lastrowid
    print(f'Created card: {card_name} (id={card_id})')

    for name, description, credit_amount, period_type, reminder_days in BENEFITS:
        cur2 = db.execute(
            'INSERT INTO benefits (card_id, name, description, credit_amount, period_type) VALUES (?, ?, ?, ?, ?)',
            (card_id, name, description, credit_amount, period_type))
        benefit_id = cur2.lastrowid
        for d in reminder_days:
            db.execute('INSERT OR IGNORE INTO reminders (benefit_id, days_before) VALUES (?, ?)',
                       (benefit_id, d))
        print(f'  + {name} ({period_type}, ${credit_amount})')

db.commit()
db.close()
print('\nDone.')
