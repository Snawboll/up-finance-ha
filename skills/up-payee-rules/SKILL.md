---
name: up-payee-rules
description: View, add, change or remove payee rules in the user's Up Finance Home Assistant add-on, which re-categorise uncategorised Up Bank payments (BPAY, bank transfers, payments through an old account) or mark them as transfers or income. Use when the user asks to categorise or label a payee, fix "Uncategorised" spending, ignore their own accounts, or review their rules.
---

# Up Finance payee rules

Payee rules live in the **Up Finance** add-on's options in Home Assistant. Read and change them
with the Home Assistant connector:
- `ha_get_app` reads the add-on and its current options.
- `ha_manage_app` changes the options, restarts the add-on, and calls the add-on's own API
  through Home Assistant (proxy mode with `path`).

This works the same from the phone app and the desktop.

## Rule format

```yaml
payee_rules:                     # first matching rule wins
  - payee: QPRC                  # required: case-insensitive part of the payee/description
    message: rates|levy          # optional: case-insensitive regex on the transaction message or note
    category: home-insurance-and-rates   # an Up category id, or a new one of your own
    parent: home                 # only for your own categories: good-life | home | personal | transport
    treat_as: spending           # spending | income | transfer (default: spending if category set)
ignore_payees:                   # shorthand for {payee: X, treat_as: transfer}
  - Old Bank Account
```

Up category ids, by group:
- **home**: groceries, homeware-and-appliances, internet, home-maintenance-and-improvements,
  pets, home-insurance-and-rates, rent-and-mortgage, utilities
- **personal**: family, clothing-and-accessories, education-and-student-loans,
  fitness-and-wellbeing, gifts-and-charity, hair-and-beauty, health-and-medical, investments,
  life-admin, mobile-phone, news-magazines-and-books, technology
- **transport**: car-insurance-and-maintenance, cycling, fuel, parking, public-transport,
  car-repayments, taxis-and-share-cars, toll-roads
- **good-life**: games-and-software, booze, events-and-gigs, hobbies, holidays-and-travel,
  lottery-and-gambling, pubs-and-bars, restaurants-and-cafes, takeaway, tobacco-and-vaping,
  tv-and-music, adult

Use an Up category when one fits. Only make up a category (with a `parent`) when none does.

## Steps

1. **Find the add-on.** Call `ha_get_app(source="installed")` and find the app named
   "Up Finance". Its slug looks like `xxxxxxxx_up_finance`. Then call `ha_get_app(slug=...)` to
   read the current `options`, especially `payee_rules` and `ignore_payees`. Keep everything else
   in the options exactly as it is.

2. **See what needs a rule** (skip if the user already said exactly what they want). Call the
   add-on's API through `ha_manage_app` in proxy mode:
   - `ha_manage_app(slug=..., path="/api/uncategorised?days=365&limit=30")`: the biggest
     uncategorised payees, with counts, totals, dates and sample messages.
   - `ha_manage_app(slug=..., path="/api/rules")`: current rules and what each one matches.
   - `/api/category_list` lists every category id, including ones you've made up.

3. **Propose rules.** Use what the user tells you, for example "QPRC is council rates" or
   "payments to Old Bank with 'car' in the message were the car loan". Where it's clear, suggest
   rules for other big uncategorised payees too, but don't guess at private payees (people's
   names): ask the user what they are.

4. **Preview before saving.** Send the complete new rule list:
   `ha_manage_app(slug=..., path="/api/rules/preview", method="POST", body={"payee_rules": [...], "ignore_payees": [...]})`.
   Show the user a short table: each rule, how many payments it matches, the total, the date
   range, and a couple of examples. Point out any rule matching nothing or matching more than
   intended (e.g. `message: car` also matching "Car Insurance"). Tighten the pattern and preview
   again if needed.

5. **Get a clear yes** from the user before changing anything.

6. **Save and restart.** Write the **complete** lists, because each one replaces the previous list:
   `ha_manage_app(slug=..., options={"payee_rules": [...], "ignore_payees": [...]})`, then
   `ha_manage_app(slug=..., action="restart")`. The add-on applies the rules to the whole
   history when it starts.

7. **Check it worked.** Wait about 20 seconds, then call `/api/rules` again and confirm the
   match counts are what the preview showed. Tell the user what changed, e.g. "$13.6k of council
   rates moved from Uncategorised to Home Insurance & Rates".

To **remove** a rule, write the lists without it (steps 4–7). To **list** rules, run steps 1–2 and
summarise them in plain language.

## Ground rules

- Never save options without the user's yes for that specific change.
- Never change any option other than `payee_rules` and `ignore_payees`. Never read out or
  change `up_token` or `api_key`.
- If the connector can't reach the add-on (it isn't installed, or Home Assistant is offline),
  say so. Don't hand the user YAML to paste unless they ask for it.
