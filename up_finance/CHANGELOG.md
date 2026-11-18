# Changelog

## 0.1.0
- First release: Up sync into SQLite, Home Assistant sensors, Finance dashboard panel,
  read-only LAN data API for Claude analysis.

## 0.2.0
- Sync only 2Up (joint) accounts by default (`account_scope`); individual accounts are never downloaded.
- Savers: covers are linked to the purchases they paid for; per-saver budgets from fortnightly
  top-ups, paid out, balance trend, and what each saver paid for.
- Home loan: interest counts as spending, drawdowns are excluded, loan balance shown separately.
- Transfers from 2Up to individual accounts are counted as personal allowances.
- `ignore_payees` option for your own accounts at other banks.
- Balance history rebuilt from transactions (12 months, weekly).
