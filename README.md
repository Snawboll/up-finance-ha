# Up Finance for Home Assistant

A Home Assistant add-on that syncs your [Up Bank](https://up.com.au) transactions into a local
SQLite database. It adds spending sensors, puts a **Finance** dashboard in the sidebar, and serves
the data to Claude for ad-hoc spending reviews.

- **Persistent history:** your full transaction history is stored on your Home Assistant box
  and included in its backups.
- **Dashboard:** monthly spending by category, income vs spending, balances, category vs
  6-month average, budgets, recurring payments, top merchants, large and new transactions, and
  searchable transactions.
- **Sensors:** totals, balances, budgets and report blocks for your own dashboards and for Claude.
- **No dependencies:** standard-library Python on a plain `python:3.12-alpine` image, talking to
  Home Assistant only through its stable REST API. Home Assistant upgrades shouldn't affect it.

## Install

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add
   `https://github.com/Snawboll/up-finance-ha`.
2. Install **Up Finance**, add your Up token on the **Configuration** tab, and start it.
3. Open **Finance** in the sidebar.

See [up_finance/DOCS.md](up_finance/DOCS.md) for options, sensors and the data API.

## Claude spending reviews

[`skills/spending-review/SKILL.md`](skills/spending-review/SKILL.md) is a Claude skill. It reads
the data either from the read-only API (on your home network) or from the `sensor.up_*`
entities through the Home Assistant connector (anywhere, including the phone app). Then it
writes a review, e.g. `/spending-review last 3 months, focus dining`.

[`skills/up-payee-rules/SKILL.md`](skills/up-payee-rules/SKILL.md) lets Claude categorise
uncategorised payments for you by managing the add-on's payee rules through the Home Assistant
connector. It previews each change and asks before saving.

To use the skills in Claude Code, copy them to `~/.claude/skills/`. To use it in the
Claude apps, zip each skill folder and upload it under **Settings → Capabilities → Skills**.

## Development

```bash
cd up_finance
python3 -m unittest discover -s tests       # tests (fake Up data)
python3 tests/demo.py                       # dashboard with fake data on http://127.0.0.1:8099
UP_TOKEN=up:yeah:... DATA_DIR=.dev-data python3 -m app.main --once   # one real sync, prints summary
```

To release a change, bump `version` in `up_finance/config.yaml` and add a line to
`up_finance/CHANGELOG.md`. Home Assistant then offers the update.

## Privacy

Your token and data stay on your Home Assistant box. This repository contains only code.
