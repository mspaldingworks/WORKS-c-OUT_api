#!/usr/bin/env python3
"""
Fill an employer's application form from a prepared draft, then stop.

Runs on the Mac, not the server: the whole point is that she reads the filled
form before it goes anywhere, and a form she reads has to be on her screen.

It never clicks submit. These postings apply through ATS portals that ask, under
her name, whether she is authorised to work and how she identifies for EEO
reporting. A script answering those is not something this will do — it fills the
mechanical fields, attaches the PDFs, and hands the tab over.

Usage:
    python3 tools/autofill.py --application 4
    python3 tools/autofill.py --list
    python3 tools/autofill.py --application 4 --dry-run
"""

import argparse
import json
import os
import pathlib
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.request

API_BASE = os.environ.get("JOB_SEARCH_API", "https://jobs.works-c-out.com")
XCCONFIG = pathlib.Path.home() / "Family_Appily_app" / "Local.xcconfig"

# Anything whose accessible name matches this is never clicked, whatever else
# happens. The script has no legitimate reason to press any of them.
NEVER_CLICK = re.compile(
    r"\b(submit|apply now|send application|finish|complete application|i agree|accept)\b", re.I
)

# Accessible-name patterns for the fields worth filling, most specific first —
# "first name" has to beat the bare "name" rule, and "email" must not match
# "email me about similar jobs".
FIELD_RULES = [
    ("first_name", r"^(first|given)\s*name"),
    ("last_name", r"^(last|family|sur)\s*name"),
    ("full_name", r"^(full\s*)?name$|^your name"),
    ("email", r"^e-?mail(\s*address)?\*?$"),
    ("phone", r"^(phone|mobile|telephone|cell)"),
    ("address", r"^(street\s*)?address(\s*line\s*1)?"),
    ("city", r"^city|^town"),
    ("state", r"^state|^province|^region"),
    ("zip", r"^zip|^postal"),
    ("linkedin", r"linkedin"),
    ("portfolio", r"website|portfolio|blog|personal site"),
    ("cover_letter_text", r"cover letter|why (are|do) you|tell us about yourself"),
]

FILE_RULES = [("resume", r"resume|cv|curriculum"), ("cover_letter", r"cover letter")]

# Dropdowns rendered as a button plus a menu, which is what most ATS use instead
# of a native <select>. Clicking one opens a list of menuitems to match against.
CHOICE_RULES = [("state", r"^state|^province|^region"), ("country", r"^country")]

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def token():
    if value := os.environ.get("JOB_SEARCH_API_TOKEN"):
        return value
    # The iOS build already holds the token; reuse it rather than keeping a
    # second copy somewhere else on disk.
    if XCCONFIG.exists():
        for line in XCCONFIG.read_text().splitlines():
            if line.startswith("JOB_SEARCH_API_TOKEN"):
                return line.split("=", 1)[1].strip()
    sys.exit("No API token. Set JOB_SEARCH_API_TOKEN or create Local.xcconfig.")


def ssl_context():
    """
    A verifying context, built explicitly.

    python.org builds on macOS ship without a CA bundle, so the default context
    fails to verify anything. Verification is not optional here — the API token
    travels in the Authorization header — so fall back to the system roots
    rather than turning checking off.
    """
    for candidate in (_certifi_path(), "/etc/ssl/cert.pem"):
        if candidate and os.path.exists(candidate):
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def _certifi_path():
    try:
        import certifi

        return certifi.where()
    except ImportError:
        return None


def api(path, binary=False):
    request = urllib.request.Request(
        f"{API_BASE}/{path}", headers={"Authorization": f"Token {token()}"}
    )
    with urllib.request.urlopen(request, timeout=120, context=ssl_context()) as response:
        return response.read() if binary else json.load(response)


def browser(*args, check=True):
    result = subprocess.run(
        ["agent-browser", *args], capture_output=True, text=True, timeout=180
    )
    if check and result.returncode != 0:
        print(f"   ! agent-browser {' '.join(args[:2])}: {result.stderr.strip()[:160]}")
    return result.stdout


def snapshot_refs():
    """{ref: {name, role}} from the interactive-only snapshot."""
    raw = browser("snapshot", "-i", "-c", "--json")
    try:
        return json.loads(raw)["data"]["refs"]
    except (json.JSONDecodeError, KeyError):
        return {}


def file_input_labels():
    """
    Map each file input's ref to the label printed above it.

    File inputs are all named "file-input" in the accessibility tree, so the
    interactive snapshot alone can't tell a resume box from a cover-letter box.
    The full tree carries the surrounding StaticText, which does.
    """
    labels, recent = {}, ""
    for line in browser("snapshot", "-c").splitlines():
        if text := re.search(r'StaticText\s+"([^"]+)"', line):
            recent = text.group(1)
        elif label := re.search(r'"([^"]*)"\s*\[ref=(e\d+)\]', line):
            name, ref = label.group(1), label.group(2)
            if name == "file-input":
                labels[ref] = recent
            elif name.lower().startswith("choose file"):
                continue
            else:
                recent = name or recent
    return labels


def values_for(application, profile):
    materials = application.get("generated_materials") or {}
    full_name = profile.get("legal_name", "")
    city_state = profile.get("city_state", "")
    city, _, state = city_state.partition(",")
    return {
        "first_name": full_name.split(" ")[0] if full_name else "",
        "last_name": " ".join(full_name.split(" ")[1:]),
        "full_name": full_name,
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "city": city.strip(),
        "state": state.strip(),
        "linkedin": profile.get("linkedin_url", ""),
        "portfolio": profile.get("portfolio_url", ""),
        "country": "United States",
        "cover_letter_text": materials.get("cover_letter", ""),
        "address": profile.get("street_address", ""),
        "zip": profile.get("postal_code", ""),
    }


def choose_from_menu(ref, label, wanted):
    """
    Drive a button-and-menu dropdown: open it, find the option, click it.

    Returns True if something was selected. On no match the menu is closed with
    Escape rather than left hanging over the rest of the form.
    """
    candidates = {w.strip().lower() for w in wanted if w and w.strip()}
    if not candidates:
        return False

    # Scroll it into view first: a control below the fold can be clicked but its
    # menu renders where it can't be read. Selecting from an earlier dropdown
    # also re-renders this one, so let the page settle before clicking or the
    # click lands mid-render and the menu never opens.
    browser("scrollintoview", f"@{ref}", check=False)
    browser("wait", "--load", "networkidle", check=False)
    browser("click", f"@{ref}")
    options = snapshot_refs()
    for option_ref, node in options.items():
        if node.get("role") not in {"menuitem", "option", "menuitemradio"}:
            continue
        if node.get("name", "").strip().lower() in candidates:
            browser("click", f"@{option_ref}")
            browser("wait", "--load", "networkidle", check=False)
            return True

    browser("press", "Escape", check=False)
    return False


def match_field(name):
    lowered = name.strip().lower().rstrip("*").strip()
    for key, pattern in FIELD_RULES:
        if re.search(pattern, lowered):
            return key
    return None


def run(application_id, dry_run):
    application = api(f"api/tracker/applications/{application_id}/")
    profiles = api("api/identity/profile/")
    profile = profiles[0] if isinstance(profiles, list) and profiles else {}

    url = application.get("apply_url") or application.get("job_url")
    if not url:
        sys.exit("That application has no apply URL.")

    print(f"\n{application['role_title']} — {application['company_name']}")
    print(f"  {url}\n")

    values = values_for(application, profile)

    # Pull the PDFs down so they can be attached to file inputs.
    documents = {}
    if not dry_run:
        temp = pathlib.Path(tempfile.mkdtemp(prefix="autofill-"))
        for kind, endpoint in (("resume", "resume"), ("cover_letter", "cover-letter")):
            try:
                data = api(f"api/tracker/applications/{application_id}/download/{endpoint}/", binary=True)
                path = temp / f"{kind}.pdf"
                path.write_bytes(data)
                documents[kind] = str(path)
            except Exception as error:
                print(f"  ! couldn't fetch the {kind} PDF: {error}")

    if dry_run:
        print("  DRY RUN — values that would be filled:")
        for key, value in values.items():
            if value:
                print(f"    {key:<18} {str(value)[:60]}")
        return

    browser("open", url)
    browser("wait", "--load", "networkidle", check=False)

    refs = snapshot_refs()
    # Many postings show the job first and hide the form behind an Apply button.
    for ref, node in refs.items():
        if re.fullmatch(r"apply(\s+for\s+this\s+job)?", node.get("name", "").strip(), re.I):
            print(f"  opening the form ({node['name']})")
            browser("click", f"@{ref}")
            browser("wait", "--load", "networkidle", check=False)
            refs = snapshot_refs()
            break

    filled, skipped = [], []
    for ref, node in sorted(refs.items(), key=lambda kv: int(kv[0][1:])):
        name, role = node.get("name", ""), node.get("role", "")
        if role not in {"textbox", "combobox", "searchbox"} or not name:
            continue
        if NEVER_CLICK.search(name):
            continue
        key = match_field(name)
        if not key or not values.get(key):
            skipped.append(name)
            continue
        browser("fill", f"@{ref}", values[key])
        filled.append(f"{name} ← {key}")

    # Dropdowns are resolved by name, one at a time, re-snapshotting before each.
    # Refs die on any page change, and selecting from one menu re-renders the
    # form — so a ref captured for the second dropdown before touching the first
    # is already stale by the time we reach it.
    pending = sorted({
        (key, node.get("name", ""))
        for node in snapshot_refs().values()
        if node.get("role") in {"button", "combobox"} and node.get("name")
        for key, pattern in CHOICE_RULES
        if re.search(pattern, node["name"].strip().lower())
    })
    for key, name in pending:
        value = values.get(key, "")
        wanted = [value, STATES.get(value.upper(), "")] if key == "state" else [value]
        ref = next(
            (r for r, n in snapshot_refs().items()
             if n.get("name") == name and n.get("role") in {"button", "combobox"}),
            None,
        )
        if ref and choose_from_menu(ref, name, wanted):
            filled.append(f"{name.split()[0]} ← {key}")
        else:
            skipped.append(f"{name.split()[0]} (dropdown)")

    for ref, label in file_input_labels().items():
        kind = next((k for k, pattern in FILE_RULES if re.search(pattern, label, re.I)), None)
        if kind and documents.get(kind):
            browser("upload", f"@{ref}", documents[kind])
            filled.append(f"{label or 'file'} ← {kind}.pdf")
        else:
            skipped.append(f"file input ({label or 'unlabelled'})")

    shot = pathlib.Path.home() / f"autofill-{application_id}.png"
    browser("screenshot", str(shot))

    print("  filled:")
    for item in filled:
        print(f"    ✓ {item}")
    if skipped:
        print("  left for you:")
        for item in skipped:
            print(f"    – {item}")
    print(f"\n  screenshot: {shot}")
    print("  NOTHING WAS SUBMITTED. Review the tab, answer the EEO and")
    print("  work-authorisation questions yourself, then submit.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", type=int)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.list:
        for row in api("api/tracker/applications/"):
            if row["status"] in {"ready", "approved"}:
                print(f"{row['id']:<4} {row['status']:<9} {row['company_name'][:26]:<26} {row['role_title'][:40]}")
        return
    if not args.application:
        parser.error("pass --application <id> or --list")
    run(args.application, args.dry_run)


if __name__ == "__main__":
    main()
