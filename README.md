> ### 🪞 This GitHub repository is a mirror
>
> Development happens on Fontem's own infrastructure; this mirror is
> updated automatically. **Issues and pull requests opened here are not
> monitored.**
>
> If you would like to contribute — code, data sources, review, or
> anything else — please get in touch at **team@fontem.eu** and we will
> set you up.

# edgar-data-init

Downloads and prepares SEC EDGAR bulk data for the **edgar-gmr-etl** investment
engine to run fully offline.  Once complete, financial data lookups go from
10–30 seconds (live XBRL parsing) to milliseconds (local disk reads).

---

## What gets downloaded

| Layer | Size (compressed) | Contents |
|---|---|---|
| `reference` | ~50 MB | Ticker → CIK mappings (4 files) |
| `companyfacts` | ~1.5–2 GB | Pre-processed XBRL facts for every public company (`CIK*.json`) |
| `submissions` | ~1.5 GB | Filing indexes and company metadata (`CIK*.json`) |

Data covers XBRL history back to ~2006 for large caps, ~2011 for smaller
companies.  Raw SGML filing bundles are **not** downloaded — the pre-processed
facts files are sufficient for all financial data (balance sheet, income
statement, cash flow).

---

## Prerequisites

The script uses the same virtualenv as **edgar-gmr-etl**:

```bash
cd /config/repos/edgar-gmr-etl
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # installs edgartools, rich, pyyaml, …
```

Or install only the edgar-data-init dependencies into an existing env:

```bash
pip install -r /config/repos/edgar-data-init/requirements.txt
```

---

## Running

### Quick smoke test (~50 MB, validates config)

```bash
python setup.py --mode smoke
```

### Download financial data (~3.5 GB, recommended)

```bash
python setup.py --mode facts
```

This downloads company facts for every public company.  Expect **5–30 min**
depending on your connection.  After this, `EntityFacts` reads from disk and
financial lookups are instant.

### Full download (~5 GB)

```bash
python setup.py --mode full
```

Adds submission indexes on top of facts (company metadata, filing discovery).

### Common options

```
--data-dir PATH      Where to store data  (default: /config/edgar-data)
--identity EMAIL     Your e-mail for the SEC API  (default: bemar-edgar@research.com)
--ttl-days N         Re-download if data is older than N days  (default: 7, 0=never)
--force              Re-download all stages regardless of state
--skip-verify        Skip the EntityFacts sanity check at the end
--verify-tickers T…  Tickers to test during verification  (default: AAPL MSFT)
--verbosity N        Log level: 1=error  2=warn  3=info  4=debug  (default: 3)
```

### Examples

```bash
# Download to a custom directory with verbose logging
python setup.py --mode facts --data-dir /data/edgar --verbosity 4

# Keep data fresh — safe to run hourly via cron
python setup.py --mode full --ttl-days 7

# Force a full re-download
python setup.py --mode full --force

# Download facts, never auto-refresh, check NVDA and TSLA instead of defaults
python setup.py --mode facts --ttl-days 0 --verify-tickers NVDA TSLA
```

---

## Resumability

Progress is tracked in **`<data-dir>/.state.yaml`**.  Each stage records its
status (`pending` / `in_progress` / `complete`) and a completion timestamp.

If the script is interrupted mid-download, the next run picks up where it left
off — stages already marked `complete` are skipped, and any `in_progress` stage
is retried from scratch.

Run the script on a schedule (e.g. hourly cron) to keep data within the TTL:

```bash
# crontab — re-download any stage older than 7 days, every day at 02:00
0 2 * * * /config/repos/edgar-gmr-etl/.venv/bin/python \
    /config/repos/edgar-data-init/setup.py --mode full --ttl-days 7
```

---

## Using the data in edgar-gmr-etl

After a successful run, `setup.py` writes **`edgar.env`** next to itself:

```bash
source /config/repos/edgar-data-init/edgar.env
```

This sets three environment variables:

```bash
EDGAR_LOCAL_DATA_DIR="/config/edgar-data"
EDGAR_USE_LOCAL_DATA="1"
EDGAR_IDENTITY="you@example.com"
```

Add the `source` line to your shell profile or `.env` file so the investment
engine picks it up automatically.

---

## Running the tests

```bash
cd /config/repos/edgar-data-init
/config/repos/edgar-gmr-etl/.venv/bin/python -m pytest tests/ -v
```

Tests are fully offline — they use synthetic CIK JSON fixtures under
`tests/fixtures/` and pre-built state scenarios under `tests/test_resources/`.
No network calls are made.

To regenerate the fixtures from live SEC data (only needed if the fixture
format changes):

```bash
python tests/fixtures/capture.py --tickers AAPL MSFT
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
