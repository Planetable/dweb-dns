# Production piling issues (2026-10-24)

Host: `ssh ipfs` (DigitalOcean NYC3). Root disk `/` is 49 GB; the volume at `/mnt/volume_nyc3_ipfs_01` is 199 GB.
Times are UTC.

`.sol` lookups returned HTTP 500 from about 07:00 to 11:11. The root disk was full, so Redis couldn't save its snapshot and stopped accepting writes. Underneath that, several things had been piling up for weeks: Redis snapshots and their leftover temp files, a 4.3 M-job pinnable queue, a 32 K-job dweb queue, retries of targets that never succeed, and logs.

## Re-check

Run from this machine:

```bash
ssh ipfs '
df -h / /mnt/volume_nyc3_ipfs_01 | tail -2
redis-cli INFO persistence | grep -E "rdb_last_bgsave_status|rdb_last_save_time"
redis-cli INFO memory | grep "^used_memory_human"; echo "keys=$(redis-cli DBSIZE)"
ls -la /mnt/volume_nyc3_ipfs_01/redis /var/lib/redis
for q in pinnable dweb cid priority; do j=$(redis-cli LINDEX rq:queue:$q 0); echo "$q: queued=$(redis-cli LLEN rq:queue:$q) oldest=$(redis-cli HGET rq:job:$j created_at) failed=$(redis-cli ZCARD rq:failed:$q)"; done
for p in ivalice:pin-failure ivalice:check-failure ipns-failure gateway-fetch-failure; do echo "$p: $(redis-cli --scan --pattern "$p:*" | wc -l)"; done
echo "log=$(du -sh /var/log | cut -f1) journal=$(du -sh /var/log/journal | cut -f1) worker-logs=$(du -ch /var/log/rq-worker-* | tail -1 | cut -f1) rotated=$(ls /var/log/rq-worker-*.log.* | wc -l)"
supervisorctl status | grep -v RUNNING; echo "(lines above = programs not running)"
'
```

What healthy looks like, with the numbers measured at 13:13 after all of today's deploys:

| Check | Healthy | 13:13 |
|---|---|---|
| `/` usage | under 80% | 56% (22 GB free) |
| Volume usage | grows slowly | 40% |
| `rdb_last_bgsave_status` | `ok` | `ok` |
| `temp-*.rdb` files | none older than a few minutes | none |
| Redis memory | under ~500 MB | 4 MB, ~3.5 K keys, 0.9 MB snapshot |
| `rq:queue:pinnable` | a few hundred at most, oldest under ~30 min | 9, oldest 1 min |
| `rq:queue:dweb` | under ~200, oldest under 10 min | 0 |
| `rq:queue:cid`, `rq:queue:priority` | near 0 | 0 |
| `ivalice:pin-failure:*` | stable or shrinking | 17 |
| `ivalice:check-failure:*` | stable or shrinking | 12 (new counter, filling up) |
| `ipns-failure:*`, `gateway-fetch-failure:*` | stable | 0 (new counters) |
| `/var/log` | under ~4 GB | 3.4 GB |
| Rotated worker logs (`rq-worker-*.log.*`) | at most 2 per worker (26) | 20 |
| `supervisorctl status` | everything RUNNING | everything RUNNING |

The `oldest=` column can come back empty for `dweb`. The job at the head of the queue may already have expired (`ttl`), and that's fine.

## 1. Redis snapshots filled the root disk

- **What piled up:**
  - 8 orphaned `temp-*.rdb` files in `/var/lib/redis` from failed background saves (May 2–11), 9.1 GB in total.
  - A 2.9 GB `dump.rdb` on a 49 GB root disk.
  - At 96% full, Redis couldn't write its snapshot (`No space left on device`). With `stop-writes-on-bgsave-error yes` it then refused all writes (`MISCONF`).
- **Impact:** every `.sol` lookup returned 500, because Redis writes in `sol_resolve` weren't caught. All RQ workers crashed, and supervisor marked them FATAL.
- **Fix:**
  - Deleted the orphaned temp files, so the next save succeeded and writes resumed at 11:11.
  - Moved the snapshot directory to the volume. `/etc/redis/redis.conf` now has `dir /mnt/volume_nyc3_ipfs_01/redis`.
  - The drop-in `/etc/systemd/system/redis-server.service.d/rdb-on-volume.conf` makes that directory writable for Redis (whose systemd unit has `ReadOnlyDirectories=/`). It also has `RequiresMountsFor=`, so Redis won't start if the volume isn't mounted.
  - Deleted the old `/var/lib/redis/dump.rdb`.
  - dweb-dns `79115aa` and `eb0aad6`: Redis write and read errors are now logged and treated as a cache miss instead of failing the lookup.
- **Watch for:**
  - `rdb_last_bgsave_status:err`.
  - New `temp-*.rdb` files that stay around, in either directory.
  - The volume filling up.

## 2. pinnable queue: 4.3 M duplicate check jobs

- **What piled up:**
  - 4,297,514 queued jobs (`check_website` and `pin_website`) in `rq:queue:pinnable`, the oldest from Sep 1.
  - About 1.8 KB each and no expiry, so about 6 GB of Redis's 6.27 GB. That's what made the snapshot too big for the disk.
- **Cause:**
  - Cron calls `/_cron` every 2 minutes. It enqueued a check for all 101 websites with no regard for checks still waiting.
  - The workers ran slightly slower than that and fell 53 days behind.
- **Fix:**
  - Cleared the stale jobs (143 s, in batches).
  - pinnable `cded47e`: `/_cron` skips a website while its previous check is still queued or running. The key is `ivalice:cron-check-job:<id>`.
  - pinnable `69676a6`: hardened the manual Pin handler.
    - It checks the website exists and belongs to the user.
    - The pin waits for the check (an RQ dependency with `allow_failure`).
    - Repeat clicks reuse the pin already in flight.
    - Shared helpers live in `galaxy/utils/jobs.py`.
- **Related:**
  - The pinnable, cid and priority workers had been running `/www/staging`, code from May 2025. That's why the pin-failure backoff from `38ef59a` never ran (0 keys).
    - Moved them to `/www/pinnable` (`directory=` in `/etc/supervisor/conf.d/pinnable.conf`).
    - The staging web app itself stays on `/www/staging`.
  - `rq-worker-priority` was defined but never loaded into supervisor; it's now running.
- **Watch for:**
  - `rq:queue:pinnable` growing past a few hundred.
  - The oldest pinnable job older than ~30 min.
  - About 101 `ivalice:cron-check-job:*` keys is normal: one per website.

## 3. dweb prewarm queue: 32 K stale jobs

- **What piled up:** 32,316 jobs in `rq:queue:dweb`, the oldest from Oct 1: 28,350 `prewarm`, 3,816 `revalidate_ipns` and 150 `request_ipfs_hash`.
- **Cause:**
  - IPNS resolves often run into the 30-second timeout, so two workers fell behind about 126 new jobs an hour.
  - Jobs had no expiry, so the backlog only ever grew.
- **Fix:**
  - Cleared 32,278 stale jobs.
  - dweb-dns `4767a5e`: prewarm, revalidation and gateway-fetch jobs are enqueued with `ttl=600` (`queued_job_ttl_seconds`). RQ drops any job that hasn't started within 10 minutes.
- **Watch for:**
  - `rq:queue:dweb` above a few hundred, or holding jobs older than 10 minutes. If either shows up, the ttl isn't being applied.

## 4. Endless retries of targets that never succeed

- **What piled up:** retries of things that can't succeed.
  - **pinnable:** 17 ENS sites fail every pin with HTTP 500 "could not resolve name".
    - Their `.eth` → IPNS step works, through eth.limo in under a second.
    - The IPNS record itself is gone. Neither our Kubo 0.15 nor public delegated routing (`delegated-ipfs.dev`) can find it. The publishers stopped republishing; these sites last pinned between 2023 and Sep 18, 2026.
    - Failed resolves in `check_website` weren't counted at all, so cron re-checked those sites every 2 minutes.
  - **dweb-dns:** an IPNS name that doesn't resolve was revalidated every 10 minutes and prewarmed hourly, each attempt holding a worker for 30–60 s.
- **Fix:** your rule. Anything that keeps failing is retried at most twice a day: once 15 minutes after the first failure, then every 12 hours until it succeeds, and success clears the record.
  - pinnable `00adeda`:
    - Separate counters `ivalice:pin-failure:<id>` and `ivalice:check-failure:<id>`.
    - Cron and the website page view skip a site while either counter is in backoff.
    - The failure reason keeps Kubo's message.
    - Not limited: the manual Pin button and the pin API.
  - dweb-dns `c11785b`:
    - Failure records `ipns-failure:<sha>` and `gateway-fetch-failure:<sha>`.
    - Revalidation, prewarm and gateway-fetch enqueues skip targets in backoff.
    - Live DNS lookups still resolve; they only record the failure.
- **Sites failing at the time:**
  - Website IDs 16, 24, 36, 39, 40, 41, 50, 56, 83, 99, 100, 106, 107, 108, 109, 119, 135.
  - Names: pwrplnt.eth, alisap.eth, bannys.eth (×2), earlieraccess.eth, token.bruno.eth, aeolian.eth, 0xaria.eth, xiaorun.eth, airwolfspace.eth and its subdomains kai, k, blog and 60, clockmini.eth, weathermini.eth, extrawdw.eth.
  - Their last pinned CIDs stay pinned.
  - At 13:14 these 17 still had a 24 h backoff (`backoff_seconds 86400`). That was set under the old 15 min / 1 h / 24 h schedule, when leftover duplicate pin jobs pushed the count up quickly. Their next failure gets 12 h.
  - The new check counter caught 12 more sites within minutes of the deploy:
    - 10 IPNS sites whose resolve times out after 30 s;
    - `sankin.eth` and `jbxcn.eth`, whose ENS names have no DNSLink record.
- **Watch for:**
  - The failure-key counts climbing steadily.
  - A `backoff_seconds` other than 900 or 43200.
  - The same site being retried more than twice a day. Its worker log lines would show it.

## 5. Root disk: logs, journal and leftovers

`/` went from 96% to 56%.

| Item | Change | Freed |
|---|---|---|
| Orphaned Redis temp files | deleted | 9.1 GB |
| Old `/var/lib/redis/dump.rdb` | deleted | 2.7 GB |
| RQ worker logs | `stdout_logfile_backups=2` on all 13 workers (`pinnable.conf`, `dweb-dns.conf`), old rotations deleted | 3.4 GB |
| systemd journal | `SystemMaxUse=500M` in `/etc/systemd/journald.conf.d/size.conf`, vacuumed, and a 2022 journal under an old machine ID removed | 2.7 GB |
| `/root/.vscode-server` | kept only the most recent build | 1.4 GB |
| `/var/log/pinning.log` | logrotate weekly with 4 compressed copies (`/etc/logrotate.d/pinning`); the existing 433 MB file is now 6 MB | 0.4 GB |

Already capped, so these don't grow past their limits:
- the nginx cache in `/data/s` (`max_size=8g`);
- `pinnable.log` and `dweb-dns.log` (supervisor defaults).

## Other changes today

- **dweb-dns:**
  - `712f27f` removed LogSnag; the LogSnag keys were also deleted from `config.py` here and on `ipfs`.
  - `b229027` added `.sns` support, sharing accounts with `.sol` and cached under `sns:` keys.
- **pinnable:** pushed the previously unpushed `38ef59a` (pin-failure backoff) to GitHub.

## Open items

- `/etc/supervisor/conf.d/pinnable.conf.*.pre-worker-dir.bak` is still on `ipfs`. Delete it once the worker move is trusted.
- **No alerting.** The 500s went unnoticed for about 4 hours. Add a cron check on `sepia.sol` returning 200, and a disk-space alert. Netdata is installed, but nobody has checked whether its alerts go anywhere.
- **Redis has no memory limit** (`maxmemory` is 0). A limit would turn a runaway queue into failed writes, which both apps now tolerate.
- **No Redis socket timeouts in dweb-dns.** A Redis that accepts connections but never answers would hang requests.
- **pinnable:**
  - ENS sites are pinned twice per manual pin, because `check_website` enqueues its own pin. This is now safe to fix, since the workers run current code.
  - The Pin button skips the `dwb_balance > 0` check that the pin API enforces. That's a policy decision.
  - The staging app (port 23456) runs 2025 code against production's database, Redis and queues. It's rarely used; the last request was Oct 23.
- **dweb-dns:**
  - A live lookup for a dead IPNS name still waits up to 30 s for Kubo.
  - Kubo is 0.15.0 (2022).
- **`.sol` and SRS:** SNS renamed existing `.sol` names to `.sns`. `.sol` is moving to the Solana Record Service (SRS), with activation targeted for Q4 2026 to Q1 2027. When that lands, revisit what `.sol` resolves to. Kubo users need a `"sns."` resolver entry to use `.sns` through this server.
