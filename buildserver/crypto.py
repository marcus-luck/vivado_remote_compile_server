"""Encryption of the secrets stored in the database: GitHub tokens.

GitHub tokens cannot be hashed like the server's own API tokens: the worker
has to send the real token to GitHub on every clone. They are encrypted with
Fernet (AES-128-CBC with an HMAC) under BUILDSERVER_TOKEN_KEY instead, so a
copy of the database, a backup say, does not give the tokens away.

Stored values carry a prefix, so plaintext values written before encryption
was introduced can be told apart and migrated.
"""

from cryptography import fernet

PREFIX = "fernet:"


def generate_key() -> str:
    """Returns a new random key, in the form BUILDSERVER_TOKEN_KEY takes."""
    return fernet.Fernet.generate_key().decode()


def is_encrypted(stored: str) -> bool:
    """Tells whether a stored value was written by TokenCipher.encrypt()."""
    return stored.startswith(PREFIX)


class TokenCipher:
    """Encrypts and decrypts stored tokens with one key."""

    def __init__(self, key: str) -> None:
        """Creates the cipher.

        Args:
            key: A key from generate_key(): 32 bytes, URL-safe base64.

        Raises:
            ValueError: If the key is not a valid Fernet key.
        """
        try:
            self._fernet = fernet.Fernet(key.encode())
        except ValueError as e:
            raise ValueError(
                "BUILDSERVER_TOKEN_KEY is not a valid key; generate one with"
                ' python -c "from buildserver import crypto;'
                ' print(crypto.generate_key())"'
            ) from e

    def encrypt(self, value: str) -> str:
        """Encrypts a token for storage.

        Args:
            value: The token.

        Returns:
            The value to store.
        """
        return PREFIX + self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, stored: str) -> str:
        """Decrypts a stored token.

        Args:
            stored: A value encrypt() returned.

        Returns:
            The token.

        Raises:
            ValueError: If the value is not encrypted, or was encrypted with
                another key or altered.
        """
        if not is_encrypted(stored):
            raise ValueError("stored token is not encrypted")
        try:
            return self._fernet.decrypt(stored[len(PREFIX) :]).decode()
        except fernet.InvalidToken as e:
            raise ValueError(
                "stored token cannot be decrypted with BUILDSERVER_TOKEN_KEY"
            ) from e


def cipher_for(key: str) -> TokenCipher | None:
    """Returns the cipher for a configured key, or None if it is unset.

    Args:
        key: The value of BUILDSERVER_TOKEN_KEY, possibly empty.

    Returns:
        The cipher, or None for an empty key.

    Raises:
        ValueError: If the key is set but not valid.
    """
    return TokenCipher(key) if key else None
