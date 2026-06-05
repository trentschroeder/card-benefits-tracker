"""Seed the catalog with common credit-card templates and their benefits.

Idempotent: a card is matched by name; if it already exists the whole card
(and its benefits) is skipped, so this is safe to re-run and will never create
duplicates. Only inserts cards that are missing.

Benefit amounts/periods are a reasonable starting point as of mid-2026 and the
issuers change them often — review each card in the Card Templates UI and edit
as needed. Benefits that aren't a fixed dollar credit (rotating-category
activations, anniversary free nights) are stored with a NULL credit_amount.

Run on the box with the project venv:
    ./venv/bin/python seed_cards.py
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'benefits.db')
SCHEMA   = os.path.join(BASE_DIR, 'schema.sql')

# Each card: name, annual_fee, published, [benefits].
# A benefit is: (name, description, credit_amount, period_type, is_subscription)
# period_type in: monthly | quarterly | semi-annual | annual
CARDS = [
    {
        'name': 'Amex Platinum',
        'annual_fee': 695,
        'published': 1,
        'benefits': [
            ('Uber Cash', 'Up to $15/mo in Uber Cash for rides or Eats (extra $20 in December).', 15, 'monthly', 0),
            ('Digital Entertainment Credit', 'Monthly credit for select streaming/digital subscriptions.', 20, 'monthly', 0),
            ('Walmart+ Membership', 'Reimburses a monthly Walmart+ membership.', 12.95, 'monthly', 1),
            ('Saks Fifth Avenue Credit', 'Up to $50 in statement credits at Saks, twice a year.', 50, 'semi-annual', 0),
            ('Airline Fee Credit', 'Up to $200/yr in incidental fees with one selected airline.', 200, 'annual', 0),
            ('Hotel Credit', 'Up to $200/yr on prepaid Fine Hotels + Resorts / The Hotel Collection bookings.', 200, 'annual', 0),
            ('CLEAR Plus Credit', 'Covers an annual CLEAR Plus membership.', 199, 'annual', 0),
            ('Equinox Credit', 'Up to $300/yr toward Equinox memberships.', 300, 'annual', 0),
        ],
    },
    {
        'name': 'Chase Sapphire Reserve',
        'annual_fee': 550,
        'published': 1,
        'benefits': [
            ('Lyft Credit', 'In-app Lyft ride credit each month.', 10, 'monthly', 0),
            ('DoorDash Restaurant Credit', 'Monthly promo credit on restaurant DoorDash orders.', 5, 'monthly', 0),
            ('Annual Travel Credit', 'Up to $300/yr automatically applied to travel purchases.', 300, 'annual', 0),
        ],
    },
    {
        'name': 'Chase Sapphire Preferred',
        'annual_fee': 95,
        'published': 1,
        'benefits': [
            ('DoorDash Grocery Credit', 'Monthly promo credit on grocery/convenience DoorDash orders.', 10, 'monthly', 0),
            ('Annual Hotel Credit', 'Up to $50/yr on hotel stays booked through Chase Travel.', 50, 'annual', 0),
        ],
    },
    {
        'name': 'Capital One Venture X',
        'annual_fee': 395,
        'published': 1,
        'benefits': [
            ('Annual Travel Credit', 'Up to $300/yr toward bookings made through Capital One Travel.', 300, 'annual', 0),
        ],
    },
    {
        'name': 'Chase Freedom Flex',
        'annual_fee': 0,
        'published': 1,
        'benefits': [
            ('5% Rotating Category Activation', 'Activate the quarterly 5% bonus categories (on up to $1,500 in spend).', None, 'quarterly', 0),
        ],
    },
    {
        'name': 'Chase Marriott Bonvoy Premier Plus',
        'annual_fee': 95,
        'published': 1,
        'benefits': [
            ('Anniversary Free Night Award', 'Free Night Award (redemption up to 35,000 points) each account anniversary.', None, 'annual', 0),
        ],
    },
]


def main():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    with open(SCHEMA) as f:
        db.executescript(f.read())

    added_cards = 0
    added_benefits = 0
    skipped = []

    for card in CARDS:
        existing = db.execute(
            'SELECT id FROM cards WHERE name = ?', (card['name'],)
        ).fetchone()
        if existing:
            skipped.append(card['name'])
            continue

        cur = db.execute(
            'INSERT INTO cards (name, annual_fee, published) VALUES (?, ?, ?)',
            (card['name'], card['annual_fee'], card['published']))
        card_id = cur.lastrowid
        added_cards += 1

        for name, desc, amount, period, is_sub in card['benefits']:
            db.execute(
                'INSERT INTO benefits (card_id, name, description, credit_amount, '
                'period_type, is_subscription) VALUES (?, ?, ?, ?, ?, ?)',
                (card_id, name, desc, amount, period, is_sub))
            added_benefits += 1
        print(f'Added "{card["name"]}" with {len(card["benefits"])} benefit(s).')

    db.commit()
    db.close()

    print()
    print(f'Done: {added_cards} card(s), {added_benefits} benefit(s) added.')
    if skipped:
        print(f'Skipped (already present): {", ".join(skipped)}')


if __name__ == '__main__':
    main()
