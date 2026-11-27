"""Excel workbook reader/writer with dynamic header detection and optional Google Sheets sync."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import openpyxl

from config import (
    COL_ADDRESS,
    COL_CATEGORY,
    COL_EMAIL,
    COL_MAPS_URL,
    COL_NAME,
    COL_PHONE,
    COL_SOCIAL,
    COL_WEBSITE,
    extract_sheet_id_and_gid,
)
from maps_extractor import MapsPlaceResult
from website_extractor import WebsiteResult, merge_phones_smart

HEADER_ALIASES: dict[str, str] = {
    "businessname": "name",
    "name": "name",
    "category": "category",
    "fulladdress": "address",
    "address": "address",
    "website": "website",
    "email": "email",
    "phonenumber": "phone",
    "phone": "phone",
    "socialmedialink": "social",
    "socialmedialinks": "social",
    "socialmedia": "social",
    "social": "social",
}


def _norm_header(val: Any) -> str:
    if val is None:
        return ""
    return re.sub(r"[^a-z]", "", str(val).lower())


def detect_columns(ws) -> dict[str, int | None]:
    """Detect Google Maps link column and target output columns from Row 1 headers."""
    cols: dict[str, int | None] = {
        "maps_url": None,
        "name": None,
        "category": None,
        "address": None,
        "website": None,
        "email": None,
        "phone": None,
        "social": None,
    }

    # Scan sample rows to locate which column holds Google Maps links
    maps_counts: dict[int, int] = {}
    for row in range(2, min(ws.max_row, 30) + 1):
        for col in range(1, ws.max_column + 1):
            v = ws.cell(row, col).value
            if v and "google.com/maps" in str(v):
                maps_counts[col] = maps_counts.get(col, 0) + 1
    if maps_counts:
        cols["maps_url"] = max(maps_counts, key=maps_counts.get)
    else:
        cols["maps_url"] = COL_MAPS_URL

    found_any_header = False
    for col in range(1, ws.max_column + 1):
        key = HEADER_ALIASES.get(_norm_header(ws.cell(1, col).value))
        if key and cols[key] is None:
            cols[key] = col
            found_any_header = True

    if not found_any_header:
        cols.update({
            "name": COL_NAME,
            "category": COL_CATEGORY,
            "address": COL_ADDRESS,
            "website": COL_WEBSITE,
            "email": COL_EMAIL,
            "phone": COL_PHONE,
            "social": COL_SOCIAL,
        })

    return cols


def load_sheet_tasks(
    excel_path: Path,
    sheet_name: str | None = None,
    start_row: int = 2,
    end_row: int | None = None,
) -> list[tuple[int, str]]:
    """Read (row_number, google_maps_url) pairs from the input Excel sheet."""
    wb = openpyxl.load_workbook(excel_path, data_only=False)
    ws = wb[sheet_name] if sheet_name else wb.active
    cols = detect_columns(ws)
    maps_col = cols["maps_url"] or COL_MAPS_URL
    max_r = min(end_row, ws.max_row) if end_row else ws.max_row
    tasks: list[tuple[int, str]] = []
    for row in range(max(2, start_row), max_r + 1):
        val = ws.cell(row, maps_col).value
        if val and "google.com/maps" in str(val):
            tasks.append((row, str(val).strip()))
    wb.close()
    return tasks


def _should_write(existing: Any, new_val: str | None, overwrite: bool) -> bool:
    if not new_val:
        return False
    if overwrite:
        return True
    return existing is None or str(existing).strip() == ""


def write_results_to_excel(
    input_excel: Path,
    output_excel: Path,
    maps_results: dict[int, MapsPlaceResult],
    website_results: dict[int, WebsiteResult],
    sheet_name: str | None = None,
    overwrite: bool = False,
) -> dict[str, int]:
    """Write extracted values into exact corresponding rows of the Excel workbook."""
    wb = openpyxl.load_workbook(input_excel)
    ws = wb[sheet_name] if sheet_name else wb.active
    cols = detect_columns(ws)

    counts = {
        "rows_touched": 0,
        "name": 0,
        "category": 0,
        "address": 0,
        "website": 0,
        "email": 0,
        "phone": 0,
        "social": 0,
    }

    all_rows = sorted(set(maps_results.keys()) | set(website_results.keys()))
    for row in all_rows:
        mres = maps_results.get(row)
        wres = website_results.get(row)
        touched = False

        name_val = mres.name if mres else None
        cat_val = mres.category if mres else None
        addr_val = mres.address if mres else None
        web_val = mres.website if mres else None

        email_val = ", ".join(wres.emails[:2]) if (wres and wres.emails) else None
        combined_phones = merge_phones_smart(
            mres.phone if mres else None,
            wres.phones if wres else None,
            mres.address if mres else None,
        )
        phone_val = ", ".join(combined_phones[:2]) if combined_phones else None
        social_val = ", ".join(wres.social_links[:5]) if (wres and wres.social_links) else None

        mappings = (
            ("name", name_val, False),
            ("category", cat_val, False),
            ("address", addr_val, False),
            ("website", web_val, False),
            ("email", email_val, False),
            ("phone", phone_val, True),
            ("social", social_val, False),
        )

        for key, val, is_text_phone in mappings:
            col_idx = cols.get(key)
            if not col_idx:
                continue
            cell = ws.cell(row, col_idx)
            # Clean Google search screenreader artifact if present in existing Business Name
            if key == "name" and cell.value and " · Visited link" in str(cell.value):
                cell.value = str(cell.value).replace(" · Visited link", "").strip()
            if _should_write(cell.value, val, overwrite):
                cell.value = val
                if is_text_phone:
                    # Prevent Excel from interpreting leading '+' as a formula
                    cell.number_format = "@"
                    cell.data_type = "s"
                counts[key] += 1
                touched = True

        if touched:
            counts["rows_touched"] += 1

    # Adjust widths cleanly for detected columns
    desired_widths = {
        "maps_url": 28,
        "name": 36,
        "category": 24,
        "address": 46,
        "website": 36,
        "email": 34,
        "phone": 20,
        "social": 48,
    }
    for key, width in desired_widths.items():
        col_idx = cols.get(key)
        if col_idx:
            col_letter = openpyxl.utils.get_column_letter(col_idx)
            current = ws.column_dimensions[col_letter].width or 0
            if current < width:
                ws.column_dimensions[col_letter].width = width

    ws.freeze_panes = "A2"
    output_excel.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_excel)
    wb.close()
    return counts


def sync_to_google_sheet(
    excel_path: Path,
    sheet_url_or_id: str,
    creds_json_path: Path,
    sheet_name: str | None = None,
) -> int:
    """Optionally push populated target columns back to Google Sheets using gspread."""
    import gspread

    sheet_id, gid = extract_sheet_id_and_gid(sheet_url_or_id)
    gc = gspread.service_account(filename=str(creds_json_path))
    sh = gc.open_by_key(sheet_id)
    if sheet_name:
        gws = sh.worksheet(sheet_name)
    elif gid is not None:
        gws = next((w for w in sh.worksheets() if str(w.id) == str(gid)), sh.sheet1)
    else:
        gws = sh.sheet1

    wb = openpyxl.load_workbook(excel_path, data_only=False)
    ws = wb[sheet_name] if sheet_name else wb.active
    cols = detect_columns(ws)
    maps_col = cols["maps_url"] or COL_MAPS_URL
    phone_col = cols.get("phone")
    target_cols = [
        cols[k] for k in ("name", "category", "address", "website", "email", "phone", "social")
        if cols.get(k)
    ]

    updates: list[dict[str, Any]] = []
    for row in range(2, ws.max_row + 1):
        if not ws.cell(row, maps_col).value:
            continue
        for col in target_cols:
            val = ws.cell(row, col).value
            if val is not None and str(val).strip() != "":
                col_letter = openpyxl.utils.get_column_letter(col)
                sval = str(val)
                if col == phone_col and sval.startswith("+"):
                    # Keep literal '+' in Google Sheets instead of formula
                    sval = "'" + sval
                updates.append({"range": f"{col_letter}{row}", "values": [[sval]]})

    wb.close()
    if updates:
        gws.batch_update(updates, value_input_option="USER_ENTERED")
    return len(updates)
