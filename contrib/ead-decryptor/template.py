#!/usr/bin/env python3
"""Skeleton of a decryptor for `nanocore assets write amp SLOT file.ead` (see docs/ead-decryptor.md).

Reads the SAPF container from standard input, writes the decrypted model to standard output.
The decryption itself is left empty: this project ships no key and no algorithm.
"""

import os
import stat
import struct
import sys

HEADER = 28  # "SAPF", version u32, 16 bytes, payload size u32
TRAILER = 32  # authentication code


def load_key() -> bytes:
    """The key you were given, from NANOCORE_EAD_KEY (hex) or from the file named by NANOCORE_EAD_KEY_FILE.

    The file must be private to you (mode 0600): keep the key out of the repository and out of your shell history.
    """

    text = os.environ.get("NANOCORE_EAD_KEY", "").strip()
    path = os.environ.get("NANOCORE_EAD_KEY_FILE", "").strip()
    if path:
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(f"{path} must not be readable by others (chmod 600)")
        with open(path, encoding="ascii") as handle:
            text = handle.read().strip()
    if not text:
        raise KeyError("set NANOCORE_EAD_KEY (hex) or NANOCORE_EAD_KEY_FILE")
    return bytes.fromhex(text)


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    """Verify ``tag`` and return the decrypted payload. Fill this in with the method you were given."""

    raise NotImplementedError("no decryption is provided here")


def main() -> int:
    data = sys.stdin.buffer.read()
    if data[:4] != b"SAPF" or len(data) < HEADER + TRAILER:
        print("not a SAPF container", file=sys.stderr)
        return 2
    size = struct.unpack_from("<I", data, 24)[0]
    if len(data) != size + HEADER + TRAILER:
        print("the declared size does not match the file", file=sys.stderr)
        return 2
    nonce, ciphertext, tag = data[8:24], data[HEADER : HEADER + size], data[-TRAILER:]
    try:
        model = decrypt(load_key(), nonce, ciphertext, tag)
    except (NotImplementedError, KeyError, PermissionError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    sys.stdout.buffer.write(model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
