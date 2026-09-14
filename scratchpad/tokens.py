"""Opaque, authenticated, fixed-width attachment tokens.

A token is exactly 22 characters from ``0-9A-Za-z`` and carries 128 bits:

    payload (16 bytes) = object_id (56 bits, big endian) || domain (8 bits) || tag (64 bits)
    tag                = blake2b(key=mac_key, digest_size=8,
                                 data=b"scratchpad-token-v1" || domain_byte || object_id_7bytes)
    prp                = AES-128 single block encryption of payload under prp_key
    token              = base62_fixed22(int.from_bytes(prp, "big"))

Why AES-ECB is the right primitive here
---------------------------------------
The requirement (spec sections 18 and 21) is a *keyed reversible permutation* on exactly
128 bits: sequential object ids must produce visually unrelated tokens, and decoding must
recover the id exactly. AES-128 is precisely a keyed pseudorandom permutation on a single
128-bit block. What is usually called "ECB mode" degenerates, for an input of exactly one
block, to a single invocation of that permutation: there is no second block, so the
well-known ECB weakness (equal plaintext blocks map to equal ciphertext blocks, leaking
structure across a message) cannot arise. There is also no confidentiality goal here and
therefore no need for an IV or a nonce: the token must be deterministic so that the same
object id always yields the same token, and it must be exactly 128 bits wide so that it
fits in 22 Base62 characters. Integrity is provided separately and explicitly by the
64-bit keyed blake2b tag inside the block, which is what decoding verifies. In short: AES
is used here as a PRP, not as a cipher mode, and the inputs are never longer than one block.

Canonical encoding
------------------
62**22 > 2**128, so some 22 character strings denote values that no token can ever take.
Encoding is fixed width, most significant digit first, left padded with ``0``, which makes
it a bijection between ``[0, 2**128)`` and the 22 character strings whose value is below
2**128. Decoding therefore rejects: wrong length, characters outside the alphabet, and
values >= 2**128. The tag comparison uses ``hmac.compare_digest``.

One cosmetic consequence: because ``2**128 / 62**21`` is about 7.8, the leading digit of
a token only ranges over ``0``-``7``. That is a property of the fixed width encoding, not
of the object id, so sequential ids still produce unrelated looking tokens; every position
after the first is uniform over the full alphabet.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from hashlib import blake2b
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

__all__ = [
    "ALPHABET",
    "DOMAIN_ATTACHMENT",
    "MAX_OBJECT_ID",
    "SECRET_SIZE",
    "TOKEN_LENGTH",
    "TokenCodec",
    "base62_decode_fixed",
    "base62_encode_fixed",
    "candidate_at",
    "derive_keys",
    "find_candidates",
    "load_or_create_secret",
]

log = logging.getLogger(__name__)

#: Base62 alphabet, digits before upper case before lower case.
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
TOKEN_LENGTH = 22
#: Domain byte for attachment tokens. Other object classes may claim other values later.
DOMAIN_ATTACHMENT = 1
#: Domain separation label mixed into every authentication tag.
MAC_LABEL = b"scratchpad-token-v1"
#: Bytes held in ``secret.key``.
SECRET_SIZE = 32
OBJECT_ID_BITS = 56
OBJECT_ID_BYTES = 7
MAX_OBJECT_ID = (1 << OBJECT_ID_BITS) - 1
DOMAIN_MAX = 0xFF
_VALUE_LIMIT = 1 << 128

_DIGIT_VALUES = {ch: i for i, ch in enumerate(ALPHABET)}

# Maximal runs of the token alphabet that are exactly TOKEN_LENGTH long. The lookaround
# assertions enforce maximality: a 23 character run contains no candidate at all.
_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Za-z])[0-9A-Za-z]{%d}(?![0-9A-Za-z])" % TOKEN_LENGTH
)
_TRAILING_RUN_RE = re.compile(r"[0-9A-Za-z]*\Z")
_LEADING_RUN_RE = re.compile(r"[0-9A-Za-z]*")
# One more than TOKEN_LENGTH: enough context to prove a run is too long.
_WINDOW = TOKEN_LENGTH + 1


# --------------------------------------------------------------------------- base62


def base62_encode_fixed(value: int, width: int = TOKEN_LENGTH) -> str:
    """Encode ``value`` as exactly ``width`` Base62 digits, most significant first."""
    if value < 0:
        raise ValueError("value must be non-negative")
    if value >= _VALUE_LIMIT:
        raise ValueError("value must be below 2**128")
    out = ["0"] * width
    i = width - 1
    while value:
        value, rem = divmod(value, 62)
        if i < 0:
            raise ValueError("value does not fit in the requested width")
        out[i] = ALPHABET[rem]
        i -= 1
    return "".join(out)


def base62_decode_fixed(token: str, width: int = TOKEN_LENGTH) -> int | None:
    """Decode a canonical fixed width Base62 string, or ``None`` if it is not one.

    Rejects the wrong length, characters outside the alphabet and values >= 2**128.
    """
    if len(token) != width:
        return None
    value = 0
    get = _DIGIT_VALUES.get
    for ch in token:
        digit = get(ch)
        if digit is None:
            return None
        value = value * 62 + digit
    if value >= _VALUE_LIMIT:
        return None
    return value


# --------------------------------------------------------------------- key material


def derive_keys(secret: bytes) -> tuple[bytes, bytes]:
    """Derive ``(prp_key, mac_key)`` from the installation secret."""
    if len(secret) < 16:
        raise ValueError("secret must be at least 16 bytes")
    prp_key = blake2b(b"prp", key=secret, digest_size=16).digest()
    mac_key = blake2b(b"mac", key=secret, digest_size=32).digest()
    return prp_key, mac_key


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_or_create_secret(path: Path) -> bytes:
    """Return the installation secret, creating it atomically on first use.

    The file holds :data:`SECRET_SIZE` random bytes with mode 0600. Creation goes through
    a temporary file plus ``os.link``, so a concurrent creator can never win a partial
    file and an existing secret is never overwritten.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        pass
    else:
        if len(data) < 16:
            raise ValueError(f"{path} is too short to be a secret key ({len(data)} bytes)")
        return data

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secrets.token_bytes(SECRET_SIZE))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)
    except FileExistsError:
        log.debug("secret key created concurrently at %s", path)
    finally:
        os.unlink(tmp)
    _fsync_dir(path.parent)
    return path.read_bytes()


# ------------------------------------------------------------------ candidate search


def find_candidates(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, candidate)`` for every maximal alphanumeric run of exactly 22.

    A longer run is not a candidate at all (spec section 20), which keeps hashes and
    encoded blobs from producing spurious candidates.
    """
    return [(m.start(), m.end(), m.group()) for m in _CANDIDATE_RE.finditer(text)]


def candidate_at(text: str, pos: int) -> tuple[int, int, str] | None:
    """Return the candidate whose run contains ``pos``, or ``None``.

    ``pos == end`` counts, so a cursor sitting directly behind a token still resolves it.
    Work is bounded to a window of 2 * 23 characters regardless of the text size.
    """
    if pos < 0 or pos > len(text):
        return None
    left = text[max(0, pos - _WINDOW) : pos]
    right = text[pos : pos + _WINDOW]
    start = pos - len(_TRAILING_RUN_RE.search(left).group())
    end = pos + len(_LEADING_RUN_RE.match(right).group())
    if end - start != TOKEN_LENGTH:
        return None
    return start, end, text[start:end]


# ------------------------------------------------------------------------- the codec


class TokenCodec:
    """Encodes and authenticates attachment tokens for one installation secret."""

    TOKEN_LENGTH = TOKEN_LENGTH
    ALPHABET = ALPHABET

    def __init__(self, secret: bytes) -> None:
        self._prp_key, self._mac_key = derive_keys(secret)
        # AES-128 used as a keyed pseudorandom permutation on exactly one 128 bit block;
        # see the module docstring for why "ECB" is the correct spelling of that here.
        self._cipher = Cipher(algorithms.AES(self._prp_key), modes.ECB())

    # -- internals ---------------------------------------------------------------

    def _tag(self, object_id: int, domain: int) -> bytes:
        return blake2b(
            MAC_LABEL + bytes((domain,)) + object_id.to_bytes(OBJECT_ID_BYTES, "big"),
            key=self._mac_key,
            digest_size=8,
        ).digest()

    def _permute(self, block: bytes) -> bytes:
        enc = self._cipher.encryptor()
        return enc.update(block) + enc.finalize()

    def _unpermute(self, block: bytes) -> bytes:
        dec = self._cipher.decryptor()
        return dec.update(block) + dec.finalize()

    # -- public ------------------------------------------------------------------

    def encode(self, object_id: int, domain: int = DOMAIN_ATTACHMENT) -> str:
        """Return the 22 character token for ``object_id`` in ``domain``."""
        if not 0 <= object_id <= MAX_OBJECT_ID:
            raise ValueError(f"object_id out of range for {OBJECT_ID_BITS} bits: {object_id}")
        if not 0 <= domain <= DOMAIN_MAX:
            raise ValueError(f"domain out of range for 8 bits: {domain}")
        payload = (
            object_id.to_bytes(OBJECT_ID_BYTES, "big")
            + bytes((domain,))
            + self._tag(object_id, domain)
        )
        return base62_encode_fixed(int.from_bytes(self._permute(payload), "big"))

    def decode(self, token: str) -> tuple[int, int] | None:
        """Return ``(object_id, domain)`` or ``None`` when ``token`` is not ours."""
        value = base62_decode_fixed(token)
        if value is None:
            return None
        payload = self._unpermute(value.to_bytes(16, "big"))
        object_id = int.from_bytes(payload[:OBJECT_ID_BYTES], "big")
        domain = payload[OBJECT_ID_BYTES]
        if not hmac.compare_digest(payload[OBJECT_ID_BYTES + 1 :], self._tag(object_id, domain)):
            return None
        return object_id, domain

    find_candidates = staticmethod(find_candidates)
    candidate_at = staticmethod(candidate_at)
