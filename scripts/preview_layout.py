"""Offline synthetic layout preview. Never queries APIs or publishes."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_ai_news_bot import sample_digest, NOW, bot

digest = sample_digest()
for item in digest["items"]:
    item["background"] = "【合成测试内容，非真实新闻】" + "这一段用于验证深度文档在较长中文背景下的自动换行与分页。实际运行时，机器人只会使用经过本次网页检索、带可靠来源及发布时间的事件，不会将此样例发布。" * 2
    item["what_happened"] = "【合成测试内容】这里放置明确的事件细节，实际日报中应包含公司、产品、更新内容和已确认的范围。未得到可靠证实的数据不写，也不通过推测补齐。"
path = ROOT / "output/pdf/aiqinbaozhan-layout-preview.pdf"
bot.build_pdf(digest, NOW.astimezone(bot.CHINA_TZ), path, demo=True)
print(path)
