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

## 0.3.0
- `payee_rules`: re-categorise uncategorised payments by payee and message, or treat them as
  transfers or income. Applied to your whole history and to every new sync.

## 0.4.0
- Rule endpoints for Claude: `/api/uncategorised`, `/api/rules`, `/api/rules/preview` (dry run).
- New `up-payee-rules` skill: Claude manages payee rules through the Home Assistant connector.

## 0.4.1
- Accept requests from Home Assistant's internal network (e.g. the HA MCP server), so Claude's
  Home Assistant connector can read the data and preview rules.

## 0.4.2
- Read-only SQL (`/api/query`), `/api/schema` and `/api/export.csv` are available through the
  Home Assistant connector too, so phone reviews can query individual transactions.
