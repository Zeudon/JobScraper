"""Filter extracted jobs against user preferences (YAML + per-company roles)."""

import re

from jobscraper.config import RolePreference
from jobscraper.storage import JobOpening


def _parse_experience_years(exp_str: str) -> tuple[int | None, int | None]:
    """Parse experience strings like '3-5 years', '2+ years', '5 years' into (min, max)."""
    if not exp_str:
        return None, None

    exp_str = exp_str.lower().strip()

    # "3-5 years"
    match = re.search(r'(\d+)\s*[-–to]+\s*(\d+)', exp_str)
    if match:
        return int(match.group(1)), int(match.group(2))

    # "3+ years"
    match = re.search(r'(\d+)\s*\+', exp_str)
    if match:
        return int(match.group(1)), None

    # "3 years"
    match = re.search(r'(\d+)', exp_str)
    if match:
        val = int(match.group(1))
        return val, val

    return None, None


def _normalize_title(title: str) -> str:
    """Normalize a title for comparison — strip punctuation, collapse whitespace."""
    title = title.lower()
    # Replace / and - with spaces so "AI/ML" matches "AI ML"
    title = re.sub(r'[/\-_,]', ' ', title)
    # Collapse whitespace
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def _title_matches(job_title: str, pref_title: str) -> bool:
    """Fuzzy title match — checks if the preference title is meaningfully contained in the job title.

    "AI Engineer" should match "AI Engineer - Platform" or "Junior AI Engineer"
    but NOT "Sales Engineer - AI" (the role is Sales Engineer, not AI Engineer).
    """
    job_norm = _normalize_title(job_title)
    pref_norm = _normalize_title(pref_title)

    # Direct containment (strongest match)
    if pref_norm in job_norm:
        return True

    # Check if pref words appear as a near-contiguous phrase in the job title.
    # Allow up to 2 intervening words between pref words.
    job_words = job_norm.split()
    pref_words = pref_norm.split()

    if len(pref_words) == 1:
        # Single word pref (like "FDE") — must appear as a standalone word
        return pref_words[0] in job_words

    # Multi-word: find first pref word, then check remaining appear nearby
    for i, jw in enumerate(job_words):
        if jw == pref_words[0]:
            # Try to match remaining pref words within a window
            remaining = pref_words[1:]
            pos = i + 1
            matched_all = True
            for pw in remaining:
                # Look for this word within 3 positions
                found = False
                for offset in range(min(3, len(job_words) - pos)):
                    if pos + offset < len(job_words) and job_words[pos + offset] == pw:
                        pos = pos + offset + 1
                        found = True
                        break
                if not found:
                    matched_all = False
                    break
            if matched_all:
                return True

    return False


def _location_matches(job_location: str, pref_locations: list[str]) -> bool:
    """Check if job location matches any preferred location."""
    if not pref_locations:
        return True  # no location preference = accept any

    job_loc_lower = job_location.lower()
    for loc in pref_locations:
        if loc.lower() in job_loc_lower:
            return True
        # "Remote" also matches "Work from home", "WFH", etc.
        if loc.lower() == "remote" and any(
            term in job_loc_lower for term in ["remote", "wfh", "work from home", "anywhere"]
        ):
            return True

    return False


def _experience_matches(
    job_exp: str,
    pref_min: int | None,
    pref_max: int | None,
) -> bool:
    """Check if job experience requirement falls within the preferred range."""
    if pref_min is None and pref_max is None:
        return True  # no preference set

    job_min, job_max = _parse_experience_years(job_exp)
    if job_min is None:
        return True  # can't parse = don't filter out

    # Check overlap between ranges
    if pref_max is not None and job_min > pref_max:
        return False  # job requires more experience than user's max
    if pref_min is not None and job_max is not None and job_max < pref_min:
        return False  # job's max is below user's min

    return True


_SENIORITY_ABBREVIATIONS = {
    "senior": ["sr.", "sr ", "snr"],
    "staff": ["staff"],
    "principal": ["principal"],
    "lead": ["lead"],
    "director": ["dir.", "dir "],
    "manager": ["mgr.", "mgr "],
}


def _keyword_check(job_title: str, pref: RolePreference) -> bool:
    """Check include/exclude keywords against job title."""
    title_lower = job_title.lower()

    # Exclude keywords take priority — also check common abbreviations
    for kw in pref.keywords_exclude:
        kw_lower = kw.lower()
        if kw_lower in title_lower:
            return False
        # Also check abbreviations
        for abbr in _SENIORITY_ABBREVIATIONS.get(kw_lower, []):
            if abbr in title_lower:
                return False

    return True


def filter_jobs(
    jobs: list[JobOpening],
    preferences: list[RolePreference],
    company_roles: list[str],
) -> list[JobOpening]:
    """Filter jobs based on YAML preferences and per-company role restrictions.

    Filter hierarchy:
    1. If company_roles is non-empty, the job title must match at least one role
       in company_roles (this is a per-company restriction).
    2. The job must also match at least one YAML preference (title, location,
       experience, keywords).
    3. If company_roles is empty, only YAML preferences are used.
    """
    filtered = []

    for job in jobs:
        # Step 1: Per-company role restriction
        if company_roles:
            role_match = any(
                _title_matches(job.title, role) for role in company_roles
            )
            if not role_match:
                continue

        # Step 2: Match against YAML preferences
        matched = False
        for pref in preferences:
            if not _title_matches(job.title, pref.title):
                continue

            if not _location_matches(job.location, pref.locations):
                continue

            if not _experience_matches(job.experience, pref.experience_min, pref.experience_max):
                continue

            if not _keyword_check(job.title, pref):
                continue

            matched = True
            break

        if matched:
            filtered.append(job)

    return filtered
