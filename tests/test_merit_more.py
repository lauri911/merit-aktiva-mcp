"""The extra read tools (merit_more.py) and the read options added to merit.py send exactly the
paths and payloads Merit's docs describe, and shape what comes back."""

import asyncio
import json
from datetime import date

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from merit_aktiva_mcp import merit, merit_more

BANK_ID = "11111111-2222-3333-4444-555555555555"
CUST_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeMerit:
    """Records every call; answers from `answers` by path (a callable gets the payload)."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple] = []

    async def call(self, path, payload=None, method="POST", text_ok=False, params=None):
        self.calls.append((path, payload, method, params))
        a = self.answers.get(path)
        return a(payload, params) if callable(a) else a

    async def call_period(self, path, start, end, extra=None):
        rows = []
        for s, e in merit._chunks(start, end):
            rows.extend(await self.call(path, {"PeriodStart": merit._ymd(s), "PeriodEnd": merit._ymd(e), **(extra or {})}) or [])
        return rows


def run(fake: FakeMerit, tool: str, **args):
    mcp = FastMCP("test")
    merit.register(mcp, fake)
    merit_more.register_more(mcp, fake)

    async def go():
        async with Client(mcp) as client:
            res = await client.call_tool(tool, args)
            if res.structured_content is not None and "result" not in res.structured_content:
                return res.structured_content
            return res.data

    return asyncio.run(go())


# ---------------------------------------------------------------------------- merit.py additions

def test_purchase_invoices_waiting_approval_use_getporders():
    fake = FakeMerit({"v2/GetPOrders": [
        {"PIHId": "p1", "BillNo": "A-1", "VendorName": "Telia Eesti AS", "CurrencyCode": "EUR", "TotalSum": 124.0},
        {"PIHId": "p2", "BillNo": "B-7", "VendorName": "Elisa", "CurrencyCode": "EUR", "TotalSum": 10.0},
    ]})
    out = run(fake, "merit_list_purchase_invoices", period_start="2026-10-01", period_end="2026-11-20",
              status="waiting_approval", vendor="telia")
    assert fake.calls == [("v2/GetPOrders", {"PeriodStart": "20261001", "PeriodEnd": "20261120", "DateFilter": 1,
                                             "StatusFilter": 0, "WithAttachments": False}, "POST", None)]
    assert [r["BillNo"] for r in out["rows"]] == ["A-1"]
    assert out["totals_by_currency"]["EUR"]["TotalSum"] == 124.0


def test_purchase_invoices_booked_unchanged():
    fake = FakeMerit({"v2/getpurchorders": []})
    run(fake, "merit_list_purchase_invoices", period_start="2026-11-01", period_end="2026-11-20", unpaid_only=True)
    assert fake.calls[0][:2] == ("v2/getpurchorders", {"PeriodStart": "20261101", "PeriodEnd": "20261120",
                                                        "UnPaid": True, "DateType": 0})


def test_gl_batches_with_lines_in_31_day_pieces():
    entry = {"AccountCode": "1200", "DebitAmount": 100.0, "CreditAmount": 0, "Memo": ""}
    fake = FakeMerit({"v1/GetGLBatchesFull": lambda p, _: [{"GLBId": p["PeriodStart"], "BatchCode": "MA", "Entries": [entry]}]})
    out = run(fake, "merit_list_gl_batches", period_start="2026-01-01", period_end="2026-03-15", with_lines=True)
    assert [c[1] for c in fake.calls] == [
        {"PeriodStart": "20260101", "PeriodEnd": "20260131", "WithLines": 1, "WithCostAlloc": 0, "DateType": 0},
        {"PeriodStart": "20260201", "PeriodEnd": "20260303", "WithLines": 1, "WithCostAlloc": 0, "DateType": 0},
        {"PeriodStart": "20260304", "PeriodEnd": "20260315", "WithLines": 1, "WithCostAlloc": 0, "DateType": 0},
    ]
    assert out["rows"][0]["Entries"] == [{"AccountCode": "1200", "DebitAmount": 100.0, "CreditAmount": 0}]


def test_gl_batches_with_lines_refuses_long_periods():
    with pytest.raises(ToolError, match="12 months"):
        run(FakeMerit({}), "merit_list_gl_batches", period_start="2025-01-01", period_end="2026-01-01", with_lines=True)


@pytest.mark.parametrize("kind,path,method,payload", [
    ("item_groups", "v2/getitemgroups", "POST", {}),
    ("units", "v2/units", "GET", None),
    ("inventory_locations", "v2/getlocations", "POST", {}),
    ("fixed_asset_locations", "v2/getfalocations", "POST", {}),
    ("fixed_asset_responsible", "v2/getfaresppersons", "POST", {}),
])
def test_new_reference_kinds(kind, path, method, payload):
    fake = FakeMerit({path: [{"Code": "TK", "Name": "Tükk", "Id": "x"}, {"Code": "H", "Name": "Tund", "Id": "y"}]})
    out = run(fake, "merit_reference_list", kind=kind, search="tund")
    assert fake.calls == [(path, payload, method, None)]
    assert out["count"] == 1


def test_payment_types_fetch_all_three_uses():
    fake = FakeMerit({"v2/getpaymenttypes": lambda p, _: [{"Id": f"id{p['Type']}", "Name": f"LHV {p['Type']}", "SourceType": 1}]})
    out = run(fake, "merit_reference_list", kind="payment_types")
    assert [c[1] for c in fake.calls] == [{"Type": 1}, {"Type": 2}, {"Type": 3}]
    assert out["rows"][2] == {"For": "sales", "Name": "LHV 3", "Id": "id3", "Kind": "cash or bank"}


def test_delivery_note_pdf():
    fake = FakeMerit({"v2/getinvoice": {"Header": {"SIHId": "s1"}}, "v2/getsalesinvpdf": {"FileName": "x.pdf"}})
    run(fake, "merit_get_sales_invoice", id="s1", include_pdf=True, delivery_note=True)
    assert fake.calls[1][:2] == ("v2/getsalesinvpdf", {"Id": "s1", "DelivNote": True})


def test_call_puts_params_in_the_query_string(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    m = merit.Merit(api_id="k", api_key="k")
    m.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(m.call("v2/PaymentImports", None, method="GET", params={"bankId": BANK_ID, "bookingDateFrom": "2026-11-01"}))
    q = seen[0].url.params
    assert seen[0].method == "GET" and seen[0].content == b""
    assert q["bankId"] == BANK_ID and q["bookingDateFrom"] == "2026-11-01"
    assert {"apiId", "timestamp", "signature"} <= set(q.keys())


# ---------------------------------------------------------------------------- merit_more.py

def test_sales_offers_list_labels_and_filters():
    fake = FakeMerit({"v2/getoffers": [
        {"SIHId": "o1", "OfferNo": "P1", "DocType": 1, "DocStatus": 3, "CustomerName": "Kask ja Kuusk OÜ", "CurrencyCode": "EUR", "TotalSum": 50},
        {"SIHId": "o2", "OfferNo": "T1", "DocType": 2, "DocStatus": 1, "CustomerName": "Kask ja Kuusk OÜ", "CurrencyCode": "EUR", "TotalSum": 70},
    ]})
    out = run(fake, "merit_list_sales_offers", period_start="2026-11-01", period_end="2026-11-20", doc_type="quote")
    assert fake.calls[0][:2] == ("v2/getoffers", {"PeriodStart": "20261101", "PeriodEnd": "20261120", "DateType": 0, "UnPaid": False})
    assert out["rows"] == [{"SIHId": "o1", "OfferNo": "P1", "DocType": "quote", "DocStatus": "approved",
                            "CustomerName": "Kask ja Kuusk OÜ", "CurrencyCode": "EUR", "TotalSum": 50}]


def test_sales_offer_detail():
    fake = FakeMerit({"v2/getoffer": {"OfferId": "o1", "DocType": 3, "DocStatus": 6, "HComment": ""}})
    out = json.loads(run(fake, "merit_get_sales_offer", id="o1"))
    assert fake.calls == [("v2/getoffer", {"Id": "o1"}, "POST", None)]
    assert out == {"OfferId": "o1", "DocType": "prepayment invoice", "DocStatus": "invoice created"}


def test_recurring_invoices_in_180_day_pieces_with_addresses():
    fake = FakeMerit({
        "v2/getperinvoices": lambda p, _: [{"SIHId": "R1", "InvoiceNo": "L-1", "CustomerName": "Põhjala Laen LTD",
                                            "CurrencyCode": "EUR", "TotalSum": 100}] if p["PeriodStart"] == "20261121" else [],
        "v2/getpershaddress": [{"Id": "r1", "InvoiceNo": "L-1", "Address": "Tallinn"}],
    })
    out = run(fake, "merit_list_recurring_invoices", period_start="2026-11-21", period_end="2027-11-20", include_addresses=True)
    assert [c[1] for c in fake.calls[:3]] == [
        {"PeriodStart": "20261121", "PeriodEnd": "20270519", "DateType": 0},
        {"PeriodStart": "20270520", "PeriodEnd": "20271115", "DateType": 0},
        {"PeriodStart": "20271116", "PeriodEnd": "20271120", "DateType": 0},
    ]
    assert fake.calls[3][:2] == ("v2/getpershaddress", {})
    assert out["rows"][0]["Address"] == "Tallinn"


def test_recurring_invoice_detail():
    fake = FakeMerit({"v2/getperinvoice": {"InvoiceNo": "L-1", "Lines": [{"ItemCode": "TEENUS", "Price": 100}]}})
    assert json.loads(run(fake, "merit_get_recurring_invoice", id="R1"))["Lines"][0]["ItemCode"] == "TEENUS"
    assert fake.calls == [("v2/getperinvoice", {"Id": "R1"}, "POST", None)]


def test_inventory_movements_and_report():
    fake = FakeMerit({"v2/getinvmovements": [{"DocumentId": "d", "DocNo": "1"}],
                      "v2/getinventoryreport": [{"ItemCode": "A", "Amount": 10.5}, {"ItemCode": "B", "Amount": 2}]})
    run(fake, "merit_list_inventory_movements", period_start="2026-11-01", period_end="2026-11-20", with_lines=True)
    assert fake.calls[0][:2] == ("v2/getinvmovements", {"PeriodStart": "20261101", "PeriodEnd": "20261120",
                                                         "WithLines": True, "Datetype": 0})
    out = run(fake, "merit_inventory_report", as_of="2026-11-20", item_groups=["KAUP"], location="LADU")
    assert fake.calls[1][:2] == ("v2/getinventoryreport", {"RepDate": "20261120", "ShowZero": False, "WithReservations": False,
                                                            "ArticleGroups": ["KAUP"], "Location": "LADU"})
    assert out["total_amount"] == 12.5


def test_customer_payment_report_follows_getmoredata():
    pages = {None: {"Data": [{"CustName": "Põhjala Laen LTD", "DocNo": "1", "UnPaidAmount": 5, "TotalAmount": 5}],
                    "HasMore": True, "Id4More": "m1"},
             "m1": {"Data": [{"CustName": "Kask ja Kuusk OÜ", "DocNo": "2", "UnPaidAmount": 7, "TotalAmount": 9}],
                    "HasMore": True, "Id4More": "m2"},
             "m2": {"Data": [{"CustName": "Põhjala Laen LTD", "DocNo": "3", "UnPaidAmount": 0, "TotalAmount": 4}], "HasMore": False}}
    fake = FakeMerit({"v2/getcustpaymrep": pages[None], "v2/getmoredata": lambda p, _: pages[p["Id4More"]]})
    out = run(fake, "merit_customer_payment_report", period_start="2026-01-01", period_end="2026-11-20",
              customer="põhjala laen", currency="eur")
    assert fake.calls == [
        ("v2/getcustpaymrep", {"PeriodStart": "20260101", "PeriodEnd": "20261120", "CurrncyCode": "EUR"}, "POST", None),
        ("v2/getmoredata", {"Id4More": "m1"}, "POST", None),
        ("v2/getmoredata", {"Id4More": "m2"}, "POST", None),
    ]
    assert [r["DocNo"] for r in out["rows"]] == ["1", "3"]
    assert out["totals"] == {"EUR": {"TotalAmount": 9, "UnPaidAmount": 5}}


def test_customer_payment_report_refuses_a_bad_id():
    with pytest.raises(ToolError, match="CustomerId"):
        run(FakeMerit({}), "merit_customer_payment_report", period_start="2026-01-01", period_end="2026-02-01", customer_id="Põhjala Laen")


def test_sales_prices():
    fake = FakeMerit({"v2/getprices": [{"ItemCode": "TEENUS", "ItemName": "Teenus", "Price0": 50, "StartDate": "1901-01-01T00:00:00"},
                                       {"ItemCode": "MUU", "ItemName": "Muu", "Price0": 1}],
                      "v2/getprice": {"ItemCode": "TEENUS", "Price": 45, "DiscountPct": 10}})
    out = run(fake, "merit_sales_prices", kind="price_list", search="teenus")
    assert out["rows"] == [{"ItemCode": "TEENUS", "ItemName": "Teenus", "Price0": 50}]
    out = run(fake, "merit_sales_prices", kind="customer_price", item_code="TEENUS", customer_id=CUST_ID, on_date="2026-11-20")
    assert fake.calls[1][:2] == ("v2/getprice", {"ItemCode": "TEENUS", "DocDate": "20261120", "CustomerId": CUST_ID})
    assert out == {"ItemCode": "TEENUS", "Price": 45, "DiscountPct": 10}
    with pytest.raises(ToolError, match="customer"):
        run(fake, "merit_sales_prices", kind="customer_price", item_code="TEENUS")


def test_fixed_assets():
    fake = FakeMerit({"v2/getfixassets": [{"FAId": "f1", "Name": "MacBook", "AcqAmount": 3000, "RemAmount": 1000, "TotalDeprAmount": 2000},
                                          {"FAId": "f2", "Name": "Auto", "AcqAmount": 20000, "RemAmount": 15000, "TotalDeprAmount": 5000}]})
    out = run(fake, "merit_list_fixed_assets", search="mac")
    assert fake.calls == [("v2/getfixassets", {}, "POST", None)]
    assert out["count"] == 1 and out["totals"] == {"AcqAmount": 3000, "TotalDeprAmount": 2000, "RemAmount": 1000}


BANKS = [{"BankId": BANK_ID, "Name": "LHV", "IBANCode": "EE12 7700 0000 1234", "CurrencyCode": "EUR"},
         {"BankId": "99999999-2222-3333-4444-555555555555", "Name": "Wise USD", "IBANCode": "BE00 1111", "CurrencyCode": "USD"}]


def test_bank_income_payments_by_bank_name():
    fake = FakeMerit({"v2/banks": BANKS, f"v2/Banks/{BANK_ID}/IncomePayments": [
        {"Id": "x", "DocumentNumber": "1", "Amount": 10, "CurrencyCode": "EUR", "Lines": [{"DocumentNumber": "2026001", "PaidAmount": 10}]}]})
    out = run(fake, "merit_list_bank_entries", bank="lhv", period_start="2026-11-01", period_end="2026-11-20")
    assert fake.calls[1] == (f"v2/Banks/{BANK_ID}/IncomePayments", None, "GET",
                             {"docDateFrom": "2026-11-01", "docDateTo": "2026-11-20"})
    assert out["rows"][0]["Lines"] == [{"DocumentNumber": "2026001", "PaidAmount": 10}]


def test_bank_expense_payments_by_created_date_and_iban():
    fake = FakeMerit({"v2/banks": BANKS, f"v2/Banks/{BANK_ID}/ExpensePayments": []})
    run(fake, "merit_list_bank_entries", bank="EE127700", kind="expense", date_type="created",
        period_start="2026-11-01", period_end="2026-11-20")
    assert fake.calls[1] == (f"v2/Banks/{BANK_ID}/ExpensePayments", None, "GET",
                             {"createdFrom": "2026-11-01", "createdTo": "2026-11-20"})


def test_bank_statement_imports():
    fake = FakeMerit({"v2/banks": BANKS, "v2/PaymentImports": [{"BookingDate": "2026-11-02T00:00:00", "Amount": 5, "CurrencyCode": "EUR"}]})
    out = run(fake, "merit_list_bank_entries", bank=BANK_ID.upper(), kind="statement_imports",
              period_start="2026-11-01", period_end="2026-11-20")
    assert fake.calls[1] == ("v2/PaymentImports", None, "GET", {"bankId": BANK_ID, "bookingDateFrom": "2026-11-01",
                                                                "bookingDateTo": "2026-11-20", "withLines": "false"})
    assert out["rows"] == [{"BookingDate": "2026-11-02", "CurrencyCode": "EUR", "Amount": 5}]


def test_bank_must_match_exactly_one_account():
    with pytest.raises(ToolError, match="no bank account"):
        run(FakeMerit({"v2/banks": BANKS}), "merit_list_bank_entries", bank="Swedbank",
            period_start="2026-11-01", period_end="2026-11-20")
    with pytest.raises(ToolError, match="more than one"):
        run(FakeMerit({"v2/banks": BANKS}), "merit_list_bank_entries", bank="e",
            period_start="2026-11-01", period_end="2026-11-20")


def test_net_style_dates_become_plain_dates():
    # getcustpaymrep sends dates as /Date(ms)/, UTC midnight of the day (checked against getcustdebtrep live).
    from merit_aktiva_mcp.merit import compact
    assert compact({"DocDate": "/Date(1793404800000)/", "Due": "/Date(1793404800000+0200)/"}) == \
        {"DocDate": "2026-10-31", "Due": "2026-10-31"}
