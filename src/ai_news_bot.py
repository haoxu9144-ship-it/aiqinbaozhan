#!/usr/bin/env python3
"""Generate a sourced Chinese AI digest and publish it once to Telegram."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
OPENAI_API_URL = "https://api.openai.com/v1/responses"
TELEGRAM_TEXT_LIMIT = 4096
DEFAULT_MODEL = "gpt-5.6-luna"
STATE_PATH = "state/publish-state.json"
MAX_RETRIES = 3


def configure_stdio() -> None:
    """Keep Chinese text and emoji readable on Windows and in CI logs."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


configure_stdio()


class AppError(RuntimeError):
    """An expected, safely reportable application error."""


@dataclass(frozen=True)
class RuntimeConfig:
    openai_api_key: str
    telegram_bot_token: str
    telegram_channel: str
    dry_run: bool
    model: str
    github_token: str
    github_repository: str


def parse_bool(value: str | None, *, default: bool = True) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AppError("DRY_RUN 必须是 true 或 false。")


def load_config(env: dict[str, str] | None = None) -> RuntimeConfig:
    values = os.environ if env is None else env
    dry_run = parse_bool(values.get("DRY_RUN"), default=True)
    config = RuntimeConfig(
        openai_api_key=values.get("OPENAI_API_KEY", "").strip(),
        telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_channel=values.get("TELEGRAM_CHANNEL", "").strip(),
        dry_run=dry_run,
        model=values.get("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        github_token=values.get("GITHUB_TOKEN", "").strip(),
        github_repository=values.get("GITHUB_REPOSITORY", "").strip(),
    )
    if not config.openai_api_key:
        raise AppError("缺少 OPENAI_API_KEY。请在 GitHub Actions Secret 中配置。")
    if not dry_run:
        missing = []
        if not config.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not config.telegram_channel:
            missing.append("TELEGRAM_CHANNEL")
        if not config.github_token:
            missing.append("GITHUB_TOKEN（由 GitHub Actions 自动提供）")
        if not config.github_repository:
            missing.append("GITHUB_REPOSITORY（由 GitHub Actions 自动提供）")
        if missing:
            raise AppError("真实发布模式缺少配置：" + "、".join(missing))
    return config


def utf16_length(text: str) -> int:
    """Telegram measures message length in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def json_schema() -> dict[str, Any]:
    news_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string"},
            "what_happened": {"type": "string"},
            "why_important": {"type": "string"},
            "published_at": {"type": "string"},
            "source_name": {"type": "string"},
            "source_url": {"type": "string"},
        },
        "required": [
            "headline",
            "what_happened",
            "why_important",
            "published_at",
            "source_name",
            "source_url",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {"type": "array", "items": news_item},
            "watch": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "thing": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["thing", "reason"],
            },
        },
        "required": ["items", "watch"],
    }


def build_prompt(now: datetime) -> str:
    window_start = now - timedelta(hours=24)
    start_text = window_start.astimezone(timezone.utc).isoformat(timespec="seconds")
    end_text = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    china_date = now.astimezone(CHINA_TZ).strftime("%Y-%m-%d")
    return f"""
你是中文科技频道“AI情报站”的严谨主编。请先使用网页搜索，再筛选新闻。

时间窗口：{start_text} 至 {end_text}（UTC）；中国日期：{china_date}。

任务：只选出时间窗口内真正重要、可信且适合公开发布的 4～6 条 AI 新闻。范围仅限：
OpenAI、Anthropic、Google、Meta、AI 模型、Agent、AI 工具、芯片/算力、融资/商业化、
AI 创业机会、重要政策。

硬性事实规则：
1. 每条都必须有可直接打开的权威来源 URL 和明确发布时间；published_at 必须是带时区的 ISO 8601。
2. 优先公司官网、政府文件、论文原文等一手来源；重大商业新闻可用 Reuters、AP、Bloomberg、
   Financial Times、Wall Street Journal 等可靠媒体。
3. 必须确认 published_at 落在上述 24 小时窗口内。旧闻、汇总文、观点文、传闻、泄露、
   无法确认发布时间或无法交叉核实的内容一律不写。
4. 不得编造新闻、数字、人物、产品更新或 URL；不要把推测写成事实。
5. 用自己的中文概括，不照抄标题或正文。语言专业、简洁、高信息密度，不夸张、不标题党。
6. what_happened 回答“发生了什么”；why_important 回答“为什么重要”，避免空话和重复。
7. source_url 必须逐字复制你实际搜索并阅读过的那篇来源页面 URL。
8. 如果严格筛选后不足 4 条，不得用低质量内容凑数；此时仍按事实返回实际条目，程序会明确失败，
   以避免发布不合格内容。
9. “今日值得关注的一件事”应从入选新闻提炼一个最值得继续跟踪的具体事项，不新增未经来源支持的事实。

仅按给定 JSON Schema 返回，不要输出 Markdown 或额外说明。
""".strip()


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 120,
    retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = {"Accept": "application/json", "User-Agent": "aiqinbaozhan/1.0"}
    if encoded is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, data=encoded, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                parsed = json.loads(payload)
                if not isinstance(parsed, dict):
                    raise AppError("远程接口返回了非对象 JSON。")
                return parsed
        except urllib.error.HTTPError as exc:
            response_text = exc.read().decode("utf-8", errors="replace")[:1200]
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < retries:
                time.sleep(2 ** (attempt - 1))
                continue
            detail = extract_remote_error(response_text)
            raise AppError(f"远程接口 HTTP {exc.code}：{detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
                continue
            raise AppError(f"远程接口网络错误：{exc.reason if hasattr(exc, 'reason') else exc}") from None
        except json.JSONDecodeError:
            raise AppError("远程接口没有返回有效 JSON。") from None
    raise AppError("远程接口请求失败。")


def extract_remote_error(text: str) -> str:
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"][:500]
            if isinstance(payload.get("description"), str):
                return payload["description"][:500]
    except json.JSONDecodeError:
        pass
    return "请求失败，远程服务未提供可用错误说明。"


def generate_digest(config: RuntimeConfig, now: datetime) -> dict[str, Any]:
    payload = {
        "model": config.model,
        "instructions": "严格执行事实核查、时间窗口和 JSON 输出要求。",
        "input": build_prompt(now),
        "tools": [
            {
                "type": "web_search_preview",
                "search_context_size": "high",
                "user_location": {
                    "type": "approximate",
                    "country": "CN",
                    "timezone": "Asia/Shanghai",
                },
            }
        ],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
        "reasoning": {"effort": "medium"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "daily_ai_brief",
                "strict": True,
                "schema": json_schema(),
            }
        },
        "max_output_tokens": 5000,
        "store": False,
    }
    response = request_json(
        OPENAI_API_URL,
        method="POST",
        headers={"Authorization": f"Bearer {config.openai_api_key}"},
        body=payload,
    )
    if response.get("status") != "completed":
        detail = response.get("error") or response.get("incomplete_details") or response.get("status")
        raise AppError(f"OpenAI 内容生成未完成：{detail}")
    output = response.get("output")
    if not isinstance(output, list) or not any(
        isinstance(item, dict) and item.get("type") == "web_search_call" for item in output
    ):
        raise AppError("OpenAI 响应中没有网页搜索记录；为避免无来源内容，停止发布。")
    text = extract_output_text(response)
    try:
        digest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(f"OpenAI 输出不是有效 JSON：第 {exc.lineno} 行第 {exc.colno} 列。") from None
    searched_urls = extract_search_source_urls(response)
    if not searched_urls:
        raise AppError("网页搜索没有返回可核验的来源 URL；为避免无来源内容，停止发布。")
    validate_digest(digest, now, searched_urls=searched_urls)
    return digest


def extract_output_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "output_text":
                value = part.get("text")
                if isinstance(value, str):
                    chunks.append(value)
    if not chunks:
        raise AppError("OpenAI 响应中没有可用文本。")
    return "".join(chunks)


def canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host + port, path, parsed.query, ""))


def extract_search_source_urls(response: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        sources = action.get("sources", []) if isinstance(action, dict) else []
        for source in sources:
            url = source.get("url") if isinstance(source, dict) else None
            if isinstance(url, str):
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme in {"http", "https"} and parsed.netloc:
                    urls.add(canonical_url(url))
    return urls


def parse_published_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AppError("新闻缺少 published_at。")
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise AppError(f"新闻发布时间不是有效 ISO 8601：{value}") from None
    if parsed.tzinfo is None:
        raise AppError(f"新闻发布时间缺少时区：{value}")
    return parsed


def validate_digest(
    digest: Any,
    now: datetime,
    *,
    searched_urls: set[str] | None = None,
) -> None:
    if not isinstance(digest, dict):
        raise AppError("生成结果不是 JSON 对象。")
    items = digest.get("items")
    if not isinstance(items, list) or not 4 <= len(items) <= 6:
        actual = len(items) if isinstance(items, list) else 0
        raise AppError(f"严格筛选后得到 {actual} 条新闻，不满足 4～6 条要求，停止发布。")
    window_start = now - timedelta(hours=24)
    seen_urls: set[str] = set()
    required_text_fields = ("headline", "what_happened", "why_important", "source_name")
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise AppError(f"第 {index} 条新闻结构无效。")
        for field in required_text_fields:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise AppError(f"第 {index} 条新闻缺少字段 {field}。")
        published_at = parse_published_at(item.get("published_at")).astimezone(timezone.utc)
        if published_at < window_start.astimezone(timezone.utc) or published_at > now.astimezone(timezone.utc):
            raise AppError(f"第 {index} 条新闻发布时间不在过去 24 小时内，停止发布。")
        source_url = item.get("source_url")
        if not isinstance(source_url, str):
            raise AppError(f"第 {index} 条新闻缺少来源 URL。")
        parsed_url = urllib.parse.urlparse(source_url.strip())
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise AppError(f"第 {index} 条新闻的来源 URL 无效。")
        normalized_url = source_url.strip().rstrip("/")
        if normalized_url in seen_urls:
            raise AppError(f"第 {index} 条新闻与其他条目使用了重复来源 URL。")
        seen_urls.add(normalized_url)
        if searched_urls is not None and canonical_url(source_url) not in searched_urls:
            raise AppError(f"第 {index} 条新闻的来源 URL 不在本次网页搜索结果中，停止发布。")
    watch = digest.get("watch")
    if not isinstance(watch, dict) or not all(
        isinstance(watch.get(field), str) and watch[field].strip() for field in ("thing", "reason")
    ):
        raise AppError("生成结果缺少“今日值得关注的一件事”。")


def clean_text(value: Any) -> str:
    text = str(value).strip()
    return re.sub(r"\s+", " ", text)


def format_post(digest: dict[str, Any], now: datetime) -> str:
    local = now.astimezone(CHINA_TZ)
    lines = [f"🔥 今日 AI 情报｜{local.year}年{local.month}月{local.day}日", ""]
    for index, item in enumerate(digest["items"], start=1):
        lines.extend(
            [
                f"{index}. {clean_text(item['headline'])}",
                f"发生了什么：{clean_text(item['what_happened'])}",
                f"为什么重要：{clean_text(item['why_important'])}",
                f"来源：{clean_text(item['source_name'])} {item['source_url'].strip()}",
                "",
            ]
        )
    watch = digest["watch"]
    lines.extend(
        [
            "今日值得关注的一件事",
            clean_text(watch["thing"]),
            f"关注理由：{clean_text(watch['reason'])}",
        ]
    )
    post = "\n".join(lines).strip()
    length = utf16_length(post)
    if length > TELEGRAM_TEXT_LIMIT:
        raise AppError(f"生成帖子长度为 {length}，超过 Telegram 的 {TELEGRAM_TEXT_LIMIT} 字限制。")
    return post


class GitHubStateStore:
    def __init__(self, token: str, repository: str) -> None:
        self.token = token
        self.repository = repository
        self.api_base = f"https://api.github.com/repos/{repository}"
        self.branch = self._get_default_branch()

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_default_branch(self) -> str:
        data = request_json(self.api_base, headers=self.headers)
        branch = data.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise AppError("无法读取 GitHub 仓库默认分支。")
        return branch

    def read(self) -> tuple[dict[str, Any], str]:
        query = urllib.parse.urlencode({"ref": self.branch})
        data = request_json(f"{self.api_base}/contents/{STATE_PATH}?{query}", headers=self.headers)
        encoded = data.get("content")
        sha = data.get("sha")
        if not isinstance(encoded, str) or not isinstance(sha, str):
            raise AppError("GitHub 发布状态文件结构无效。")
        try:
            state = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise AppError("GitHub 发布状态文件无法解析。") from None
        if not isinstance(state, dict):
            raise AppError("GitHub 发布状态不是 JSON 对象。")
        return state, sha

    def write(self, state: dict[str, Any], sha: str, message: str) -> str:
        content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "sha": sha,
            "branch": self.branch,
        }
        data = request_json(
            f"{self.api_base}/contents/{STATE_PATH}",
            method="PUT",
            headers=self.headers,
            body=body,
        )
        result = data.get("content")
        new_sha = result.get("sha") if isinstance(result, dict) else None
        if not isinstance(new_sha, str):
            raise AppError("GitHub 没有确认发布状态更新。")
        return new_sha


def publish_to_telegram(token: str, channel: str, post: str) -> int:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {
        "chat_id": channel,
        "text": post,
        "disable_web_page_preview": True,
    }
    # Never automatically retry sendMessage: a timeout after Telegram accepted the
    # message is ambiguous, and retrying could create a duplicate post.
    response = request_json(url, method="POST", body=body, retries=1)
    if response.get("ok") is not True:
        description = response.get("description", "Telegram 未确认发送成功。")
        raise AppError(f"Telegram 发送失败：{description}")
    result = response.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not isinstance(message_id, int):
        raise AppError("Telegram 返回成功但没有 message_id；按未知发送状态处理。")
    return message_id


def is_claimed_for_today(state: dict[str, Any], date_key: str) -> bool:
    return state.get("date") == date_key and state.get("status") in {"sending", "published"}


def run(config: RuntimeConfig, now: datetime | None = None) -> int:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    date_key = current.astimezone(CHINA_TZ).date().isoformat()
    store: GitHubStateStore | None = None
    state: dict[str, Any] | None = None
    state_sha: str | None = None

    if not config.dry_run:
        store = GitHubStateStore(config.github_token, config.github_repository)
        state, state_sha = store.read()
        if is_claimed_for_today(state, date_key):
            print(f"{date_key} 已存在状态 {state['status']}，为避免重复发送，本次跳过。")
            return 0

    digest = generate_digest(config, current)
    post = format_post(digest, current)

    print("\n===== 生成的 Telegram 帖子 =====\n")
    print(post)
    print("\n===== 帖子结束 =====\n")

    if config.dry_run:
        print("DRY_RUN=true：仅输出预览，没有调用 Telegram，也没有修改发布状态。")
        return 0

    assert store is not None and state_sha is not None
    content_hash = hashlib.sha256(post.encode("utf-8")).hexdigest()
    claimed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    claim = {
        "date": date_key,
        "status": "sending",
        "claimed_at": claimed_at,
        "content_sha256": content_hash,
    }
    state_sha = store.write(claim, state_sha, f"chore: claim publication for {date_key}")
    print(f"已为 {date_key} 写入发送占位，后续重复运行将自动跳过。")

    try:
        message_id = publish_to_telegram(
            config.telegram_bot_token,
            config.telegram_channel,
            post,
        )
    except Exception as exc:
        raise AppError(
            "Telegram 发布未确认成功；为避免重复，状态保留为 sending。"
            "请先检查频道，再按 README 的故障恢复步骤处理。原始错误："
            f"{exc}"
        ) from None

    published = {
        **claim,
        "status": "published",
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "telegram_message_id": message_id,
    }
    try:
        store.write(published, state_sha, f"chore: mark publication complete for {date_key}")
    except Exception as exc:
        raise AppError(
            f"Telegram 消息 {message_id} 已发送，但 GitHub 状态未能标记为 published；"
            f"sending 占位仍会阻止重复发送。错误：{exc}"
        ) from None
    print(f"发布成功：频道 {config.telegram_channel}，message_id={message_id}。")
    return 0


def redact_secrets(message: str, config: RuntimeConfig | None = None) -> str:
    safe = message
    if config:
        for secret in (config.openai_api_key, config.telegram_bot_token, config.github_token):
            if secret:
                safe = safe.replace(secret, "[REDACTED]")
    safe = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", safe)
    safe = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-[REDACTED]", safe)
    safe = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", safe, flags=re.I)
    return safe


def main() -> int:
    config: RuntimeConfig | None = None
    try:
        config = load_config()
        return run(config)
    except KeyboardInterrupt:
        print("错误：运行被中断。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{redact_secrets(str(exc), config)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
