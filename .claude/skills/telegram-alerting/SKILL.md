---
name: telegram-alerting
description: Format and send deal alerts to a Telegram bot, including message templating, MarkdownV2 escaping, inline keyboard buttons (mute/snooze/book), and idempotent sending that never spams. Use this whenever the task involves Telegram notifications, sending alerts, the Bot API, formatting a deal message, or wiring the notification step of the pipeline. Reach for it even if the user just says "send me a message when a deal drops" or "set up the bot".
---

# Telegram alerting

Delivery is the easy part of notifications; **not spamming is the hard part**, and that
lives in `deal-valuation`'s dedup. This skill covers formatting and sending cleanly.

**Discord (webhook-based) is the current default notifier** (`watchlist.yaml`'s
`notifier: discord`), not Telegram — chosen for zero bot-setup ceremony (no BotFather, no
chat-id lookup) and richer structured embeds. Telegram remains a fully-supported, swappable
alternate implementation behind the same `Notifier` interface; this skill documents how to
build/maintain it. Swapping notifiers is a one-line `watchlist.yaml` config change, zero
code changes.

Both notifiers share the same real bug and its fix: Get Trips returns itineraries across
ALL cabins for an `AvailabilityID`, not just the one that was searched — see
`seats-aero-integration`'s `select_trip_for_cabin`. Before that fix, a business-class award
alert was titled (and priced) using an economy trip that happened to be `trips[0]`, in
**both** notifiers, because both formatters trusted the raw Get Trips dict's `Cabin` field
instead of the already-matched `AwardAvailability.cabin`. If you touch one notifier's
formatter, check whether the other has (or needs) the same fix — they are not automatically
kept in sync just because they implement the same interface.

## Bot setup (one-time, by the owner)

1. Message `@BotFather`, create a bot, get the **bot token** → store as `TELEGRAM_BOT_TOKEN`.
2. Send the bot a message, then find the **chat id** (via `getUpdates`) → store as
   `TELEGRAM_CHAT_ID`. Both load from `secrets.py`, never committed.

Outbound alerts need **no polling and no webhook** — we only push. Just HTTP POST to the
Bot API. (A webhook/long-poll is only needed later if we add interactive buttons that call
back; see the inline-keyboard note.)

## Notifier interface (swap point)

The real interface takes raw domain objects, not a pre-formatted string, so each backend
renders however it needs to (MarkdownV2 text here, a structured Discord embed there)
without the poller knowing or caring which one is active:

```python
class Notifier(Protocol):
    def send_award_alert(
        self, award: AwardAvailability, verdict: Verdict, trip: dict, *, deep_link: str | None = None
    ) -> None: ...
    def send_cash_alert(self, fare: CashFare, verdict: Verdict, baseline: Baseline) -> None: ...
```

`send_cash_alert` has no `deep_link` kwarg, unlike `send_award_alert`: `CashFare` (unlike
`AwardAvailability`) already carries its own `deep_link` field, so there's nothing for the
caller to supply separately.

A lower-level `send(message: str, buttons: list[Button] | None = None)` still exists, but
only as a Telegram-internal primitive that `send_award_alert`/`send_cash_alert` are built
on top of (Discord has no equivalent — it posts embed payloads directly). Treat `send()` as
an implementation detail of this module, not part of the interface other code should depend
on; poller.py only ever calls `send_award_alert`/`send_cash_alert`.

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
🎯 *Business* IAD → FCO
📅 2026-05-14 (nonstop)
💳 88,000 Aeroplan + $180  →  4.8¢/pt vs $5,900 cash
🔗 Book on Aeroplan
```

No "saver" in the label — there's no per-item saver flag on the wire (see
`seats-aero-integration`); saver-equivalence comes entirely from the Cached Search
request-time `include_filtered` param, so claiming it per-item in the message text would
assert something we can't actually verify from the response. An earlier version of this
template included it; that was corrected after a live dry run made the wording's false
implication obvious.

Include, in order: cabin, route, date + nonstop/stops, the cost (miles + taxes
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
