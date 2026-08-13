import json
import tempfile
import time
import unittest
from pathlib import Path

from codex_lcd import ClaudeUsage, normalize_window, parse_rollout, read_usage_cache, select_quota_windows


class ParserTests(unittest.TestCase):
    def test_parse_rollout(self):
        records = [
            {"type": "session_meta", "payload": {"cwd": "D:/codes/esp32", "model": "gpt-test"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 1234}}, "rate_limits": None}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-test.jsonl"
            path.write_text("\n".join(json.dumps(x) for x in records), encoding="utf-8")
            result = parse_rollout(path)
        self.assertEqual(result["model"], "gpt-test")
        self.assertEqual(result["workspace"], "esp32")
        self.assertEqual(result["tokens"], 1234)

    def test_usage_cache_five_hour_window(self):
        cache = {
            "schema_version": 2,
            "primary": {"pct": 40, "window_secs": 18000, "resets_at": "2030-01-01T00:00:00Z"},
            "secondary": {"pct": 25, "window_secs": 604800, "resets_at": "2030-01-02T00:00:00Z"},
        }
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "usage-limits.json").write_text(json.dumps(cache), encoding="utf-8")
            limits = read_usage_cache(home)
        five, week = select_quota_windows(limits)
        self.assertEqual(normalize_window(five)[0], 60)
        self.assertEqual(normalize_window(week)[0], 75)
    def test_claude_remaining_percent(self):
        self.assertEqual(ClaudeUsage._remaining({"utilization": 37}), 63)
        self.assertIsNone(ClaudeUsage._remaining(None))
    def test_normalize_window(self):
        remaining, reset = normalize_window({"usedPercent": 25, "resetsAt": time.time() + 3700})
        self.assertEqual(remaining, 75)
        self.assertRegex(reset, r"^01:0[01]$")


if __name__ == "__main__":
    unittest.main()


