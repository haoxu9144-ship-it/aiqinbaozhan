import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "src" / "ai_news_bot.py"
SPEC = importlib.util.spec_from_file_location("ai_news_bot", MODULE_PATH)
bot = importlib.util.module_from_spec(SPEC)
sys.modules["ai_news_bot"] = bot
assert SPEC.loader is not None
SPEC.loader.exec_module(bot)


NOW = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)


def sample_digest(count=4):
    return {
        "items": [
            {
                "headline": f"可信 AI 新闻 {index}",
                "what_happened": "某公司发布了经过来源确认的重要更新。",
                "why_important": "这会影响模型能力、开发者成本或行业竞争格局。",
                "published_at": (NOW - timedelta(hours=index)).isoformat(),
                "source_name": "官方来源",
                "source_url": f"https://example.com/news/{index}",
            }
            for index in range(1, count + 1)
        ],
        "watch": {"thing": "关注更新的实际可用范围", "reason": "官方后续文档会决定其真实影响。"},
    }


class ConfigTests(unittest.TestCase):
    def test_dry_run_only_requires_openai_key(self):
        config = bot.load_config({"OPENAI_API_KEY": "test", "DRY_RUN": "true"})
        self.assertTrue(config.dry_run)

    def test_live_mode_requires_publish_settings(self):
        with self.assertRaisesRegex(bot.AppError, "TELEGRAM_BOT_TOKEN"):
            bot.load_config({"OPENAI_API_KEY": "test", "DRY_RUN": "false"})

    def test_invalid_boolean_is_rejected(self):
        with self.assertRaisesRegex(bot.AppError, "true 或 false"):
            bot.load_config({"OPENAI_API_KEY": "test", "DRY_RUN": "maybe"})


class ValidationTests(unittest.TestCase):
    def test_valid_digest_and_title(self):
        digest = sample_digest()
        bot.validate_digest(digest, NOW)
        post = bot.format_post(digest, NOW)
        self.assertTrue(post.startswith("🔥 今日 AI 情报｜2026年9月2日"))
        self.assertIn("发生了什么：", post)
        self.assertIn("为什么重要：", post)
        self.assertLessEqual(bot.utf16_length(post), 4096)

    def test_rejects_less_than_four_items(self):
        with self.assertRaisesRegex(bot.AppError, "不满足 4～6 条"):
            bot.validate_digest(sample_digest(3), NOW)

    def test_rejects_old_news(self):
        digest = sample_digest()
        digest["items"][0]["published_at"] = (NOW - timedelta(hours=25)).isoformat()
        with self.assertRaisesRegex(bot.AppError, "不在过去 24 小时内"):
            bot.validate_digest(digest, NOW)

    def test_rejects_future_news(self):
        digest = sample_digest()
        digest["items"][0]["published_at"] = (NOW + timedelta(minutes=1)).isoformat()
        with self.assertRaisesRegex(bot.AppError, "不在过去 24 小时内"):
            bot.validate_digest(digest, NOW)

    def test_rejects_duplicate_sources(self):
        digest = sample_digest()
        digest["items"][1]["source_url"] = digest["items"][0]["source_url"]
        with self.assertRaisesRegex(bot.AppError, "重复来源"):
            bot.validate_digest(digest, NOW)

    def test_telegram_length_counts_emoji_as_two_units(self):
        self.assertEqual(bot.utf16_length("🔥a"), 3)


class ResponseTests(unittest.TestCase):
    def test_extract_output_text(self):
        payload = {
            "output": [
                {"type": "web_search_call"},
                {"type": "message", "content": [{"type": "output_text", "text": "{\"ok\":true}"}]},
            ]
        }
        self.assertEqual(bot.extract_output_text(payload), '{"ok":true}')

    def test_generate_requires_web_search_record(self):
        config = bot.RuntimeConfig("key", "", "", True, "model", "", "")
        response = {
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(sample_digest())}]}
            ],
        }
        with patch.object(bot, "request_json", return_value=response):
            with self.assertRaisesRegex(bot.AppError, "没有网页搜索记录"):
                bot.generate_digest(config, NOW)

    def test_extracts_and_canonicalizes_search_sources(self):
        payload = {
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [
                            {"type": "url", "url": "https://Example.com/news/1/#section"}
                        ]
                    },
                }
            ]
        }
        self.assertEqual(
            bot.extract_search_source_urls(payload),
            {"https://example.com/news/1"},
        )

    def test_telegram_send_is_never_automatically_retried(self):
        with patch.object(
            bot,
            "request_json",
            return_value={"ok": True, "result": {"message_id": 7}},
        ) as request:
            self.assertEqual(bot.publish_to_telegram("token", "@channel", "post"), 7)
            self.assertEqual(request.call_args.kwargs["retries"], 1)


class FlowTests(unittest.TestCase):
    def test_dry_run_never_sends_or_touches_state(self):
        config = bot.RuntimeConfig("key", "", "", True, "model", "", "")
        with (
            patch.object(bot, "generate_digest", return_value=sample_digest()),
            patch.object(bot, "publish_to_telegram") as send,
            patch.object(bot, "GitHubStateStore") as store,
        ):
            self.assertEqual(bot.run(config, NOW), 0)
            send.assert_not_called()
            store.assert_not_called()

    def test_existing_claim_skips_before_generation(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        fake_store = unittest.mock.Mock()
        fake_store.read.return_value = ({"date": "2026-09-02", "status": "published"}, "sha")
        with (
            patch.object(bot, "GitHubStateStore", return_value=fake_store),
            patch.object(bot, "generate_digest") as generate,
        ):
            self.assertEqual(bot.run(config, NOW), 0)
            generate.assert_not_called()

    def test_live_mode_claims_before_sending(self):
        config = bot.RuntimeConfig("key", "token", "@channel", False, "model", "gh", "owner/repo")
        fake_store = unittest.mock.Mock()
        fake_store.read.return_value = ({"date": None, "status": "never_published"}, "old-sha")
        fake_store.write.side_effect = ["claim-sha", "done-sha"]
        events = []

        def record_send(*_args):
            events.append("send")
            self.assertEqual(fake_store.write.call_count, 1)
            return 123

        with (
            patch.object(bot, "GitHubStateStore", return_value=fake_store),
            patch.object(bot, "generate_digest", return_value=sample_digest()),
            patch.object(bot, "publish_to_telegram", side_effect=record_send),
        ):
            self.assertEqual(bot.run(config, NOW), 0)
        self.assertEqual(events, ["send"])
        self.assertEqual(fake_store.write.call_count, 2)
        self.assertEqual(fake_store.write.call_args_list[0].args[0]["status"], "sending")
        self.assertEqual(fake_store.write.call_args_list[1].args[0]["status"], "published")


class RedactionTests(unittest.TestCase):
    def test_redacts_all_tokens(self):
        config = bot.RuntimeConfig(
            "sk-secret-openai-value",
            "123456:telegram_secret",
            "@channel",
            False,
            "model",
            "github-secret-value",
            "owner/repo",
        )
        raw = "sk-secret-openai-value 123456:telegram_secret github-secret-value Bearer abc.def"
        safe = bot.redact_secrets(raw, config)
        self.assertNotIn("secret", safe)
        self.assertNotIn("abc.def", safe)


if __name__ == "__main__":
    unittest.main()
