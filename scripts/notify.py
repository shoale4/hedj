"""
Phone push notifications via ntfy.sh (free, no account).

Subscribe in the ntfy iOS/Android app to the topic in NTFY_TOPIC. The topic
name is the only secret, so keep it long and random.

    from notify import push
    push("Kill switch", "Daily loss limit hit, trading halted", priority="urgent")
"""

import os
import sys
from urllib.request import Request, urlopen

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

PRIORITIES = {"min", "low", "default", "high", "urgent"}


def push(title: str, message: str, priority: str = "default", tags: str = "") -> bool:
    """Send a push. Never raises: a failed notification must not stop trading."""
    if not NTFY_TOPIC:
        return False
    headers = {"Title": title, "Priority": priority if priority in PRIORITIES else "default"}
    if tags:
        headers["Tags"] = tags
    try:
        req = Request(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=message.encode(), headers=headers)
        urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"push failed: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    ok = push("Hedj test", "Push notifications are working.", tags="white_check_mark")
    print("sent" if ok else "not sent (NTFY_TOPIC unset or request failed)")
