"""Payment write tools: each writes nothing on the first call, sends exactly the previewed payload
after approval, and refuses unsafe payments (already paid, too much, ambiguous bank, unbalanced
settlement, a file that is not CAMT.053)."""

import asyncio
import copy
import sys
from dataclasses import dataclass

import pytest
from fastmcp import Client, FastMCP

from merit_aktiva_mcp.files import ServerFiles
from merit_aktiva_mcp import merit_payments, merit_write
from merit_aktiva_mcp.merit import _chunks, _ymd

BANK_ID = "11111111-1111-1111-1111-111111111111"
USD_BANK_ID = "22222222-2222-2222-2222-222222222222"
SALES_ID = "aaaaaaaa-0000-0000-0000-000000000001"
CREDIT_ID = "aaaaaaaa-0000-0000-0000-000000000002"
PURCH_ID = "bbbbbbbb-0000-0000-0000-000000000001"
OFFER_ID = "cccccccc-0000-0000-0000-000000000001"
PAYMENT_ID = "dddddddd-0000-0000-0000-000000000001"
CUSTOMER_ID = "eeeeeeee-0000-0000-0000-000000000001"
VENDOR_ID = "ffffffff-0000-0000-0000-000000000001"

BANKS = [
    {"BankId": BANK_ID, "Name": "Swedbank", "IBANCode": "EE38 2200 2210 2014 5685", "CurrencyCode": "EUR",
     "AccountCode": "1020"},
    {"BankId": USD_BANK_ID, "Name": "Swedbank USD", "IBANCode": "EE902200221099999999", "CurrencyCode": "USD",
     "AccountCode": "1021"},
]
ACCOUNTS = [{"Code": "8510", "Name": "Pangateenused"}, {"Code": "1020", "Name": "Swedbank"}]


def sales_invoice(id, no, total, paid=0.0, currency="EUR"):
    return {"Header": {"SIHId": id, "InvoiceNo": no, "CustomerId": CUSTOMER_ID, "CustomerName": "Kask ja Kuusk OÜ",
                       "CustomerRegNo": "14000000", "DocumentDate": "2026-11-02T00:00:00",
                       "DueDate": "2026-11-16T00:00:00", "CurrencyCode": currency, "TotalSum": total,
                       "PaidAmount": paid, "Paid": paid >= total > 0, "ReferenceNo": "20260491",
                       "ChangedDate": "2026-11-02T10:00:00"}, "Lines": [], "Payments": []}


class FakeMerit:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.sales = {SALES_ID: sales_invoice(SALES_ID, "2026049", 1240.0),
                      CREDIT_ID: sales_invoice(CREDIT_ID, "2026050", -240.0)}
        self.purchases = {PURCH_ID: {"Header": {"PIHId": PURCH_ID, "BillNo": "INV-7", "VendorId": VENDOR_ID,
                                                "VendorName": "Kask ja Kuusk OÜ", "VendorRegNo": "14000000",
                                                "DocumentDate": "2026-11-03T00:00:00", "CurrencyCode": "EUR",
                                                "TotalSum": 500.0, "PaidAmount": 0.0, "Paid": False,
                                                "ChangedDate": "2026-11-03T09:00:00"}, "Lines": [], "Payments": []}}
        self.offer_paid = 0.0
        self.payments = [{"PHId": PAYMENT_ID, "DocumentDate": "2026-11-10T00:00:00", "DocumentNo": "PA-12",
                          "BankName": "Swedbank", "Direction": 1, "CounterPartName": "Kask ja Kuusk OÜ",
                          "CurrencyCode": "EUR", "Amount": 100.0, "ChangedDate": "2026-11-10T12:00:00",
                          "PaymAPIDetails": [{"DocNo": "2026049", "DocAmount": 1240.0, "PaidAmount": 100.0,
                                              "CurrencyCode": "EUR"}]}]

    def writes(self):
        return [c for c in self.calls if not c[0].startswith(("v1/get", "v2/get", "v2/banks", "v2/accounts"))]

    async def call_period(self, path, start, end, extra=None):
        rows = []
        for s, e in _chunks(start, end):
            rows += await self.call(path, {"PeriodStart": _ymd(s), "PeriodEnd": _ymd(e), **(extra or {})}) or []
        return rows

    async def call(self, path, payload=None, method="POST", text_ok=False):
        self.calls.append((path, copy.deepcopy(payload)))
        if path == "v2/banks":
            return BANKS
        if path == "v2/accounts":
            return ACCOUNTS
        if path == "v2/getinvoices2":
            return [inv["Header"] for inv in self.sales.values() if inv["Header"]["InvoiceNo"] == payload["InvNo"]]
        if path == "v2/getinvoice":
            return copy.deepcopy(self.sales.get(payload["Id"], {}))
        if path == "v2/getpurchorder":
            return copy.deepcopy(self.purchases[payload["Id"]])
        if path == "v1/getcustomers":
            return [{"CustomerId": CUSTOMER_ID, "Name": "Kask ja Kuusk OÜ", "RegNo": "14000000"}]
        if path == "v2/getoffer":
            return {"OfferId": OFFER_ID, "OfferNo": "P-5", "CustomerName": "Kask ja Kuusk OÜ", "DocDate": "2026-11-05T00:00:00"}
        if path == "v2/getoffers":
            return [{"SIHId": OFFER_ID, "OfferNo": "P-5", "CustomerName": "Kask ja Kuusk OÜ", "DocumentDate": "2026-11-05",
                     "CurrencyCode": "EUR", "TotalSum": 300.0, "PaidAmount": self.offer_paid, "Paid": False,
                     "DocType": 3, "DocStatus": 3}]
        if path == "v2/getpayments":
            return list(self.payments)
        if path == "v2/sendpayment":
            h = next(i["Header"] for i in self.sales.values() if i["Header"]["InvoiceNo"] == payload["InvoiceNo"])
            h["PaidAmount"] += payload["Amount"]
            return "OK"
        if path == "v2/sendPaymentV":
            self.purchases[PURCH_ID]["Header"]["PaidAmount"] += payload["Amount"]
            return "OK"
        if path == "v2/sendPaymentO":
            self.offer_paid += payload["Amount"]
            return "OK"
        if path.startswith("v2/deletepayment/"):
            self.payments = [p for p in self.payments if p["PHId"] != path.rsplit("/", 1)[1]]
            return "OK"
        if path.startswith("v2/Banks/"):
            return {"BatchId": "99999999-0000-0000-0000-000000000001", "BatchInfo": "PA-77"}
        if path == "v2/sendsettlement":
            return {"DocumentId": "99999999-0000-0000-0000-000000000002", "DocumentNo": "TA-3"}
        if path == "v2/sendcamt53":
            return "Imporditi 2 makserida (ridu kokku 2 )."
        raise AssertionError(f"unexpected call {path}")


class FakeUploads:
    def __init__(self, files):
        self.files = files

    def get(self, upload_id):
        name, data = self.files[upload_id]
        return name, data, "application/xml"


def run(fake: FakeMerit, tool: str, args: dict, uploads=None) -> dict:
    """A dry run, which must write nothing, then the real call."""
    mcp = FastMCP("test")
    merit_payments.register_payments(mcp, fake, merit_write.Books(fake), ServerFiles(uploads or FakeUploads({})))

    async def go():
        async with Client(mcp) as client:
            first = (await client.call_tool(tool, {**args, "dry_run": True})).structured_content
            assert first["written"] is False and first["dry_run"] is True
            assert fake.writes() == [], "a dry run must not write"
            return first, (await client.call_tool(tool, args)).structured_content

    return asyncio.run(go())


def preview_only(fake: FakeMerit, tool: str, args: dict, uploads=None) -> dict:
    mcp = FastMCP("test")
    merit_payments.register_payments(mcp, fake, merit_write.Books(fake), ServerFiles(uploads or FakeUploads({})))

    async def go():
        async with Client(mcp) as client:
            return (await client.call_tool(tool, {**args, "dry_run": True})).structured_content

    return asyncio.run(go())


def test_sales_invoice_payment_by_number():
    fake = FakeMerit()
    first, out = run(fake, "merit_record_payment", {"kind": "sales_invoice", "invoice_no": "2026049", "bank": "EE382200221020145685",
                                                    "amount": 1240, "payment_date": "2026-11-20"})
    assert first["preview"]["paying"]["unpaid"] == 1240.0
    assert fake.writes() == [("v2/sendpayment", {
        "BankId": BANK_ID, "IBAN": "EE382200221020145685", "CustomerName": "Kask ja Kuusk OÜ", "InvoiceNo": "2026049",
        "RefNo": "20260491", "PaymentDate": "20261120000000", "Amount": 1240.0, "CurrencyCode": "EUR"})]
    assert out["written"] is True and out["paid"]["unpaid"] == 0


def test_purchase_invoice_part_payment():
    fake = FakeMerit()
    first, out = run(fake, "merit_record_payment", {"kind": "purchase_invoice", "document_id": PURCH_ID,
                                                    "bank": "swedbank", "amount": 200, "payment_date": "2026-11-20"})
    assert any("part payment" in n for n in first["preview"]["notes"])
    assert fake.writes() == [("v2/sendPaymentV", {
        "BankId": BANK_ID, "IBAN": "EE382200221020145685", "VendorName": "Kask ja Kuusk OÜ", "BillNo": "INV-7",
        "PaymentDate": "20261120000000", "Amount": 200.0, "CurrencyCode": "EUR"})]
    assert out["written"] is True


def test_sales_offer_payment():
    fake = FakeMerit()
    _, out = run(fake, "merit_record_payment", {"kind": "sales_offer", "document_id": OFFER_ID, "bank": BANK_ID,
                                                "amount": 300, "payment_date": "2026-11-20"})
    assert fake.writes() == [("v2/sendPaymentO", {
        "BankId": BANK_ID, "IBAN": "EE382200221020145685", "CustomerName": "Kask ja Kuusk OÜ", "OfferNo": "P-5",
        "PaymentDate": "20261120000000", "Amount": 300.0, "CurrencyCode": "EUR"})]
    assert out["written"] is True


def test_refuses_overpayment_paid_invoice_currency_and_ambiguous_bank():
    fake = FakeMerit()
    base = {"kind": "sales_invoice", "document_id": SALES_ID, "bank": "Swedbank", "payment_date": "2026-11-20"}
    with pytest.raises(Exception, match="more than the unpaid"):
        preview_only(fake, "merit_record_payment", {**base, "amount": 1300})
    with pytest.raises(Exception, match="more than one account"):
        preview_only(fake, "merit_record_payment", {**base, "bank": "swed", "amount": 10})
    with pytest.raises(Exception, match="allow_currency_mismatch"):
        preview_only(fake, "merit_record_payment", {**base, "bank": USD_BANK_ID, "amount": 10})
    fake.sales[SALES_ID]["Header"].update(PaidAmount=1240.0, Paid=True)
    with pytest.raises(Exception, match="already paid"):
        preview_only(fake, "merit_record_payment", {**base, "amount": 10})
    assert fake.writes() == []


def test_overpayment_allowed_explicitly():
    fake = FakeMerit()
    first, _ = run(fake, "merit_record_payment", {"kind": "sales_invoice", "document_id": SALES_ID, "bank": "Swedbank",
                                                  "amount": 1300, "allow_overpayment": True, "payment_date": "2026-11-20"})
    assert any("overpayment" in n for n in first["preview"]["notes"])
    assert fake.writes()[0][1]["Amount"] == 1300.0


def test_delete_payment():
    fake = FakeMerit()
    first, out = run(fake, "merit_delete_payment", {"payment_id": PAYMENT_ID, "payment_date": "2026-11-10"})
    assert first["preview"]["delete"]["paid_documents"][0]["document_no"] == "2026049"
    assert fake.writes() == [(f"v2/deletepayment/{PAYMENT_ID}", {})]
    assert out["written"] is True


def test_delete_unknown_payment_is_refused():
    with pytest.raises(Exception, match="no payment"):
        preview_only(FakeMerit(), "merit_delete_payment", {"payment_id": "dddddddd-0000-0000-0000-000000000009",
                                                           "payment_date": "2026-11-10"})


def test_bank_fee_expense():
    fake = FakeMerit()
    run(fake, "merit_record_bank_transaction", {"direction": "expense", "bank": "EE382200221020145685",
                                                "payment_date": "2026-11-20",
                                                "lines": [{"account": "8510", "amount": 1.5, "description": "Fee"}]})
    assert fake.writes() == [(f"v2/Banks/{BANK_ID}/ExpensePayments", {
        "DocumentDate": "2026-11-20", "CurrencyCode": "EUR",
        "Lines": [{"AccountCode": "8510", "Quantity": 1, "Price": 1.5, "Amount": 1.5, "Description": "Fee"}]})]


def test_bank_transaction_refuses_unknown_account():
    with pytest.raises(Exception, match="no account"):
        preview_only(FakeMerit(), "merit_record_bank_transaction",
                     {"direction": "income", "bank": BANK_ID, "lines": [{"account": "9999", "amount": 5}]})


def test_customer_prepayment():
    fake = FakeMerit()
    run(fake, "merit_record_prepayment", {"party": "customer", "party_id": CUSTOMER_ID, "bank": BANK_ID,
                                          "amount": 50, "payment_date": "2026-11-20"})
    assert fake.writes() == [(f"v2/Banks/{BANK_ID}/PrePayments/ForCustomer/{CUSTOMER_ID}", {
        "DocumentDate": "2026-11-20", "CurrencyCode": "EUR", "Amount": 50.0})]


def test_settlement_of_invoice_and_credit_invoice():
    fake = FakeMerit()
    run(fake, "merit_record_settlement", {"customer_lines": [{"invoice_id": SALES_ID, "amount": 240},
                                                             {"invoice_id": CREDIT_ID, "amount": -240}],
                                          "doc_date": "2026-11-20"})
    line = {"CustVendName": "Kask ja Kuusk OÜ", "CustVendId": CUSTOMER_ID, "CustVendRegNo": "14000000"}
    assert fake.writes() == [("v2/sendsettlement", {
        "DocDate": "20261120", "CurrencyCode": "EUR", "VendLines": [],
        "CustLines": [{**line, "DocNo": "2026049", "Amount": 240.0}, {**line, "DocNo": "2026050", "Amount": -240.0}]})]


def test_settlement_against_purchase_invoice_must_balance():
    with pytest.raises(Exception, match="must be equal"):
        preview_only(FakeMerit(), "merit_record_settlement",
                     {"customer_lines": [{"invoice_id": SALES_ID, "amount": 300}],
                      "vendor_lines": [{"invoice_id": PURCH_ID, "amount": 200}]})


CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
<Acct><Id><IBAN>EE382200221020145685</IBAN></Id><Ccy>EUR</Ccy></Acct>
<FrToDt><FrDtTm>2026-11-01T00:00:00</FrDtTm><ToDtTm>2026-11-15T23:59:59</ToDtTm></FrToDt>
<Ntry><Amt Ccy="EUR">1240.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-11-10</Dt></BookgDt></Ntry>
<Ntry><Amt Ccy="EUR">1.50</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-11-11</Dt></BookgDt></Ntry>
</Stmt></BkToCstmrStmt></Document>"""


def test_bank_statement_import():
    fake = FakeMerit()
    up = FakeUploads({"up_1": ("statement.xml", CAMT)})
    first, out = run(fake, "merit_import_bank_statement", {"upload_id": "up_1"}, uploads=up)
    st = first["preview"]["statements"][0]
    assert (st["account"], st["entries"], st["money_in"], st["money_out"]) == ("Swedbank", 2, 1240.0, 1.5)
    assert st["period"] == "2026-11-01 to 2026-11-15"
    assert first["preview"]["notes"]  # Merit already has a payment on that account in the period
    assert fake.writes() == [("v2/sendcamt53", CAMT.decode())]
    assert out["imported_payment_rows"] == 2


def test_bank_statement_refuses_other_files():
    up = FakeUploads({"pdf": ("x.pdf", b"%PDF-1.4"), "pain": ("p.xml", CAMT.replace(b"camt.053.001.02", b"pain.001.001.03")),
                      "unknown_iban": ("s.xml", CAMT.replace(b"EE382200221020145685", b"EE000000000000000000"))})
    with pytest.raises(Exception, match="not XML"):
        preview_only(FakeMerit(), "merit_import_bank_statement", {"upload_id": "pdf"}, uploads=up)
    with pytest.raises(Exception, match="not a CAMT.053"):
        preview_only(FakeMerit(), "merit_import_bank_statement", {"upload_id": "pain"}, uploads=up)
    with pytest.raises(Exception, match="no bank account in Merit"):
        preview_only(FakeMerit(), "merit_import_bank_statement", {"upload_id": "unknown_iban"}, uploads=up)


def test_bank_statement_picks_the_account_by_currency_when_an_iban_is_shared(monkeypatch):
    # LHV keeps EUR and USD as two accounts in Merit with one IBAN (seen live, 2026-11-21).
    shared = [{"BankId": BANK_ID, "Name": "LHV", "IBANCode": "EE382200221020145685", "CurrencyCode": "EUR"},
              {"BankId": USD_BANK_ID, "Name": "LHV USD", "IBANCode": "EE382200221020145685", "CurrencyCode": "USD"}]
    monkeypatch.setattr(sys.modules[__name__], "BANKS", shared)
    fake = FakeMerit()
    first, _ = run(fake, "merit_import_bank_statement", {"upload_id": "up_1"},
                   uploads=FakeUploads({"up_1": ("statement.xml", CAMT)}))
    assert first["preview"]["statements"][0]["account"] == "LHV"


def test_payment_on_a_shared_iban_sends_only_the_bank_id(monkeypatch):
    # Merit matches payment methods by IBAN; LHV's EUR and USD accounts share one (seen live), so the IBAN
    # would pick the wrong account. A BankId alone books on that account (live test, 2026-11-21).
    shared = [dict(BANKS[0]), {**BANKS[0], "BankId": USD_BANK_ID, "Name": "Swedbank USD", "CurrencyCode": "USD"}]
    monkeypatch.setattr(sys.modules[__name__], "BANKS", shared)
    fake = FakeMerit()
    run(fake, "merit_record_payment", {"kind": "sales_invoice", "invoice_no": "2026049", "bank": BANK_ID,
                                       "amount": 1240, "payment_date": "2026-11-20"})
    sent = fake.writes()[0][1]
    assert sent["BankId"] == BANK_ID and "IBAN" not in sent
