"""Run once to create the initial admin user.

Creates the database schema if needed and inserts a single admin user
into the users table. Refuses to run if an admin user already exists
(use the in-app admin UI to add more users in that case)."""
import getpass
import os
import sqlite3

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'benefits.db')
SCHEMA   = os.path.join(BASE_DIR, 'schema.sql')


def main():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    with open(SCHEMA) as f:
        db.executescript(f.read())

    existing = db.execute(
        'SELECT email FROM users WHERE is_admin = 1 LIMIT 1'
    ).fetchone()
    if existing:
        print(f'An admin user already exists ({existing["email"]}).')
        print('Refusing to create another. Manage users from inside the app.')
        return

    email    = input('Admin email: ').strip()
    if not email:
        print('Email is required.')
        return
    password = getpass.getpass('Choose a password: ')
    if not password:
        print('Password is required.')
        return

    db.execute(
        'INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, 1)',
        (email, generate_password_hash(password))
    )
    db.commit()
    db.close()
    print(f'Created admin user "{email}".')
    print('You can now run: python app.py')


if __name__ == '__main__':
    main()
