# Dispatcher profiles

`profiles.json` in this folder defines the dispatchers shown in the top-left
dropdown. Edit it to fill in each dispatcher's phone and the states they cover.

```jsonc
{
  "id": "soyo",          // stable key — don't change once set
  "name": "Soyo",        // shown in the dropdown
  "phone": "310-555-0123", // fills the <dispatcher> token in the SD instructions
  "states": ["FL", "GA"]   // 2-letter pickup states this dispatcher pulls from the
                           // Excel. EMPTY [] = no filter yet (all VINs pass).
}
```

Notes:
- A profile **must be selected** before an Excel can be uploaded/run.
- The selected dispatcher filters the Excel to its `states` (by pickup state), so
  only those VINs are scraped.
- The `phone` is injected wherever `<dispatcher>` appears in the load-board and order
  instructions (see `config.py`).
- States accept either codes (`FL`) or full names (`Florida`) — both normalize.
- `profiles.json` is gitignored (it holds real phone numbers); `profiles.example.json`
  is the committed template.
