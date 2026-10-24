"""Contract tests: crypto helpers (contract = fixed algorithm behaviour)."""

import base64


def test_aes_cbc_roundtrip(ccm):
    crypto = ccm.modules["crypto"]
    key = b"1234123412ABCDEF"
    iv = b"ABCDEF1234123412"
    ct = crypto.aes_cbc_encrypt(key, iv, '{"status":"000000"}')
    assert crypto.aes_cbc_decrypt(key, iv, ct).decode() == '{"status":"000000"}'


def test_decrypt_payload_hex_and_plain_passthrough(ccm):
    crypto = ccm.modules["crypto"]
    key, iv = b"1234123412ABCDEF", b"ABCDEF1234123412"
    ct = crypto.aes_cbc_encrypt(key, iv, '{"a":1}')
    hex_payload = ct.hex()
    assert crypto.decrypt_payload(hex_payload, key, iv) == '{"a":1}'
    # non-encrypted body must pass through unchanged
    assert crypto.decrypt_payload("plain text", b"k" * 16, b"i" * 16) == "plain text"


def test_aes_ecb_b64_block_size(ccm):
    crypto = ccm.modules["crypto"]
    enc = crypto.aes_ecb_encrypt_b64("0.5432", b"0123456789abcdef")
    assert len(base64.b64decode(enc)) % 16 == 0


def test_rsa_ciphertext_lengths(ccm):
    crypto = ccm.modules["crypto"]
    # telecom/unicom-login: 1024-bit keys -> 128 bytes; mini-program: 2048 -> 256
    pub1024 = (
        "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDBkLT15ThVgz6/NOl6s8GNPofd"
        "WzWbCkWnkaAm7O2LjkM1H7dMvzkiqdxU02jamGRHLX/ZNMCXHnPcW/sDhiFCBN18"
        "qFvy8g6VYb9QtroI09e176s+ZCtiv7hbin2cCTj99iUpnEloZm19lwHyo69u5UMi"
        "PMpq0/XKBO8lYhN/gwIDAQAB"
    )
    assert len(base64.b64decode(crypto.rsa_encrypt_b64(pub1024, "x"))) == 128


def test_parse_jsonp_and_plain(ccm):
    crypto = ccm.modules["crypto"]
    assert crypto.parse_maybe_json('cb({"a":1})') == {"a": 1}
    assert crypto.parse_maybe_json('{"a":1}') == {"a": 1}
    assert crypto.parse_maybe_json("garbage") is None


def test_parse_curl_bash_windows_and_bare_url(ccm):
    crypto = ccm.modules["crypto"]
    bash = (
        "curl 'https://www.10099.com.cn/api/qryUserRes?x=1' \\\n"
        "  -H 'Cookie: JSESSIONID=abc; sid=xyz' \\\n"
        "  --data-raw '{\"foo\":1}'"
    )
    p = crypto.parse_curl(bash)
    assert p["url"].startswith("https://www.10099.com.cn")
    assert "sid=xyz" in p["cookie"]
    assert p["body"] == '{"foo":1}'

    win = 'curl "https://www.10099.com.cn/a/b" -H "Cookie: sid=1" --data "a=1"'
    p2 = crypto.parse_curl(win)
    assert p2["url"] == "https://www.10099.com.cn/a/b"
    assert p2["cookie"] == "sid=1"
    assert p2["body"] == "a=1"

    p3 = crypto.parse_curl("https://www.10099.com.cn/api/x?a=1")
    assert p3["url"] == "https://www.10099.com.cn/api/x?a=1"


def test_caesar(ccm):
    crypto = ccm.modules["crypto"]
    assert crypto.caesar("13800138000", 2) == "".join(chr(ord(c) + 2) for c in "13800138000")
