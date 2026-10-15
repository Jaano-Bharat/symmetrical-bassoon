#!/usr/bin/env python3
"""
Single-run version for GitHub Actions with state tracking:
- Remembers the last processed timestamp so logs are NEVER notified twice,
  even with a 20-minute lookback window.
- Converts timestamps to IST (Indian Standard Time).
- Applies a 60-second cooldown per category so opening Instagram or a website
  sends 1 clean alert instead of 20+ subdomain alerts.
"""

import os
import datetime
import requests

NEXTDNS_API_KEY = os.environ["NEXTDNS_API_KEY"].strip()
NEXTDNS_PROFILE_ID = os.environ["NEXTDNS_PROFILE_ID"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

LOOKBACK_MINUTES = 20  # Covers GitHub Actions scheduling delays safely
CATEGORY_COOLDOWN_SECONDS = 60  # Prevents subdomain spam bursts
STATE_FILE = ".last_checked_ts.txt"
IST_TIMEZONE = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

NEXTDNS_LOGS_URL = f"https://api.nextdns.io/profiles/{NEXTDNS_PROFILE_ID}/logs"
TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# 1. Exact domain matches (for WhatsApp)
EXACT_RULES = {
    "g.whatsapp.net": {
        "key": "wa_notify",
        "title": "🔔 WhatsApp: Notification Came",
        "activity": "Background sync / incoming notification triggered",
    },
    "api.whatsapp.net": {
        "key": "wa_msg",
        "title": "💬 WhatsApp: Message Sent / Active Action",
        "activity": "API request / outgoing message or active sync",
    },
}

# 2. Keyword / similar matches (if the domain contains these strings)
PARTIAL_RULES = {
    "pornhat": {
        "key": "pornhat",
        "title": "🔞 Alert: Pornhat Opened",
        "activity": "Adult website accessed (Pornhat)",
    },
    "xhamster": {
        "key": "xhamster",
        "title": "🔞 Alert: xHamster Opened",
        "activity": "Adult website accessed (xHamster)",
    },
    "instagram": {
        "key": "instagram",
        "title": "📸 Social: Instagram Opened",
        "activity": "Instagram app or web feed active",
    },
}


def get_cutoff_time():
    """Use saved timestamp from previous run if recent, otherwise fallback to LOOKBACK_MINUTES."""
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    default_cutoff = utc_now - datetime.timedelta(minutes=LOOKBACK_MINUTES)

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved_raw = f.read().strip()
            saved_ts = datetime.datetime.fromisoformat(saved_raw)
            # Only use saved timestamp if it isn't older than our max lookback
            if saved_ts > default_cutoff:
                print(f"Resuming from last saved timestamp: {saved_ts.isoformat()}")
                return saved_ts
        except Exception as e:
            print(f"Could not read state file, using default cutoff: {e}")

    print(f"Using default lookback cutoff: {default_cutoff.isoformat()}")
    return default_cutoff


def save_latest_timestamp(ts):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(ts.isoformat())


def fetch_recent_logs(cutoff_utc):
    headers = {"X-Api-Key": NEXTDNS_API_KEY}
    params = {
        "limit": 500,
        "from": cutoff_utc.isoformat(),
    }
    resp = requests.get(NEXTDNS_LOGS_URL, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", [])


def match_domain(domain):
    domain_lower = domain.lower()
    if domain_lower in EXACT_RULES:
        return EXACT_RULES[domain_lower]
    for keyword, rule in PARTIAL_RULES.items():
        if keyword in domain_lower:
            return rule
    return None


def format_extra_details(entry):
    device_info = entry.get("device", {})
    device_name = device_info.get("name") or device_info.get("model") or "Unknown Device"
    client_ip = entry.get("clientIp", "N/A")
    protocol = entry.get("protocol", "DNS")

    raw_status = entry.get("status", "default")
    status_str = "🚫 Blocked by NextDNS" if raw_status == "blocked" else "✅ Allowed (Resolved)"
    return device_name, client_ip, protocol, status_str


def send_notification(rule, domain, ist_time_str, entry):
    device_name, client_ip, protocol, status_str = format_extra_details(entry)

    text = (
        f"{rule['title']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Action: {rule['activity']}\n"
        f"🌐 Domain: {domain}\n"
        f"🕒 Time: {ist_time_str} IST\n"
        f"📱 Device: {device_name} ({client_ip})\n"
        f"🔒 Protocol: {protocol}\n"
        f"🛡 Status: {status_str}"
    )

    resp = requests.post(
        TELEGRAM_SEND_URL,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=15,
    )
    if not resp.ok:
        print(f"[Telegram Error {resp.status_code}] {resp.text}")
    resp.raise_for_status()


def main():
    cutoff = get_cutoff_time()
    logs = fetch_recent_logs(cutoff)

    hits = 0
    latest_seen_ts = cutoff
    last_notified_by_category = {}

    # Process oldest-to-newest
    for entry in reversed(logs):
        domain = entry.get("domain", "")
        ts_raw = entry.get("timestamp", "")

        try:
            ts = datetime.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue

        # Strictly greater than cutoff prevents re-sending the last boundary log
        if ts <= cutoff:
            continue

        if ts > latest_seen_ts:
            latest_seen_ts = ts

        rule = match_domain(domain)
        if not rule:
            continue

        # Cooldown check: skip if the same category triggered within 60 seconds
        cat_key = rule["key"]
        prev_cat_ts = last_notified_by_category.get(cat_key)
        if prev_cat_ts and (ts - prev_cat_ts).total_seconds() < CATEGORY_COOLDOWN_SECONDS:
            print(f"[skip-cooldown] {domain} @ {ts_raw}")
            continue

        ist_time_str = ts.astimezone(IST_TIMEZONE).strftime("%d %b %Y, %I:%M:%S %p")
        print(f"[hit] {rule['title']} | {domain} @ {ist_time_str} IST")
        send_notification(rule, domain, ist_time_str, entry)

        last_notified_by_category[cat_key] = ts
        hits += 1

    # Save the newest timestamp so the next run picks up right after this point
    save_latest_timestamp(latest_seen_ts)
    print(f"Done. {hits} new hit(s) notified. Saved state: {latest_seen_ts.isoformat()}")


if __name__ == "__main__":
    main()
