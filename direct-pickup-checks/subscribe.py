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


def _write_env_tokens(tokens: set[str]) -> bool:
    """Persist SD's per-action verification tokens into this folder's .env as
    SD_WEBHOOK_VERIFICATION_TOKENS (comma-separated) so the listener accepts any of
    them. Also blanks the legacy single token. Returns True if .env was updated."""
    import pathlib, re
    env = pathlib.Path(__file__).with_name(".env")
    if not env.exists():
        return False
    text = env.read_text()
    joined = ",".join(sorted(tokens))
    def upsert(t, key, val):
        line = f"{key}={val}"
        if re.search(rf"(?m)^{key}=", t):
            return re.sub(rf"(?m)^{key}=.*$", line, t)
        return t + ("\n" if not t.endswith("\n") else "") + line + "\n"
    text = upsert(text, "SD_WEBHOOK_VERIFICATION_TOKENS", joined)
    text = upsert(text, "SD_WEBHOOK_VERIFICATION_TOKEN", "")   # legacy single -> cleared
    env.write_text(text)
    return True


def cmd_subscribe() -> None:
    url = config.callback_url()
    actions = list(config.SUBSCRIBE_ACTIONS)
    print(f"Registering callback {url}")
    print(f"  for actions: {', '.join(actions)}")
    results = sd_client.subscribe(url, actions)
    for r in results:
        print(f"  OK {r.get('action')}: guid={r.get('guid')} active={r.get('is_active')}")
    # SD issues one verification_token PER action — capture them all for the listener.
    tokens = {r.get("verification_token") for r in results
              if isinstance(r, dict) and r.get("verification_token")}
    if tokens:
        wrote = _write_env_tokens(tokens)
        print(f"\nverification tokens from Super Dispatch ({len(tokens)}): {sorted(tokens)}")
        print(".env updated (SD_WEBHOOK_VERIFICATION_TOKENS)." if wrote
              else "Set SD_WEBHOOK_VERIFICATION_TOKENS in .env (comma-separated) to the values above.")
        print("RESTART the listener so it validates against these (./run.sh or systemctl restart direct-pickup-listener).")
    print("\nReminder: webhooks are forward-only — past pickups are not replayed.")


def cmd_unsubscribe(action: str) -> None:
    sd_client.unsubscribe(action)
    print(f"Unsubscribed {action}")


def cmd_unsubscribe_all() -> None:
    for action in config.SUBSCRIBE_ACTIONS:
        try:
            sd_client.unsubscribe(action)
            print(f"Unsubscribed {action}")
        except Exception as e:                       # noqa: BLE001
            print(f"  ({action}: {e})")


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
            print("usage: python subscribe.py unsubscribe <action>  (e.g. order.picked_up)")
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
