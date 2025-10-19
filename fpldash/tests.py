from django.test import TestCase, Client


class SmokeTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_index_page_renders(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        # Check that the main table header text exists in the page
        self.assertIn(b"FPL Top Players", resp.content)

