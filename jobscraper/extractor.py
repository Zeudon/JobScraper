"""LLM-powered job extraction and navigation planning using Google Gemini."""

import json
import re
import time

from google import genai

from jobscraper.browser import NavigationPlan, PageSnapshot
from jobscraper.config import get_env
from jobscraper.storage import JobOpening

_client = None

MAX_RETRIES = 3
RETRY_DELAY = 8  # seconds


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = get_env("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set in .env file")
        _client = genai.Client(api_key=api_key)
    return _client


def _get_model() -> str:
    return get_env("LLM_MODEL", "gemini-3.5-flash")


def _call_llm(prompt: str) -> str:
    """Call Gemini with retry on rate limit errors."""
    client = _get_client()
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=_get_model(),
                contents=prompt,
            )
            return response.text
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_DELAY * (attempt + 1)
                    print(f"    Rate limited, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
            raise
    return ""


def _parse_json_from_response(text: str) -> list | dict:
    """Extract JSON from LLM response, handling markdown code blocks."""
    # Try to find JSON in code blocks first
    match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if match:
        text = match.group(1)

    # Clean up and parse
    text = text.strip()
    return json.loads(text)


async def llm_plan_navigation(
    snapshot: PageSnapshot,
    company_name: str,
    role_hints: list[str],
) -> NavigationPlan:
    """Ask the LLM to analyze a careers page and produce a complete navigation plan.

    Instead of step-by-step navigation, this produces a PLAN upfront:
    - Which department/category URLs to visit
    - Whether the main page already shows individual jobs
    - Which elements to expand for dynamic content
    """

    # Prioritize career-relevant links
    career_keywords = ["career", "job", "position", "opening", "department", "team",
                       "role", "engineer", "hiring", "apply", "greenhouse", "lever",
                       "workday", "boards", "software", "data", "product", "design",
                       "machine", "artificial", "research"]

    career_links = []
    other_links = []
    for link in snapshot.links:
        href_lower = link["href"].lower()
        text_lower = link["text"].lower()
        if any(kw in href_lower or kw in text_lower for kw in career_keywords):
            career_links.append(link)
        else:
            other_links.append(link)

    prioritized = career_links + other_links[:40]
    links_text = "\n".join(
        f'  - "{link["text"]}" -> {link["href"]}'
        for link in prioritized[:150]
    )

    roles_context = ""
    if role_hints:
        roles_context = f"We are specifically looking for roles related to: {', '.join(role_hints)}."
    else:
        roles_context = "We are looking for all open job positions related to: Software Engineering, ML/AI, Data Science, and related technical roles."

    prompt = f"""You are analyzing the careers website for {company_name} to plan how to scrape ALL relevant job listings.
{roles_context}

Current page: {snapshot.url}
Page title: {snapshot.title}

Page content (excerpt):
{snapshot.text_content[:10000]}

Links on this page:
{links_text}

Analyze this page and return a JSON navigation plan. The page could be one of several types:

TYPE A — The page shows INDIVIDUAL JOB TITLES (like "Software Engineer", "ML Engineer") with links to details.
TYPE B — The page shows DEPARTMENTS/CATEGORIES (like "Engineering - 10 openings") with links to department-specific job lists.
TYPE C — The page is a landing page with a link/button to a job board (greenhouse, lever, workday, or another section of the site).
TYPE D — The page uses dynamic content / SPA where clicking elements reveals jobs without navigating to a new URL.

Return a JSON object with this structure:
{{
    "page_type": "A" | "B" | "C" | "D",
    "main_page_has_jobs": true/false,
    "job_listing_urls": ["url1", "url2", ...],
    "elements_to_expand": ["element text 1", "element text 2", ...],
    "reasoning": "brief explanation"
}}

RULES:
- "main_page_has_jobs": true ONLY if this page already shows specific job titles (not departments)
- "job_listing_urls": Include ALL department/category URLs that could contain relevant technical roles.
  For example, if you see Engineering, Product, IT, Data, etc. — include ALL of them since they may all have software/ML/AI roles.
  ONLY include full URLs (starting with http), not anchor links.
  Include ALL relevant departments, not just the first one.
- "elements_to_expand": For Type D (SPA/dynamic), list text of elements to click to reveal jobs.
  This could be department names, "View Jobs" buttons, accordion headers, tab names, etc.
- If you see a link to an external job board (greenhouse.io, lever.co, boards.greenhouse.io, jobs.lever.co, myworkdayjobs.com), put that URL in job_listing_urls.
- Include departments like Engineering, Technology, IT, Product, Data, Research, AI/ML, Platform, Infrastructure — any department that might have the roles we're looking for.
- Do NOT include purely non-technical departments (Sales, Marketing, Legal, HR, Finance) unless we're specifically looking for those roles.

Return ONLY the JSON object.
"""

    try:
        response = _call_llm(prompt)
        data = _parse_json_from_response(response)

        # Extract URLs and validate them
        urls = [u for u in data.get("job_listing_urls", [])
                if isinstance(u, str) and u.startswith("http")]

        elements = data.get("elements_to_expand") or []
        if isinstance(elements, str):
            elements = [elements]

        return NavigationPlan(
            job_listing_urls=urls,
            main_page_has_jobs=bool(data.get("main_page_has_jobs", False)),
            elements_to_expand=elements if elements else None,
        )

    except Exception as e:
        print(f"  LLM planning error: {e}")
        # Fallback: treat main page as having jobs
        return NavigationPlan(
            job_listing_urls=[],
            main_page_has_jobs=True,
        )


def extract_jobs_from_snapshot(
    snapshot: PageSnapshot,
    company_name: str,
) -> list[JobOpening]:
    """Use the LLM to extract structured job data from a page snapshot."""

    # Prioritize job-related links for the extraction prompt
    job_links = []
    other_links = []
    for link in snapshot.links:
        href_lower = link["href"].lower()
        text_lower = link["text"].lower()
        # Job detail pages often have patterns like /job/, /position/, req IDs, etc.
        if any(kw in href_lower for kw in ["job", "position", "req", "opening", "apply",
                                             "posting", "role", "vacancy"]):
            job_links.append(link)
        elif len(link["text"]) > 15 and not any(
            skip in text_lower for skip in ["privacy", "cookie", "terms", "contact", "about",
                                             "blog", "news", "login", "sign"]
        ):
            other_links.append(link)

    # Show job-related links first
    display_links = job_links + other_links
    links_text = "\n".join(
        f'  - "{link["text"]}" -> {link["href"]}'
        for link in display_links[:250]
    )

    prompt = f"""Extract ALL job openings from this careers page for {company_name}.

Page URL: {snapshot.url}

Page content:
{snapshot.text_content[:20000]}

Links on page:
{links_text}

Return a JSON array of EVERY job posting visible on this page. Each job should have:
- "title": the exact job title as shown (e.g. "Software Engineer - Platform")
- "url": the direct URL to the job posting (from the links list above)
- "location": where the job is located (e.g. "San Francisco, CA" or "Remote"), empty string if not shown
- "experience": years of experience required if mentioned (e.g. "3-5 years"), empty string if not stated
- "date_posted": posting date if visible, empty string if not

RULES:
- Include EVERY job posting you can find on this page — do not skip any.
- Each job MUST have a title and a URL.
- A job posting typically has: a descriptive title (like "Software Engineer", "ML Platform Lead"), a link to apply/view details.
- Do NOT include: navigation links, department category names, blog posts, company info pages.
- If a link text IS a job title (e.g. "Backend Engineer - Distributed Systems"), include it.
- Do NOT invent URLs — only use URLs that appear in the links list above.
- If a job appears multiple times (same title + same URL), include it only once.
- Return ONLY the JSON array, no other text.
- If there are NO job openings visible, return: []
"""

    try:
        response = _call_llm(prompt)
        jobs_data = _parse_json_from_response(response)

        if not isinstance(jobs_data, list):
            return []

        jobs = []
        seen_urls = set()
        for item in jobs_data:
            title = item.get("title", "").strip()
            url = item.get("url", "").strip()
            if not title or not url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            jobs.append(JobOpening(
                job_id=url,
                company=company_name,
                title=title,
                url=url,
                location=item.get("location", ""),
                experience=item.get("experience", ""),
                date_posted=item.get("date_posted", ""),
            ))

        return jobs

    except Exception as e:
        print(f"  LLM extraction error for {company_name}: {e}")
        return []
