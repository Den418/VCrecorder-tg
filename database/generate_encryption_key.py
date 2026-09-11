"""
Run once to generate a key for WORKER_ENCRYPTION_KEY in .env:

    python -m database.generate_encryption_key
"""
from cryptography.fernet import Fernet

if __name__ == "__main__":
    key = Fernet.generate_key().decode()
    print("\nДобавьте это значение в .env как WORKER_ENCRYPTION_KEY:\n")
    print(key)
    print("\nПотеря этого ключа = потеря доступа ко всем сохранённым сессиям воркеров. Храните его так же бережно, как пароли.\n")
