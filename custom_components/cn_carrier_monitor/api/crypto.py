"""Crypto helpers shared by carrier clients.

Only ``cryptography`` is used — it ships with Home Assistant core, so the
integration has no extra manifest requirements.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

_LOGGER = logging.getLogger(__name__)


def b64_decode(value: str) -> bytes:
    """Base64 decode tolerating missing padding and URL-safe variants."""
    cleaned = re.sub(r"\s+", "", value or "")
    cleaned = cleaned.replace("-", "+").replace("_", "/")
    cleaned += "=" * ((4 - len(cleaned) % 4) % 4)
    return base64.b64decode(cleaned)


def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: str) -> bytes:
    """AES-128/256-CBC with PKCS7 padding."""
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128/256-CBC decrypt + PKCS7 unpad; raises on bad input."""
    if len(data) == 0 or len(data) % 16 != 0:
        raise ValueError("ciphertext length is not a multiple of the block size")
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    padded = dec.update(data) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def decrypt_payload(text: str, key: bytes, iv: bytes) -> str:
    """Decrypt a carrier response that may be hex- or base64-encoded AES-CBC.

    Returns the original text unchanged when it does not look encrypted.
    """
    if not text:
        return text
    stripped = re.sub(r"\s+", "", text)
    candidates: list[bytes] = []
    if len(stripped) >= 32 and all(c in "0123456789abcdefABCDEF" for c in stripped):
        try:
            candidates.append(binascii.unhexlify(stripped))
        except (ValueError, binascii.Error):
            pass
    if stripped.endswith("=") or (len(stripped) % 4 == 0 and "=" in stripped):
        try:
            candidates.append(b64_decode(stripped))
        except (ValueError, binascii.Error):
            pass
    for data in candidates:
        try:
            return aes_cbc_decrypt(key, iv, data).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            continue
    return text


def rsa_encrypt_b64(public_key_b64: str, text: str, *, der: bool = True) -> str:
    """RSA PKCS#1 v1.5 encrypt, returning base64. ``der=True`` for raw SPKI b64."""
    key_bytes = b64_decode(public_key_b64) if der else public_key_b64.encode()
    public_key = serialization.load_der_public_key(key_bytes)
    encrypted = public_key.encrypt(text.encode("utf-8"), asym_padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


def caesar(text: str, shift: int = 2) -> str:
    """Per-character shift used by the China Telecom app protocol."""
    return "".join(chr((ord(ch) + shift) & 0xFFFF) for ch in text)


def parse_jsonp(text: str) -> str | None:
    """Unwrap a JSONP response such as ``callback({...})`` to the JSON string."""
    if not text:
        return None
    value = text.strip()
    start = value.find("(")
    end = value.rfind(")")
    if start != -1 and end > start:
        inner = value[start + 1 : end].strip().rstrip(";").strip()
        if inner.startswith("{"):
            return inner
    return None


def parse_maybe_json(text: str) -> dict[str, Any] | None:
    """Parse a response that may be plain JSON or JSONP."""
    import json

    if not text:
        return None
    value = text.strip()
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    unwrapped = parse_jsonp(value)
    if unwrapped is None:
        return None
    try:
        parsed = json.loads(unwrapped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_curl(source: str) -> dict[str, str]:
    """Extract url/cookie/method/body from a pasted ``curl`` command.

    Users capture the official query request with the browser "Copy as cURL"
    command; this makes importing a Broadnet session a one-paste operation.
    Handles bash (backslash-newline, single quotes) and Windows (``^`` line
    continuation, double quotes) variants.
    """
    import shlex

    text = re.sub(r"[\^\\]\r?\n", " ", source.strip())
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = shlex.split(text, posix=False)

    url = cookie = method = body = ""
    skip_flags = {
        "-s", "-k", "-l", "-L", "--insecure", "--compressed", "--location",
        "-v", "--silent", "--http1.1", "--http2", "-4", "-6",
    }
    value_flags = {
        "-h", "--header", "-x", "--request", "-d", "--data", "--data-raw",
        "--data-binary", "--data-ascii", "--data-urlencode", "-a", "-b",
    }
    i = 0
    while i < len(tokens):
        token = tokens[i]
        lowered = token.lower()
        if lowered.lstrip("$") == "curl":
            pass
        elif token in skip_flags:
            pass
        elif lowered.startswith("http://") or lowered.startswith("https://"):
            if not url:
                url = token
        elif lowered in ("-h", "--header"):
            i += 1
            if i < len(tokens):
                name, _, value = tokens[i].partition(":")
                if name.strip().lower() == "cookie":
                    cookie = value.strip()
        elif lowered in ("-x", "--request"):
            i += 1
            if i < len(tokens):
                method = tokens[i].strip().upper()
        elif lowered in ("-d", "--data", "--data-raw", "--data-binary",
                         "--data-ascii", "--data-urlencode", "-b"):
            i += 1
            if i < len(tokens) and lowered != "-b":
                body = tokens[i]
        elif token.startswith("-"):
            # unknown value flags (-A, -e, -H with attached value, …) — skip
            pass
        elif not url:
            url = token
        i += 1
    if not url:
        match = re.search(r"https?://\S+", text)
        if match:
            url = match.group(0).rstrip("',\"")
    return {"url": url, "cookie": cookie, "method": method, "body": body}


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()
