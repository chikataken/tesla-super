"""
One-shot webhook subscription manager.

Registers (or lists / removes) the callback URL for the three pickup actions:
    order.picked_up, order.manually_marked_as_picked_up, order.picked_up_bol
The callback URL is your Cloudflare Tunnel public origin + the listener path
(TUNNEL_PUBLIC_URL + WEBHOOK_PATH, built in config.callback_url()).

    python subscribe.py actions       # print the LIVE webhook action list (verify names)
    python subscribe.py list          # show current subscriptions
    python subscribe.py subscribe     # register the callback URL for our 3 actions
    python subscribe.py unsubscribe <subscription_guid>
    python subscribe.py unsubscribe-all

NOTE: webhooks are forward-only. Subscribing does NOT replay pickups that happened
before the subscription existed — you only get events from now on.
"""
from __future__ import annotations
import sys

import config
import sd_client
from logging_setup import setup, get_logger

setup("subscribe")
log = get_logger(__name__)


def cmd_actions() -> None:
    actions = sd_client.list_webhook_actions()
    print(f"{len(actions)} webhook actions available:")
    for a in actions:
        # shape unknown -> print whole record; VERIFY the name field
        print(f"  {a}")
    names = {str(a.get('action') or a.get('name') or a) for a in actions if isinstance(a, dict)}
    want = set(config.SUBSCRIBE_ACTIONS)
    missing = want - names if names else set()
    if names and missing:
        print(f"\n⚠️  We intend to subscribe to {sorted(want)} but these are NOT in the "
              f"live list (verify names): {sorted(missing)}")


def cmd_list() -> None:
    subs = sd_client.list_subscriptions()
    if not subs:
        print("No subscriptions.")
        return
    print(f"{len(subs)} subscription(s):")
    for s in subs:
        print(f"  {s}")


def cmd_subscribe() -> None:
    url = config.callback_url()
    actions = list(config.SUBSCRIBE_ACTIONS)
    print(f"Registering callback {url}")
    print(f"  for actions: {', '.join(actions)}")
    result = sd_client.subscribe(
        url, actions, verification_token=config.SD_WEBHOOK_VERIFICATION_TOKEN or None)
    print(f"OK: {result}")
    print("\nReminder: webhooks are forward-only — past pickups are not replayed.")


def cmd_unsubscribe(guid: str) -> None:
    sd_client.unsubscribe(guid)
    print(f"Removed subscription {guid}")


def cmd_unsubscribe_all() -> None:
    subs = sd_client.list_subscriptions()
    for s in subs:
        guid = s.get("guid") or s.get("id") if isinstance(s, dict) else None
        if guid:
            sd_client.unsubscribe(guid)
            print(f"Removed {guid}")
    print(f"Removed {len(subs)} subscription(s).")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "actions":
        cmd_actions()
    elif cmd == "list":
        cmd_list()
    elif cmd == "subscribe":
        cmd_subscribe()
    elif cmd == "unsubscribe":
        if len(argv) < 2:
            print("usage: python subscribe.py unsubscribe <subscription_guid>")
            return 2
        cmd_unsubscribe(argv[1])
    elif cmd == "unsubscribe-all":
        cmd_unsubscribe_all()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
