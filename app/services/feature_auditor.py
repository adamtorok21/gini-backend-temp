"""
Feature Auditor Service
========================
Reads the Google Sheets tracker and the certified_registry.json written by the
autonomous QA loop (scripts/qa_orchestrator.py) to produce a single merged
status that the /quality dashboard reads.

This service does NOT run tests itself — it is a read/sync layer only.
Actual test execution + AI fixing happens in the QA loop CI job.

Runs every 12 hours in the background to keep the dashboard current.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from app.services.google_sheets import get_workbook

if sys.stdout.encoding != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

logger = logging.getLogger(__name__)

FEATURE_SHEET_ID = "14aIShymyjqNy0ZqE_YEeDNA1ZH8QjAmpmqefR_MJjV4"
DATA_DIR          = "data"
CERTIFICATION_PATH = os.path.join(DATA_DIR, "certified_registry.json")


class FeatureAuditorService:
    """Merges sheet status + QA-loop results into a single certified registry."""

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)

    def _load_qa_registry(self) -> Dict[str, Dict]:
        """Load the registry written by the QA loop orchestrator (keyed by feature name)."""
        path = Path(CERTIFICATION_PATH)
        if not path.exists():
            return {}
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
            return {r["feature"]: r for r in records if isinstance(r, dict)}
        except Exception:
            return {}

    async def synchronize_and_audit(self):
        """Sync tracker sheet with QA loop results and write merged registry."""
        try:
            logger.info("Feature auditor: syncing sheet vs QA loop registry…")
            workbook = get_workbook(FEATURE_SHEET_ID)
            sheet    = workbook.get_worksheet(0)
            records  = sheet.get_all_records()

            qa_registry = self._load_qa_registry()
            merged: List[Dict[str, Any]] = []

            for r in records:
                m_val = str(r.get("Milestone", "")).strip()
                if m_val not in ["1", "2", "Milestone 1", "Milestone 2"]:
                    continue

                name = str(r.get("Feature / Deliverable", "")).strip()
                if not name:
                    continue

                sheet_done = "yes" in str(r.get("Completed Yes/No", "")).lower()

                # QA loop result takes precedence when available
                qa_entry = qa_registry.get(name)
                if qa_entry:
                    status     = qa_entry.get("status", "pending")
                    source     = qa_entry.get("source", "qa-loop")
                    confidence = qa_entry.get("confidence", "high" if status == "passed" else "none")
                    timestamp  = qa_entry.get("timestamp", datetime.utcnow().isoformat())
                elif sheet_done:
                    # Sheet says done but QA loop has not run yet — mark as sheet-confirmed
                    status     = "passed"
                    source     = "sheet-confirmed"
                    confidence = "medium"
                    timestamp  = datetime.utcnow().isoformat()
                else:
                    status     = "pending"
                    source     = "sheet-sync"
                    confidence = "none"
                    timestamp  = datetime.utcnow().isoformat()

                merged.append({
                    "feature"     : name,
                    "status"      : status,
                    "sheet_status": r.get("Completed Yes/No", "N/A"),
                    "confidence"  : confidence,
                    "certified"   : "YES" if status == "passed" else "NO",
                    "timestamp"   : timestamp,
                    "source"      : source,
                    "milestone"   : m_val,
                    "sl_no"       : r.get("Sl. No"),
                })

            # Write backend registry
            with open(CERTIFICATION_PATH, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)

            # Mirror to frontend public dir (for /quality dashboard)
            frontend_dir = Path("../bali-frontend/public/test-results")
            frontend_dir.mkdir(parents=True, exist_ok=True)
            with open(frontend_dir / "registry.json", "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)

            passed = sum(1 for e in merged if e["status"] == "passed")
            logger.info(f"Feature auditor: {passed}/{len(merged)} certified.")

        except Exception as e:
            logger.error(f"Feature auditor sync failed: {e}")


# ── Module-level singleton ────────────────────────────────────────────────────
feature_auditor = FeatureAuditorService()


async def start_auditor_loop():
    """
    Background loop started by main.py on app startup.
    Runs the feature auditor every 12 hours to keep the QA registry current.
    """
    while True:
        try:
            await feature_auditor.synchronize_and_audit()
        except Exception as e:
            logger.error(f"Feature auditor loop error: {e}")
        await asyncio.sleep(12 * 60 * 60)

