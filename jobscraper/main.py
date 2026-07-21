"""Orchestrator — wires browser, extractor, filter, and storage together."""

import argparse
import asyncio
import sys
import time

from jobscraper.browser import navigate_and_extract
from jobscraper.config import Company, load_companies, load_filters
from jobscraper.discover import discover_missing_careers_urls
from jobscraper.enricher import enrich_and_judge_jobs
from jobscraper.extractor import extract_jobs_from_snapshot, llm_plan_navigation, should_skip_page
from jobscraper.filter import filter_by_date, filter_jobs
from jobscraper.llm import LLMUnavailableError, reset_failure_state
from jobscraper.storage import load_existing_urls, save_new_jobs


async def scrape_company(
    company: Company,
    preferences,
    existing_urls: set[str],
) -> int:
    """Scrape a single company and return count of new jobs saved."""
    print(f"\n{'='*60}")
    print(f"Scraping: {company.name}")
    print(f"URLs: {', '.join(company.careers_urls)}")
    if company.roles:
        print(f"Roles filter: {', '.join(company.roles)}")
    print(f"{'='*60}")

    # Step 1: Navigate each careers URL and get job listing pages
    all_jobs = []
    for careers_url in company.careers_urls:
        print(f"\n  --- {careers_url} ---")
        snapshots = await navigate_and_extract(
            careers_url=careers_url,
            company_name=company.name,
            role_hints=company.roles,
            llm_planner=llm_plan_navigation,
        )

        if not snapshots:
            print(f"  No job listing pages found at {careers_url}")
            continue

        # Step 2: Extract jobs from each page snapshot
        for snapshot in snapshots:
            skip_reason = should_skip_page(snapshot)
            if skip_reason:
                print(f"  Skipped ({skip_reason}): {snapshot.url}")
                continue
            jobs = extract_jobs_from_snapshot(snapshot, company.name)
            print(f"  Extracted {len(jobs)} jobs from {snapshot.url}")
            all_jobs.extend(jobs)

    if not all_jobs:
        print(f"  No jobs extracted for {company.name}")
        return 0

    # Step 3: Deterministic pre-filter (loose pass to narrow candidates)
    filtered = filter_jobs(all_jobs, preferences, company.roles)
    print(f"  Pre-filter: {len(filtered)} / {len(all_jobs)} jobs are potential matches")

    # Step 3b: Date filter — only keep jobs from the last N days
    pre_date_count = len(filtered)
    filtered = filter_by_date(filtered)
    if len(filtered) < pre_date_count:
        print(f"  Date filter: {len(filtered)} / {pre_date_count} jobs within date range")

    # Step 4: Deduplicate against existing
    new_jobs = [j for j in filtered if j.url not in existing_urls]
    print(f"  New (not previously scraped): {len(new_jobs)} jobs")

    # Step 5: LLM judgment — visit each candidate's detail page
    # The LLM reads the full job description and decides:
    #   - Is this genuinely relevant to the user's preferences?
    #   - What's the experience requirement?
    if new_jobs:
        new_jobs = await enrich_and_judge_jobs(new_jobs, preferences, company.roles)

    # Step 6: Save
    saved = save_new_jobs(new_jobs)

    # Update existing URLs for subsequent companies
    for job in new_jobs:
        existing_urls.add(job.url)

    return saved


async def run(company_filter: str | None = None):
    """Main run loop — scrape all (or one) company."""
    print("JobScraper — Starting run")
    print("-" * 40)

    # Load config
    preferences = load_filters()
    print(f"Loaded {len(preferences)} role preferences from filters.yaml")

    companies = load_companies()
    print(f"Loaded {len(companies)} companies from Preferred_Companies.xlsx")

    if company_filter:
        companies = [c for c in companies if company_filter.lower() in c.name.lower()]
        if not companies:
            print(f"No company matching '{company_filter}' found.")
            return
        print(f"Filtered to {len(companies)} company/companies matching '{company_filter}'")

    # Discover careers URLs for companies that don't have one
    companies = await discover_missing_careers_urls(companies)

    existing_urls = load_existing_urls()
    print(f"Existing job entries: {len(existing_urls)}")

    total_saved = 0
    start = time.time()

    # Track results per company for final report
    results: dict[str, str] = {}  # company_name -> status

    for company in companies:
        # Fresh circuit breaker per company: repeated LLM failures abandon the
        # CURRENT company only, never the whole run.
        reset_failure_state()
        try:
            saved = await scrape_company(company, preferences, existing_urls)
            total_saved += saved
            results[company.name] = f"OK — {saved} new jobs saved"
        except LLMUnavailableError as e:
            err_msg = str(e).encode('ascii', errors='replace').decode()
            print(f"\nSKIPPED {company.name}: LLM unavailable — {err_msg[:80]}")
            results[company.name] = "SKIPPED — LLM unavailable (rate-limited/down)"
            continue
        except Exception as e:
            err_msg = str(e).encode('ascii', errors='replace').decode()
            print(f"\nERROR scraping {company.name}: {err_msg}")
            results[company.name] = f"FAILED — {err_msg[:80]}"
            continue

        # Polite delay between companies
        if company != companies[-1]:
            await asyncio.sleep(3)

    elapsed = time.time() - start

    # Final report
    print(f"\n{'='*60}")
    print(f"SCRAPE REPORT  ({elapsed:.1f}s)")
    print(f"{'='*60}")
    for name, status in results.items():
        print(f"  {name}: {status}")
    print(f"{'-'*60}")

    ok = sum(1 for s in results.values() if s.startswith("OK"))
    skipped = sum(1 for s in results.values() if s.startswith("SKIPPED"))
    failed = sum(1 for s in results.values() if s.startswith("FAILED"))
    print(f"Companies: {ok} scraped, {skipped} skipped (LLM unavailable), {failed} failed")
    print(f"Total: {total_saved} new jobs saved")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="JobScraper — Scrape job openings from preferred companies"
    )
    subparsers = parser.add_subparsers(dest="command")

    # `run` command
    run_parser = subparsers.add_parser("run", help="Run the scraper")
    run_parser.add_argument(
        "--company", "-c",
        help="Only scrape a specific company (partial name match)",
        default=None,
    )
    run_parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed (visible) mode",
    )

    args = parser.parse_args()

    if args.command == "run":
        if args.headed:
            import os
            os.environ["BROWSER_MODE"] = "headed"

        asyncio.run(run(company_filter=args.company))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
