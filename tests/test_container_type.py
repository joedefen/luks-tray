"""`ContainerType`: the salt that identifies a container with no readable UUID.

There is no magic to detect: a TrueCrypt/VeraCrypt header is encrypted, so the
only thing in such a file that can be read without the passphrase is the salt
it starts with, and that is what the history keys the entry by. These tests pin
what that leaves us with -- stable across opens, different after a re-create,
and nothing at all for a file that cannot be a container.
"""

import hashlib

from luks_tray.ContainerType import (SALT_KEY_PREFIX, SALT_SIZE, header_salt,
                                     salt_identity)


def write(tmp_path, body, name='c.img'):
    target = tmp_path / name
    target.write_bytes(body)
    return str(target)


def test_the_salt_is_the_first_64_bytes(tmp_path):
    salt = bytes(range(SALT_SIZE))
    path = write(tmp_path, salt + b'\x00' * 200)
    assert header_salt(path) == salt


def test_a_file_too_short_to_hold_a_header_has_no_identity(tmp_path):
    # The read returns what is there, but a salt that short identifies nothing.
    path = write(tmp_path, b'LUKS\xba\xbe')
    assert header_salt(path) == b'LUKS\xba\xbe'
    assert salt_identity(path) == ''


def test_a_missing_file_has_no_salt(tmp_path):
    assert header_salt(str(tmp_path / 'gone.img')) == b''
    assert header_salt('') == b''


def test_a_directory_has_no_salt(tmp_path):
    assert header_salt(str(tmp_path)) == b''
    assert salt_identity(str(tmp_path)) == ''


def test_the_identity_is_a_truncated_digest_of_the_salt(tmp_path):
    salt = bytes(range(SALT_SIZE))
    path = write(tmp_path, salt + b'noise')
    expected = SALT_KEY_PREFIX + hashlib.sha256(salt).hexdigest()[:32]
    assert salt_identity(path) == expected


def test_the_identity_ignores_everything_after_the_salt(tmp_path):
    # The rest of the header is ciphertext and changes on every write of the
    # volume's metadata; only the salt identifies the container itself.
    salt = b'\x07' * SALT_SIZE
    first = write(tmp_path, salt + b'\x01' * 100, name='first.img')
    second = write(tmp_path, salt + b'\x02' * 100, name='second.img')
    assert salt_identity(first) == salt_identity(second)


def test_a_re_created_container_gets_a_new_identity(tmp_path):
    before = salt_identity(write(tmp_path, b'\x11' * SALT_SIZE + b'x' * 100))
    after = salt_identity(write(tmp_path, b'\x22' * SALT_SIZE + b'x' * 100))
    assert before and after
    assert before != after


def test_a_short_file_gets_no_identity_even_when_the_salt_matches(tmp_path):
    # A 64-byte file is all salt and no header: identifying it would let a
    # stray file inherit an entry.
    path = write(tmp_path, b'\x11' * (SALT_SIZE - 1))
    assert salt_identity(path) == ''
