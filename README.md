# Google Maps & Website Lead Enrichment Automation

Automated pipeline that reads Google Maps place URLs from a Google Sheet (or local `.xlsx` file), extracts business profile metadata from Google Maps, crawls each business's linked website (including contact, reservation, and Spanish/English legal/privacy subpages) for email addresses, phone numbers, and social media profiles, and writes everything cleanly back into the corresponding rows of an Excel workbook.

## Column Mapping

| Column | Field | Source |
|---|---|---|
| **B** | Business Name | Google Maps |
| **C** | Category | Google Maps |
| **D** | Google Maps Link | Input Sheet |
| **E** | Full Address | Google Maps |
| **F** | Website | Google Maps |
| **G** | Email | Linked Website (+ contact / legal / privacy subpages) |
| **H** | Phone Number | Linked Website & Google Maps (merged, deduplicated, stored as text) |
| **L** | Social Media Link | Linked Website (Instagram, Facebook, TikTok, LinkedIn, X/Twitter, YouTube) |

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

Run the full automation directly against the Google Sheet and produce `leads_populated.xlsx`:

```bash
python main.py
```

Test on a small slice first (e.g. rows 9–20):

```bash
python main.py --start-row 9 --end-row 20 --output test_leads.xlsx
```

Run from a local `.xlsx` file and overwrite existing non-empty cells:

```bash
python main.py --input input_sheet.xlsx --output leads_populated.xlsx --overwrite
```

Optional direct sync back to Google Sheets (requires a Google Cloud service account JSON shared as Editor on the sheet):

```bash
python main.py --gsheets-creds service_account.json
```

## Notes

- Progress is checkpointed continuously to `checkpoint.jsonl` so interrupted runs resume automatically without re-scraping completed rows. Pass `--fresh` to start clean.
- By default (`overwrite=False`), existing non-empty cells in the sheet (such as manual example rows 2–8 or pre-filled categories) are preserved while all blank target cells are populated.
"# collect-map-data-automation" 
