"""IPP media names -> the printer's paper sizes and media types. jobwriter.PAPER is keyed by PWG media names
(PWG 5101.1) and jobwriter.MEDIA_TYPES by IPP media-type keywords, so this only handles suffixes and short names."""
from .jobwriter import MEDIA_TYPES, PAPER

DEFAULT_PAPER = "iso_a4_210x297mm"
DEFAULT_TYPE = "stationery"
SHORT_NAMES = {
    "a4": "iso_a4_210x297mm", "letter": "na_letter_8.5x11in", "legal": "na_legal_8.5x14in",
    "a5": "iso_a5_148x210mm", "b5": "jis_b5_182x257mm", "executive": "na_executive_7.25x10.5in",
    "env10": "na_number-10_4.125x9.5in", "dl": "iso_dl_110x220mm", "c5": "iso_c5_162x229mm",
    "monarch": "na_monarch_3.875x7.5in",
}


def paper_from_ipp(media, default=DEFAULT_PAPER):
    """IPP 'media' value (PWG name, possibly with a source suffix) or a short name (a4, letter, ...) -> PAPER key"""
    if not media:
        return default
    name = media.split(",")[0].strip()
    return name if name in PAPER else SHORT_NAMES.get(name.lower(), default)


def media_type_from_ipp(media_type, default=DEFAULT_TYPE):
    """IPP media-type keyword -> MEDIA_TYPES key. 'envelope' and unknown types print as stationery; the envelope
    sizes carry the envelope type code themselves (jobwriter.PAPER)."""
    t = (media_type or "").strip()
    return t if t in MEDIA_TYPES else default
