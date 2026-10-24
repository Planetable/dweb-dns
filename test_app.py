import time
import unittest
from unittest import mock
import dns.message
import redis
import prewarm
from app import app, enqueue_prewarm, enqueue_revalidate_ipns, q

class BasicTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        app.config['DEBUG'] = False
        self.client = app.test_client()

    def tearDown(self):
        pass

    def test_sol_ohlife(self):
        response = self.client.get('/dns-query?name=ohlife.sol')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'/ipns/k51qzi5uqu5dhbdxrvoeqy96c3krp2t1d9hfscjyn2mwts5f1w8f1sq99xwh3r', response.data)

    def test_sns_ohlife(self):
        response = self.client.get('/dns-query?name=ohlife.sns')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'/ipns/k51qzi5uqu5dhbdxrvoeqy96c3krp2t1d9hfscjyn2mwts5f1w8f1sq99xwh3r', response.data)

    def test_sns_ohlife_wire(self):
        query = dns.message.make_query('_dnslink.ohlife.sns', 'TXT')
        response = self.client.post('/dns-query', data=query.to_wire(), content_type='application/dns-message')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'/ipns/k51qzi5uqu5dhbdxrvoeqy96c3krp2t1d9hfscjyn2mwts5f1w8f1sq99xwh3r', response.data)

    def test_sol_survives_redis_write_errors(self):
        # Redis refuses writes (MISCONF) when it can't persist; lookups must still answer.
        misconf = redis.exceptions.ResponseError("MISCONF Redis is configured to save RDB snapshots")
        with mock.patch.object(redis.Redis, 'get', return_value=None):
            with mock.patch.object(redis.Redis, 'set', side_effect=misconf):
                found = self.client.get('/dns-query?name=ohlife.sol')
                missing = self.client.get('/dns-query?name=zz-no-such-name-71f3.sol')
        self.assertEqual(found.status_code, 200)
        self.assertIn(b'/ipns/k51qzi5uqu5dhbdxrvoeqy96c3krp2t1d9hfscjyn2mwts5f1w8f1sq99xwh3r', found.data)
        self.assertEqual(missing.status_code, 200)

    def test_sol_survives_redis_down(self):
        # Every Redis command fails, reads included, as when the server is unreachable.
        refused = redis.exceptions.ConnectionError("Error 111 connecting to localhost:6379. Connection refused.")
        with mock.patch.object(redis.Redis, 'execute_command', side_effect=refused):
            for name in ['ohlife.sol', 'ohlife.sns']:
                response = self.client.get('/dns-query?name=' + name)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'/ipns/k51qzi5uqu5dhbdxrvoeqy96c3krp2t1d9hfscjyn2mwts5f1w8f1sq99xwh3r', response.data)
            missing = self.client.get('/dns-query?name=zz-no-such-name-71f3.sol')
        self.assertEqual(missing.status_code, 200)

    def test_queued_jobs_expire(self):
        # Without a ttl, jobs nobody got to piled up in the dweb queue for weeks.
        for queue, enqueue in [(q, lambda: enqueue_prewarm('dnslink=/ipfs/bafytest')),
                               (q, lambda: enqueue_revalidate_ipns('k51test')),
                               (prewarm.q, lambda: prewarm.load_ipfs_hash('/ipfs/bafytest'))]:
            with mock.patch.object(queue.connection, 'set', return_value=True):
                with mock.patch.object(queue, 'enqueue') as enqueued:
                    enqueue()
            self.assertTrue(enqueued.called)
            for call in enqueued.call_args_list:
                self.assertEqual(call.kwargs['ttl'], 600)

    def test_failing_targets_retry_twice_a_day(self):
        ipns, url = 'k51-test-always-fails', 'https://dweb.link/ipns/k51-test-always-fails'
        try:
            # An IPNS name that doesn't resolve: retried after 15 minutes, then every 12 hours.
            failed = mock.Mock(status_code=500, text='{"Message":"could not resolve name"}')
            with mock.patch.object(prewarm.requests, 'post', return_value=failed):
                self.assertEqual(prewarm.resolve_ipns(ipns), '/ipns/' + ipns)
            key = prewarm._lock_key('ipns-failure', ipns)
            self.assertAlmostEqual(int(prewarm.q.connection.hget(key, 'backoff_until')) - time.time(), 15 * 60, delta=5)
            prewarm.record_failure('ipns', ipns)
            self.assertAlmostEqual(int(prewarm.q.connection.hget(key, 'backoff_until')) - time.time(), 12 * 3600, delta=5)
            with mock.patch.object(q.connection, 'set', return_value=True):
                with mock.patch.object(q, 'enqueue') as enqueued:
                    enqueue_revalidate_ipns(ipns)
                    enqueue_prewarm('dnslink=/ipns/' + ipns)
            self.assertFalse(enqueued.called)
            resolved = mock.Mock(status_code=200, text='{"Path":"/ipfs/bafytest"}\n')
            with mock.patch.object(prewarm.requests, 'post', return_value=resolved):
                prewarm.resolve_ipns(ipns)
            self.assertFalse(prewarm.in_failure_backoff('ipns', ipns))

            # A gateway fetch that times out stops being enqueued.
            with mock.patch.object(prewarm.requests, 'get', side_effect=prewarm.requests.exceptions.Timeout()):
                prewarm.request_ipfs_hash(url)
            self.assertTrue(prewarm.in_failure_backoff('gateway-fetch', url))
            with mock.patch.object(prewarm.q.connection, 'set', return_value=True):
                with mock.patch.object(prewarm.q, 'enqueue') as enqueued:
                    prewarm.load_ipfs_hash('/ipns/' + ipns)
            # Other gateways are still prewarmed; only the failing URL is skipped.
            self.assertNotIn(url, [call.args[1] for call in enqueued.call_args_list])
        finally:
            prewarm.clear_failure('ipns', ipns)
            prewarm.clear_failure('gateway-fetch', url)
            prewarm.q.connection.delete('ipns:' + ipns + ':results')

if __name__ == "__main__":
    unittest.main()
