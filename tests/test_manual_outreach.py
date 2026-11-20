"""The by-hand list: businesses Sellbird can't email, ready to reach another way."""

from types import SimpleNamespace

import pytest

from sellbird.collectors.discover import Business, OpenStreetMap, listing_contact_notes
from sellbird.config import ManualOutreachConfig
from sellbird.manual_outreach import (
    build_manual_list, fill_message, instagram_url, listing_contacts, to_csv, whatsapp_link,
)


def config(message="Hola, vi {negocio} en {comuna}."):
    return SimpleNamespace(
        icp=SimpleNamespace(industries=["pastelería"], titles=[], hiring_signals=[]),
        compliance=SimpleNamespace(do_not_contact=["blocked.example"]),
        channels=SimpleNamespace(email=SimpleNamespace(send_to_risky=False)),
        manual_outreach=ManualOutreachConfig(country_code="56", message=message))


def co(cid, name, domain="", phone="", notes="", **extra):
    return {"id": cid, "name": name, "domain": domain, "phone": phone, "notes": notes,
            "website": f"https://{domain}" if domain else "", "industry": "pastry",
            "location": "Viña del Mar, Valparaíso", "created_at": "2026-11-19 10:00",
            "source": "discover:osm", "source_url": f"https://www.openstreetmap.org/node/{cid}",
            **extra}


NO_SITE = co("c1", "Dulce Ejemplo", phone="+56 9 1234 5678",
             notes="Instagram: @dulce.ejemplo\nEmail: hola@dulce.example")
NO_SITE_NO_CONTACT = co("c2", "Sin Datos")
EMAILED = co("c3", "Ya Escrita", domain="escrita.example")
UNREAD_SITE = co("c4", "Sitio Sin Leer", domain="unread.example")
NO_EMAIL_FOUND = co("c5", "Sin Correo", domain="nomail.example", phone="32 212 3456",
                    prospects_checked_at="2026-11-19")
BLOCKED = co("c6", "Bloqueada", domain="blocked.example", prospects_checked_at="2026-11-19")


@pytest.mark.asyncio
async def test_only_businesses_sellbird_cannot_email_are_listed():
    companies = [NO_SITE, NO_SITE_NO_CONTACT, EMAILED, UNREAD_SITE, NO_EMAIL_FOUND, BLOCKED]

    async def query_db(sql, params=()):
        if "FROM companies" in sql:
            return companies
        if "FROM prospects" in sql:
            return [{"company_id": "c3", "first_name": "Ana", "last_name": "", "title": "Dueña",
                     "email": "ana@escrita.example", "email_status": "verified", "status": "contacted"}]
        if "FROM outbox" in sql:
            return [{"company_id": "c3", "status": "sent", "send_at": ""}]
        return []

    rows = await build_manual_list(query_db, config())
    names = [r["company"] for r in rows]
    # Reachable ones first, then the no-website one with nothing to go on.
    assert names == ["Dulce Ejemplo", "Sin Correo", "Sin Datos"]

    first = rows[0]
    assert first["no_website"] == "yes"
    assert first["instagram"] == "https://www.instagram.com/dulce.ejemplo/"
    assert first["email"] == "hola@dulce.example"
    assert first["message"] == "Hola, vi Dulce Ejemplo en Viña del Mar."
    assert first["whatsapp"].startswith("https://wa.me/56912345678?text=Hola%2C")
    assert rows[1]["whatsapp"].startswith("https://wa.me/56322123456")
    assert rows[1]["no_website"] == ""

    text = to_csv(rows)
    assert text.splitlines()[0].startswith("company,sector,area,phone,whatsapp")
    assert "'+56 9 1234 5678" in text   # a leading + never runs as a formula


@pytest.mark.asyncio
async def test_chains_are_left_out():
    chain = [co(f"k{i}", "Cafe  Cadena", phone="+56 9 1111 222{i}") for i in range(3)]
    pair = [co("p0", "Dos Locales"), co("p1", "Dos Locales", location="Quilpué")]

    async def query_db(sql, params=()):
        return chain + pair if "FROM companies" in sql else []

    rows = await build_manual_list(query_db, config())
    assert [r["company"] for r in rows] == ["Dos Locales", "Dos Locales"]


@pytest.mark.asyncio
async def test_a_business_found_by_two_sources_is_listed_once():
    from_map = co("m1", "Florería Ejemplo", location="Concón")
    from_listing = co("l1", "Floreria ejemplo", phone="+56 9 3333 4444",
                      location="Concón, Valparaíso")
    same_phone = co("l2", "Otro Nombre", phone="9 3333 4444", location="Quilpué")
    other_town = co("l3", "Florería Ejemplo", location="Quilpué")

    async def query_db(sql, params=()):
        return [from_map, from_listing, same_phone, other_town] if "FROM companies" in sql else []

    rows = await build_manual_list(query_db, config())
    assert [(r["company"], r["area"]) for r in rows] == [
        ("Floreria ejemplo", "Concón, Valparaíso"),   # the copy with a phone wins
        ("Florería Ejemplo", "Quilpué"),
    ]


@pytest.mark.parametrize("phone,cc,expect", [
    ("+56 9 1234 5678", "", "https://wa.me/56912345678"),
    ("9 1234 5678", "56", "https://wa.me/56912345678"),
    ("56912345678", "56", "https://wa.me/56912345678"),
    ("0056 9 1234 5678", "", "https://wa.me/56912345678"),
    ("+56 9 1234 5678; +56 32 212 3456", "", "https://wa.me/56912345678"),
    ("9 1234 5678", "", ""),          # local number, no country to place it in
    ("123", "56", ""),
    ("", "56", ""),
])
def test_whatsapp_links(phone, cc, expect):
    assert whatsapp_link(phone, cc) == expect


@pytest.mark.parametrize("raw,expect", [
    ("@dulce.ejemplo", "https://www.instagram.com/dulce.ejemplo/"),
    ("dulce_ejemplo", "https://www.instagram.com/dulce_ejemplo/"),
    ("https://instagram.com/dulce.ejemplo?igsh=x", "https://www.instagram.com/dulce.ejemplo/"),
    ("https://www.instagram.com/dulce.ejemplo/", "https://www.instagram.com/dulce.ejemplo/"),
    ("not a handle at all", ""),
    ("", ""),
])
def test_instagram_urls(raw, expect):
    assert instagram_url(raw) == expect


def test_message_placeholders_never_break_on_stray_braces():
    assert fill_message("{negocio} {x} en {area}, {rubro}", "Uno", "Concón", "florist") == \
        "Uno {x} en Concón, florist"
    assert fill_message("", "Uno", "", "") == ""


def test_listing_contacts_round_trip_through_company_notes():
    biz = Business(name="Uno", instagram="@uno.example", email="hola@uno.example")
    assert listing_contacts(listing_contact_notes(biz)) == {
        "instagram": "@uno.example", "social": "", "email": "hola@uno.example"}
    assert listing_contact_notes(Business(name="Dos")) == ""


@pytest.mark.parametrize("term,tag", [
    ("pastelería", "shop=pastry"),
    ("gasfitería", "craft=plumber"),
    ("climatización", "craft=hvac"),
    ("peluquería", "shop=hairdresser"),
    ("estética", "shop=beauty"),
    ("florería", "shop=florist"),
    ("veterinaria", "amenity=veterinary"),
    ("cafetería", "amenity=cafe"),
    ("contador", "office=accountant"),
])
def test_osm_knows_small_business_trades_in_spanish(term, tag):
    assert tag in OpenStreetMap()._tags(term)
