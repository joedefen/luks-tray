#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" The two header types luks-tray opens, and the identity of one with no UUID.

    `blkid` names a LUKS volume because a LUKS header is cleartext, and that is
    how every *file* entry in the history used to be keyed: by the UUID inside
    the file. A TrueCrypt/VeraCrypt header is different in kind -- it is
    encrypted under a key derived from the passphrase. The file therefore holds
    no magic, no version and no UUID that anything can read without that
    passphrase, which is why `blkid` reports no signature for it and why
    `cryptsetup tcryptDump` prints no UUID for either format.

    Two consequences shape this module:

    - the *type* cannot be discovered, only asked for. `LuksOps.
      unlock_container()` tries the declared type and takes cryptsetup's rc=1
      "is not a valid LUKS device" as the one definitive "the type was wrong"
      answer;
    - the *identity* of such a container is its salt: the 64 bytes its header
      starts with, random per container and re-drawn whenever one is created.
      That is stable across opens and mounts and changes when the file is
      re-made, which is exactly what the history's purge check needs.

    Everything here is a plain read of the file: no subprocess, no privilege,
    no passphrase. A TCRYPT *device* is still out of reach, because `/dev/sdX`
    is root/`disk`-only while a file container is readable by its owner.
"""

import hashlib

SALT_SIZE = 64            # every TrueCrypt/VeraCrypt header starts with one
SALT_KEY_PREFIX = 'salt-'

LUKS = 'luks'             # the values `cryptsetup open --type` takes
TCRYPT = 'tcrypt'
CONTAINER_TYPES = (LUKS, TCRYPT)


def header_salt(path):
    """ The first `SALT_SIZE` bytes of `path`, or b'' if they cannot be read. """
    if not path:
        return b''
    try:
        with open(path, 'rb') as stream:
            return stream.read(SALT_SIZE)
    except OSError:
        return b''


def salt_identity(path):
    """ 'salt-<digest>' for a container with an encrypted header, else ''.

        '' is also the answer for a file too short to hold a header and for one
        that cannot be read at all; the caller then falls back to whatever it
        does for a file it cannot identify.

        The digest is truncated to 32 hex digits: long enough that a collision
        among a handful of history entries cannot matter, short enough to read
        in a log line.
    """
    salt = header_salt(path)
    if len(salt) < SALT_SIZE:
        return ''
    return SALT_KEY_PREFIX + hashlib.sha256(salt).hexdigest()[:32]
