import json
import unittest

from monitor import classify_post, merge_posts, parse_feed, parse_radar_json


class ClassifierTests(unittest.TestCase):
    def assert_kind(self, text: str, expected: str) -> None:
        self.assertEqual(classify_post(text).kind, expected, text)

    def test_explicit_future_reset(self):
        self.assert_kind("I will reset usage limits this evening", "PREANNOUNCEMENT")

    def test_reset_incoming(self):
        self.assert_kind("reset incoming", "PREANNOUNCEMENT")

    def test_reset_will_land(self):
        self.assert_kind("The reset will land in the next hour for paid users", "PREANNOUNCEMENT")

    def test_resetting_limits_tomorrow(self):
        self.assert_kind("Resetting the limits tomorrow morning to celebrate.", "PREANNOUNCEMENT")

    def test_full_reset_tomorrow(self):
        self.assert_kind("Full reset for Codex users tomorrow", "PREANNOUNCEMENT")

    def test_completed_propagated(self):
        self.assert_kind("The reset has been propagated to paid users", "COMPLETED")

    def test_completed_limits(self):
        self.assert_kind("Usage limits have been reset", "COMPLETED")

    def test_context_but_unclear(self):
        self.assert_kind("Looking into a Codex usage limit reset", "AMBIGUOUS")

    def test_unrelated_reset(self):
        self.assert_kind("I reset my password", "IRRELEVANT")

    def test_no_reset(self):
        self.assert_kind("Shipping a nice Codex improvement today", "IRRELEVANT")


class SourceParserTests(unittest.TestCase):
    def test_parse_rss(self):
        raw = b'''<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Resetting the limits tomorrow morning to celebrate.</title>
        <link>https://x.com/thsottiaux/status/2060964284117782996</link>
        <guid>https://x.com/thsottiaux/status/2060964284117782996</guid>
        <pubDate>Sun, 31 May 2026 10:00:00 GMT</pubDate></item>
        </channel></rss>'''
        posts = parse_feed(raw, "thsottiaux", "https://example.test/feed.xml")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["id"], "2060964284117782996")
        self.assertIn("Resetting", posts[0]["text"])
        self.assertEqual(posts[0]["created_at"], "2026-05-31T10:00:00Z")

    def test_parse_atom(self):
        raw = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>reset incoming</title><id>https://x.com/thsottiaux/status/2099999999999999999</id>
        <link href="https://x.com/thsottiaux/status/2099999999999999999" />
        <updated>2026-08-28T00:00:00Z</updated></entry></feed>'''
        posts = parse_feed(raw, "thsottiaux", "https://example.test/feed.xml")
        self.assertEqual(posts[0]["id"], "2099999999999999999")

    def test_parse_radar_schema_agnostic(self):
        data = {
            "signals": [
                {
                    "source": "https://x.com/thsottiaux/status/2060964284117782996",
                    "text": "Five million users would agree. Resetting the limits tomorrow morning to celebrate.",
                    "date": "2026-05-31T10:00:00Z",
                }
            ]
        }
        posts = parse_radar_json(data, "thsottiaux", "https://codexradar.test/current.json")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["id"], "2060964284117782996")
        self.assertIn("Resetting", posts[0]["text"])

    def test_radar_ignores_other_user(self):
        data = {"text": "reset tomorrow", "url": "https://x.com/sama/status/2060964284117782997"}
        self.assertEqual(parse_radar_json(data, "thsottiaux", "source"), [])

    def test_merge_dedupes(self):
        a = [{"id": "2", "text": "short", "created_at": None, "source": "a"}]
        b = [{"id": "2", "text": "a much longer reset text", "created_at": "2026-01-01T00:00:00Z", "source": "b"}]
        merged = merge_posts(a, b)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["text"], "a much longer reset text")
        self.assertEqual(merged[0]["created_at"], "2026-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
