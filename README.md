# JobScraper

Scrapes job openings from preferred companies' career pages using an AI-guided browser agent. Runs on-demand or as a daily scheduled task.

## How it works

1. Reads your preferred companies and their careers page URLs from an Excel file
2. An AI-guided browser (Playwright) navigates each careers site — clicking through from the root page to find job listings
3. Gemini Flash extracts structured job data from the page
4. Jobs are filtered against your preferences (YAML config + per-company role restrictions)
5. New jobs (deduped by URL) are appended to `Job_Openings.xlsx`

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Configure API key
cp .env.example .env
# Edit .env and add your GEMINI_API_KEY

# Create sample companies Excel (edit with your companies afterward)
python create_sample_excel.py
```

## Configuration

### `data/Preferred_Companies.xlsx`

| Company | Careers URL                 | Roles                          |
| ------- | --------------------------- | ------------------------------ |
| Google  | https://careers.google.com/ | Software Engineer, ML Engineer |
| Stripe  | https://stripe.com/jobs     |                                |

- **Roles column** (optional): Comma-separated role titles. Acts as a per-company restriction on top of the YAML filters. If empty, all YAML-matching roles are scraped.

### `data/filters.yaml`

Global preferences — titles to look for, preferred locations, experience range, keywords.

```yaml
preferences:
  - title: "Software Engineer"
    locations: ["Remote", "Bangalore"]
    experience:
      min: 0
      max: 3
    keywords:
      include: ["python", "backend"]
      exclude: ["senior", "staff"]
```

## Usage

All commands are run from the project root.

### Scrape all companies

```bash
python -m jobscraper run
```

Iterates over every row in `data/Preferred_Companies.xlsx`. Each company is
independent — if one fails or gets rate-limited, it's skipped and the run
continues, and a per-company summary is printed at the end:

```
Companies: 19 scraped, 2 skipped (LLM unavailable), 0 failed
Total: 18 new jobs saved
```

### Scrape specific companies

`--company` / `-c` does a case-insensitive **partial** match on the company name:

```bash
python -m jobscraper run --company "Stripe"    # just Stripe
python -m jobscraper run -c uber               # matches "Uber"
python -m jobscraper run -c ai                 # matches every name containing "ai"
```

Handy for re-running only the companies that got skipped in a previous run.

### Scrape a single ad-hoc URL

`--url` scrapes one URL directly against `filters.yaml`, with no Excel row
required. Pass a **pre-filtered** careers/job-board URL (e.g. a Greenhouse or
Lever board already narrowed by department/keyword) — the scraper paginates
**that exact URL as-is** and never wanders off into other departments, so your
filters are preserved:

```bash
python -m jobscraper run --url "https://boards.greenhouse.io/acme?departments[]=eng"
```

- **Pagination (20–30+ pages):** it follows Next / "Load More" buttons, and
  falls back to incrementing `?page=`/`?offset=` params. Raise the cap for big
  boards with `--max-pages`:

  ```bash
  python -m jobscraper run --url "<filtered-url>" --max-pages 30
  ```
- **`--name`** sets the Company label recorded for those jobs (otherwise it's
  derived from the URL):

  ```bash
  python -m jobscraper run --url "<filtered-url>" --name "Acme Corp"
  ```
- **`--discover`** switches from direct-listing to full LLM department
  discovery — use it only when the URL is a *generic* careers landing page
  rather than an already-filtered listing.

> Excluded-keyword titles (senior, staff, manager, lead, …) are dropped up front
> from the listing itself — those jobs are never opened or sent to the LLM.

### Headed vs. headless (background) mode

The browser mode is controlled by the `BROWSER_MODE` env var (default: `headed`).

- **Headed** — a visible browser window; good for watching/debugging:

  ```bash
  python -m jobscraper run --headed
  ```
- **Headless** — no window; use this for scheduled/background/server runs.
  Set `BROWSER_MODE=headless`, either permanently in `.env`, or per-run:

  ```bash
  # macOS / Linux (bash)
  BROWSER_MODE=headless python -m jobscraper run

  # Windows (PowerShell)
  $env:BROWSER_MODE="headless"; python -m jobscraper run
  ```

  To run fully in the background and log to a file:

  ```bash
  # macOS / Linux
  BROWSER_MODE=headless nohup python -u -m jobscraper run > scrape.log 2>&1 &

  # Windows (PowerShell)
  $env:BROWSER_MODE="headless"; Start-Process python -ArgumentList "-u","-m","jobscraper","run" -RedirectStandardOutput scrape.log -RedirectStandardError scrape.err -NoNewWindow
  ```

### Stopping a run / cleaning up stuck headless sessions

A normal run closes each browser automatically. If it gets stuck (e.g. a slow
site) or you hard-kill the terminal, an orphaned headless browser can linger.

1. **Stop the run:** press `Ctrl+C` in its terminal. For a backgrounded run:

   ```bash
   # macOS / Linux
   pkill -f "jobscraper"

   # Windows (PowerShell) — find then stop the scraper process
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     Where-Object { $_.CommandLine -like "*jobscraper*" } |
     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```
2. **Clean up orphaned Playwright browsers** (safe — this only targets browsers
   under Playwright's install dir, never your own Chrome):

   ```bash
   # macOS / Linux
   pkill -f "ms-playwright"

   # Windows (PowerShell)
   Get-Process -ErrorAction SilentlyContinue |
     Where-Object { $_.Path -like "*ms-playwright*" } |
     Stop-Process -Force
   ```

> **Note:** repeated LLM rate-limit/timeout failures for a single company trip a
> per-company circuit breaker, so the run **skips** that company and moves on
> rather than hanging. A run rarely gets "stuck" on the LLM — if it stalls, it's
> usually a browser waiting on a slow page.

## Environment variables

Set these in `.env` (see `.env.example`). All are optional except the API key.

| Variable                                                                      | Default                         | Purpose                                                                                     |
| ----------------------------------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------- |
| `LLM_PROVIDER`                                                              | `groq`                        | `gemini` or `groq`                                                                      |
| `GEMINI_API_KEY` / `GROQ_API_KEY`                                         | —                              | API key for the chosen provider                                                             |
| `LLM_MODEL`                                                                 | provider default                | e.g.`gemini-3.1-flash-lite`, `gemini-flash-latest`                                      |
| `BROWSER_MODE`                                                              | `headed`                      | `headed` (visible) or `headless` (background)                                           |
| `LLM_CALL_DELAY`                                                            | `5` (groq) / `6.5` (gemini) | Seconds between LLM calls, to stay under rate limits                                        |
| `MAX_PAGINATION_PAGES`                                                      | `20`                          | Max listing pages to follow per listing (override per-run with `--max-pages`)               |
| `MAX_JOB_AGE_DAYS`                                                          | `30`                          | Drop postings older than this                                                               |
| `MAX_CONSECUTIVE_LLM_FAILURES`                                              | `2`                           | Failures before a company is skipped (circuit breaker)                                      |
| `EXTRACT_CONTENT_CHARS` / `PLAN_CONTENT_CHARS` / `DETAIL_CONTENT_CHARS` | see`.env.example`             | Page text sent to the LLM per prompt (shrink these for Groq's small free-tier token budget) |

## Output

Results are saved to `data/Job_Openings.xlsx` with columns:

| Job ID | Company | Title | URL | Location | Experience | Date Posted | Date Scraped |
