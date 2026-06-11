import os
from cryptography.fernet import Fernet

# Key management: Simple file-based key for this project
KEY_FILE = os.path.join(os.path.dirname(__file__), '.secret.key')

def get_or_create_key():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, 'wb') as f:
            f.write(key)
        return key

def encrypt_password(password: str) -> str:
    key = get_or_create_key()
    f = Fernet(key)
    return f.encrypt(password.encode()).decode()

def decrypt_password(encrypted_password: str) -> str:
    key = get_or_create_key()
    f = Fernet(key)
    return f.decrypt(encrypted_password.encode()).decode()

import logging

class SuppressApiCollectFilter(logging.Filter):
    def filter(self, record):
        # Daphne/Django access logs usually contain the path in the message
        return "/api/collect/" not in record.getMessage()
