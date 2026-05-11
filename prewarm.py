import json
import crayons
import hashlib
import redis
import requests
import time
from rq import Queue
import config

GATEWAYS = config.gateways
q = Queue("dweb", connection=redis.Redis())
PREWARM_READ_BYTES = getattr(config, "prewarm_read_bytes", 4096)

def _lock_key(prefix, value):
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def prewarm(result: str):
    if result.startswith('dnslink='):
        # drop the dnslink= prefix
        result = result[8:]
    if result.startswith('/ipfs/'):
        ipfs = result[6:]
        if ipfs.startswith("ba"):
            # q.enqueue(eth_limo_ipfs, ipfs)
            pass
        print("Prewarming IPFS: " + result, flush=True)
        load_ipfs_hash(result)
    if result.startswith('/ipns/'):
        ipns = result[6:]
        # q.enqueue(eth_limo_ipns, ipns)
        resolved = resolve_ipns(ipns)
        if resolved.startswith('/ipfs/'):
            ipfs = resolved[6:]
            if ipfs.startswith("ba"):
                # q.enqueue(eth_limo_ipfs, ipfs)
                pass
        print("Prewarming Resolved IPNS: " + result + " -> " + resolved, flush=True)
        load_ipfs_hash(resolved)

def load_ipfs_hash(ipfs_hash):
    for gateway in GATEWAYS:
        url = gateway + ipfs_hash
        lock_key = _lock_key("gateway-fetch", url)
        interval = getattr(config, "gateway_fetch_interval_seconds", 3600)
        if q.connection.set(lock_key, "1", nx=True, ex=interval):
            print("Enqueueing: " + url, flush=True)
            q.enqueue(
                request_ipfs_hash,
                url,
                job_timeout=75,
                result_ttl=0,
                failure_ttl=300,
            )
    return None

def request_ipfs_hash(ipfs_hash):
    try:
        with requests.get(ipfs_hash, timeout=60, allow_redirects=True, stream=True) as response:
            if response.status_code == 200:
                next(response.iter_content(chunk_size=PREWARM_READ_BYTES), b"")
    except requests.exceptions.RequestException as e:
        return None


def eth_limo_ipns(ipns: str):
    try:
        url = "https://" + ipns + ".ipfs2.eth.limo/"
        response = requests.get(url, timeout=60, allow_redirects=True)
        if response.status_code == 200:
            print(crayons.green("OK: " + url), flush=True)
            return response.text
    except requests.exceptions.RequestException as e:
        return None


def eth_limo_ipfs(ipfs: str):
    try:
        url = "https://" + ipfs + ".ipfs2.eth.limo/"
        response = requests.get(url, timeout=60, allow_redirects=True)
        if response.status_code == 200:
            print(crayons.green("OK: " + url), flush=True)
            return response.text
    except requests.exceptions.RequestException as e:
        return None
    

def resolve_ipns(ipns: str) -> str:
    rc = redis.Redis(host="localhost", port=6379, db=0)
    r_key = "ipns:" + ipns + ":results"
    print("Resolving IPNS: " + ipns, flush=True)
    url = config.ipfs_api_server + "api/v0/name/resolve?arg=" + ipns + "&recursive=true&stream=true&nocache=true"
    try:
        print("POST: " + url, flush=True)
        r = requests.post(url, timeout=30)
        if r.status_code == 200:
            lines = r.text.split("\n")
            for line in lines:
                if line.startswith("{"):
                    o = json.loads(line)
                    existing = rc.zscore(r_key, o["Path"])
                    if existing is None:
                        print("Adding: " + o["Path"], flush=True)
                        rc.zadd(r_key, {o["Path"]: int(time.time())}, nx=True)
                        return o["Path"]
            latest = rc.zrevrange(r_key, 0, 0)
            if latest is not None and len(latest) > 0:
                return latest[0].decode("utf-8")
        else:
            print("Error: " + str(r.status_code), flush=True)
            print("Error: " + str(r.text), flush=True)
        return "/ipns/" + ipns
    except Exception as e:
        print("Error: " + str(e), flush=True)
        return "/ipns/" + ipns
