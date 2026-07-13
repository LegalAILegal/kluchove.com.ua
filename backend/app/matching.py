"""Server-side port of the name/address matching used in index.html.

Kept behaviourally identical to the front-end so search results don't change
when the lookup moves behind the backend.
"""
import re

# Visually-identical Latin letters that appear as typos in the source data.
LATIN_TO_CYR = {
    "a": "а", "c": "с", "e": "е", "i": "і", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
}

_LATIN_RE = re.compile(r"[a-zA-Z]")
_APOS_RE = re.compile(r"[`'’ʼ]")
# NBSP, narrow spaces, hyphen-like separators, zero-width space -> plain space
_SPACEY_RE = re.compile(r"[   ‑–—​-]")
_WS_RE = re.compile(r"\s+")


def normalize_name(s: str) -> str:
    if not s:
        return ""
    s = _LATIN_RE.sub(lambda m: LATIN_TO_CYR.get(m.group(0), m.group(0)), str(s))
    s = s.lower()
    s = _APOS_RE.sub("'", s)
    s = _SPACEY_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s)
    return s.strip()


def name_variants(full_name: str) -> set[str]:
    """All normalized forms a stored name can match against.

    Aliases in source data look like "Surname Name Patronymic/AltSurname".
    """
    variants = {normalize_name(full_name)}
    if "/" in full_name:
        slash_parts = full_name.split("/")
        variants.add(normalize_name(slash_parts[0]))
        main_tokens = slash_parts[0].strip().split()
        if len(main_tokens) >= 2:
            rest = " ".join(main_tokens[1:])
            for alt_part in slash_parts[1:]:
                alt = alt_part.strip().split()
                if alt:
                    variants.add(normalize_name(alt[0] + " " + rest))
    return variants


def name_matches(query: str, full_name: str) -> bool:
    q = normalize_name(query)
    if not q:
        return False
    return q in name_variants(full_name)


ADDR_ABBR = ["смт", "вул", "просп", "пров", "корп", "буд", "кв", "пр", "м", "б", "с"]
UNIT_ABBR = ["кв", "буд", "б", "корп"]
# "not a letter" boundary — Python's re has no \p{L}, so enumerate Cyrillic + Latin.
_NL = r"[^A-Za-zА-Яа-яЁёЇїІіЄєҐґ]"


def normalize_address(addr: str) -> str:
    if not addr:
        return ""
    r = str(addr)
    for a in ADDR_ABBR:
        r = re.sub(rf"(^|{_NL}){re.escape(a)}\.?\s*,\s*", rf"\g<1>{a}. ",
                   r, flags=re.IGNORECASE)
    for a in UNIT_ABBR:
        r = re.sub(rf"(^|{_NL}){re.escape(a)}\.?(\d)", rf"\g<1>{a}. \g<2>",
                   r, flags=re.IGNORECASE)
    r = re.sub(r",(\S)", r", \1", r)
    r = _WS_RE.sub(" ", r)
    r = re.sub(r"\s+,", ",", r)
    return r.strip()


def normalize_phone(raw: str) -> str | None:
    """Normalize a stored phone to Kyivstar's `to` format: 380XXXXXXXXX (12 digits, no +)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("380") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 10:      # 0XXXXXXXXX
        return "38" + digits
    if len(digits) == 9:                                  # XXXXXXXXX (missing 0)
        return "380" + digits
    if digits.startswith("80") and len(digits) == 11:     # 80XXXXXXXXX
        return "3" + digits
    return None
