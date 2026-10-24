import unittest
import dns.message
from app import app

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

if __name__ == "__main__":
    unittest.main()
