#!/usr/bin/env python3
"""Header types luks-tray opens and the identity of a container without a UUID."""

import hashlib

SALT_SIZE = 64
SALT_KEY_PREFIX = 'salt-'

LUKS = 'luks'
TCRYPT = 'tcrypt'
CONTAINER_TYPES = (LUKS, TCRYPT)


def header_salt(path):
    """Return the first `SALT_SIZE` bytes of `path`, or `b''` on error.

    Args:
        path: Container file path.

    Returns:
        bytes: The header salt.
    """
    if not path:
        return b''
    try:
        with open(path, 'rb') as stream:
            return stream.read(SALT_SIZE)
    except OSError:
        return b''


def salt_identity(path):
    """Return a `salt-<digest>` identity for a container with an encrypted header.

    Args:
        path: Container file path.

    Returns:
        str: The identity, or `''` when the file has no readable salt.
    """
    salt = header_salt(path)
    if len(salt) < SALT_SIZE:
        return ''
    return SALT_KEY_PREFIX + hashlib.sha256(salt).hexdigest()[:32]
