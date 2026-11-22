# Up Finance

Keeps a local copy of your [Up Bank](https://up.com.au) transactions in Home Assistant, adds
spending sensors, and puts a **Finance** dashboard in the sidebar.

## Setup

1. Get a personal access token at <https://api.up.com.au/getting_started>. Up tokens are
   read-only: they can't move money.
2. Paste it into **up_token** on the **Configuration** tab, then start the add-on.
3. The first sync downloads your full history, which takes about a minute. After that it
   syncs every `sync_interval_minutes`.
4. Open **Finance** in the sidebar.

## Options

| Option | Description |
|---|---|
| `up_token` | Your Up personal access token (required). |
| `api_key` | Optional. A long random string (16+ characters) that turns on the read-only data API for Claude. See below. |
| `account_scope` | `joint` (default) syncs only 2Up accounts and never downloads individual accounts. `all` syncs every account the token can see. |
| `ignore_payees` | Payee names that are really your own accounts at other banks (e.g. `Old Bank Account`). Their payments count as transfers instead of income or spending. |
| `payee_rules` | Re-categorise payments Up leaves uncategorised (rates, childcare, payments through an old bank account). See below. |
| `sync_interval_minutes` | How often to sync (5–1440, default 30). |
| `reserve_saver` | The saver all your income collects in, which the fortnightly top-ups are paid from (default `Savings`). Matched by name, ignoring emoji and case. Leave blank if you don't run your money this way. |
| `float_target` | The float you keep in the spending account, in dollars (e.g. `1000`). Turns on the float gap in **To reconcile**. `0` (default) still lists the items, without a target. |
| `float_account` | The spending account to reconcile, matched by name (default: the first joint spending account, e.g. `2Up Spending`). |
| `reconcile_button` | A Home Assistant button that marks the account as reconciled when pressed (default `input_button.up_finance_reconciled`). See below. |
| `large_transaction_threshold` | Spending at or above this many dollars is listed as a large transaction (default 200). |
| `budgets` | Optional monthly budgets, keyed by Up category id. |
| `log_level` | `debug`, `info`, `warning` or `error`. |

Budget example (YAML mode on the Configuration tab):

```yaml
budgets:
  - category: groceries
    monthly: 600
  - category: restaurants-and-cafes
    monthly: 200
  - category: good-life      # a parent category covers all its children
    monthly: 700
```

The category ids are the ones Up uses. You can see them in the Transactions table's category filter, or at
`/api/category_list`.

## Payee rules

Up can't categorise bank transfers, BPAY and similar payments, so they all land in
"Uncategorised". Rules fix that. Each rule matches a payee (a case-insensitive part of the
description) and, optionally, the transaction message (a case-insensitive regular expression).
The first matching rule wins.

```yaml
payee_rules:
  - payee: City Council              # BPAY to the council -> Up's rates category
    category: home-insurance-and-rates
  - payee: Old Bank Account          # payments via an old account, split by message
    message: car (re)?pay|pay off car
    category: car-repayments
  - payee: Old Bank Account
    message: cleaner|babysit
    category: household-help         # your own category...
    parent: home                     # ...shown under Home
  - payee: Old Bank Account
    message: funding|transfer
    treat_as: transfer               # moving your own money: not income or spending
```

`category` can be one of Up's category ids (listed at `/api/category_list`) or your own.
`treat_as` can be `spending`, `income` or `transfer`. Rules apply to your whole history straight
away, and to every new sync after that.

### Letting Claude manage rules

The `up-payee-rules` skill in this repository lets Claude manage your rules through the Home
Assistant connector, from the phone app or desktop. It finds your biggest uncategorised payees,
proposes rules, previews what they'd match, and saves them only after you say yes. The add-on
API it uses (also reachable through ingress):

- `GET /api/uncategorised?days=365`: biggest uncategorised payees, with sample messages
- `GET /api/rules`: current rules and what each matches
- `POST /api/rules/preview` with `{"payee_rules": [...], "ignore_payees": [...]}`: a dry run

## How spending is counted

- **Transfers** between the synced accounts (saver top-ups, covers, round-ups) are ignored.
- **Allowances** are money sent from a synced account to one of your Up accounts outside the
  scope (e.g. 2Up to your individual account). They're counted separately from spending.
- **Spending** is categorised transactions (refunds count against them) plus uncategorised money
  going out, such as rent, BPAY and Pay Anyone.
- **Income** is uncategorised money coming in, such as salary and interest.
- **Home loan**: interest counts as spending ("Home Loan Interest"). Drawdowns are excluded,
  repayments from a saver are transfers, and the loan balance is shown separately.

## Savers

If you top up savers with Up's automatic transfers and pay for things with **Cover**, each
saver works as a budget. After every sync the add-on links each "Cover from <saver>" to the
purchase it paid for (same amount, close in time). Then it shows, for each saver:

- its regular top-up, and what that comes to per month (its budget)
- what it paid out this month and on average over the last 3 months (covers plus direct home loan repayments)
- which categories and merchants it paid for, and whether its balance is growing or shrinking

## Reconciling the float

If you keep a fixed float in the spending account and pay for things with Cover, a few payments
slip through each fortnight. **To reconcile** (at the top of the dashboard, and in
`sensor.up_reconcile`) lists each one and what to do with it:

| Action | What it is | Effect on the spending account |
|---|---|---|
| **Cover** | A purchase no saver paid for. The saver shown is the one that usually pays that merchant or category. | Money in |
| **Forward** | Money that came in and should move on: income, Medicare rebates and refunds. Income goes to the reserve saver. | Money out |
| **Return** | A cover, or a transfer in from a saver, with no purchase to match, e.g. the same bill covered twice | Money out |
| **Send** | A regular automatic transfer that ran in at least 3 of the last 4 cycles but not this one | Money out |

The list starts from when you last marked the account as reconciled. If you've never marked it,
it starts from the last day the balance ended at or above `float_target`. An item drops off by
itself when you deal with it:

- a cover for the same amount, even weeks later (covers are matched to purchases up to 60 days
  back, and to purchases up to 14 days *after* the cover, for pre-authorisations such as Costco
  and fuel), or one cover for the exact total of several purchases;
- a transfer to a saver for the same amount, or for the exact total of several items (e.g. all
  of a fortnight's Medicare rebates in one go);
- a catch-up transfer to the account a missed automatic transfer goes to.

The summary shows the balance after every action. If that isn't exactly the float, something
else moved money (or the list started from a day you were already over or under). Once it's
right, mark the account as reconciled so the next list starts clean:

- the **Mark as reconciled** button on the dashboard, or
- a Home Assistant button. Create a **Button** helper (Settings → Devices & services → Helpers →
  Create helper → Button) named `Up Finance reconciled`, so its entity is
  `input_button.up_finance_reconciled`. The add-on checks it every minute. Pressing it from a
  dashboard or an automation marks the account as reconciled at the time of the press.

## The fortnightly plan

The dashboard is built around one way of running money, which suits irregular income (contracting,
commissions): every dollar of income lands in one saver, the **reserve**, and fixed fortnightly
top-ups go from there into the other savers. Spending doesn't depend on when pay arrives.

- **Reserve**: its balance at the end of each fortnight, and its **runway**: how many fortnights it
  would last with no income, at the current rate of money going out.
- **Going out per fortnight** = the savers' top-ups (the plan) + average spending outside the plan
  + average personal money (transfers to individual accounts).
- **Income per fortnight** is averaged over the last 26 fortnights, so one late or large invoice
  doesn't swing it. Money moved in from a personal account counts as income; **personal** is only
  money moved out to personal accounts.
- **Spending savers** pay for things with Cover; their top-up is their budget for the fortnight.
  **Goal savers** only build up (e.g. a holiday or a child's account).
- **Outside the plan** is spending that no topped-up saver paid for: it came out of the reserve
  directly, or was never covered. It's worked out from the savers' cover totals, so a cover the
  add-on couldn't match to its purchase still counts for its saver. The category breakdown and
  purchase list do rely on matching, so they can include the odd purchase a saver did pay for.

Fortnights line up with the most recent automatic top-up into any saver. The calendar-month charts
are still there, in **Monthly view** at the bottom of the page.

## Sensors

| Entity | |
|---|---|
| `sensor.up_spending_this_month` / `_last_month` / `projected_spending_this_month` | Spending totals |
| `sensor.up_income_this_month` / `_last_month`, `sensor.up_net_last_month`, `sensor.up_savings_rate_last_month` | Income and savings |
| `sensor.up_spending_good_life` / `_home` / `_personal` / `_transport` / `_uncategorised` | This month by Up category group |
| `sensor.up_balance_<account>`, `sensor.up_total_balance`, `sensor.up_total_savers` | Balances |
| `sensor.up_budget_<category>` | Budget progress (attributes: budget, projected, pace %) |
| `sensor.up_saver_<saver>` | Paid out this month (attributes: balance, top-up, monthly budget, 3-month average, top categories) |
| `sensor.up_home_loan_owing` | Home loan balance |
| `sensor.up_reserve_balance`, `sensor.up_reserve_runway` | The reserve saver's balance, and how many fortnights it would last with no income |
| `sensor.up_outgoings_per_fortnight` | Plan + outside-the-plan average + personal (attributes: each part, income average, % covered by income) |
| `sensor.up_outside_plan_this_fortnight` | Spending outside the plan so far this fortnight (attributes: average, top categories) |
| `sensor.up_reconcile` | Number of payments to reconcile (attributes: balance, float, gap, totals per action, balance after reconciling, and the items) |
| `sensor.up_float_gap` | How far the spending account is below its float (negative = over). Only with `float_target` set |
| `sensor.up_report_*` | Data blocks (monthly history, categories, recurring, merchants, large transactions) that Claude reads through the Home Assistant connector |
| `sensor.up_last_sync` | Time of the last successful sync |

Home Assistant records history for these sensors, so you can chart them with the usual cards. The
`report` sensors change only when the data does. If you don't want them in your history
database, exclude them in `configuration.yaml`:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.up_report_*
```

## Read-only data API (for Claude on your home network)

Set `api_key`, then on the **Network** tab map container port `8098` to a host port (e.g. 8098)
and restart the add-on. Every request needs `Authorization: Bearer <api_key>`:

- `GET /api/report`, `/api/overview`, `/api/monthly`, `/api/categories`, `/api/recurring`, ...
- `GET /api/schema` lists the tables and views.
- `POST /api/query` with `{"sql": "SELECT ..."}` runs a read-only query (5,000 rows, 10 seconds max).
- `GET /api/export.csv` downloads all transactions.
- `GET /api/reconcile` lists the payments to reconcile. Marking the account as reconciled
  (`POST /api/reconcile/mark`) only works from Home Assistant and the dashboard, not this port.

The data is opened read-only. Leave the port unmapped if you don't need it.

## Data and backups

Everything is stored in `/data/up_finance.db` (SQLite) inside the add-on. It's included in Home
Assistant backups, and you can always rebuild it from Up by reinstalling the add-on.
