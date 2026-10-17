import unittest

from bot import authorized
from codex_limits import format_limits


class BotTests(unittest.TestCase):
    def test_only_owner_private_chat_is_authorized(self):
        self.assertTrue(authorized({"from": {"id": 42}, "chat": {"id": 42, "type": "private"}}, 42))
        self.assertFalse(authorized({"from": {"id": 43}, "chat": {"id": 43, "type": "private"}}, 42))
        self.assertFalse(authorized({"from": {"id": 42}, "chat": {"id": -42, "type": "group"}}, 42))
        self.assertFalse(authorized({}, 42))

    def test_multiple_limit_buckets_and_missing_windows(self):
        text = format_limits({"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1730947200}},
            "other": {"secondary": {"usedPercent": 90, "windowDurationMins": 10080}},
        }})
        self.assertIn("осталось 75%", text)
        self.assertIn("осталось 10%", text)
        self.assertIn("10080 мин", text)
        self.assertIn("неизвестно", text)

    def test_single_bucket_fallback(self):
        text = format_limits({"rateLimits": {"primary": {"usedPercent": 100}}})
        self.assertIn("осталось 0%", text)

    def test_missing_quota_does_not_claim_full_balance(self):
        text = format_limits({"rateLimits": {"primary": None}})
        self.assertIn("не предоставлены", text)
        self.assertNotIn("100%", text)


if __name__ == "__main__":
    unittest.main()
