"""Run once to create the .credentials file for app login."""
import os
import getpass
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(BASE_DIR, '.credentials')

if os.path.exists(CREDS_FILE):
    print('.credentials already exists. Delete it first to reset.')
else:
    username = input('Choose a username: ').strip()
    password = getpass.getpass('Choose a password: ')
    with open(CREDS_FILE, 'w') as f:
        f.write(f'{username}:{generate_password_hash(password)}')
    print(f'Created .credentials for user "{username}".')
    print('You can now run: python app.py')
