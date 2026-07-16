---
name: telegram-alerting
description: Format and send deal alerts to a Telegram bot, including message templating, MarkdownV2 escaping, inline keyboard buttons (mute/snooze/book), and idempotent sending that never spams. Use this whenever the task involves Telegram notifications, sending alerts, the Bot API, formatting a deal message, or wiring the notification step of the pipeline. Reach for it even if the user just says "send me a message when a deal drops" or "set up the bot".
---

# Telegram alerting

Delivery is the easy part of notifications; **not spamming is the hard part**, and that
lives in `deal-valuation`'s dedup. This skill covers formatting and sending cleanly.

## Bot setup (one-time, by the owner)

1. Message `@BotFather`, create a bot, get the **bot token** → store as `TELEGRAM_BOT_TOKEN`.
2. Send the bot a message, then find the **chat id** (via `getUpdates`) → store as
   `TELEGRAM_CHAT_ID`. Both load from `secrets.py`, never committed.

Outbound alerts need **no polling and no webhook** — we only push. Just HTTP POST to the
Bot API. (A webhook/long-poll is only needed later if we add interactive buttons that call
back; see the inline-keyboard note.)

## Notifier interface (swap point)

```python
class Notifier(Protocol):
    def send(self, message: str, buttons: list[Button] | None = None) -> None: ...
```

Default impl posts to `https://api.telegram.org/bot<token>/sendMessage`.

```python
import httpx

def send(self, text: str, reply_markup: dict | None = None) -> None:
    payload = {
        "chat_id": self.chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                   json=payload, timeout=10)
    r.raise_for_status()
```

## MarkdownV2 escaping (the classic footgun)

MarkdownV2 requires escaping these characters *in any literal text*:
`_ * [ ] ( ) ~ \` > # + - = | { } . !`

Unescaped `.` or `-` (extremely common in prices, dates, and routes) will 400 the request.
Always run dynamic values through an escaper before interpolation:

```python
import re
_MDV2 = r"_*[]()~`>#+-=|{}.!"
def esc(s: str) -> str:
    return re.sub(f"([{re.escape(_MDV2)}])", r"\\\1", str(s))
```

If escaping becomes annoying, `parse_mode: "HTML"` is a valid alternative with a smaller
escape set (`< > &`). Pick one and be consistent.

## Message template

Lead with the verdict, make the key numbers scannable, and include a booking link. Keep the
one-line "why" from the valuation layer.

```
🎯 *Business saver* IAD → FCO
📅 2026-05-14 (nonstop)
💳 88,000 Aeroplan + $180  →  4.8¢/pt vs $5,900 cash
🔗 Book on Aeroplan
```

Include, in order: cabin + fare type, route, date + nonstop/stops, the cost (miles + taxes
for award, or price + drop for cash) with the valuation one-liner, and a deep link. For cash
drops: `💰 $438 (was $690, ↓37%)`.

## Inline keyboard (nice-to-have, v1.2)

Buttons like *Mute this route 24h* / *Snooze* / *Book* improve UX cheaply. Send them via
`reply_markup`:

```python
reply_markup = {"inline_keyboard": [[
    {"text": "Book", "url": deep_link},
    {"text": "Mute 24h", "callback_data": f"mute:{route_key}"},
]]}
```

A `url` button needs nothing extra. A `callback_data` button (mute/snooze) **does** require
handling the callback — that's the one case you need a webhook or long-poll consumer that
writes a mute flag into the state store, which `deal-valuation` then checks. Don't add
callback buttons until you're ready to run that consumer; url buttons are free.

## Idempotency

`send()` is called only after `deal-valuation` clears dedup, and the alert is recorded only
after a successful send. If a send fails, do **not** record the alert — let the next poll
retry. Keep the whole poll idempotent so a Lambda retry re-evaluates rather than
double-messaging.
