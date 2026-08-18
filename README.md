# Personal Finance

A zero-based budgeting, planning and forecasting application that runs entirely on
your own computer. No account, no cloud, no telemetry — your financial data lives in
a single SQLite file next to the code.

It exists to replace an advanced personal-budget spreadsheet, and specifically to
handle the things a spreadsheet handles badly: income that arrives late, expenses
that only occur in March, a salary that rises every January, electricity that costs
more in summer, and the difference between *money you have* and *money you can
actually spend*.

---

## Contents

- [Quick start](#quick-start)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [The database](#the-database)
- [Launching the app](#launching-the-app)
- [The nine screens](#the-nine-screens)
- [Key concepts](#key-concepts)
- [Importing data](#importing-data)
- [Exporting data](#exporting-data)
- [Backup and restore](#backup-and-restore)
- [Running the tests](#running-the-tests)
- [Project architecture](#project-architecture)
- [Main financial calculations](#main-financial-calculations)
- [Privacy and where your data lives](#privacy-and-where-your-data-lives)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)

---

## Quick start

**macOS / Linux**

```bash
./run.sh
```

**Windows**

```bat
run.bat
```

That is all. The script creates a virtual environment, installs the dependencies,
and opens the app at <http://localhost:8501>. On first launch it fills the database
with 18 months of realistic demo data so nothing looks empty — clear it whenever you
like from **Settings → Data → Clear all financial data**, and your accounts and
categories are kept.

If you would rather drive it yourself:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

---

## Prerequisites

| | |
|---|---|
| **Python** | 3.10 or newer (3.12 recommended) |
| **Operating system** | macOS, Linux or Windows |
| **Disk** | about 150 MB for the virtual environment, plus a few MB of data |
| **Network** | needed once, so `pip` can download the libraries. The app itself never uses it |

Check your Python with `python3 --version`. If it is missing or older than 3.10,
install it from <https://www.python.org/downloads/>.

Everything the app depends on is a normal open-source library: Streamlit for the
interface, SQLAlchemy for the database, Plotly for charts, Pydantic for validation,
openpyxl for Excel export.

---

## Installation

`run.sh` / `run.bat` do this for you, but here it is in full:

```bash
cd Financial-App
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Useful flags on the launcher:

```bash
./run.sh --update    # reinstall dependencies (after pulling changes), then launch
./run.sh --test      # run the test suite instead of launching
```

---

## The database

**There is no setup step.** On first launch the app:

1. creates `data/finance.db` (SQLite),
2. builds the schema,
3. seeds ~145 default categories across income, expenses, savings, investments and
   debt, plus three starter accounts,
4. loads the demo dataset if the book is empty.

Every launch afterwards runs a lightweight migration check: any column added to the
models is applied to your existing file automatically, so upgrading never means
starting over. Nothing destructive ever happens without an explicit confirmation.

To move your data somewhere else, set `PFA_DATA_DIR` before launching:

```bash
PFA_DATA_DIR=~/Documents/finance ./run.sh
```

---

## Launching the app

```bash
streamlit run app.py
```

It opens on <http://localhost:8501>, bound to localhost only. Stop it with `Ctrl+C`.

To work on a throwaway copy without touching your real book:

```bash
PFA_DEMO=1 streamlit run app.py     # uses data/finance_demo.db instead
```

---

## The nine screens

| Screen | What it is for |
|---|---|
| **Dashboard** | The control centre: cash, net worth, savings rate, budget performance, alerts, forward projection |
| **Budget planning** | Build a zero-based plan for one period, or generate a year at a time from rules |
| **Budget tracking** | Plan against reality line by line, plus a fast way to tick off what has happened |
| **Transactions** | Enter, search, edit, transfer, import CSV, export, and restore from the recycle bin |
| **Accounts** | Balances, net worth, manual valuations, interest accrual, category management |
| **Goals & debts** | Targets with required monthly contributions; payoff projections and strategy comparison |
| **Forecast** | Where the money is heading, with what-if scenarios and a net-worth outlook |
| **Reports** | History, category trends, month-over-month and year-over-year, spending patterns, budget accuracy |
| **Settings** | Currency, period boundaries, income availability rules, thresholds, backup, data management |

Colour is never the only signal: every status carries an icon and a number, and each
chart has a **Show the numbers** table underneath it.

---

## Key concepts

### Zero-based budgeting

```
available money − planned allocations = 0
```

*Available money* is the free cash you start the period with plus the income that
becomes **available** during it. *Allocations* are every planned outflow — expenses,
savings, investments, debt payments, goal funding.

The Budget planning screen tells you whether the period is **balanced**,
**under-allocated** (money with no job yet) or **over-allocated** (the plan promises
money that does not exist), and offers concrete ways to reach zero.

### Income availability — the late paycheck

A salary earned on 31 January is not necessarily January's money. Choose the rule
that matches your life in **Settings → Budgeting rules**:

| Rule | Behaviour |
|---|---|
| Same period it was earned in | Accrual thinking: January's salary funds January |
| Period containing the actual payment date | Follows the deposit |
| Always the following period | The common "this paycheck pays next month's bills" pattern |
| Following period after a cut-off day | Money arriving after (say) the 25th funds the next period |

Any single payment can override the rule from the Transactions screen — for the one
month the bank was slow.

The app distinguishes **earned**, **expected**, **received**, **available to budget**
and **arrived late**, and shows all five on the Dashboard's Cash flow tab.

### Earmarked money vs. free cash

Moving 500 from checking to savings does not change how much money exists, but it is
no longer free to spend. So:

- **cash** — every cash-like account added up
- **earmarked** — the balance of goals held in cash accounts
- **free cash** — cash minus earmarked

Free cash is what carries into the next period as available-to-budget. Without that
subtraction your emergency fund would look spendable every single month and get
allocated again and again.

### Transfers are never income or expense

Paying a credit card, funding savings, buying investments — these relocate money;
they do not earn or consume it. Transfers are modelled as their own kind and are
excluded from income and expense totals, so those totals stay honest.

### Credit cards, and the double-allocation trap

Budgeting groceries charged to a card *and* a "card payment" line allocates the same
money twice. Turn **"Give it a budget line"** off on a card's debt record when you
already budget the spending by category; the app also detects the situation and warns
you. The same guard catches two lines funding one category, and goals or debts funded
both directly and through a category line.

### Real-world recurrence

One rule per repeating item, with the awkward cases built in:

- growth — *+5% every January*, *+2% every 6 months*
- seasonality — *electricity 50% higher in January, 25% lower in July*
- frequency — daily, weekly, fortnightly, monthly, quarterly, half-yearly, annual,
  every N days, every N months, one-off
- anchoring — *insurance every March*, *tax in February, May, August, November*
- weekend handling — move to the previous, next or nearest business day
- settlement — the gap between the due date and the day cash actually moves

Generation is idempotent: running it twice never produces duplicates, never touches a
transaction you have already completed, and respects an occurrence you deliberately
deleted.

---

## Importing data

**Transactions → Import CSV.** The flow is read → preview → commit, and nothing is
written until you confirm.

The importer:

- guesses the delimiter (comma, semicolon, tab, pipe) and the encoding,
- matches column names in English **or** Brazilian Portuguese, and lets you correct
  the mapping,
- parses `1.234,56`, `1,234.56`, `-284.90` and `(1.234,56)` correctly,
- reads dates in many formats,
- treats a negative amount as an expense when there is no type column,
- validates every row and lists the ones it cannot use, with reasons,
- flags rows that exactly match an existing transaction, and rows duplicated inside
  the file itself, and leaves them out by default,
- records the whole import as a batch you can **roll back in one click**.

Download a template from the same tab. Minimum columns: `date`, `description`,
`amount`.

---

## Exporting data

| What | Where |
|---|---|
| Transactions as CSV | Transactions → Export |
| Full Excel workbook | Transactions → Export → *Build the full Excel workbook* |
| Any report table as CSV | the download button under each table |
| Complete JSON dump | Settings → Backup & restore |

The Excel workbook has nine sheets — About, Transactions, Budget, History, Accounts,
Goals, Debts, Net worth, Forecast — with live `SUM` and variance **formulas**, so it
still adds up if you edit a figure in Excel.

---

## Backup and restore

**Settings → Backup & restore.**

| Format | Use it when |
|---|---|
| **SQLite snapshot** (`.db`) | You want an exact copy. This is the one to restore from |
| **JSON dump** (`.json`) | You want something readable, diffable, and portable across versions |
| **Combined archive** (`.zip`) | Belt and braces — both, plus a README |

Snapshots are taken through SQLite's own backup API, so they are consistent even if
something is mid-write.

Restoring **always** copies your current database aside first, so a restore is itself
reversible. A file that is not a valid backup is rejected before anything is touched.

From the command line:

```python
from database.database import init_db, session_scope
from import_export import backup

init_db()
with session_scope() as session:
    print(backup.create_zip_backup(session))
```

---

## Running the tests

```bash
./run.sh --test        # or, inside the virtual environment:
python -m pytest
```

**370 tests** covering:

| File | What it proves |
|---|---|
| `test_money_and_periods.py` | Exact decimal arithmetic, cent-perfect allocation, currency formatting, leap years, month-end clamping, custom period start days, business-day adjustment |
| `test_budgeting.py` | Zero-based balance, under/over allocation, duplicate and double-allocation detection |
| `test_variance.py` | The `actual − planned` sign convention, favourability per line type, status bands, plan accuracy |
| `test_recurrence.py` | Every frequency, growth anchoring, seasonality, settlement offsets, idempotent occurrence keys |
| `test_cashflow.py` | Account balances, credit cards, transfers, all four availability rules, cross-period settlement |
| `test_transactions.py` | Validation, duplicate protection, soft delete and undo, filtering, bulk operations |
| `test_forecasting.py` | Period chaining, source precedence, earmarked money, scenarios, alerts |
| `test_goals_debt.py` | Goal progress and required contributions, amortisation, payoff strategies, the never-pays-off case |
| `test_net_worth.py` | Asset/liability split, overdrawn accounts, history, projection, liquidity |
| `test_financial_calculations.py` | End-to-end: budget generation, override survival, CSV round-trip, Excel export, backup/restore, migrations, demo data |
| `test_ui_smoke.py` | Every one of the nine screens actually renders, against a populated database |
| `test_theme.py` | Every colour pair the interface puts on screen clears its WCAG threshold in both modes; the palette reaches Streamlit's own theme; `config.toml` cannot drift from the palette or disable runtime theme switching |

Edge cases are deliberate, not incidental: negative balances, missing income, leap
years, 31 February, quarterly anchoring, transactions crossing a period boundary,
partial months, zero-income periods, a payment smaller than the monthly interest.

---

## Project architecture

Four layers, each depending only on the ones below it. Business logic never imports
Streamlit, which is why all of it is testable without a browser.

```
Financial-App/
│
├── app.py                    entry point: bootstrap, navigation, dispatch
├── config.py                 filesystem paths and defaults
├── constants.py              enums and lookup tables (no third-party imports)
├── requirements.txt
├── run.sh / run.bat          one-command launchers
│
├── database/                 persistence
│   ├── models.py             ORM models; money stored as INTEGER cents
│   ├── database.py           engine, sessions, pragmas
│   ├── migrations.py         forward-only runner, auto-adds new columns
│   └── seed.py               default categories and accounts
│
├── schemas/
│   └── validation.py         Pydantic input schemas — every write passes through
│
├── calculations/             pure functions, no database, no UI
│   ├── money.py              exact decimals, parsing, locale formatting
│   ├── periods.py            period/date arithmetic
│   ├── recurrence.py         the recurrence engine
│   ├── budgeting.py          zero-based budgeting
│   ├── variance.py           planned vs actual
│   ├── cashflow.py           balances and income availability
│   ├── forecasting.py        forward projection
│   ├── goals.py              goal progress
│   ├── debt.py               amortisation and payoff strategies
│   └── net_worth.py          assets minus liabilities
│
├── services/                 ORM ⟷ calculations; all reads and writes
│   ├── common.py             loaders, settings snapshot, recycle bin
│   ├── settings_service.py   category_service.py   account_service.py
│   ├── transaction_service.py   recurring_service.py   budget_service.py
│   ├── goal_service.py   debt_service.py   forecast_service.py
│   └── networth_service.py   reporting_service.py
│
├── charts/
│   ├── theme.py              validated palette, layout defaults
│   ├── dashboard_charts.py   financial_charts.py
│
├── ui/                       Streamlit only
│   ├── palette.py            light and dark colour, the single source of truth
│   ├── components.py         shared widgets, formatting, confirmations
│   └── dashboard.py  budget.py  tracking.py  transactions.py  accounts.py
│       goals.py  forecast.py  reports.py  settings.py
│
├── import_export/
│   ├── csv_handler.py        excel_handler.py        backup.py
│
├── demo/
│   └── demo_data.py          seeded 18-month dataset
│
└── tests/                    370 tests
```

### Design decisions worth knowing

**Money is integer cents in the database.** SQLite has no decimal type; persisting
`Numeric` there round-trips through a C double and eventually produces balances like
`1234.9999999999998`. A `TypeDecorator` stores exact integer cents and hands back
`Decimal` — and percentages get the same treatment at six decimal places.

**Amounts are always positive.** Direction comes from the transaction kind, which
removes an entire class of sign bugs from the reporting layer.

**Balances are signed: assets positive, liabilities negative.** A card you owe 1.200
on has a balance of `-1200`. The UI flips it back so you always type and read positive
numbers.

**Deleting is reversible.** Transactions are soft-deleted; everything else is
snapshotted as JSON into a recycle bin before removal. Only an explicit purge — behind
a typed confirmation — destroys anything.

**Reads and writes use separate sessions.** A render pass opens a read-only session;
an action opens a transactional one, commits, and reruns. A half-finished form can
never leave a partial write behind.

**The chart palette is validated, not chosen by eye.** Eight categorical hues that
clear colour-vision-deficiency and normal-vision separation gates in both light and
dark mode, assigned in fixed order and never cycled — a ninth series folds into
"Other". Three light-mode slots sit below 3:1 contrast, so every chart also ships
direct value labels and a table view.

**One palette drives everything that has a colour.** `ui/palette.py` holds the two
palettes, and three separate layers read from it — get this wrong and you get the
classic half-dark screen, dark cards stranded on a white page with unreadable dark
text inside them.

| Layer | What it paints | Why it cannot be skipped |
|---|---|---|
| Streamlit's own theme | Inputs, tabs, expanders, alerts, the sidebar, **the dataframe grid** | The grid renders to a `<canvas>`; no stylesheet can reach it. This layer is the only way a table goes dark |
| `ui/components.inject_css` | Metric cards, status pills, empty states — the things Streamlit does not ship | Hardcoding a single hex here is what stranded cards on the wrong background before |
| `charts/theme.get_theme` | Plotly surfaces, grids, axes, ink | A figure must sit on the same plane as the page behind it |

Switching theme in **Settings** pushes the palette into Streamlit's live config and
reruns once, so the change lands immediately rather than on next launch.
`.streamlit/config.toml` is generated from the same file — deliberately as a single
base-level `[theme]` block, because declaring `[theme.light]` *and* `[theme.dark]`
hands theme selection to the browser and silently disables the in-app setting.

One markdown quirk is worth knowing about, since it bites currencies whose symbol
is `$`: Streamlit reads `$…$` as LaTeX, so two amounts in one block turn the money
between them into a maths expression. Formatted amounts that go into markdown pass
through `ui.md()` (or `fmt.md_money()`), and two tests enforce it — one renders every
screen and inspects the output, the other reads the source for branches a render
never reaches.

Contrast is measured rather than eyeballed: `contrast_checks()` computes the WCAG
ratio for every pair the interface actually puts together, and `test_theme.py` fails
the build if any of them drops below 4.5:1 for body text or 3:1 for large text and
meaningful marks. To change a colour, edit `ui/palette.py` and regenerate:

```bash
python -c "from ui.palette import write_config; write_config()"
```

---

## Main financial calculations

| Quantity | Definition |
|---|---|
| **Available to budget** | `free cash carried in + income available in the period` |
| **Zero-based remainder** | `available − Σ allocations`; balanced within half a cent |
| **Free cash** | `cash across cash-like accounts − goal money held in those accounts` |
| **Account balance** | `sign × opening balance + Σ(income) − Σ(expense) + Σ(transfer in) − Σ(transfer out)`, where sign is −1 for liabilities |
| **Variance** | `actual − planned`. Favourable when ≤ 0 for expenses, ≥ 0 for income |
| **Consumed** | `actual ÷ planned`; drives the warning and over-budget bands |
| **Plan accuracy** | `100 − |variance| ÷ planned`, floored at 0 |
| **Savings rate** | `(savings + investments) ÷ income received` |
| **Net cash flow** | `income received − expenses paid + transfers in − transfers out` |
| **Recurring amount** | `base × (1 + growth)^steps × seasonal factor`, evaluated per occurrence date |
| **Forecast net flow** | `income − expenses − savings that leave cash − investments − debt payments` |
| **Goal required monthly** | `remaining ÷ months to the target date` |
| **Goal completion date** | months needed at the current contribution, rounded up |
| **Debt interest** | `balance × annual rate ÷ 1200`, compounded monthly |
| **Debt payoff** | iterated month by month; the final payment is trimmed so the balance lands on exactly zero |
| **Never pays off** | flagged when `payment ≤ monthly interest` |
| **Snowball / avalanche** | every minimum paid each month; the extra pool plus every freed minimum attacks the front of the queue |
| **Net worth** | `Σ assets − Σ liabilities`, where debts linked to an account are counted through that account and never twice |
| **Emergency cover** | `cash ÷ average monthly spending`, in months |

Every one of these is a pure function in `calculations/`, computed with
`decimal.Decimal` and `ROUND_HALF_UP` — the rounding people expect from money, unlike
Python's default banker's rounding.

---

## Privacy and where your data lives

```
data/finance.db          your financial data
data/backups/            snapshots you create
data/exports/            files you export
```

- **No network calls with your data.** No sync service, no cloud account, no API keys.
- **No analytics or telemetry.** Streamlit's usage statistics are switched off in
  `.streamlit/config.toml`.
- **No credentials anywhere.** The app never asks for a bank login because it never
  connects to one — data comes in by CSV.
- **Imported data is sanitised** and every write passes a validation layer that
  rejects impossible values.

Two honest caveats: the SQLite file is **not encrypted**, so on a shared machine rely
on your operating system's user accounts and full-disk encryption; and if you point
backups at a synced folder, that provider then holds a copy.

`.gitignore` already excludes `data/`, `*.db`, `*.csv` and `*.xlsx`, so committing
this folder will not commit your finances.

---

## Configuration

In the app, **Settings** covers currency, date format, cents, theme, the first day of
the budgeting month, fiscal year start, budgeting method, income availability rule and
cut-off day, carry-over, warning and over-budget thresholds, variance tolerance,
forecast length and the backup folder.

Environment variables, for when you need them:

| Variable | Effect |
|---|---|
| `PFA_DATA_DIR` | Where the database, backups and exports live |
| `PFA_DB_PATH` | Point at one explicit database file |
| `PFA_BACKUP_DIR` | Backups only |
| `PFA_EXPORT_DIR` | Exports only |
| `PFA_DEMO=1` | Use `finance_demo.db` instead of your real book |

Brazilian Portuguese currency formatting (`R$ 1.234,56`) applies automatically when
the currency is BRL; ten currencies are supported and adding another is one entry in
`constants.CURRENCY_FORMATS`.

---

## Troubleshooting

**"Python 3.10 or newer is required."** Install it from python.org, then delete the
`.venv` folder and run the launcher again.

**Port 8501 is already in use.**

```bash
streamlit run app.py --server.port 8600
```

**A dependency failed to install.** Update pip first: `./run.sh --update`.

**The numbers look wrong after importing.** Check the Transactions → Import history
tab and roll the batch back; then re-import with the column mapping corrected. A
common cause is a file where positive amounts mean expenses — untick *"Negative
amounts are expenses"*.

**A category shows spending you did not budget.** That is by design — Budget tracking
lists unbudgeted categories separately so next period's plan can be realistic.

**A debt says it never gets paid off.** The planned payment is at or below the monthly
interest. The Goals & debts screen shows the monthly interest figure; anything above
it starts reducing the balance.

**I want to start over.** Settings → Data → *Clear all financial data* keeps your
accounts and categories; *Reset everything* restores factory defaults. Both need a
typed confirmation, and both are worth a backup first.

**Something crashed.** The stack trace names the file. `python -m pytest` will tell
you whether the calculation layer or the interface is at fault; the UI smoke test
covers all nine screens.

---

## Licence and scope

Written for personal use. It is a budgeting and planning tool, not tax software and
not financial advice — the projections assume today's habits continue unchanged,
which is exactly why they are worth looking at, and exactly why they will be wrong in
detail.
