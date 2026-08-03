"""Solana Name Service (SNS) resolution over Solana JSON RPC.

Derives .sol domain and record accounts on-chain and extracts their
IPNS/IPFS records, replacing external resolver APIs.

Reference: https://github.com/SolanaNameService/sns-sdk
"""
import base64
import hashlib
import re

import requests

import config

HASH_PREFIX = "SPL Name Service"
NAME_PROGRAM_ID = "namesLPneVptA9Z5rqUDD9tMTWEJwofgaYwp8cawRkX"
ROOT_DOMAIN_ACCOUNT = "58PwtjSDuFHuUkYjH9BYnnQKHfwo9reZhC2zMJv9JPkx"
CENTRAL_STATE_DOMAIN_RECORDS = "2pMnqHvei2N5oDcVGCRdZx48gqti199wr5CsyTTafsbo"

NAME_REGISTRY_LEN = 96
RECORD_HEADER_LEN = 8
# sns-records Validation enum -> validation id length in bytes
VALIDATION_LENGTHS = {0: 0, 1: 32, 2: 20, 3: 32}

RPC_URL = "https://mainnet.helius-rpc.com/?api-key=" + config.helius_api_key

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58_ALPHABET[r] + s
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + s


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


# ed25519 point decompression (RFC 8032) to check whether a candidate
# program-derived address lies on the curve; PDAs must not.
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _is_on_curve(point: bytes) -> bool:
    y = int.from_bytes(point, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return False
    y2 = (y * y) % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x = (u * pow(v, 3, _P)) * pow((u * pow(v, 7, _P)) % _P, (_P - 5) // 8, _P) % _P
    vx2 = (v * x * x) % _P
    if vx2 != u:
        if vx2 != (-u) % _P:
            return False
        x = (x * pow(2, (_P - 1) // 4, _P)) % _P
    if x == 0 and sign == 1:
        return False
    return True


def find_program_address(seeds, program_id: bytes) -> bytes:
    for bump in range(255, -1, -1):
        h = hashlib.sha256()
        for seed in seeds:
            h.update(seed)
        h.update(bytes([bump]))
        h.update(program_id)
        h.update(b"ProgramDerivedAddress")
        digest = h.digest()
        if not _is_on_curve(digest):
            return digest
    raise ValueError("Unable to find a viable program address bump seed")


def _derive(name: str, parent: bytes, name_class: bytes = None) -> bytes:
    hashed = hashlib.sha256((HASH_PREFIX + name).encode("utf-8")).digest()
    seeds = [hashed, name_class or bytes(32), parent]
    return find_program_address(seeds, b58decode(NAME_PROGRAM_ID))


def get_domain_key(domain: str, record_version: int = None) -> bytes:
    if domain.endswith(".sol"):
        domain = domain[:-4]
    record_class = b58decode(CENTRAL_STATE_DOMAIN_RECORDS) if record_version == 2 else None
    prefix = {1: "\x01", 2: "\x02"}.get(record_version, "\x00")
    root = b58decode(ROOT_DOMAIN_ACCOUNT)
    parts = domain.split(".")
    if len(parts) == 2:
        parent = _derive(parts[1], root)
        return _derive(prefix + parts[0], parent, record_class)
    elif len(parts) == 3 and record_version is not None:
        parent = _derive(parts[2], root)
        sub = _derive("\x00" + parts[1], parent)
        return _derive(prefix + parts[0], sub, record_class)
    elif len(parts) >= 3:
        raise ValueError("The domain is malformed: " + domain)
    return _derive(domain, root)


def get_record_v1_key(domain: str, record: str) -> bytes:
    return get_domain_key(record + "." + domain, record_version=1)


def get_record_v2_key(domain: str, record: str) -> bytes:
    domain_key = get_domain_key(domain)
    return _derive("\x02" + record, domain_key, b58decode(CENTRAL_STATE_DOMAIN_RECORDS))


def _get_multiple_accounts(keys):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getMultipleAccounts",
        "params": [
            [b58encode(k) for k in keys],
            {"encoding": "base64", "commitment": "confirmed"},
        ],
    }
    r = requests.post(RPC_URL, json=payload, timeout=10)
    r.raise_for_status()
    values = r.json().get("result", {}).get("value")
    if values is None:
        return [None] * len(keys)
    return [base64.b64decode(v["data"][0]) if v is not None else None for v in values]


def _record_v2_content(data: bytes) -> bytes:
    body = data[NAME_REGISTRY_LEN:]
    if len(body) < RECORD_HEADER_LEN:
        return None
    staleness = int.from_bytes(body[0:2], "little")
    roa = int.from_bytes(body[2:4], "little")
    content_length = int.from_bytes(body[4:8], "little")
    if staleness not in VALIDATION_LENGTHS or roa not in VALIDATION_LENGTHS:
        return None
    start = RECORD_HEADER_LEN + VALIDATION_LENGTHS[staleness] + VALIDATION_LENGTHS[roa]
    end = start + content_length
    if end > len(body):
        return None
    return body[start:end]


def _record_v1_content(data: bytes) -> bytes:
    return data[NAME_REGISTRY_LEN:].rstrip(b"\x00")


def _dnslink_path(kind: str, content: bytes) -> str:
    if not content:
        return None
    value = content.decode("utf-8", errors="ignore").strip().strip("\x00")
    if kind == "ipns":
        if value.startswith("ipns://"):
            value = value[len("ipns://"):]
        if value.startswith("k51") or value.startswith("k2"):
            return "/ipns/" + value
    if kind == "ipfs":
        if value.startswith("ipfs://"):
            value = value[len("ipfs://"):]
        if value.startswith("Qm") or value.startswith("baf"):
            return "/ipfs/" + value
    return None


def resolve_ipfs_ipns(name: str) -> str:
    """Resolve a .sol name to /ipns/... or /ipfs/..., or None if not found."""
    # DNS names are case-insensitive; on-chain SNS domains are canonical lowercase
    name = name.strip().lower()
    domain = name[:-4] if name.endswith(".sol") else name
    keys = [
        get_record_v2_key(domain, "IPNS"),
        get_record_v2_key(domain, "IPFS"),
        get_record_v1_key(domain, "IPNS"),
        get_record_v1_key(domain, "IPFS"),
        get_domain_key(domain),
    ]
    accounts = _get_multiple_accounts(keys)
    for data, kind, parse in [
        (accounts[0], "ipns", _record_v2_content),
        (accounts[1], "ipfs", _record_v2_content),
        (accounts[2], "ipns", _record_v1_content),
        (accounts[3], "ipfs", _record_v1_content),
    ]:
        if data is None:
            continue
        path = _dnslink_path(kind, parse(data))
        if path is not None:
            return path
    # legacy fallback: "ipns=k51..." stored directly in the domain account data
    if accounts[4] is not None:
        decoded = accounts[4].decode("utf-8", errors="ignore")
        m = re.search(r"ipns=(k51[a-zA-Z0-9]{59})", decoded)
        if m is not None:
            return "/ipns/" + m.group(1)
    return None
