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

if __name__ == "__main__":
    unittest.main()
