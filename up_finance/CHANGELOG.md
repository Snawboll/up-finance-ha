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

## 0.4.3
- Sync progress: the log shows when a sync starts and every 1,000 transactions downloaded;
  the Finance page shows "Syncing… N transactions downloaded" and refreshes when it's done.

## 0.5.0
- Dashboard rebuilt around a fortnightly plan: all income collects in a reserve saver
  (`reserve_saver`, default `Savings`) and fixed fortnightly top-ups go to the other savers.
  New sections: the reserve's balance and runway, income vs money going out per fortnight,
  spending and goal savers per fortnight, and spending outside the plan by category and purchase.
- Monthly charts moved into a collapsed "Monthly view".
- New `/api/plan` endpoint and sensors: `up_reserve_balance`, `up_reserve_runway`,
  `up_outgoings_per_fortnight`, `up_outside_plan_this_fortnight`, `up_report_plan`.

## 0.5.1
- Money moved in from personal accounts counts as household income in the fortnightly plan,
  instead of reducing personal money.
- Spending savers' average is taken over 12 fortnights, so a monthly loan repayment landing two
  or three times in a shorter window doesn't distort it.

## 0.6.0
- **To reconcile**: lists what needs doing to get the spending account back to its float
  (`float_target`): purchases nobody covered, income, rebates and refunds to forward, covers with
  no purchase, and regular automatic transfers that didn't run. Items drop off when a later cover
  or transfer settles them, including one cover or transfer for the total of several items.
- Mark the account as reconciled from the dashboard or a Home Assistant button helper
  (`reconcile_button`); new `sensor.up_reconcile` and `sensor.up_float_gap`, and
  `GET /api/reconcile`.
- Covers match purchases up to 60 days earlier (was 14), and purchases up to 14 days after the
  cover (pre-authorisations that settle as a new transaction).

## 0.6.1
- The first press of a newly created reconcile button now marks the account as reconciled
  (it was only remembered, because a never-pressed button has no time yet).
