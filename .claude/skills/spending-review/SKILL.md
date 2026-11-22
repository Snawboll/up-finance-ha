---
name: spending-review
description: Analyse the user's Up Bank spending from their Up Finance Home Assistant add-on. Use when they ask to review, analyse or explain their spending, budgets, subscriptions or savings, or run /spending-review. Optional arguments set the period and focus, e.g. "last 3 months", "focus dining", "quick".
---

# Spending review

Analyse the user's spending using data from their **Up Finance** Home Assistant add-on,
then write a clear, honest review with specific numbers and a few concrete actions.

Arguments (all optional, free text): `$ARGUMENTS`
- **period**: e.g. "this month", "last month", "last 3 months", "2026", "since July". Default: this
  month so far, read against the previous 6 months.
- **focus**: a category, merchant or question, e.g. "dining", "subscriptions", "why was October high?".
- **depth**: "quick" (5-line summary) or "full" (default).

## 1. Get the data

Try these in order and use the first that works. Say which one you used in one short line.

### A. Read-only data API on the home network (from a computer at home, if `api_key` is set)

The API key is kept in the macOS Keychain. Never print it.

```bash
UPF_KEY=$(security find-generic-password -s up-finance-api -w 2>/dev/null)
UPF_URL=${UPF_URL:-http://homeassistant.local:8098}
curl -sf -m 10 -H "Authorization: Bearer $UPF_KEY" "$UPF_URL/api/report"
```

If that works, use it for the overview, then run SQL for anything else:

```bash
curl -sf -m 30 -H "Authorization: Bearer $UPF_KEY" -H "Content-Type: application/json" \
  -d '{"sql": "SELECT month, category, SUM(-amount_cents)/100.0 AS spent FROM flows WHERE kind = '\''spending'\'' GROUP BY 1, 2 ORDER BY 1"}' \
  "$UPF_URL/api/query"
```

`GET /api/schema` describes every table. Queries are read-only, one statement each, up to 5,000 rows.

Other endpoints: `/api/overview`, `/api/savers`, `/api/monthly?months=24`, `/api/categories?month=YYYY-MM`,
`/api/recurring`, `/api/merchants?days=90&limit=50`, `/api/large?days=30`, `/api/reconcile` (payments to reconcile back to the float),
`/api/transactions?q=&month=&category=&kind=&limit=`, and `/api/export.csv`.

If the Keychain item is missing, tell the user how to add it (they type the key themselves):
`security add-generic-password -s up-finance-api -a up-finance -w` (it prompts for the key).

### B. Home Assistant connector (works anywhere, including the phone app)

The app's API is reachable through the connector's `ha_manage_app` tool in proxy mode. Find the
slug with `ha_get_app(source="installed")` (the app named "Up Finance", e.g. `xxxxxxxx_up_finance`), then:

- `ha_manage_app(slug=..., path="/api/report")`: everything in one call (overview, savers,
  13 months by category, recurring, merchants, large and new transactions, budgets, and the
  fortnightly plan under `plan`)
- `ha_manage_app(slug=..., path="/api/query", method="POST", body={"sql": "SELECT ..."})`:
  read-only SQL, same as option A (`/api/schema` describes the tables)
- any other endpoint listed in option A, e.g. `path="/api/transactions?q=uber&month=2026-10"`

If the proxy isn't available, the same summary data is in these entities' **attributes**
(read them with `ha_get_state`, or find them with `ha_search` for `sensor.up_`):

| Entity | What's in it |
|---|---|
| `sensor.up_report_plan` | the fortnightly plan: reserve and runway, income vs going out per fortnight, savers, outside-the-plan categories |
| `sensor.up_report_savers` | every saver: top-up (= its budget), paid out per month, balance trend, what it paid for |
| `sensor.up_report_monthly` | 13 months: total spending, income, allowances, spending by category group |
| `sensor.up_report_categories` | this month by category, with the 6-month average and budget |
| `sensor.up_report_recurring` | subscriptions and bills, cadence, amounts, price changes, annual total |
| `sensor.up_report_merchants` | top merchants (90 days) and new merchants (30 days) |
| `sensor.up_report_large_transactions` | large transactions in the last 30 days |
| `sensor.up_spending_this_month` | spent so far; attributes: same point last month, projected |
| `sensor.up_balance_*`, `sensor.up_total_balance`, `sensor.up_home_loan_owing` | balances |
| `sensor.up_savings_rate_last_month`, `sensor.up_income_*` | income and savings rate |

If neither source works, say what failed. Don't guess at numbers.

## 2. How to read the data

- **Spending** = categorised transactions (refunds reduce spending) plus uncategorised money
  going out (rent, BPAY, Pay Anyone). **Income** = uncategorised money coming in (salary,
  interest, money from friends). **Transfers** between the user's own Up accounts (savers,
  round-ups, 2Up) are excluded from both.
- **This household runs on a fortnightly plan.** All income collects in a reserve saver
  (`plan.reserve`, usually "💰 Savings") and fixed fortnightly top-ups go to the other savers.
  Read money per fortnight, not per month: contractor income makes single months swing. The key
  numbers are in `plan.summary`: the plan (top-ups), spending outside the plan, personal money,
  income averaged over 26 fortnights, and the reserve's runway in fortnights. Spending outside
  the plan (`plan.outside_categories`, `plan.outside_purchases`) is what drains the reserve: say
  what it was and whether it belongs in a saver's top-up. Don't judge a month's "savings rate".
- **Savers can be budgets.** Many Up users top up each saver with a regular automatic transfer
  and pay for purchases with Cover. When a saver has a regular top-up, that top-up is its budget,
  and its "paid out" amount (covers plus any direct home loan repayments) is what it really cost.
  Spending covered from a saver with no regular top-up (often the main savings saver) is
  unplanned spending: call it out.
- Covers are linked to purchases by amount and time (about 90% match). In SQL, `flows.covered_by`
  is the saver's name; `cover_links` has the raw links (unmatched covers have `spend_id` NULL).
- Only 2Up (joint) accounts are synced. Money moved from 2Up to either partner's individual
  account is `kind = 'allowance'`: personal money, not household spending.
- Home loan interest is spending (category `home-loan-interest`). Drawdowns are `kind = 'loan'`
  and are excluded. Principal repayments are transfers from a saver.
- "Uncategorised" often hides the biggest items (rent, bills paid by bank transfer). Name the
  largest payees in it instead of treating it as one blob.
- The current month is partial. Compare it to **the same point last month** or the projection,
  never to a full month.
- Amounts in the database are integer cents (`amount_cents`, negative = money out). The API's
  derived values are dollars.
- Do any arithmetic in SQL or a quick script, not in your head, and check totals add up.

## 3. Write the review

Keep it short. Lead with the answer, give numbers in AUD rounded to the dollar, and compare
each number with something (last month, the 6-month average, the budget).

**Full review structure:**
1. **Headline**: one or two sentences. Is this month normal, high or low, and why?
2. **Savers**: for each saver with a top-up, paid out vs budget (this month and the 3-month
   average) and its balance trend. Flag savers running above 100%, savers that keep hitting $0,
   any unplanned spending covered from Savings, and savers that just accumulate (is the top-up
   still needed?).
3. **Where the money went**: top categories for the period against their averages. Point out the
   2–3 biggest changes and the merchants behind them.
4. **Recurring costs**: total per year, any price rises, possible duplicates (e.g. two streaming
   services or music apps), and anything that looks unused or lapsed.
5. **Unusual activity**: large or one-off purchases and new merchants. Mention them without judging.
6. **Budgets**: which ones are over pace, by how much, and what that means for the rest of the month.
7. **Savings**: income vs spending and the savings rate, with the trend over the last few months.
8. **Three actions**: specific and sized, e.g. "Cancel X ($17/mo, $204/yr)", "The groceries
   saver runs about $X/mo over its top-up: raise the top-up to $Y or cut takeaway", or "Takeaway
   is averaging $X/mo; a $Y cap saves about $Z/yr".

**Quick review:** the headline, any saver over budget, the biggest change, and one action.

If there's a **focus**, answer that question first and in the most depth, then add only the
parts of the full structure that are relevant.

## 4. Ground rules

- This is budgeting help, not financial advice. Don't recommend investments, financial
  products or tax strategies. If asked, say you aren't a licensed adviser.
- The data is private. Don't send it to any other service, and don't paste the whole
  transaction list into the answer.
- If the data looks wrong (for example a stale `last_sync_at`, or a gap in months), say so
  before drawing conclusions.
- If budgets aren't set up, suggest some based on the 6-month averages, and tell the user they
  can add them in the add-on's Configuration tab.
