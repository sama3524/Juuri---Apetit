"""Paths and global settings for Juuri.

The data package is expected unzipped at ``package/APETIT_CLIMATE_NATURE_NAVIGATOR_2026`` next to
this repository's root (``make setup`` / ``python -m juuri setup`` does that). Override with the
``JUURI_PACKAGE`` environment variable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ZIP = REPO_ROOT / "Data" / "APETIT_CLIMATE_NATURE_NAVIGATOR_2026.zip"
PACKAGE_ROOT = Path(os.environ.get("JUURI_PACKAGE", REPO_ROOT / "package" / "APETIT_CLIMATE_NATURE_NAVIGATOR_2026"))
DATA = PACKAGE_ROOT / "data"
TASKS = PACKAGE_ROOT / "tasks"
OUTPUTS = Path(os.environ.get("JUURI_OUTPUTS", REPO_ROOT / "outputs"))
REVIEW_DIR = REPO_ROOT / "review"
DECISIONS_FILE = REVIEW_DIR / "decisions.csv"

INVENTORY_YEARS = (2021, 2022, 2023, 2024, 2025)
REPORT_YEAR = 2025
APETIT_CROPS = ["pea", "potato", "carrot", "spinach", "turnip_rape"]
CROP_MATERIAL = {"M100": "turnip_rape", "M110": "pea", "M111": "potato", "M112": "carrot", "M113": "spinach", "M114": "pea"}


def use_reference_tools() -> None:
    """Make the package's MIT-licensed ``ccn_tools`` importable (reference calculator, factors, scorer)."""
    tools = str(PACKAGE_ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)


def ensure_package() -> None:
    if not DATA.exists():
        raise FileNotFoundError(
            f"Data package not found at {PACKAGE_ROOT}. Run `python -m juuri setup` to unzip "
            f"{PACKAGE_ZIP.name}, or set JUURI_PACKAGE to the unzipped folder.")
