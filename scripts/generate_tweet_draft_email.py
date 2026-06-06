import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage


GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_int_env(name, default):
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def get_bool_env(name, default=True):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_prompt(topic, style, count, max_chars, extra_instructions, enable_grounding):
    instructions = [
        "Generate tweet drafts for a human to review before posting.",
        f"Topic: {topic}",
        f"Style: {style}",
        f"Create exactly {count} distinct options.",
        f"Each option must be {max_chars} characters or fewer.",
        "Do not include numbering inside the tweet text.",
        "Return only valid JSON.",
    ]

    if enable_grounding:
        instructions.extend(
            [
                "Use Google Search grounding for current or factual claims.",
                "Do not invent news, dates, statistics, sources, or URLs.",
                "If strong current sources are not available, write evergreen drafts and explain that limitation.",
                "Return JSON in this shape: {\"items\":[{\"tweet\":\"tweet text\",\"summary\":\"short evidence summary\",\"sources\":[{\"title\":\"source title\",\"url\":\"https://example.com\"}]}]}.",
            ]
        )
    else:
        instructions.extend(
            [
                "Avoid unsupported factual claims unless the prompt provides the facts.",
                "Return JSON in this shape: {\"items\":[{\"tweet\":\"tweet text\",\"summary\":\"\",\"sources\":[]}]}.",
            ]
        )

    if extra_instructions:
        instructions.append(f"Extra instructions: {extra_instructions}")

    return "\n".join(instructions)


def call_gemini(api_key, model, prompt, enable_grounding):
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }

    if enable_grounding:
        payload["tools"] = [{"google_search": {}}]

    request = urllib.request.Request(
        GEMINI_ENDPOINT.format(
            model=urllib.parse.quote(model, safe=""),
            api_key=urllib.parse.quote(api_key, safe=""),
        ),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API request failed: {exc.code} {body}") from exc


def extract_items(response):
    try:
        text = response["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response shape: {json.dumps(response)[:1000]}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise RuntimeError(f"Gemini did not return JSON: {text}")
        data = json.loads(match.group(0))

    raw_items = data.get("items")
    if raw_items is None and isinstance(data.get("tweets"), list):
        raw_items = [{"tweet": tweet, "summary": "", "sources": []} for tweet in data["tweets"]]

    if not isinstance(raw_items, list) or not raw_items:
        raise RuntimeError(f"Gemini JSON must include a non-empty items array: {text}")

    items = []
    for raw_item in raw_items:
        if isinstance(raw_item, str):
            tweet = clean_tweet(raw_item)
            summary = ""
            sources = []
        elif isinstance(raw_item, dict):
            tweet = clean_tweet(raw_item.get("tweet", ""))
            summary = clean_tweet(raw_item.get("summary", ""))
            sources = normalize_sources(raw_item.get("sources", []))
        else:
            continue

        if tweet:
            items.append({"tweet": tweet, "summary": summary, "sources": sources})

    if not items:
        raise RuntimeError(f"Gemini did not return any usable tweet items: {text}")

    grounding_sources = extract_grounding_sources(response)
    if grounding_sources:
        for item in items:
            existing_urls = {source["url"] for source in item["sources"]}
            item["sources"].extend(
                source for source in grounding_sources if source["url"] not in existing_urls
            )

    return items


def normalize_sources(raw_sources):
    if not isinstance(raw_sources, list):
        return []

    sources = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            continue

        title = clean_tweet(raw_source.get("title", "Source"))
        url = clean_tweet(raw_source.get("url", ""))
        if url.startswith(("http://", "https://")):
            sources.append({"title": title or "Source", "url": url})

    return sources


def extract_grounding_sources(response):
    chunks = []
    try:
        chunks = response["candidates"][0].get("groundingMetadata", {}).get("groundingChunks", [])
    except (KeyError, IndexError, AttributeError):
        return []

    sources = []
    seen_urls = set()
    for chunk in chunks:
        web = chunk.get("web", {}) if isinstance(chunk, dict) else {}
        url = clean_tweet(web.get("uri", ""))
        title = clean_tweet(web.get("title", "Source"))
        if url.startswith(("http://", "https://")) and url not in seen_urls:
            seen_urls.add(url)
            sources.append({"title": title or "Source", "url": url})

    return sources


def clean_tweet(tweet):
    return " ".join(str(tweet).strip().split())


def validate_items(items, max_chars):
    valid_items = []
    for item in items:
        if len(item["tweet"]) <= max_chars:
            valid_items.append(item)

    if not valid_items:
        raise RuntimeError(f"No generated tweets were {max_chars} characters or fewer")

    return valid_items


def x_intent_url(tweet):
    return "https://x.com/intent/tweet?text=" + urllib.parse.quote(tweet)


def build_email(topic, items, enable_grounding):
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"Tweet drafts: {topic}"

    text_lines = [
        f"Tweet drafts for: {topic}",
        f"Generated: {generated_at}",
        "",
    ]

    html_lines = [
        "<!doctype html>",
        "<html>",
        "<body>",
        f"<h2>Tweet drafts for: {html.escape(topic)}</h2>",
        f"<p><strong>Generated:</strong> {html.escape(generated_at)}</p>",
        f"<p><strong>Google Search grounding:</strong> {'enabled' if enable_grounding else 'disabled'}</p>",
        "<ol>",
    ]

    text_lines.append(f"Google Search grounding: {'enabled' if enable_grounding else 'disabled'}")
    text_lines.append("")

    for index, item in enumerate(items, start=1):
        tweet = item["tweet"]
        intent_url = x_intent_url(tweet)
        text_lines.extend(
            [
                f"{index}. {tweet}",
                f"Summary: {item['summary']}" if item["summary"] else "Summary: Not provided",
                f"Post link: {intent_url}",
            ]
        )

        if item["sources"]:
            text_lines.append("Sources:")
            for source in item["sources"]:
                text_lines.append(f"- {source['title']}: {source['url']}")

        text_lines.append("")

        source_html = ""
        if item["sources"]:
            source_items = "".join(
                "<li>"
                f"<a href=\"{html.escape(source['url'])}\">{html.escape(source['title'])}</a>"
                "</li>"
                for source in item["sources"]
            )
            source_html = f"<p><strong>Sources:</strong></p><ul>{source_items}</ul>"

        summary_html = (
            f"<p><strong>Summary:</strong> {html.escape(item['summary'])}</p>"
            if item["summary"]
            else ""
        )

        html_lines.append(
            "<li>"
            f"<p>{html.escape(tweet)}</p>"
            f"{summary_html}"
            f"{source_html}"
            f"<p><a href=\"{html.escape(intent_url)}\">Open in X composer</a></p>"
            "</li>"
        )

    html_lines.extend(["</ol>", "</body>", "</html>"])
    return subject, "\n".join(text_lines), "\n".join(html_lines)


def send_email(subject, text_body, html_body):
    smtp_host = require_env("SMTP_HOST")
    smtp_port = get_int_env("SMTP_PORT", 587)
    smtp_username = require_env("SMTP_USERNAME")
    smtp_password = require_env("SMTP_PASSWORD")
    mail_from = require_env("MAIL_FROM")
    mail_to = require_env("MAIL_TO")
    use_tls = get_bool_env("SMTP_USE_TLS", True)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = mail_to
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    if use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
            server.starttls(context=context)
            server.login(smtp_username, smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60) as server:
            server.login(smtp_username, smtp_password)
            server.send_message(message)


def main():
    api_key = require_env("GEMINI_API_KEY")
    model = os.environ.get("BOT_MODEL") or "gemini-3.1-flash-lite"
    topic = os.environ.get("BOT_TOPIC") or "one useful observation for builders, creators, or founders"
    style = os.environ.get("BOT_STYLE") or "concise, clear, useful, and non-clickbait"
    count = get_int_env("BOT_COUNT", 3)
    max_chars = get_int_env("TWEET_MAX_CHARS", 280)
    extra_instructions = os.environ.get("EXTRA_INSTRUCTIONS", "")
    enable_grounding = get_bool_env("ENABLE_GOOGLE_SEARCH_GROUNDING", False)

    prompt = build_prompt(topic, style, count, max_chars, extra_instructions, enable_grounding)
    response = call_gemini(api_key, model, prompt, enable_grounding)
    items = validate_items(extract_items(response), max_chars)
    subject, text_body, html_body = build_email(topic, items, enable_grounding)
    send_email(subject, text_body, html_body)

    print(f"Emailed {len(items)} tweet draft(s) for topic: {topic}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
