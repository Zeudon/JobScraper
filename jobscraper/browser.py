"""Agentic browser that navigates career pages using Playwright + LLM guidance.

Architecture:
- Phase 1: Land on careers page, identify all relevant department/category URLs
- Phase 2: Visit each department URL, extract jobs (handling pagination)
- Handles: dynamic content (SPA), multi-page listings, expandable sections, new page navigations
"""

import asyncio
import hashlib
from dataclasses import dataclass

from playwright.async_api import Page, BrowserContext, async_playwright

from jobscraper.config import get_env
from jobscraper.llm import LLMUnavailableError

PAGE_LOAD_TIMEOUT = 30_000  # ms
MAX_CONTENT_LENGTH = 90_000  # chars — large pages (Apple, Amazon) can have 100+ roles;
                             # Gemini's 1M context easily holds this. Extraction then
                             # slices to EXTRACT_CONTENT_CHARS.
MAX_PAGINATION_PAGES = int(get_env("MAX_PAGINATION_PAGES", "20"))  # per department, configurable via .env


@dataclass
class PageSnapshot:
    """Snapshot of a page's content for LLM processing."""
    url: str
    title: str
    text_content: str
    links: list[dict]  # [{"text": "...", "href": "..."}]


@dataclass
class NavigationPlan:
    """LLM's plan for scraping a careers site."""
    # URLs to visit that contain job listings (department pages, filtered views, etc.)
    job_listing_urls: list[str]
    # If the main page itself already has jobs, flag it
    main_page_has_jobs: bool = False
    # Elements to click on the main page to reveal jobs (for dynamic/SPA pages)
    elements_to_expand: list[str] | None = None


async def _dismiss_overlays(page: Page):
    """Try to dismiss cookie banners and popups."""
    for selector in [
        'button:has-text("Accept All")',
        'button:has-text("Accept")',
        'button:has-text("Reject All")',
        'button:has-text("Got it")',
        'button:has-text("Close")',
        '[aria-label="Close"]',
        '[aria-label="close"]',
        '#onetrust-accept-btn-handler',
        '[id*="cookie"] button',
        '[class*="cookie"] button',
        '[class*="consent"] button',
    ]:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=600):
                await el.evaluate("el => el.click()")
                await asyncio.sleep(1)
                return
        except Exception:
            continue


async def _scroll_full_page(page: Page):
    """Scroll through the entire page to trigger lazy loading."""
    await page.evaluate("""async () => {
        const delay = ms => new Promise(r => setTimeout(r, ms));
        const height = document.body.scrollHeight;
        for (let y = 0; y <= height; y += 500) {
            window.scrollTo(0, y);
            await delay(150);
        }
        window.scrollTo(0, 0);
    }""")
    await asyncio.sleep(1)


async def _extract_page_snapshot(page: Page) -> PageSnapshot:
    """Extract text content and links from the current page."""
    title = await page.title()
    url = page.url

    # Get visible text content (excluding nav/footer noise)
    text_content = await page.evaluate("""() => {
        const clone = document.body.cloneNode(true);
        clone.querySelectorAll('script, style, noscript, svg, img, header, nav, footer, [class*="nav"], [class*="footer"], [class*="cookie"]').forEach(el => el.remove());
        return clone.innerText.substring(0, 100000);
    }""")

    # Get all links with text, href, and context from surrounding elements
    links = await page.evaluate("""() => {
        const anchors = document.querySelectorAll('a[href]');
        const seen = new Set();
        const results = [];
        for (const a of anchors) {
            let text = a.innerText.trim().substring(0, 120);
            const href = a.href;
            if (!text || !href || seen.has(href)) continue;
            if (href.startsWith('javascript:') || href === '#' || href.endsWith('#')) continue;
            // For generic link text, add context from parent/sibling
            const genericTexts = ['View Openings', 'View', 'Learn More', 'Apply', 'Details', 'View Jobs', 'See Jobs', 'Open Positions', 'View All'];
            if (genericTexts.some(g => text === g || text === g.toLowerCase())) {
                const parent = a.closest('[class*="item"], [class*="card"], [class*="wrapper"], [class*="department"], [class*="category"], li, tr, article, section');
                if (parent) {
                    const heading = parent.querySelector('h1, h2, h3, h4, h5, strong, [class*="title"], [class*="name"]');
                    if (heading && heading.innerText.trim()) {
                        text = heading.innerText.trim().substring(0, 60) + ' — ' + text;
                    }
                }
            }
            seen.add(href);
            results.push({ text, href });
        }
        return results.slice(0, 500);
    }""")

    return PageSnapshot(
        url=url,
        title=title,
        text_content=text_content[:MAX_CONTENT_LENGTH],
        links=links,
    )


def _content_hash(text: str) -> str:
    """Hash full text content to detect changes.

    Must hash the ENTIRE captured text, not just a prefix: "Load More" /
    infinite-scroll pages append new jobs at the bottom while the top stays
    identical. Hashing only a prefix would treat the grown page as unchanged
    and stop pagination early, dropping every job loaded after the first click.
    """
    return hashlib.md5(text.encode()).hexdigest()


async def _click_target(page: Page, target: str) -> bool:
    """Click a target — either a URL (navigate) or text (find and click element).

    Returns True if the action succeeded (page navigated or content changed).
    """
    # Direct URL navigation
    if target.startswith("http"):
        try:
            await page.goto(target, wait_until="domcontentloaded",
                            timeout=PAGE_LOAD_TIMEOUT)
            await asyncio.sleep(3)
            return True
        except Exception:
            return False

    # Try Playwright selectors (JS click to bypass overlays)
    for selector in [
        f'a:has-text("{target}")',
        f'button:has-text("{target}")',
        f'[role="link"]:has-text("{target}")',
        f'[role="button"]:has-text("{target}")',
    ]:
        try:
            element = page.locator(selector).first
            if await element.is_visible(timeout=2000):
                await element.evaluate("el => el.click()")
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                return True
        except Exception:
            continue

    # Broader JS fallback
    try:
        clicked = await page.evaluate("""(target) => {
            const selectors = 'a, button, [role="link"], [role="button"], [onclick], h3, h4, summary, details, [class*="accordion"], [class*="expand"], [class*="toggle"], [class*="tab"], [class*="category"], [class*="department"]';
            const elements = document.querySelectorAll(selectors);
            for (const el of elements) {
                const text = el.innerText ? el.innerText.trim() : '';
                if (text && (text === target || text.toLowerCase().includes(target.toLowerCase()))) {
                    el.scrollIntoView({behavior: 'instant', block: 'center'});
                    el.click();
                    return true;
                }
            }
            return false;
        }""", target)
        if clicked:
            await asyncio.sleep(3)
            return True
    except Exception:
        pass

    return False


async def _handle_pagination(page: Page) -> bool:
    """Try to go to the next page of results. Returns True if successful."""
    # Common pagination patterns
    next_selectors = [
        'a:has-text("Next")',
        'button:has-text("Next")',
        '[aria-label="Next"]',
        '[aria-label="next page"]',
        'a:has-text(">")',
        'a:has-text("›")',
        '.pagination a:last-child',
        '[class*="pagination"] a:last-child',
        '[class*="next"]',
    ]
    for selector in next_selectors:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1000):
                await el.evaluate("el => el.click()")
                await asyncio.sleep(3)
                return True
        except Exception:
            continue

    # Also try "Load More" / "Show More" buttons (infinite scroll pattern)
    for pattern in ["Load More", "Show More", "View More", "See More",
                    "Load more", "Show more", "View more", "See all"]:
        try:
            btn = page.locator(f'button:has-text("{pattern}")').first
            if await btn.is_visible(timeout=800):
                await btn.evaluate("el => el.click()")
                await asyncio.sleep(2)
                return True
        except Exception:
            continue

    return False


async def _expand_dynamic_sections(page: Page, elements_to_expand: list[str]):
    """Click elements that expand/reveal job listings dynamically on the same page."""
    for element_text in elements_to_expand:
        try:
            await _click_target(page, element_text)
            await asyncio.sleep(2)
        except Exception:
            continue


async def _extract_from_page_with_pagination(
    page: Page,
    company_name: str,
    source_label: str,
) -> list[PageSnapshot]:
    """Extract snapshots from a page, following pagination if present."""
    snapshots = []
    seen_hashes = set()

    for page_num in range(MAX_PAGINATION_PAGES):
        await _scroll_full_page(page)
        snapshot = await _extract_page_snapshot(page)

        content_hash = _content_hash(snapshot.text_content)
        if content_hash in seen_hashes:
            break  # No new content, stop paginating
        seen_hashes.add(content_hash)

        snapshots.append(snapshot)
        label = f"{source_label} (page {page_num + 1})" if page_num > 0 else source_label
        print(f"    Captured: {label}")

        # Try to go to next page
        if not await _handle_pagination(page):
            break

    return snapshots


async def navigate_and_extract(
    careers_url: str,
    company_name: str,
    role_hints: list[str],
    llm_planner,
) -> list[PageSnapshot]:
    """Navigate a careers site and return page snapshots of ALL relevant job listing pages.

    Two-phase approach:
    1. Load careers page, ask LLM to identify ALL relevant department/category URLs
    2. Visit each URL and extract jobs (with pagination support)

    Handles:
    - Multi-department sites (Rubrik, Meta, Google)
    - Single-page job boards (Stripe, startups)
    - Dynamic/SPA content that loads on click
    - Paginated job listings (Apple, Amazon with 100s of roles)
    - External job board embeds (Greenhouse, Lever, Workday)
    """
    headless = get_env("BROWSER_MODE", "headed") != "headed"
    all_snapshots: list[PageSnapshot] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

        try:
            page = await context.new_page()
            print(f"  Loading {careers_url} ...")
            await page.goto(careers_url, wait_until="domcontentloaded",
                            timeout=PAGE_LOAD_TIMEOUT)
            await asyncio.sleep(5)
            await _dismiss_overlays(page)
            await _scroll_full_page(page)

            # Phase 1: Ask LLM to analyze the page and create a navigation plan
            snapshot = await _extract_page_snapshot(page)
            plan = await llm_planner(snapshot, company_name, role_hints)

            # Phase 2a: If the main page itself has jobs (single-page sites)
            if plan.main_page_has_jobs:
                # Handle dynamic expansions first
                if plan.elements_to_expand:
                    print(f"  Expanding {len(plan.elements_to_expand)} sections on main page...")
                    await _expand_dynamic_sections(page, plan.elements_to_expand)
                    await _scroll_full_page(page)

                page_snapshots = await _extract_from_page_with_pagination(
                    page, company_name, "main page"
                )
                all_snapshots.extend(page_snapshots)

            # Phase 2b: Visit each department/category URL
            if plan.job_listing_urls:
                print(f"  Found {len(plan.job_listing_urls)} department/category pages to check")
                for i, url in enumerate(plan.job_listing_urls):
                    dept_label = url.split("/")[-1].replace("-", " ").title()
                    print(f"  [{i+1}/{len(plan.job_listing_urls)}] Visiting: {dept_label}")

                    try:
                        await page.goto(url, wait_until="domcontentloaded",
                                        timeout=PAGE_LOAD_TIMEOUT)
                        await asyncio.sleep(3)
                        await _dismiss_overlays(page)
                        await _scroll_full_page(page)

                        page_snapshots = await _extract_from_page_with_pagination(
                            page, company_name, dept_label
                        )
                        all_snapshots.extend(page_snapshots)

                    except Exception as e:
                        print(f"    Error loading {url}: {e}")
                        continue

                    await asyncio.sleep(2)  # polite delay between departments

            # Phase 2c: If plan has elements to expand but no URLs (pure SPA)
            if plan.elements_to_expand and not plan.main_page_has_jobs and not plan.job_listing_urls:
                print(f"  SPA detected — expanding {len(plan.elements_to_expand)} sections...")
                # Go back to careers page
                await page.goto(careers_url, wait_until="domcontentloaded",
                                timeout=PAGE_LOAD_TIMEOUT)
                await asyncio.sleep(5)
                await _dismiss_overlays(page)

                for element_text in plan.elements_to_expand:
                    print(f"    Expanding: {element_text}")
                    pre_hash = _content_hash((await _extract_page_snapshot(page)).text_content)
                    await _click_target(page, element_text)
                    await asyncio.sleep(3)
                    await _scroll_full_page(page)

                    post_snapshot = await _extract_page_snapshot(page)
                    post_hash = _content_hash(post_snapshot.text_content)

                    # Check if clicking changed the page or navigated
                    if page.url != careers_url:
                        # Navigated to a new page — extract from there
                        page_snapshots = await _extract_from_page_with_pagination(
                            page, company_name, element_text
                        )
                        all_snapshots.extend(page_snapshots)
                        # Go back for next element
                        await page.goto(careers_url, wait_until="domcontentloaded",
                                        timeout=PAGE_LOAD_TIMEOUT)
                        await asyncio.sleep(3)
                        await _dismiss_overlays(page)
                    elif post_hash != pre_hash:
                        # Content changed dynamically — extract current state
                        all_snapshots.append(post_snapshot)
                        print(f"    Dynamic content loaded for: {element_text}")

            if not all_snapshots:
                # Fallback: if nothing worked, just extract from the main page
                print("  Fallback: extracting from main careers page as-is")
                await page.goto(careers_url, wait_until="domcontentloaded",
                                timeout=PAGE_LOAD_TIMEOUT)
                await asyncio.sleep(5)
                await _scroll_full_page(page)
                fallback_snapshot = await _extract_page_snapshot(page)
                all_snapshots.append(fallback_snapshot)

        except LLMUnavailableError:
            raise  # let the circuit breaker abandon this company
        except Exception as e:
            print(f"  Error navigating {company_name}: {e}")
        finally:
            await browser.close()

    return all_snapshots
