# secrets/ — one place for all API keys

Both tools (`shipment-creator` and `tesla-reconcile`) read their credentials
from **`secrets/.env`** in this folder. Put your keys here once and both apps
pick them up.

## Setup

```sh
cp secrets/.env.example secrets/.env   # then edit secrets/.env
```

Fill in:

| Key                          | Used by           | Where to get it                     |
| ---------------------------- | ----------------- | ----------------------------------- |
| `ANTHROPIC_API_KEY`          | tesla-reconcile   | console.anthropic.com               |
| `SUPERDISPATCH_CLIENT_ID`    | shipment-creator  | Super Dispatch account (API creds)  |
| `SUPERDISPATCH_CLIENT_SECRET`| shipment-creator  | Super Dispatch account (API creds)  |
| `DIDI_EMAIL_USER` / `_PASSWORD` | shipment-creator (email segment) | didi@tfitrans.com mailbox password (iPage IMAP) |

## Precedence

When an app starts it loads, in order of priority:

1. Real environment variables (highest — overrides everything)
2. `secrets/.env` (this folder — the normal place to put keys)
3. An app-local `.env` inside `shipment-creator/` or `tesla-reconcile/` (legacy fallback)

So if you previously had keys in a per-app `.env`, move them here and delete the
old files to avoid confusion.

## Safety

- `secrets/.env` is gitignored (`**/.env`) and must **never** be committed.
- Only `.env.example` (placeholders) and this README are tracked.
- If a key is ever committed or pushed, treat it as compromised and **rotate it**.
