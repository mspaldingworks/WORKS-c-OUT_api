# Auto-fill (Phase 3)

Fills an employer's application form from a prepared draft, then stops.

## Why it runs on the Mac

The model is "fill the form, you review and submit". A form you review has to be
on your screen, so this drives a real Chrome window here rather than running on
the VPS. The phone does the half it's good at — browse, rank, select, prepare,
approve, and record that you applied.

## It never submits

Every one of these postings applies through an ATS (Workday, iCIMS, Greenhouse,
BambooHR, SmartRecruiters) that asks, under your name, whether you're authorised
to work and how you identify for EEO reporting. Those answers are yours to give.
The script fills the mechanical fields, attaches the PDFs, and hands over the tab.
`NEVER_CLICK` in `autofill.py` is a hard block on any submit-shaped control.

## Setup

```bash
npm i -g agent-browser && agent-browser install     # one time
```

The API token is read from `JOB_SEARCH_API_TOKEN`, falling back to the
`Local.xcconfig` the iOS build already uses — no second copy on disk.

## Use

```bash
python3 tools/autofill.py --list                 # drafts ready to fill
python3 tools/autofill.py --application 4 --dry-run
python3 tools/autofill.py --application 4        # opens Chrome and fills
```

It prints what it filled, what it left, and writes a screenshot to `~/autofill-<id>.png`.

## What it handles, and what it doesn't

Matches fields by accessible name: first/last/full name, email, phone, address,
city, state, ZIP, LinkedIn, portfolio, and a cover-letter textarea. File inputs
are matched by the label printed above them — in the accessibility tree every
file input is just called "file-input", so the interactive snapshot alone can't
tell a resume box from a cover-letter box; the full tree carries the surrounding
text, which can.

Known gaps, reported rather than guessed at:

- **Custom dropdowns are handled** — the button-and-menu pattern most ATS use.
  Each is resolved by name and re-snapshotted immediately before use: selecting
  from one re-renders the others, and a ref captured earlier is already dead.
  State accepts either "KY" or "Kentucky".
- **Multi-step and account-gated flows.** Workday and iCIMS want an account
  before the form appears. Log in first, then run this on the form page.
- **Captchas and consent checkboxes.** Deliberately untouched.

Verified against a live BambooHR form: 9 fields including State, Country and
both PDFs, nothing submitted.
