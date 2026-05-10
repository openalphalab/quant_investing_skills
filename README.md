# Quant Investing Skills

Claude Code skills for building point-in-time, survivorship-bias-aware US equity research workflows from public data. The bundle covers fund holdings, industry classification, fundamentals, and price history, so an agent can assemble research-ready inputs for quantitative investing without mixing future data into an `as_of` view.

Repository: <https://github.com/openalphalab/quant_investing_skills>

| Skill | What it does | Primary source |
|---|---|---|
| [`fetching-fund-universe`](.claude/skills/fetching-fund-universe) | Pull mutual fund and ETF holdings from NPORT-P filings, with optional ISIN-to-ticker resolution | SEC EDGAR, yfinance |
| [`classifying-industry`](.claude/skills/classifying-industry) | Attach MSCI-style sector and industry labels plus SEC SIC codes to holdings tables | financedatabase, SEC EDGAR |
| [`fetching-pit-financials`](.claude/skills/fetching-pit-financials) | Retrieve point-in-time XBRL facts, balance sheet snapshots, and trailing twelve month income or cashflow values with look-ahead guards | SEC EDGAR XBRL |
| [`fetching-prices`](.claude/skills/fetching-prices) | Fetch daily OHLC and adjusted close data, including delisted ticker coverage through an akshare fallback | yfinance, akshare |

The intended workflow is:

```text
fund universe -> industry classification -> point-in-time fundamentals -> price history
```

Each step can be run independently, but the skills are designed to compose around a single `as_of` date.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Configure SEC identity](#configure-sec-identity)
- [Use With Claude Code](#use-with-claude-code)
- [Use From Python](#use-from-python)
- [Use From The CLI](#use-from-the-cli)
- [Repository Layout](#repository-layout)
- [Tests](#tests)
- [Vendored Shared Code](#vendored-shared-code)
- [License](#license)

## Requirements

- Python 3.10 or newer
- Internet access for SEC EDGAR, yfinance, and akshare
- A contact email for SEC EDGAR requests

Each skill declares its own dependencies in a local `requirements.txt`. The dependency sets overlap, so installing all skills into one virtual environment is usually the simplest setup.

## Install

Clone the repository and create a virtual environment:

```bash
git clone https://github.com/openalphalab/quant_investing_skills.git
cd quant_investing_skills

python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

Install all skill dependencies:

```bash
pip install \
    -r .claude/skills/fetching-fund-universe/requirements.txt \
    -r .claude/skills/classifying-industry/requirements.txt \
    -r .claude/skills/fetching-pit-financials/requirements.txt \
    -r .claude/skills/fetching-prices/requirements.txt
```

You can also install only the `requirements.txt` file for the skill you plan to use.

## Configure SEC Identity

SEC EDGAR requires a contact identity on requests. Set one before using the fund universe, industry classification, or point-in-time financials skills:

```bash
# macOS / Linux
export EDGAR_IDENTITY="you@example.com"
```

```powershell
# Windows PowerShell
$env:EDGAR_IDENTITY = "you@example.com"
```

From Python, set the identity before the first SEC-backed call:

```python
from pit_financials import set_identity

set_identity("you@example.com")
```

CLI commands also accept `--identity you@example.com`.

## Use With Claude Code

Open this repository in Claude Code. The `.claude/skills/` directory is discoverable from the repository root, and each `SKILL.md` file tells the agent when to use that capability.

Example prompts:

- "Pull SPY holdings for 2024-12-31 and group them by sector."
- "What was NVDA's TTM net income knowable on 2024-06-30?"
- "Get TWTR adjusted close through the take-private date."
- "Build a point-in-time factor table for AAPL, MSFT, and NVDA as of 2024-12-31."

To make a skill available in another project, copy the relevant `.claude/skills/<skill-name>` directory into that project's `.claude/skills/` folder, or into `~/.claude/skills/` for global use.

## Use From Python

Each skill includes a `scripts/` module that can be imported directly. For example, point-in-time financial values:

```python
import sys

sys.path.insert(0, ".claude/skills/fetching-pit-financials/scripts")

from pit_financials import get_pit_value_batch, set_identity

set_identity("you@example.com")

snap = get_pit_value_batch(
    ["AAPL", "MSFT", "NVDA"],
    "Assets",
    as_of="2024-12-31",
)

print(snap[["ticker", "value", "unit"]])
```

See each skill's `examples.md` for skill-specific recipes.

## Use From The CLI

Each skill script can also be run from the command line:

```bash
# Fund holdings
python .claude/skills/fetching-fund-universe/scripts/fund_holdings.py SPY \
    --dates 2024-12-31 --out spy_2024Q4.parquet --identity you@example.com

# Sector and industry classification
python .claude/skills/classifying-industry/scripts/industry_classifications.py \
    --from spy_2024Q4.parquet --out spy_industry.parquet --identity you@example.com

# Point-in-time fundamentals
python .claude/skills/fetching-pit-financials/scripts/pit_financials.py \
    AAPL NetIncomeLoss --as-of 2024-06-30 --identity you@example.com

# Price history
python .claude/skills/fetching-prices/scripts/price_history.py \
    TWTR --end 2022-10-27 --out twtr.parquet
```

Generated data files such as `.parquet`, `.csv`, and `.png` outputs are ignored by default.

## Repository Layout

```text
quant_investing_skills/
|-- .claude/
|   `-- skills/
|       |-- classifying-industry/
|       |-- fetching-fund-universe/
|       |-- fetching-pit-financials/
|       `-- fetching-prices/
|-- tools/
|   `-- sync_vendored.py
|-- LICENSE
|-- pytest.ini
|-- README.md
`-- .gitignore
```

Every skill is intended to be self-contained. You can copy one `.claude/skills/<skill-name>` directory into another project and use it without copying the whole repository.

## Tests

The bundled smoke tests avoid live network calls and focus on parsing and join behavior:

```bash
pip install pytest
pytest
```

`pytest.ini` points test discovery at the skill test directories and uses importlib mode so duplicate test filenames across skills do not collide.

## Vendored Shared Code

`fetching-fund-universe/scripts/fund_holdings.py` is reused by `classifying-industry` so that industry classification can fetch holdings when given only a fund ticker. The canonical copy lives under `fetching-fund-universe`; the vendored copy under `classifying-industry` carries a `# >>> VENDORED - DO NOT EDIT >>>` banner.

Refresh or check vendored copies with:

```bash
python tools/sync_vendored.py
python tools/sync_vendored.py --check
```

## License

MIT. See [LICENSE](LICENSE).
