"""Translations shared by the web app and the report generator (web/i18n/{en,fi,sv}.json)."""
from __future__ import annotations

import json
import math
from functools import lru_cache

from . import config as C

LANGS = ("en", "fi", "sv")
I18N_DIR = C.REPO_ROOT / "web" / "i18n"
LANG_NAME = {"en": "English", "fi": "Finnish", "sv": "Swedish"}


@lru_cache(maxsize=3)
def table(lang: str) -> dict:
    lang = lang if lang in LANGS else "en"
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def t(lang: str, key: str, **kw) -> str:
    s = table(lang).get(key) or table("en").get(key) or key
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def num(lang: str, x, digits: int = 0) -> str:
    """Locale number format: 1,234.5 (en) / 1 234,5 (fi, sv)."""
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "–"
    s = f"{float(x):,.{digits}f}"
    if lang in ("fi", "sv"):
        s = s.replace(",", " ").replace(".", ",")
    return s


def pct(lang: str, x, digits: int = 0) -> str:
    return num(lang, x, digits) + " %"


def tonnes(lang: str, x) -> str:
    return num(lang, x, 0 if abs(float(x)) >= 10 else 1) + " t"


def eur(lang: str, x) -> str:
    return ("€" + num(lang, x, 0)) if lang == "en" else (num(lang, x, 0) + " €")


def label(lang: str, prefix: str, code: str) -> str:
    v = table(lang).get(f"{prefix}.{code}") or table("en").get(f"{prefix}.{code}")
    return v or str(code)


@lru_cache(maxsize=1)
def _catalogue() -> dict:
    return json.loads((I18N_DIR / "catalogue.json").read_text(encoding="utf-8"))


def lever_name(lang: str, lever_id: str, default: str) -> str:
    return _catalogue()["lever"].get(lever_id, {}).get(lang, default)


def owner(lang: str, name: str) -> str:
    return _catalogue()["owner"].get(name, {}).get(lang, name)


def confidence(lang: str, c: str) -> str:
    return _catalogue()["confidence"].get(c, {}).get(lang, c)
