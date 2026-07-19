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

| Company | Careers URL | Roles |
|---------|------------|-------|
| Google | https://careers.google.com/ | Software Engineer, ML Engineer |
| Stripe | https://stripe.com/jobs | |

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

```bash
# Scrape all companies
python -m jobscraper run

# Scrape a specific company
python -m jobscraper run --company "Google"

# Watch the browser (headed mode)
python -m jobscraper run --headed
```

## Output

Results are saved to `data/Job_Openings.xlsx` with columns:

| Job ID | Company | Title | URL | Location | Experience | Date Posted | Date Scraped |
