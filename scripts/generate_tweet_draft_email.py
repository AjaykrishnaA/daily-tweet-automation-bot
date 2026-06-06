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


def build_prompt(topic, style, count, max_chars, extra_instructions):
    instructions = [
        "Generate tweet drafts for a human to review before posting.",
        f"Topic: {topic}",
        f"Style: {style}",
        f"Create exactly {count} distinct options.",
        f"Each option must be {max_chars} characters or fewer.",
        "Do not include numbering inside the tweet text.",
        "Avoid unsupported factual claims unless the prompt provides the facts.",
        "Return only valid JSON in this shape: {\"tweets\":[\"tweet one\",\"tweet two\"]}.",
    ]

    if extra_instructions:
        instructions.append(f"Extra instructions: {extra_instructions}")

    return "\n".join(instructions)


def call_gemini(api_key, model, prompt):
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


def extract_tweets(response):
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

    tweets = data.get("tweets")
    if not isinstance(tweets, list) or not tweets:
        raise RuntimeError(f"Gemini JSON must include a non-empty tweets array: {text}")

    return [clean_tweet(tweet) for tweet in tweets if clean_tweet(tweet)]


def clean_tweet(tweet):
    return " ".join(str(tweet).strip().split())


def validate_tweets(tweets, max_chars):
    valid_tweets = []
    for tweet in tweets:
        if len(tweet) <= max_chars:
            valid_tweets.append(tweet)

    if not valid_tweets:
        raise RuntimeError(f"No generated tweets were {max_chars} characters or fewer")

    return valid_tweets


def x_intent_url(tweet):
    return "https://x.com/intent/tweet?text=" + urllib.parse.quote(tweet)


def build_email(topic, tweets):
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
        "<ol>",
    ]

    for index, tweet in enumerate(tweets, start=1):
        intent_url = x_intent_url(tweet)
        text_lines.extend(
            [
                f"{index}. {tweet}",
                f"Post link: {intent_url}",
                "",
            ]
        )
        html_lines.append(
            "<li>"
            f"<p>{html.escape(tweet)}</p>"
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

    prompt = build_prompt(topic, style, count, max_chars, extra_instructions)
    response = call_gemini(api_key, model, prompt)
    tweets = validate_tweets(extract_tweets(response), max_chars)
    subject, text_body, html_body = build_email(topic, tweets)
    send_email(subject, text_body, html_body)

    print(f"Emailed {len(tweets)} tweet draft(s) for topic: {topic}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
