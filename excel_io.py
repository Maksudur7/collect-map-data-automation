"""Excel workbook reader/writer and optional Google Sheets sync."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment

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
from website_extractor import WebsiteResult, merge_phones


def load_sheet_tasks(
    excel_path: Path,
    sheet_name: str | None = None,
    start_row: int = 2,
    end_row: int | None = None,
) -> list[tuple[int, str]]:
    """Read (row_number, google_maps_url) pairs from the input Excel sheet."""
    wb = openpyxl.load_workbook(excel_path, data_only=False)
    ws = wb[sheet_name] if sheet_name else wb.active
    max_r = min(end_row, ws.max_row) if end_row else ws.max_row
    tasks: list[tuple[int, str]] = []
    for row in range(max(2, start_row), max_r + 1):
        val = ws.cell(row, COL_MAPS_URL).value
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

        email_val = ", ".join(wres.emails[:3]) if (wres and wres.emails) else None
        combined_phones = merge_phones(
            wres.phones if wres else None,
            mres.phone if mres else None,
        )
        phone_val = ", ".join(combined_phones[:2]) if combined_phones else None
        social_val = ", ".join(wres.social_links) if (wres and wres.social_links) else None

        mappings = (
            (COL_NAME, name_val, "name", False),
            (COL_CATEGORY, cat_val, "category", False),
            (COL_ADDRESS, addr_val, "address", False),
            (COL_WEBSITE, web_val, "website", False),
            (COL_EMAIL, email_val, "email", False),
            (COL_PHONE, phone_val, "phone", True),
            (COL_SOCIAL, social_val, "social", False),
        )

        for col_idx, val, key, is_text_phone in mappings:
            cell = ws.cell(row, col_idx)
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

    # Improve readability without altering header styling or row order
    col_widths = {
        "A": 8,
        "B": 34,
        "C": 24,
        "D": 28,
        "E": 44,
        "F": 34,
        "G": 32,
        "H": 20,
        "L": 46,
    }
    for col_letter, width in col_widths.items():
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
    """Optionally push populated columns B,C,E,F,G,H,L back to Google Sheets using gspread."""
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

    updates: list[dict[str, Any]] = []
    target_cols = (COL_NAME, COL_CATEGORY, COL_ADDRESS, COL_WEBSITE, COL_EMAIL, COL_PHONE, COL_SOCIAL)
    for row in range(2, ws.max_row + 1):
        if not ws.cell(row, COL_MAPS_URL).value:
            continue
        for col in target_cols:
            val = ws.cell(row, col).value
            if val is not None and str(val).strip() != "":
                col_letter = openpyxl.utils.get_column_letter(col)
                sval = str(val)
                if col == COL_PHONE and sval.startswith("+"):
                    # Keep literal '+' in Google Sheets instead of formula
                    sval = "'" + sval
                updates.append({"range": f"{col_letter}{row}", "values": [[sval]]})

    wb.close()
    if updates:
        gws.batch_update(updates, value_input_option="USER_ENTERED")
    return len(updates)
