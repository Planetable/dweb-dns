import copy
import crayons
import hashlib
import json
import re
import time
import traceback

import dns.message
import dns.name
import flask
import redis
import requests
from rq import Queue
from flask import Flask, make_response, request
from markupsafe import escape
from logsnag import LogSnag
from prewarm import resolve_ipns, prewarm
import config
import sns

logsnag = LogSnag(token=config.logsnag_token, project=config.logsnag_project)

app = Flask(__name__)
q = Queue("dweb", connection=redis.Redis())


def _lock_key(prefix, value):
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def enqueue_prewarm(result):
    lock_key = _lock_key("prewarm", result)
    interval = getattr(config, "prewarm_interval_seconds", 3600)
    if q.connection.set(lock_key, "1", nx=True, ex=interval):
        q.enqueue(
            prewarm,
            result,
            job_timeout=120,
            result_ttl=0,
            failure_ttl=300,
        )


def enqueue_revalidate_ipns(ipns):
    lock_key = _lock_key("revalidate-ipns", ipns)
    interval = getattr(config, "revalidate_interval_seconds", 600)
    if q.connection.set(lock_key, "1", nx=True, ex=interval):
        q.enqueue(
            revalidate_ipns,
            ipns,
            job_timeout=60,
            result_ttl=0,
            failure_ttl=300,
        )


def log_successful_resolve(channel, name, result):
    if channel == "furl" and not name.endswith(".fc"):
        name = name + ".fc"
    logsnag.track(
        channel=channel,
        event=f"Successful Resolve",
        description=f"{channel}/{name} -> {result}",
        user_id=name,
        notify=True,
        tags={
            "channel": channel,
            "result": result
        }
    )

def make_rr(simple, rdata):
    csimple = copy.copy(simple)
    rdata_text = rdata.to_text()
    if rdata_text.startswith('"') and rdata_text.endswith('"'):
        rdata_text = rdata_text[1:-1]
    csimple["data"] = rdata_text
    return csimple


def flatten_rrset(rrs):
    simple = {
        "name": str(rrs.name),
        "type": rrs.rdtype,
    }
    if len(rrs) > 0:
        simple["TTL"] = rrs.ttl
        return [make_rr(simple, rdata) for rdata in rrs]
    else:
        return [simple]


def to_doh_simple(message):
    simple = {"Status": int(message.rcode())}
    for f in dns.flags.Flag:
        if f != dns.flags.Flag.AA and f != dns.flags.Flag.QR:
            # DoH JSON doesn't need AA and omits it.  DoH JSON is only
            # used in replies so the QR flag is implied.
            simple[f.name] = (message.flags & f) != 0
    for i, s in enumerate(message.sections):
        k = dns.message.MessageSection.to_text(i).title()
        simple[k] = []
        for rrs in s:
            simple[k].extend(flatten_rrset(rrs))
    # we don't encode the ecs_client_subnet field
    response = flask.Response(json.dumps(simple, indent=2) + "\n")
    response.headers.set("Content-Type", "application/dns-json")
    response.headers.set("Cloudflare-CDN-Cache-Control", f"public, max-age=${config.dns_cache_ttl}")
    return response


def make_empty_message(name="", t=dns.rdatatype.TXT):
    name = dns.name.from_text(name)
    return dns.message.make_response(dns.message.make_query(name, t))


def output(message, ct="application/dns-json"):
    if ct == "application/dns-message":
        response = make_response(message.to_wire())
        response.headers.set("Content-Type", "application/dns-message")
        response.headers.set("Cloudflare-CDN-Cache-Control", f"public, max-age=${config.dns_cache_ttl}")
        return response
    elif ct == "application/x-javascript":
        return to_doh_simple(message)
    elif ct == "application/dns-json":
        return to_doh_simple(message)
    else:
        return to_doh_simple(message)

def cache_result(cache_key, result, ttl=30):
    rs = redis.Redis(host="localhost", port=6379, db=0)
    rs.set(cache_key, result)
    rs.expire(cache_key, ttl)

def sol_resolve(name):
    print(crayons.cyan("Resolving Solana Name: " + name), flush=True)
    cache_key = "sol:" + name
    rs = redis.Redis(host="localhost", port=6379, db=0)
    cached = rs.get(cache_key)
    if cached is not None:
        result = cached.decode("utf-8")
        log_successful_resolve("sol", name, result)
        print(crayons.green("Cache Hit: " + name + " -> " + result), flush=True)
        return result
    try:
        path = sns.resolve_ipfs_ipns(name)
    except Exception as e:
        print("Error resolving via Solana RPC: " + str(e), flush=True)
        print(traceback.format_exc(), flush=True)
        return None
    if path is not None:
        result = "dnslink=" + path
        log_successful_resolve("sol", name, result)
        enqueue_prewarm(result)
        cache_result(cache_key, result)
        return result
    return None


def fc_resolve(name):
    print(crayons.cyan("Resolving Farcaster Name: " + name), flush=True)
    # first: try bio with warpcast API
    api = "https://client.warpcast.com/v2/user-by-username?username="
    name = name.strip().lower()
    if name.endswith(".fc"):
        name = name[0:-3]
    cache_key = "furl:" + name
    rs = redis.Redis(host="localhost", port=6379, db=0)
    cached = rs.get(cache_key)
    if cached is not None:
        result = cached.decode("utf-8")
        log_successful_resolve("furl", name, result)
        print(crayons.green("Cache Hit: " + name + " -> " + result), flush=True)
        return result
    api = api + name
    r = requests.get(api)
    if r.status_code == 200:
        print("Found bio for " + name, flush=True)
        o = r.json()
        if "result" in o and "user" in o["result"] and "profile" in o["result"]["user"] and "bio" in o["result"]["user"]["profile"]:
            bio = o["result"]["user"]["profile"]["bio"]["text"]
            # find IPNS in bio
            ipns = re.compile(r"(k51[a-zA-Z0-9]{59})").search(bio)
            if ipns is not None:
                result = "dnslink=/ipns/" + ipns.group(1)
                log_successful_resolve("furl", name, result)
                enqueue_prewarm(result)
                cache_result(cache_key, result)
                return result
            # find CIDv0 in bio
            cidv0 = re.compile(r"(Qm[a-zA-Z0-9]{44})").search(bio)
            if cidv0 is not None:
                result = "dnslink=/ipfs/" + cidv0.group(1)
                log_successful_resolve("furl", name, result)
                enqueue_prewarm(result)
                cache_result(cache_key, result)
                return result
            # find CIDv1 in bio
            cidv1 = re.compile(r"(baf[a-zA-Z0-9]{56})").search(bio)
            if cidv1 is not None:
                result = "dnslink=/ipfs/" + cidv1.group(1)
                log_successful_resolve("furl", name, result)
                enqueue_prewarm(result)
                cache_result(cache_key, result)
                return result
    # second: try USER_DATA_TYPE=5 with Hub
    hub = config.farcaster_hub
    api = hub + "/v1/userNameProofByName?name=" + name
    r = requests.get(api)
    if r.status_code == 200:
        o = r.json()
        if "fid" in o:
            fid = o["fid"]
            print("Found fid for " + name + ": " + str(fid), flush=True)
            api = hub + "/v1/userDataByFid?fid=" + str(fid) + "&user_data_type=5"
            r = requests.get(api)
            if r.status_code == 200:
                o = r.json()
                print("Found user data for " + name + ": " + str(o), flush=True)
                if "messages" in o:
                    messages = o["messages"]
                else:
                    messages = [o]
                for message in messages:
                    if "data" in message and "userDataBody" in message["data"] and "value" in message["data"]["userDataBody"] and message["data"]["userDataBody"]["type"] == "USER_DATA_TYPE_URL":
                        value = message["data"]["userDataBody"]["value"]
                        # find IPNS in value
                        ipns = re.compile(r"(k51[a-zA-Z0-9]{59})").search(value)
                        if ipns is not None:
                            result = "dnslink=/ipns/" + ipns.group(1)
                            log_successful_resolve("furl", name, result)
                            enqueue_prewarm(result)
                            cache_result(cache_key, result)
                            return result
                        # find CIDv0 in value
                        cidv0 = re.compile(r"(Qm[a-zA-Z0-9]{44})").search(value)
                        if cidv0 is not None:
                            result = "dnslink=/ipfs/" + cidv0.group(1)
                            log_successful_resolve("furl", name, result)
                            enqueue_prewarm(result)
                            cache_result(cache_key, result)
                            return result
                        # find CIDv1 in value
                        cidv1 = re.compile(r"(baf[a-zA-Z0-9]{56})").search(value)
                        if cidv1 is not None:
                            result = "dnslink=/ipfs/" + cidv1.group(1)
                            log_successful_resolve("furl", name, result)
                            enqueue_prewarm(result)
                            cache_result(cache_key, result)
                            return result
    return None


def dotbit_resolve(name):
    indexer = "https://indexer-v1.did.id/v1/account/records"
    payload = {"account": name}
    r = requests.post(indexer, json=payload)
    if r.status_code == 200:
        o = r.json()
        print(r.text, flush=True)
        if "data" in o and o["data"] is not None and "records" in o["data"]:
            records = o["data"]["records"]
            for record in records:
                if record["key"] == "dweb.ipns":
                    return "dnslink=" + handle_ipns(record["value"])
                if record["key"] == "dweb.ipfs":
                    return "dnslink=/ipfs/" + record["value"]
    return None


def ens_resolve(name):
    resolver = "https://api.planetable.xyz/ens/resolve/"
    r = requests.get(resolver + name)
    if r.status_code == 200:
        o = r.json()
        if (
            "contentHash" in o
            and o["contentHash"] is not None
            and o["contentHash"] != ""
        ):
            content_hash = o["contentHash"]
            if content_hash.startswith("ipfs://"):
                return "dnslink=/ipfs/" + content_hash[len("ipfs://") :]
            if content_hash.startswith("ipns://"):
                ipns = str(content_hash[len("ipns://") :])
                result = handle_ipns(ipns)
                return "dnslink=" + result
    return None


@app.route("/dns-query", methods=["GET", "POST"])
def dns_query():
    if request.data:
        try:
            question = dns.message.from_wire(request.data)
            if question.question:
                q = question.question[0]
                print(str(q), flush=True)
                if q.rdtype == 16:
                    name = str(q.name)
                    if name.endswith(".bit."):
                        name = name[0:-1]
                    if name.endswith(".sol."):
                        name = name[0:-1]
                    if name.endswith(".fc."):
                        name = name[0:-1]
                    if name.endswith(".eth."):
                        name = name[0:-1]
                    if name.startswith("_dnslink."):
                        name = name[len("_dnslink.") :]
                    result = None
                    if name.endswith(".bit"):
                        result = dotbit_resolve(name)
                    if name.endswith(".sol"):
                        result = sol_resolve(name)
                    if name.endswith(".fc"):
                        result = fc_resolve(name)
                    if name.endswith(".eth"):
                        result = ens_resolve(name)
                    if result is not None:
                        print("Resolved: " + name + " -> " + result, flush=True)
                        response = dns.message.make_response(question)
                        response.answer.append(
                            dns.rrset.from_text(
                                q.name,
                                config.dns_cache_ttl,
                                dns.rdataclass.IN,
                                dns.rdatatype.TXT,
                                result,
                            )
                        )
                        return output(response, "application/dns-message")
            return output(make_empty_message())
        except Exception as e:
            print("Error: " + str(e), flush=True)
            print(traceback.format_exc(), flush=True)
            return output(make_empty_message())
    # get type
    t = request.values.get("type")
    if not t:
        # set default type to TXT
        t = dns.rdatatype.TXT
    try:
        t = dns.rdatatype.from_text(dns.rdatatype.to_text(int(t)))
    except:
        t = dns.rdatatype.TXT
    if t not in [dns.rdatatype.TXT, dns.rdatatype.A, dns.rdatatype.AAAA]:
        t = dns.rdatatype.TXT

    # get ct (content-type)
    ct = request.values.get("ct")
    ct_accept = request.headers.get("Accept")
    if not ct and not ct_accept:
        # set default ct to json
        ct = "application/x-javascript"
    elif not ct and ct_accept:
        ct = ct_accept
    else:
        if ct not in [
            "application/dns-message",
            "application/x-javascript",
            "application/dns-json",
        ]:
            ct = "application/x-javascript"

    # name is required
    name = request.values.get("name")
    if not name:
        print("Please provide a name", flush=True)
        return output(make_empty_message(t=t), ct)

    name = name.lower()
    if name.endswith(".bit."):
        name = name[0:-1]
    if name.endswith(".sol."):
        name = name[0:-1]
    if name.endswith(".fc."):
        name = name[0:-1]


    if t != dns.rdatatype.TXT:
        print("Type is not TXT: " + str(t), flush=True)
        return output(make_empty_message(name, t=t), ct)

    result = None
    if name.endswith(".bit"):
        result = dotbit_resolve(name)
    if name.endswith(".sol"):
        result = sol_resolve(name)
    if name.endswith(".fc"):
        result = fc_resolve(name)
    if name.endswith(".eth"):
        result = ens_resolve(name)
    if result is not None:
        print("Resolved: " + name + " -> " + result, flush=True)
        response = dns.message.make_response(dns.message.make_query(name, t))
        if ct == "application/dns-message":
            response.answer.append(
                dns.rrset.from_text(
                    name + ".", config.dns_cache_ttl, dns.rdataclass.IN, dns.rdatatype.TXT, result
                )
            )
        else:
            response.answer.append(
                dns.rrset.from_text(
                    name, config.dns_cache_ttl, dns.rdataclass.IN, dns.rdatatype.TXT, result
                )
            )
        return output(response, ct)
    print("Unsupported: name=" + str(name) + " / t=" + str(t), flush=True)
    return output(make_empty_message(name, t=t), ct)


def handle_ipns(ipns: str) -> str:
    r = redis.Redis(host="localhost", port=6379, db=0)
    r_key = "ipns:" + ipns + ":results"
    latest = r.zrevrange(r_key, 0, 0)
    if latest is not None and len(latest) > 0:
        enqueue_revalidate_ipns(ipns)
        return latest[0].decode("utf-8")
    else:
        return resolve_ipns(ipns)


def revalidate_ipns(ipns: str):
    value = resolve_ipns(ipns)
    print("Revalidated: " + ipns + " -> " + value, flush=True)
