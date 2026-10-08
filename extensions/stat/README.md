# stat

Records what the agent spends — tool calls, token usage — into SQLite, reports it, and can cap each user's estimated cost.

- `/stat` renders the numbers in chat; the CLI prints the same report and manages budgets.
- A `before_execution` hook refuses a run whose user has already spent their budget.

## Layout

| module | responsibility |
|---|---|
| `db.py` | table definitions, migration, typed rows (`pydantic`) |
| `pricing.py` | the `token_cost` formula and K/M/B formatting — the only place pricing lives |
| `query.py` | filters, natural-period windows, the aggregate queries |
| `budget.py` | budget rows, spent-so-far, and the `before_execution` check |
| `report.py` | presentation: agent HTML and CLI console |
| `cli.py` | argparse subcommands |
| `test_stat.py` | `python -m unittest extensions.stat.test_stat -v` (from the repo root) |

Data lives in `$XUN_HOME/extension_data/stat/stat.db` (WAL, opened read-only for reports).

## Schema

```sql
toolcall(toolcall_id, toolname, user, timestamp, success)      -- success NULL until the result lands
token(model_call_id, user, timestamp,
      completion_tokens, prompt_tokens, total_tokens, prompt_tokens_cached)
token_budget(username PRIMARY KEY, per_day, per_week, per_month)  -- NULL = unlimited
```

Each table is declared once in `db.py` as DDL plus its row model; INSERT/SELECT are derived from the model's fields, and `init_db` adds columns an older database lacks (`prompt_tokens_cached` was added this way).

## token_cost

```
cost = 0.05 * cached_prompt + 0.2 * (prompt - cached_prompt) + 1.0 * completion
```

Change the three weights in `pricing.PRICING`; they feed both the Python and the SQL side, and because cost is computed from the raw columns at query time, a new pricing reprices history instead of leaving stale sums behind.

## CLI

```
python -m extensions.stat.cli show [--db PATH] [--user NAME] [--days N] [--until YYYYMMDD] [--top N]
python -m extensions.stat.cli budget show
python -m extensions.stat.cli budget set [--user NAME] [--day COST] [--week COST] [--month COST]
python -m extensions.stat.cli budget clear [--user NAME]
```

Counts accept a `K`/`M`/`B` suffix (`--day 50K`), and `none` removes a ceiling. `budget set` leaves periods it is not given untouched; the current user comes from `_XUN_USERNAME`. Budgets are `token_cost` units, not tokens.

## Budgets

Periods are natural: today since midnight, this week since Monday, this month since the 1st. A missing row, or a NULL column, means unlimited. Before each execution the extension sums the user's cost since each period's start and, once the spent amount reaches a ceiling, raises `CancelledError` naming the exhausted periods — the run never reaches the model.

The `user` column comes from `_XUN_USERNAME` (empty when unset), so budgets are per user; `/stat` and `show` pair the budget line with the same user they filter on.
