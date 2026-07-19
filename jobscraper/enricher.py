"""Enrich and validate jobs by visiting detail pages.

Uses the LLM to make the final relevance judgment and extract experience,
reading the full job description for context. This replaces brittle regex
and deterministic matching with an intelligent assessment.
"""

import asyncio

import yaml
from playwright.async_api import async_playwright

from jobscraper.config import RolePreference, get_env
from jobscraper.storage import JobOpening

PAGE_LOAD_TIMEOUT = 20_000


def _format_preferences_for_prompt(preferences: list[RolePreference], company_roles: list[str]) -> str:
    """Format user preferences into a clear prompt section."""
    lines = []
    lines.append("USER'S JOB PREFERENCES:")

    if company_roles:
        lines.append(f"  For this company, specifically looking for: {', '.join(company_roles)}")
        lines.append("")

    lines.append("  Role preferences (from most to least specific):")
    for pref in preferences:
        parts = [f'    - Title: "{pref.title}"']
        if pref.locations:
            parts.append(f"      Locations: {', '.join(pref.locations)}")
        if pref.experience_min is not None or pref.experience_max is not None:
            exp = ""
            if pref.experience_min is not None:
                exp += f"min {pref.experience_min}"
            if pref.experience_max is not None:
                exp += f" max {pref.experience_max}"
            parts.append(f"      Experience: {exp.strip()} years")
        if pref.keywords_include:
            parts.append(f"      Desired keywords: {', '.join(pref.keywords_include)}")
        if pref.keywords_exclude:
            parts.append(f"      Exclude if title contains: {', '.join(pref.keywords_exclude)}")
        lines.append("\n".join(parts))

    return "\n".join(lines)


async def enrich_and_judge_jobs(
    jobs: list[JobOpening],
    preferences: list[RolePreference],
    company_roles: list[str],
) -> list[JobOpening]:
    """Visit each job's detail page and use LLM to judge relevance + extract experience.

    This is the final validation step. The LLM reads the full job description and:
    1. Determines if the job genuinely matches the user's preferences
    2. Extracts the experience requirement
    3. Extracts the location if not already known

    Only runs on pre-filtered candidates (typically 5-20 jobs), so cost is minimal.
    """
    if not jobs:
        return []

    from jobscraper.extractor import _call_llm, _parse_json_from_response

    prefs_text = _format_preferences_for_prompt(preferences, company_roles)

    print(f"  Evaluating {len(jobs)} candidates via detail pages...")

    headless = get_env("BROWSER_MODE", "headed") != "headed"
    approved_jobs: list[JobOpening] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        for job in jobs:
            try:
                await page.goto(job.url, wait_until="domcontentloaded",
                                timeout=PAGE_LOAD_TIMEOUT)
                await asyncio.sleep(3)

                # Get page text content
                text = await page.evaluate("""() => {
                    const clone = document.body.cloneNode(true);
                    clone.querySelectorAll('script, style, noscript, svg, img, header, nav, footer, [class*="cookie"], [class*="banner"]').forEach(el => el.remove());
                    return clone.innerText.substring(0, 8000);
                }""")

                # Ask LLM to judge relevance and extract details
                prompt = f"""You are evaluating whether a job posting matches a user's preferences.

JOB POSTING:
  Title: {job.title}
  Company: {job.company}
  URL: {job.url}
  Listed Location: {job.location or 'Not specified on listing'}

FULL JOB DESCRIPTION:
{text[:6000]}

{prefs_text}

TASK: Evaluate this job against the user's preferences and return a JSON object:

{{
    "relevant": true/false,
    "experience_required": "X-Y years" or "X+ years" or "" if not mentioned,
    "location": "the actual job location from the description",
    "reason": "brief explanation of why relevant or not"
}}

RULES FOR RELEVANCE:
- The job title must genuinely match one of the user's preferred roles (not just share a word — "Sales Engineer" is NOT an "AI Engineer")
- The location must match one of the preferred locations for that role (or be Remote)
- If experience is stated and exceeds the user's max preference, mark as NOT relevant
- If the title contains seniority levels the user wants to exclude (senior, staff, principal, lead, director, manager, Sr., etc.), mark as NOT relevant
- If the company_roles restriction is set, the job must match one of those specific roles
- When in doubt about title match, consider the actual job responsibilities described

Return ONLY the JSON object.
"""

                response = _call_llm(prompt)
                data = _parse_json_from_response(response)

                is_relevant = data.get("relevant", False)
                experience = data.get("experience_required", "")
                location = data.get("location", "")
                reason = data.get("reason", "")

                if is_relevant:
                    job.experience = experience or job.experience
                    if location and not job.location:
                        job.location = location
                    approved_jobs.append(job)
                    print(f"    [YES] {job.title} | {experience} | {reason}")
                else:
                    print(f"    [NO]  {job.title} | {reason}")

            except Exception as e:
                # If we can't visit the page, keep the job (benefit of the doubt)
                err_msg = str(e).encode('ascii', errors='replace').decode()
                print(f"    [?]   {job.title} | Error: {err_msg} (keeping)")
                approved_jobs.append(job)

            await asyncio.sleep(1)  # polite delay

        await browser.close()

    print(f"  LLM approved: {len(approved_jobs)} / {len(jobs)} candidates")
    return approved_jobs
