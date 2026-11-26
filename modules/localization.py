"""Server-side text localization.

Hardcoded UI strings in http_config.py (item names, descriptions, ...) live in
responses/localization.json, keyed by LANGUAGE and then a dotted path:
{"chi": {"ITEMS.501.name": "..."}}. The code keeps its literals as the fallback, so a missing key
or an empty file never changes the shipped game.

LANGUAGE is FO3_LANGUAGE (default "ind", Indonesian): /info/server/locale reports it and the client
picks externalAssets/language_<LANGUAGE>.big with it.

The table overlays at the bottom are called by http_config right after it builds ITEMS / PACKAGES /
MONTHLY / PACKS: the shop tables stay where the shop is, only their text comes from here.
"""
import json
import os

# responses/ sits next to this package.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


LANGUAGE = os.environ.get("FO3_LANGUAGE", "ind")
LOCALIZATION_FILE = os.path.join(ROOT, "responses", "localization.json")
_LOCALIZATION = None


def localization():
    """{language: {dotted_key: text}} from responses/localization.json (lazy, cached)."""
    global _LOCALIZATION
    if _LOCALIZATION is None:
        try:
            with open(LOCALIZATION_FILE, encoding="utf-8") as f:
                _LOCALIZATION = json.load(f) or {}
        except (OSError, ValueError):
            _LOCALIZATION = {}
    return _LOCALIZATION


def tr(key, default=None):
    """Translated text for `key` in the current LANGUAGE; `default` if absent."""
    return localization().get(LANGUAGE, {}).get(key, default)


def trf(key, default, **kw):
    """`tr()` with str.format placeholders, e.g. trf("TEAMCOLOR.season.description",
    "Pemain season {label}", label="07"). The literal default must carry the same names."""
    return tr(key, default).format(**kw)


# --- Table overlays: http_config calls these right after it builds ITEMS / PACKAGES / MONTHLY / PACKS


def localize_tuple_table(table, prefix, slots):
    """Overlay a table's tuple text fields from localization.json for the current LANGUAGE.

    Keys are "<prefix>.<row_id>.<field>" (e.g. "ITEMS.501.name"); `slots` maps a field
    name to its tuple index. Only fields present in the JSON are replaced, so every
    hardcoded literal stays as the fallback and the table's structure is untouched.
    """
    for key, text in localization().get(LANGUAGE, {}).items():
        parts = key.split(".")
        if len(parts) != 3 or parts[0] != prefix or parts[2] not in slots:
            continue
        try:
            row_id = int(parts[1])
        except ValueError:
            continue
        if row_id in table:
            row = list(table[row_id])
            row[slots[parts[2]]] = text
            table[row_id] = tuple(row)


def localize_pack_terms(packs, prefix="PACK_TERM."):
    """Translate recurring words inside pack names for the current LANGUAGE.

    Unlike whole-name overrides, this rewrites just the listed terms so a name like
    "World Legend Draft Pick 3" becomes "世界传奇 选秀 3" while numbers/grade suffixes
    survive. Keys are "<prefix><term>": "<translation>" in localization.json; terms are
    applied longest-first so overlapping phrases (e.g. "Team of the Season" vs "Season")
    resolve to the most specific one. `pool` (index 1) is never touched.
    """
    terms = {k[len(prefix):]: v for k, v in localization().get(LANGUAGE, {}).items()
             if k.startswith(prefix) and isinstance(v, str)}
    if not terms:
        return
    ordered = sorted(terms.items(), key=lambda kv: -len(kv[0]))  # longest phrase first
    for itemid, (name, pool) in list(packs.items()):
        new = name
        for term, zh in ordered:
            new = new.replace(term, zh)
        if new != name:
            packs[itemid] = (new, pool)
