"""
Identifies which applicant tracking system a posting applies through.

The distinction that matters is whether the employer's portal makes you create
an account before it will even show the form. That changes what she can do from
the phone, so the feed marks those postings and links straight to the sign-in
page rather than dropping her on a form she can't fill.
"""

import re
import urllib.parse

# Workday tenants may or may not carry a locale before the site name:
#   /healogics/job/...              -> site is "healogics"
#   /en-US/UofLCareerSite/job/...   -> site is "UofLCareerSite"
LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")

# hostname fragment -> (label, requires_account, how to build the sign-in URL)
#
# "requires_account" is about seeing the form at all, not about whether an
# account is offered. Greenhouse and SmartRecruiters take an application from a
# stranger; Workday and iCIMS do not.
PLATFORMS = [
    ("myworkdayjobs.com", "Workday", True, "workday"),
    ("icims.com", "iCIMS", True, "origin"),
    ("taleo.net", "Taleo", True, "origin"),
    ("successfactors", "SAP SuccessFactors", True, "origin"),
    ("csod.com", "Cornerstone", True, "origin"),
    ("paycomonline.net", "Paycom", True, "origin"),
    ("paylocity.com", "Paylocity", True, "origin"),
    ("brassring.com", "BrassRing", True, "origin"),
    # Indeed is deliberately NOT account-gated: about half its links redirect
    # to the employer's own site, and the other half use Indeed Apply. Flagging
    # them all put a badge on 25 of 59 postings pointing at indeed.com's
    # homepage, which is both wrong and useless — a badge on half the feed
    # carries no information.
    ("indeed.com", "Indeed", False, None),
    ("greenhouse.io", "Greenhouse", False, None),
    ("grnh.se", "Greenhouse", False, None),
    ("smartrecruiters.com", "SmartRecruiters", False, None),
    ("bamboohr.com", "BambooHR", False, None),
    ("lever.co", "Lever", False, None),
    ("gusto.com", "Gusto", False, None),
    ("jobvite.com", "Jobvite", False, None),
    ("ashbyhq.com", "Ashby", False, None),
]


def _sign_in_url(url, parsed, style):
    if style == "origin":
        return f"{parsed.scheme}://{parsed.netloc}/"
    if style == "workday":
        # A Workday tenant's bare origin returns 406, and the sign-in page sits
        # under the site name. Keep any locale prefix: dropping it gives
        # /en-US/login, which 404s.
        segments = [segment for segment in parsed.path.split("/") if segment]
        keep = segments[:2] if segments and LOCALE.match(segments[0]) else segments[:1]
        if keep:
            return f"{parsed.scheme}://{parsed.netloc}/{'/'.join(keep)}/login"
        return f"{parsed.scheme}://{parsed.netloc}/"
    return url


def describe(url):
    """
    Returns {platform, requires_account, sign_in_url} for an apply URL.

    Unknown hosts are reported as not requiring an account: guessing wrong in
    that direction just means she finds out at the portal, whereas a false
    "account needed" badge would make a perfectly open application look shut.
    """
    blank = {"platform": "", "requires_account": False, "sign_in_url": ""}
    if not url:
        return blank

    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or "").lower()
    if not host:
        return blank

    for fragment, label, requires_account, style in PLATFORMS:
        if fragment in host:
            return {
                "platform": label,
                "requires_account": requires_account,
                "sign_in_url": _sign_in_url(url, parsed, style) if requires_account else "",
            }
    return blank
