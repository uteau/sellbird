"""The by-hand list: businesses Sellbird can't email, ready to contact another way.

Many of the best prospects for a local service have no website and no email
Sellbird can find, so they never reach the Writer. They still have a phone
number or an Instagram account on their listing. This builds one row per such
business with a WhatsApp link, its Instagram, and a message filled in from
``manual_outreach.message``, for the operator to send themselves (a DM, a
WhatsApp, a call). Nothing here sends anything.

Built on the company register (sellbird.register), so a company that was
emailed, opted out, is on the do-not-contact list, or is waiting on a draft
never shows up here.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import Counter
from urllib.parse import quote

from sellbird.register import _cell, build_register

COLUMNS = [
    "company", "sector", "area", "phone", "whatsapp", "instagram", "social", "email",
    "website", "contact_form", "no_website", "why", "message", "fit", "source",
]

# Register statuses that mean Sellbird is handling (or has handled) the
# company by email, or must not contact it at all.
SKIP = {"Excluded", "Opted out", "Emailed", "Replied", "Approved",
        "Draft waiting", "Ready", "Queued"}

CHAIN_MIN_BRANCHES = 3


def _name_key(name) -> str:
    return " ".join(str(name or "").lower().split())


_HANDLE_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)", re.IGNORECASE)


def instagram_url(raw: str) -> str:
    """A full Instagram profile URL from a URL, "@handle" or "handle", or ""."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _HANDLE_RE.search(raw)
    handle = m.group(1) if m else raw.lstrip("@").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", handle):
        return ""
    return f"https://www.instagram.com/{handle}/"


def whatsapp_link(phone: str, country_code: str = "", text: str = "") -> str:
    """A wa.me link for ``phone``, with ``text`` prefilled, or "".

    A number written with "+" or "00" keeps its own country code. A local
    number gets ``country_code`` in front unless it already starts with it;
    with no country code configured, a local number gets no link, since
    WhatsApp can't place it."""
    raw = (phone or "").strip()
    # Several numbers in one field ("+56 9 1234 5678; +56 32 123 4567"): the first.
    raw = re.split(r"[;,/]", raw)[0]
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return ""
    cc = re.sub(r"\D", "", country_code or "")
    if raw.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif cc and not (digits.startswith(cc) and len(digits) > len(cc) + 7):
        digits = cc + digits.lstrip("0")
    elif not cc:
        return ""
    link = f"https://wa.me/{digits}"
    return f"{link}?text={quote(text)}" if text else link


def listing_contacts(notes: str) -> dict[str, str]:
    """The "Instagram: ...", "Social: ..." and "Email: ..." lines discovery keeps in a
    company's notes (collectors.discover.listing_contact_notes)."""
    found = {"instagram": "", "social": "", "email": ""}
    for line in (notes or "").splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in found and value.strip() and not found[key]:
            found[key] = value.strip()
    return found


def fill_message(template: str, company: str, area: str, sector: str) -> str:
    """``template`` with its placeholders filled. Plain replacement, so a
    stray brace in the operator's text is never an error."""
    out = template or ""
    for keys, value in ((("{negocio}", "{company}"), company),
                        (("{comuna}", "{area}"), area),
                        (("{rubro}", "{sector}"), sector)):
        for key in keys:
            out = out.replace(key, value or "")
    return " ".join(out.split())


async def build_manual_list(query_db, config) -> list[dict]:
    """Every company to contact by hand, the ones with a phone or Instagram first."""
    register = await build_register(query_db, config)
    if not register:
        return []
    extra = {r["id"]: r for r in await query_db(
        "SELECT id, domain, phone, notes FROM companies")}
    settings = getattr(config, "manual_outreach", None)
    template = str(getattr(settings, "message", "") or "")
    country_code = str(getattr(settings, "country_code", "") or "")

    # The same name at CHAIN_MIN_BRANCHES or more places is a chain: its
    # branches don't buy their own website.
    branches = Counter(_name_key(r.get("company")) for r in register)

    rows = []
    for row in register:
        if row.get("status") in SKIP:
            continue
        if branches[_name_key(row.get("company"))] >= CHAIN_MIN_BRANCHES:
            continue
        company = extra.get(row["id"], {})
        has_site = bool(company.get("domain"))
        # A company with a website that Sellbird hasn't read yet may still
        # turn up an email: leave it to Sellbird for now.
        if has_site and row.get("status") != "Not contacted":
            continue
        contacts = listing_contacts(company.get("notes") or "")
        area = (row.get("area") or "").split(",")[0].strip()
        message = fill_message(template, row.get("company") or "", area, row.get("sector") or "")
        phone = (company.get("phone") or "").strip()
        rows.append({
            "company": row.get("company") or "",
            "sector": row.get("sector") or "",
            "area": row.get("area") or "",
            "phone": phone,
            "whatsapp": whatsapp_link(phone, country_code, message),
            "instagram": instagram_url(contacts["instagram"]),
            "social": contacts["social"],
            "email": contacts["email"],
            "website": row.get("website") or "",
            "contact_form": row.get("contact_form") or "",
            "no_website": "yes" if not has_site else "",
            "why": ("No website: the best kind of prospect for a new site"
                    if not has_site else row.get("why_not_contacted") or ""),
            "message": message,
            "fit": row.get("fit"),
            "source": row.get("found_via") or "",
        })
    rows.sort(key=lambda r: (not (r["phone"] or r["instagram"] or r["social"] or r["email"]),
                             not r["no_website"], -(r["fit"] or 0)))
    return _one_per_business(rows)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    return re.sub(r"[^a-z0-9]", "", text.encode("ascii", "ignore").decode())


def _one_per_business(rows: list[dict]) -> list[dict]:
    """The same business found by two sources (a map and a listing) once:
    the same name in the same town, or the same phone number. ``rows`` come
    best-first, so the copy with the most ways to reach it is the one kept."""
    seen_names, seen_phones, out = set(), set(), []
    for r in rows:
        name = (_fold(r["company"]), _fold((r["area"] or "").split(",")[0]))
        phone = re.sub(r"\D", "", r["phone"])[-8:]
        if name in seen_names or (len(phone) == 8 and phone in seen_phones):
            continue
        seen_names.add(name)
        if len(phone) == 8:
            seen_phones.add(phone)
        out.append(r)
    return out


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in COLUMNS})
    return buf.getvalue()
