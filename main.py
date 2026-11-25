"""End-to-end automation entrypoint for Google Maps + Website lead enrichment."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from config import DEFAULT_SHEET_URL, download_google_sheet
from excel_io import load_sheet_tasks, sync_to_google_sheet, write_results_to_excel
from maps_extractor import MapsPlaceResult, extract_maps_batch
from website_extractor import WebsiteResult, _rank_emails, extract_websites_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("automation")


def _load_checkpoint(
    path: Path,
    retry_web_errors: bool = False,
) -> tuple[dict[int, MapsPlaceResult], dict[int, WebsiteResult]]:
    maps_done: dict[int, MapsPlaceResult] = {}
    web_done: dict[int, WebsiteResult] = {}
    if not path.exists():
        return maps_done, web_done

    import urllib.parse

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            kind = rec.get("kind")
            payload: dict[str, Any] = rec.get("data", {})
            if kind == "maps":
                obj = MapsPlaceResult(**payload)
                if obj.name:
                    maps_done[obj.row] = obj
            elif kind == "web":
                obj = WebsiteResult(**payload)
                if retry_web_errors and obj.error:
                    continue
                site_host = urllib.parse.urlparse(obj.website).netloc
                obj.emails = _rank_emails(obj.emails, site_host)
                web_done[obj.row] = obj
    return maps_done, web_done


def _append_checkpoint(path: Path, kind: str, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": kind, "data": data}, ensure_ascii=False) + "\n")


async def run_pipeline(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir).resolve()
    input_excel = Path(args.input).resolve() if args.input else (work_dir / "input_sheet.xlsx")
    output_excel = Path(args.output).resolve() if args.output else (work_dir / "leads_populated.xlsx")
    checkpoint_path = work_dir / args.checkpoint

    if not args.input:
        logger.info("Downloading Google Sheet to %s ...", input_excel)
        download_google_sheet(args.sheet_url, input_excel)
    elif not input_excel.exists():
        raise FileNotFoundError(f"Input Excel file not found: {input_excel}")

    if args.fresh and checkpoint_path.exists():
        checkpoint_path.unlink()

    all_tasks = load_sheet_tasks(
        input_excel,
        sheet_name=args.sheet_name,
        start_row=args.start_row,
        end_row=args.end_row,
    )
    if args.limit:
        all_tasks = all_tasks[: args.limit]
    logger.info("Loaded %d Google Maps rows to process.", len(all_tasks))

    maps_results, web_results = _load_checkpoint(checkpoint_path, retry_web_errors=args.retry_errors)
    pending_maps = [(r, u) for r, u in all_tasks if r not in maps_results]
    logger.info(
        "Maps stage: %d cached in checkpoint, %d remaining.",
        len(all_tasks) - len(pending_maps),
        len(pending_maps),
    )

    if pending_maps:
        processed_maps = 0

        def _on_maps(res: MapsPlaceResult) -> None:
            nonlocal processed_maps
            processed_maps += 1
            if res.name:
                _append_checkpoint(checkpoint_path, "maps", res.to_dict())
            if processed_maps % 25 == 0 or processed_maps == len(pending_maps):
                logger.info("Maps progress: %d / %d", processed_maps, len(pending_maps))

        new_maps = await extract_maps_batch(
            pending_maps,
            concurrency=args.maps_concurrency,
            on_result=_on_maps,
        )
        maps_results.update(new_maps)

    # Queue websites for all target rows that have a website from Google Maps
    target_row_set = {r for r, _ in all_tasks}
    pending_web: list[tuple[int, str]] = []
    for row in sorted(target_row_set):
        mres = maps_results.get(row)
        if mres and mres.website and row not in web_results:
            pending_web.append((row, mres.website))

    logger.info("Website stage: %d websites to scrape.", len(pending_web))
    if pending_web:
        processed_web = 0

        def _on_web(res: WebsiteResult) -> None:
            nonlocal processed_web
            processed_web += 1
            _append_checkpoint(checkpoint_path, "web", res.to_dict())
            if processed_web % 25 == 0 or processed_web == len(pending_web):
                logger.info("Website progress: %d / %d", processed_web, len(pending_web))

        new_web = await extract_websites_batch(
            pending_web,
            concurrency=args.web_concurrency,
            use_browser_fallback=not args.no_browser_fallback,
            on_result=_on_web,
        )
        web_results.update(new_web)

    # Filter results to only rows selected in this run before writing
    filtered_maps = {r: v for r, v in maps_results.items() if r in target_row_set}
    filtered_web = {r: v for r, v in web_results.items() if r in target_row_set}

    counts = write_results_to_excel(
        input_excel=input_excel,
        output_excel=output_excel,
        maps_results=filtered_maps,
        website_results=filtered_web,
        sheet_name=args.sheet_name,
        overwrite=args.overwrite,
    )
    logger.info("Saved populated workbook to %s | Write counts: %s", output_excel, counts)

    if args.gsheets_creds:
        updated_cells = sync_to_google_sheet(
            excel_path=output_excel,
            sheet_url_or_id=args.sheet_url,
            creds_json_path=Path(args.gsheets_creds).resolve(),
            sheet_name=args.sheet_name,
        )
        logger.info("Synced %d cells back to Google Sheets.", updated_cells)

    maps_errors = sum(1 for r in filtered_maps.values() if r.error)
    web_errors = sum(1 for r in filtered_web.values() if r.error)
    logger.info("Done. Maps errors: %d | Website errors: %d", maps_errors, web_errors)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automate Google Maps + Website lead extraction into Excel / Google Sheets."
    )
    parser.add_argument("--sheet-url", default=DEFAULT_SHEET_URL, help="Google Sheet URL or spreadsheet ID.")
    parser.add_argument("--input", help="Optional local .xlsx input file instead of downloading Google Sheet.")
    parser.add_argument("--output", help="Output .xlsx path (default: leads_populated.xlsx).")
    parser.add_argument("--sheet-name", help="Optional worksheet tab name.")
    parser.add_argument("--work-dir", default=".", help="Working directory for downloaded input & checkpoints.")
    parser.add_argument("--checkpoint", default="checkpoint.jsonl", help="Checkpoint JSONL filename.")
    parser.add_argument("--start-row", type=int, default=2, help="First 1-based Excel row to process.")
    parser.add_argument("--end-row", type=int, help="Last 1-based Excel row to process (inclusive).")
    parser.add_argument("--limit", type=int, help="Process at most N rows (useful for testing).")
    parser.add_argument("--maps-concurrency", type=int, default=6, help="Concurrent Playwright tabs for Google Maps.")
    parser.add_argument("--web-concurrency", type=int, default=12, help="Concurrent HTTP workers for websites.")
    parser.add_argument("--no-browser-fallback", action="store_true", help="Disable Playwright fallback for websites.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite already populated cells in target columns.")
    parser.add_argument("--fresh", action="store_true", help="Ignore existing checkpoint and scrape from scratch.")
    parser.add_argument("--retry-errors", action="store_true", help="Re-scrape cached website rows that previously errored.")
    parser.add_argument("--gsheets-creds", help="Optional service-account JSON to sync results back to Google Sheets.")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    return asyncio.run(run_pipeline(args))


if __name__ == "__main__":
    sys.exit(main())
