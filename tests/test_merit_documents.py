"""merit_documents: every write is a preview first, then exactly the previewed payload once approved."""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from merit_aktiva_mcp.files import ServerFiles
from merit_aktiva_mcp import merit_documents, merit_write

VAT24 = "b9b25735-6a15-4d4e-8720-25b254ae3d21"
CUST = "11111111-1111-1111-1111-111111111111"
OFFER = "22222222-2222-2222-2222-222222222222"
INV = "33333333-3333-3333-3333-333333333333"
TAXES = [{"Id": VAT24, "Code": "24", "Name": "24% käibemaks", "TaxPct": 24.0, "NonActive": False}]
ITEMS = [{"Code": "TEENUS", "Name": "Teenus", "UnitofMeasureName": "h"},
         {"Code": "lamp", "Name": "Lamp", "UnitofMeasureName": "tk"}]
ACCOUNTS = [{"Code": "1020", "Name": "Pank", "NonActive": False},
            {"Code": "4000", "Name": "Kulud", "NonActive": False},
            {"Code": "4999", "Name": "Vana", "NonActive": True}]
OFFER_ROW = {"ArticleCode": "TEENUS", "Description": "Konsultatsioon", "Quantity": 2, "Price": 50,
             "AmountExclVat": 100, "VatAmount": 24, "TaxId": VAT24}


class FakeMerit:
    """Answers by path; records every call so tests can assert exactly what was sent."""

    def __init__(self, answers: dict):
        self.answers = {"v2/taxes": TAXES, "v1/getitems": ITEMS, "v2/accounts": ACCOUNTS,
                        "v1/getcustomers": [{"CustomerId": CUST, "Name": "Klient OÜ", "PaymentDeadLine": 7}],
                        **answers}
        self.calls: list[tuple[str, object]] = []

    async def call(self, path, payload=None, method="POST", text_ok=False):
        self.calls.append((path, payload))
        a = self.answers.get(path, [])
        return a(payload) if callable(a) else a

    async def call_period(self, path, start, end, extra=None):
        self.calls.append((path, {"start": start, "end": end, **(extra or {})}))
        a = self.answers.get(path, [])
        return a(extra) if callable(a) else a

    def sent(self, path):
        return [p for c, p in self.calls if c == path]


class FakeUploads:
    def __init__(self, files: dict):
        self.files = files

    def get(self, upload_id):
        name, data = self.files[upload_id]
        return name, data, ""


WRITE_WORDS = ("send", "update", "offer2inv", "setofferstatus", "delete")


def _writes(fake):
    return [p for p, _ in fake.calls if any(w in p.lower() for w in WRITE_WORDS)]


def run(fake: FakeMerit, tool: str, args: dict, files: dict | None = None):
    """A dry run, which must write nothing, then the real call."""
    mcp = FastMCP("test")
    merit_documents.register_documents(mcp, fake, merit_write.Books(fake), ServerFiles(FakeUploads(files or {})))

    async def go():
        async with Client(mcp) as client:
            first = (await client.call_tool(tool, {**args, "dry_run": True})).structured_content
            assert first["written"] is False and _writes(fake) == [], _writes(fake)
            second = (await client.call_tool(tool, args)).structured_content
            return first, second

    return asyncio.run(go())


def preview_only(fake, tool, args, files=None):
    mcp = FastMCP("test")
    merit_documents.register_documents(mcp, fake, merit_write.Books(fake), ServerFiles(FakeUploads(files or {})))

    async def go():
        async with Client(mcp) as client:
            return (await client.call_tool(tool, {**args, "dry_run": True})).structured_content

    return asyncio.run(go())


# ---------------------------------------------------------------------------- general ledger

GL_ARGS = {"doc_no": "K-1", "entry_date": "2026-11-20",
           "lines": [{"account": "4000", "debit": 100.004, "memo": "Korrigeerimine"}, {"account": "1020", "credit": 100}]}


def test_gl_entry_previews_then_sends_exactly_the_payload():
    fake = FakeMerit({"v1/sendglbatch": {"Id": "44444444-4444-4444-4444-444444444444"}})
    first, second = run(fake, "merit_create_gl_entry", GL_ARGS)
    assert first["written"] is False and first["dry_run"] is True
    assert first["preview"]["lines"][0] == {"account": "4000", "account_name": "Kulud", "debit": 100.0,
                                            "memo": "Korrigeerimine"}
    assert fake.sent("v1/sendglbatch") == [{
        "DocNo": "K-1", "BatchDate": "20261120", "CurrencyCode": "EUR",
        "EntryRow": [{"AccountCode": "4000", "Debit": 100.0, "Credit": 0.0, "Memo": "Korrigeerimine"},
                     {"AccountCode": "1020", "Debit": 0.0, "Credit": 100.0}]}]
    assert second == {"written": True, "gl_batch_id": "44444444-4444-4444-4444-444444444444", "doc_no": "K-1",
                      "total": 100.0}


def test_gl_entry_unbalanced_is_refused():
    fake = FakeMerit({})
    args = {**GL_ARGS, "lines": [{"account": "4000", "debit": 100}, {"account": "1020", "credit": 90}]}
    with pytest.raises(ToolError, match="does not balance"):
        preview_only(fake, "merit_create_gl_entry", args)
    assert not fake.sent("v1/sendglbatch")


def test_gl_entry_inactive_account_and_duplicate_are_refused():
    with pytest.raises(ToolError, match="not active"):
        preview_only(FakeMerit({}), "merit_create_gl_entry",
                     {**GL_ARGS, "lines": [{"account": "4999", "debit": 1}, {"account": "1020", "credit": 1}]})
    dupe = FakeMerit({"v1/getglbatches": [{"Document": "k-1", "BatchCode": "PK", "No": 7, "BatchDate": "2026-11-01"}]})
    with pytest.raises(ToolError, match="already in Merit"):
        preview_only(dupe, "merit_create_gl_entry", GL_ARGS)


def test_gl_entry_without_id_in_answer_is_confirmed_by_lookup():
    found = [{"Document": "K-1", "GLBId": "55555555-5555-5555-5555-555555555555"}]
    fake = FakeMerit({"v1/sendglbatch": "OK", "v1/getglbatches": lambda extra: found if fake.sent("v1/sendglbatch") else []})
    _first, second = run(fake, "merit_create_gl_entry", GL_ARGS)
    assert second["gl_batch_id"] == "55555555-5555-5555-5555-555555555555"


# ---------------------------------------------------------------------------- sales offers

OFFER_ARGS = {"customer_id": CUST, "doc_date": "2026-11-20",
              "rows": [{"item_code": "TEENUS", "description": "Konsultatsioon", "quantity": 2, "price": 50, "vat": "24"}]}


def test_create_offer_numbers_counts_vat_and_sends():
    fake = FakeMerit({"v2/getoffers": [{"OfferNo": "1009"}, {"OfferNo": "P-3"}],
                      "v2/sendoffer": {"InvoiceId": OFFER, "InvoiceNo": "1010", "CustomerId": CUST},
                      "v2/getoffer": {"OfferId": OFFER, "OfferNo": "1010", "TotalAmount": 100, "TaxAmount": 24}})
    first, second = run(fake, "merit_create_sales_offer", OFFER_ARGS)
    assert first["preview"]["offer_no"] == "1010" and first["preview"]["total_with_vat"] == 124.0
    assert first["preview"]["valid_until"] == "2026-12-20"
    assert fake.sent("v2/sendoffer") == [{
        "Customer": {"Id": CUST}, "DocDate": "20261120", "ExpireDate": "20261220", "OfferNo": "1010", "DocType": 1,
        "DocStatus": 1, "CurrencyCode": "EUR",
        "OfferRow": [{"Item": {"Code": "TEENUS", "Description": "Konsultatsioon"}, "Quantity": 2.0, "Price": 50.0,
                      "TaxId": VAT24, "DiscountPct": 0.0, "DiscountAmount": 0.0}],
        "TotalAmount": 100.0, "RoundingAmount": 0, "TaxAmount": [{"TaxId": VAT24, "Amount": 24.0}]}]
    assert second["written"] is True and second["offer_id"] == OFFER and "warning" not in second


def test_create_offer_refuses_a_used_number():
    fake = FakeMerit({"v2/getoffers": [{"OfferNo": "1009"}]})
    with pytest.raises(ToolError, match="already used"):
        preview_only(fake, "merit_create_sales_offer", {**OFFER_ARGS, "offer_no": "1009"})


def offer(status: int) -> dict:
    return {"OfferId": OFFER, "OfferNo": "1010", "CustomerName": "Klient OÜ", "DocStatus": status, "DocType": 1,
            "TotalAmount": 100, "TaxAmount": 24, "HComment": "Tere", "FComment": "", "OfferRow": [OFFER_ROW]}


def test_update_offer_status_and_text():
    state = {"o": offer(1)}

    def setstatus(p):
        state["o"] = {**state["o"], "DocStatus": p["NewStatus"]}
        return "OK"

    def update(p):
        state["o"] = {**state["o"], "FComment": p["FComment"]}
        return "OK"

    fake = FakeMerit({"v2/getoffer": lambda p: state["o"], "v2/setofferstatus": setstatus, "v2/updateoffer": update})
    first, second = run(fake, "merit_update_sales_offer", {"offer_id": OFFER, "status": "sent", "footer_comment": "Aitäh"})
    assert first["preview"]["changes"]["status"] == {"from": "created", "to": "sent"}
    assert fake.sent("v2/updateoffer") == [{"Id": OFFER, "OfferNo": "1010", "HComment": "Tere", "FComment": "Aitäh"}]
    assert fake.sent("v2/setofferstatus") == [{"Id": OFFER, "NewStatus": 2}]
    assert second == {"written": True, "offer_no": "1010", "verified": True}


def test_invoiced_offer_cannot_be_changed_or_invoiced_again():
    fake = FakeMerit({"v2/getoffer": offer(6)})
    with pytest.raises(ToolError, match="already turned into an invoice"):
        preview_only(fake, "merit_update_sales_offer", {"offer_id": OFFER, "status": "created"})
    with pytest.raises(ToolError, match="invoice created"):
        preview_only(fake, "merit_invoice_from_offer", {"offer_id": OFFER})


def test_invoice_from_offer():
    fake = FakeMerit({"v2/getoffer": offer(3), "v2/offer2inv": {"InvoiceId": INV, "InvoiceNo": "2026101", "RefNo": "123"},
                      "v2/getinvoice": {"Header": {"InvoiceNo": "2026101", "TotalSum": 124}}})
    first, second = run(fake, "merit_invoice_from_offer", {"offer_id": OFFER})
    assert first["preview"]["offer"]["rows"][0]["item"] == "TEENUS"
    assert fake.sent("v2/offer2inv") == [{"Id": OFFER}]
    assert second["invoice_no"] == "2026101" and "warning" not in second


# ---------------------------------------------------------------------------- recurring invoices

def test_recurring_invoice_payload_and_duplicate():
    args = {**OFFER_ARGS, "contract_no": "L-7", "start_date": "2026-12-01", "cycle": "month"}
    args.pop("doc_date")
    fake = FakeMerit({"v2/getpershaddress": [{"Id": "x", "InvoiceNo": "L-1", "Address": "Tamme 1"}],
                      "v2/sendperinvoice": {"InvoiceId": INV}})
    _first, second = run(fake, "merit_create_recurring_invoice", args)
    assert fake.sent("v2/sendperinvoice") == [{
        "Customer": {"Id": CUST}, "InvoiceNo": "L-7", "StartDate": "20261201", "NextDate": "20261201", "Cycle": 1,
        "Period": 2, "CurrencyCode": "EUR", "PriceInclVat": False,
        "InvoiceRow": [{"Item": {"Code": "TEENUS", "Description": "Konsultatsioon"}, "Quantity": 2.0, "Price": 50.0,
                        "TaxId": VAT24, "DiscountPct": 0.0, "DiscountAmount": 0.0}],
        "TaxAmount": [{"TaxId": VAT24, "Amount": 24.0}], "TotalAmount": 100.0, "TotalSum": 124.0, "RoundingAmount": 0}]
    assert second["recurring_invoice_id"] == INV
    with pytest.raises(ToolError, match="already has number"):
        preview_only(fake, "merit_create_recurring_invoice", {**args, "contract_no": "l-1"})


def test_meter_readings():
    fake = FakeMerit({"v2/getpershaddress": [{"Id": "x", "InvoiceNo": "L-1", "Address": "Tamme 4-2"}],
                      "v1/sendindvalues": "Näidud kanti üle 2 lepingu 2 reale ja kogused ning summad arvutati uuesti."})
    args = {"item_code": "TEENUS", "readings": [{"customer_id": CUST, "end_value": 1234.5},
                                                 {"address": "tamme 4-2", "quantity": 12}]}
    first, second = run(fake, "merit_send_meter_readings", args)
    assert first["preview"]["readings"][1] == {"address": "Tamme 4-2", "contracts": ["L-1"], "quantity": 12.0}
    assert fake.sent("v1/sendindvalues") == [{"ArtCode": "TEENUS", "IndValue": [
        {"CustomerId": CUST, "EndValue": 1234.5}, {"Address": "Tamme 4-2", "Quantity": 12.0}]}]
    assert second["contracts"] == 2 and second["rows"] == 2


# ---------------------------------------------------------------------------- inventory

LOCATIONS = [{"Code": "1", "Name": "Pood", "InBPrefix": "SL-", "InBNextNo": 5, "Loc2LocPrefix": "LL-", "Loc2LocNextNo": 4},
             {"Code": "2", "Name": "Ladu"}]


def test_inventory_transfer_takes_the_stocks_next_number():
    fake = FakeMerit({"v2/getlocations": LOCATIONS, "v2/SendInvMovement": {"Id": "66666666-6666-6666-6666-666666666666"}})
    args = {"movement": "transfer", "location": "1", "to_location": "2", "doc_date": "2026-11-20",
            "rows": [{"item_code": "lamp", "quantity": 10}]}
    first, second = run(fake, "merit_create_inventory_movement", args)
    assert first["preview"]["doc_no"] == "LL-4"
    assert fake.sent("v2/SendInvMovement") == [{"DocDate": "20261120", "DocNo": "LL-4", "Location1Code": "1", "Type": 3,
                                                "Rows": [{"ArticleCode": "lamp", "Quantity": 10.0}],
                                                "Location2Code": "2"}]
    assert second["doc_no"] == "LL-4"


def test_inventory_refuses_unknown_item_and_missing_cost():
    fake = FakeMerit({"v2/getlocations": LOCATIONS})
    with pytest.raises(ToolError, match="does not exist"):
        preview_only(fake, "merit_create_inventory_movement",
                     {"movement": "out", "location": "1", "rows": [{"item_code": "pirn", "quantity": 1}]})
    with pytest.raises(ToolError, match="unit_cost is required"):
        preview_only(fake, "merit_create_inventory_movement",
                     {"movement": "in", "location": "1", "rows": [{"item_code": "lamp", "quantity": 1}]})
    assert not fake.sent("v2/SendInvMovement")


# ---------------------------------------------------------------------------- fixed assets

FA_ANSWERS = {"v2/getfixassets": [{"InventaryNo": "PV-1", "Name": "Arvuti", "FAFroupName": "Arvutid"}],
              "v2/getfaresppersons": [{"Id": "a", "Name": "Mari Maasikas"}],
              "v2/getfalocations": [{"Id": "b", "Name": "Kontor"}]}


def test_fixed_asset_payload():
    fake = FakeMerit({**FA_ANSWERS, "v2/sendfixedassets": [{"Id": "c", "InventaryNo": "PV-2", "Name": "Monitor"}]})
    args = {"assets": [{"inventory_no": "PV-2", "name": "Monitor", "group": "arvutid", "depreciation_pct": 33.33,
                        "responsible_person": "mari maasikas", "location": "Kontor"}]}
    first, second = run(fake, "merit_create_fixed_assets", args)
    assert fake.sent("v2/sendfixedassets") == [{"FixedAssets": [{
        "InventaryNo": "PV-2", "Name": "Monitor", "FAFroupName": "arvutid", "DeprCalcMethod": 1, "DeprPct": 33.33,
        "RespPersonName": "Mari Maasikas", "Loc2name": "Kontor"}]}]
    assert second["fixed_assets"] == [{"id": "c", "inventory_no": "PV-2", "name": "Monitor"}]


def test_fixed_asset_refusals():
    fake = FakeMerit(FA_ANSWERS)
    base = {"inventory_no": "PV-2", "name": "Monitor", "group": "Arvutid", "depreciation_pct": 20, "location": "Kontor",
            "responsible_person": "Mari Maasikas"}
    with pytest.raises(ToolError, match="already used"):
        preview_only(fake, "merit_create_fixed_assets", {"assets": [{**base, "inventory_no": "pv-1"}]})
    with pytest.raises(ToolError, match="no responsible employee"):
        preview_only(fake, "merit_create_fixed_assets", {"assets": [{**base, "responsible_person": "Keegi"}]})


# ---------------------------------------------------------------------------- e-invoice XML

def einvoice(number="2026050", product=True) -> bytes:
    code = "<SellerProductId>TEENUS</SellerProductId>" if product else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<E_Invoice><Header><Date>2026-11-20</Date><FileId>1</FileId><Version>1.2</Version></Header>
<Invoice invoiceId="{number}" regNumber="12345678" sellerRegnumber="87654321">
<InvoiceParties><SellerParty><Name>Näidis Müüja OÜ</Name><RegNumber>87654321</RegNumber></SellerParty>
<BuyerParty><Name>Klient OÜ</Name><RegNumber>12345678</RegNumber></BuyerParty></InvoiceParties>
<InvoiceInformation><Type type="DEB"/><DocumentName>Arve</DocumentName><InvoiceNumber>{number}</InvoiceNumber>
<InvoiceDate>2026-11-20</InvoiceDate><DueDate>2026-11-27</DueDate></InvoiceInformation>
<InvoiceSumGroup><InvoiceSum>100.00</InvoiceSum><TotalVATSum>24.00</TotalVATSum><TotalSum>124.00</TotalSum>
<Currency>EUR</Currency></InvoiceSumGroup>
<InvoiceItem><InvoiceItemGroup><ItemEntry>{code}<Description>Konsultatsioon õhtul</Description>
<ItemDetailInfo><ItemAmount>2</ItemAmount><ItemPrice>50</ItemPrice></ItemDetailInfo><ItemSum>100.00</ItemSum>
</ItemEntry></InvoiceItemGroup></InvoiceItem></Invoice>
<Footer><TotalNumberInvoices>1</TotalNumberInvoices><TotalAmount>124.00</TotalAmount></Footer></E_Invoice>""".encode()


def test_sales_einvoice_sends_the_xml_text():
    xml = einvoice()
    fake = FakeMerit({"v2/getinvoices2": [], "v2/sendinvoicexml": {"InvoiceId": INV, "InvoiceNo": "2026050"},
                      "v2/getinvoice": {"Header": {"TotalSum": 124}}})
    first, second = run(fake, "merit_import_einvoice", {"direction": "sales", "upload_id": "up_1"},
                        {"up_1": ("arve.xml", xml)})
    p = first["preview"]
    assert (p["invoice_no"], p["seller"], p["buyer"], p["total_with_vat"]) == ("2026050", "Näidis Müüja OÜ", "Klient OÜ", "124.00")
    assert p["rows"] == [{"description": "Konsultatsioon õhtul", "quantity": "2", "unit_price": "50", "net": "100.00"}]
    assert fake.sent("v2/sendinvoicexml") == [xml.decode()]
    assert second["invoice_id"] == INV and "warning" not in second


def test_einvoice_refusals():
    # getinvoices2 may match part of a number: only an exact InvoiceNo is a duplicate.
    fake = FakeMerit({"v2/getinvoices2": [{"SIHId": INV, "InvoiceNo": "2026050"}, {"SIHId": "other", "InvoiceNo": "12026050"}]})
    files = {"dup": ("a.xml", einvoice()), "html": ("a.xml", b"<html><body/></html>"),
             "dtd": ("a.xml", b'<?xml version="1.0"?><!DOCTYPE E_Invoice [<!ENTITY a "x">]><E_Invoice/>'),
             "nocode": ("a.xml", einvoice(product=False))}
    for upload, msg in (("dup", "already in Merit"), ("html", "not an Estonian e-invoice"), ("dtd", "DOCTYPE"),
                        ("nocode", "SellerProductId")):
        with pytest.raises(ToolError, match=msg):
            preview_only(fake, "merit_import_einvoice", {"direction": "sales", "upload_id": upload}, files)
    assert not fake.sent("v2/sendinvoicexml")


def test_purchase_einvoice_refuses_a_bill_already_in_merit():
    fake = FakeMerit({"v1/getvendors": [{"VendorId": "v1", "Name": "Näidis Müüja OÜ"}],
                      "v2/getpurchorders": [{"VendorId": "v1", "BillNo": "2026050", "PIHId": "p1"}]})
    with pytest.raises(ToolError, match="already in Merit"):
        preview_only(fake, "merit_import_einvoice", {"direction": "purchase", "upload_id": "u"}, {"u": ("a.xml", einvoice())})


def test_purchase_einvoice_waits_for_approval():
    xml = einvoice("A-1")
    fake = FakeMerit({"v1/getvendors": [{"VendorId": "v1", "Name": "Näidis Müüja OÜ"}],
                      "v2/sendpurchorderxml": {"BillId": "77777777-7777-7777-7777-777777777777"}})
    _first, second = run(fake, "merit_import_einvoice", {"direction": "purchase", "upload_id": "u"}, {"u": ("a.xml", xml)})
    assert fake.sent("v2/sendpurchorderxml") == [xml.decode()]
    assert second["waiting_for_approval"] is True and second["bill_no"] == "A-1"


def test_einvoice_from_a_local_file_path(tmp_path):
    # Local mode: the file is read from this computer; upload ids are refused.
    from merit_aktiva_mcp.files import LocalFiles
    xml = einvoice("A-2")
    path = tmp_path / "bill.xml"
    path.write_bytes(xml)
    fake = FakeMerit({"v1/getvendors": [{"VendorId": "v1", "Name": "Tarnija OÜ"}],
                      "v2/sendpurchorderxml": {"BillId": "77777777-7777-7777-7777-777777777777"}})
    mcp = FastMCP("test")
    merit_documents.register_documents(mcp, fake, merit_write.Books(fake), LocalFiles())

    async def go():
        async with Client(mcp) as client:
            out = (await client.call_tool("merit_import_einvoice", {"direction": "purchase",
                                                                    "file_path": str(path)})).structured_content
            with pytest.raises(ToolError, match="upload links exist only on a server"):
                await client.call_tool("merit_import_einvoice", {"direction": "purchase", "upload_id": "u"})
            return out

    assert asyncio.run(go())["bill_no"] == "A-2"
    assert fake.sent("v2/sendpurchorderxml") == [xml.decode()]
