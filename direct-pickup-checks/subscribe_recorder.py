"""One-shot: expand webhook subscriptions so the recorder mirror updates live for
the whole lifecycle (not just pickups), then print the FULL verification-token set
the listener must accept.

Subscribes the target actions to the SAME callback the existing pickup subscriptions
use (read from the live subscription list, so we don't depend on TUNNEL_PUBLIC_URL).
Idempotent: re-subscribing an existing action is a no-op on SD's side.

    python subscribe_recorder.py            # subscribe + print tokens
    python subscribe_recorder.py --list     # just show current subscriptions
"""
from __future__ import annotations
import sys
import sd_client

# Every action we want the recorder to react to. The API has no catalog endpoint,
# so we ATTEMPT the full candidate list — SD accepts the valid ones and errors on the
# rest (reported as FAIL, harmless). order.changed is the linchpin: it fires on ANY
# order edit/status transition, so even lifecycle states without a discrete action get
# captured (we re-fetch get_order, which returns the current status).
TARGET = [
    # confirmed-from-docs / code
    "order.created", "order.picked_up", "order.manually_marked_as_picked_up",
    "order.picked_up_bol", "order.picked_up.ignored", "order.delivered",
    "order.delivered_bol", "order.archived", "order.canceled", "order.changed",
    # likely lifecycle/status actions (validated by the attempt)
    "order.accepted", "order.declined", "order.dispatched",
    "order.posted_to_loadboard", "order.invoiced", "order.uninvoiced",
    "order.paid", "order.on_hold", "order.flagged", "order.deleted",
    "order.restored", "order.marked_as_delivered", "order.tonu",
]


def _write_env_tokens(subs: list[dict]) -> int:
    """Sync ALL current subscription verification tokens into this folder's .env so
    the listener accepts every action's events. Returns the token count written."""
    import pathlib, re
    toks = sorted({s.get("verification_token") for s in subs if s.get("verification_token")})
    env = pathlib.Path(__file__).with_name(".env")
    text = env.read_text()
    line = "SD_WEBHOOK_VERIFICATION_TOKENS=" + ",".join(toks)
    if re.search(r"(?m)^SD_WEBHOOK_VERIFICATION_TOKENS=", text):
        text = re.sub(r"(?m)^SD_WEBHOOK_VERIFICATION_TOKENS=.*$", line, text)
    else:
        text += ("\n" if not text.endswith("\n") else "") + line + "\n"
    env.write_text(text)
    return len(toks)


def _subs() -> list[dict]:
    return sd_client.list_subscriptions()


def main(argv: list[str]) -> int:
    subs = _subs()
    have = {s.get("action") for s in subs}
    callback = next((s.get("callback_url") for s in subs if s.get("callback_url")), None)
    print(f"current: {len(subs)} subscription(s); callback={callback}")
    for s in subs:
        print(f"  - {s.get('action')}  (active={s.get('is_active')})")

    if argv and argv[0] == "--list":
        return 0
    if not callback:
        print("No existing callback to mirror — aborting (subscribe the pickup actions first).")
        return 1

    todo = [a for a in TARGET if a not in have]
    print(f"\nsubscribing {len(todo)} new action(s): {todo}")
    for action in todo:
        try:
            sd_client.subscribe(callback, [action])
            print(f"  OK  {action}")
        except Exception as e:                               # noqa: BLE001
            print(f"  FAIL {action}: {str(e)[:160]}")

    # Re-list, then sync EVERY token into .env so the listener accepts all events.
    subs2 = _subs()
    print(f"\nnow {len(subs2)} active subscription(s):")
    for s in subs2:
        print(f"  - {s.get('action')}")
    n = _write_env_tokens(subs2)
    print(f"\nwrote {n} verification tokens to .env "
          f"(SD_WEBHOOK_VERIFICATION_TOKENS).")
    print("RESTART the listener to load them: sudo systemctl restart direct-pickup-listener")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
