"""More read tools for Merit Aktiva: sales offers, recurring invoices, inventory, the customer
payment report, sales prices and discounts, fixed assets and per-bank payments and statement
imports. Read only, like merit.py.
"""

import json
import re
from datetime import date, timedelta
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .merit import READ, End, Limit, Merit, Start, _chunks, _contains, _day_chunks, _listing, _ymd, compact

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
NO_DATE = "1901-01-01"  # Merit's "not set" for price and discount dates
MAX_MORE_PAGES = 50  # getmoredata pages followed for one report

OFFER_TYPE = {1: "quote", 2: "sales order", 3: "prepayment invoice"}
OFFER_STATUS = {1: "created", 2: "sent", 3: "approved", 4: "rejected", 5: "comment received", 6: "invoice created",
                7: "canceled"}

OFFER_FIELDS = ("SIHId", "OfferNo", "DocType", "DocStatus", "DocumentDate", "DueDate", "DeliveryDate", "CustomerName",
                "CurrencyCode", "TotalAmount", "TaxAmount", "TotalSum", "PaidAmount", "Paid", "ReferenceNo",
                "DepartmentName", "HComment")
RECURRING_FIELDS = ("SIHId", "InvoiceNo", "CustomerName", "StartDate", "EndDate", "NextDate", "Cycle", "Period",
                    "PerLen", "ContrEnded", "CurrencyCode", "TotalAmount", "TaxAmount", "TotalSum", "Address")
MOVEMENT_FIELDS = ("DocumentId", "DocNo", "DocDate", "Type", "Location1Code", "Location2Code", "DepartmentCode",
                   "Comment", "Rows")
STOCK_FIELDS = ("ItemCode", "ItemName", "EANCode", "LocName", "Quantity", "ReservedQuantity", "UnitCode", "Price", "Amount")
CUST_PAYM_FIELDS = ("CustName", "CustId", "DocNo", "DocDate", "DueDate", "TotalAmount", "UnPaidAmount", "OverDue")
PRICE_FIELDS = ("ItemCode", "ItemName", "TGroupType", "TGroupName", "CurrencyCode", "Price0", "UOMName", "AllowDisc",
                "StartDate", "EndDate")
DISCOUNT_FIELDS = ("GroupType", "GroupCode", "GroupName", "TGgroupType", "TGroupName", "Discount0", "StartDate", "EndDate")
ASSET_FIELDS = ("FAId", "InventaryNo", "Name", "FAFroupName", "DepartName", "RespPersonName", "Loc2name", "AcqDate",
                "AcqAmount", "DepPct", "PerDeprAmount", "TotalDeprAmount", "RemAmount", "LastDeprDate", "LiqDate",
                "LiqAmount", "Comment")
BANK_PAYMENT_FIELDS = ("Id", "DocumentDate", "DocumentNumber", "CounterPartName", "CurrencyCode", "Amount", "ChangedOn",
                       "Lines")
IMPORT_FIELDS = ("BookingDate", "PartName", "Document", "Description", "CurrencyCode", "Amount", "AccServiceRRef")


def _list(rows) -> list[dict]:
    return [rows] if isinstance(rows, dict) else [r for r in (rows or []) if r]


def _iso(d: date) -> str:
    return d.isoformat()


def _no_unset_dates(row: dict) -> dict:
    return {k: v for k, v in row.items() if not (isinstance(v, str) and v.startswith(NO_DATE))}


def _offer_labels(row: dict) -> dict:
    row = dict(row)
    row["DocType"] = OFFER_TYPE.get(row.get("DocType"), row.get("DocType"))
    row["DocStatus"] = OFFER_STATUS.get(row.get("DocStatus"), row.get("DocStatus"))
    return row


def register_more(mcp: FastMCP, merit: Merit) -> None:
    @mcp.tool(annotations=READ)
    async def merit_list_sales_offers(
        period_start: Start,
        period_end: End,
        doc_type: Annotated[Literal["all", "quote", "sales order", "prepayment invoice"], Field(description="Which documents")] = "all",
        unpaid_only: bool = False,
        customer: Annotated[str | None, Field(description="Only customers whose name contains this")] = None,
        date_type: Annotated[Literal["document", "changed"], Field(description="Filter on document date, or on when it was last changed")] = "document",
        limit: Limit = 200,
    ) -> dict:
        """Sales offers (pakkumised): quotes, sales orders and prepayment invoices in a period, with
        status (created, sent, approved, rejected, invoice created, canceled …) and amounts.
        TotalAmount is without VAT, TotalSum with VAT."""
        rows = await merit.call_period("v2/getoffers", period_start, period_end,
                                       {"DateType": 0 if date_type == "document" else 1, "UnPaid": unpaid_only})
        rows = [_offer_labels(r) for r in rows if r and _contains(r, "CustomerName", customer)]
        rows = [r for r in rows if doc_type == "all" or r["DocType"] == doc_type]
        return _listing(rows, OFFER_FIELDS, limit, ("TotalAmount", "TaxAmount", "TotalSum", "PaidAmount"))

    @mcp.tool(annotations=READ)
    async def merit_get_sales_offer(
        id: Annotated[str, Field(description="SIHId from merit_list_sales_offers")],
    ) -> str:
        """One sales offer (quote, sales order or prepayment invoice) in full: header, rows, status."""
        offer = await merit.call("v2/getoffer", {"Id": id})
        if not offer:
            raise ToolError(f"no sales offer with Id {id}")
        return json.dumps(compact(_offer_labels(offer)), ensure_ascii=False)

    @mcp.tool(annotations=READ)
    async def merit_list_recurring_invoices(
        period_start: Annotated[date | None, Field(description="Next invoice date from, YYYY-MM-DD; default today")] = None,
        period_end: Annotated[date | None, Field(description="Next invoice date until, YYYY-MM-DD; default a year after period_start")] = None,
        date_type: Annotated[Literal["next_invoice", "changed"], Field(
            description="Filter on the next invoice date, or on when the contract was last changed")] = "next_invoice",
        customer: Annotated[str | None, Field(description="Only customers whose name contains this")] = None,
        include_addresses: Annotated[bool, Field(description="Also the address each running contract's invoices go to")] = False,
        limit: Limit = 200,
    ) -> dict:
        """Recurring invoice contracts (perioodilised arved): customer, cycle, next invoice date,
        amounts per invoice. Merit makes the invoices from these on the next date."""
        start = period_start or date.today()
        end = period_end or start + timedelta(days=365)
        rows = []
        # getperinvoices accepts at most 180 days per call.
        for s, e in _day_chunks(start, end, 180):
            part = await merit.call("v2/getperinvoices", {"PeriodStart": _ymd(s), "PeriodEnd": _ymd(e),
                                                          "DateType": 0 if date_type == "next_invoice" else 1})
            rows.extend(_list(part))
        rows = [r for r in rows if _contains(r, "CustomerName", customer)]
        if include_addresses:
            # The docs list Id, InvoiceNo (contract number) and Address; we take Id to be the contract's
            # SIHId and fall back to the contract number.
            addresses = _list(await merit.call("v2/getpershaddress", {}))
            by_id = {(a.get("Id") or "").lower(): a.get("Address") for a in addresses}
            by_no = {a.get("InvoiceNo"): a.get("Address") for a in addresses if a.get("InvoiceNo")}
            for r in rows:
                r["Address"] = by_id.get((r.get("SIHId") or "").lower()) or by_no.get(r.get("InvoiceNo"))
        return _listing(rows, RECURRING_FIELDS, limit, ("TotalAmount", "TaxAmount", "TotalSum"))

    @mcp.tool(annotations=READ)
    async def merit_get_recurring_invoice(
        id: Annotated[str, Field(description="SIHId from merit_list_recurring_invoices")],
    ) -> str:
        """One recurring invoice contract in full: customer, payer, cycle, payment terms and rows."""
        inv = await merit.call("v2/getperinvoice", {"Id": id})
        if not inv:
            raise ToolError(f"no recurring invoice with Id {id}")
        return json.dumps(compact(inv), ensure_ascii=False)

    @mcp.tool(annotations=READ)
    async def merit_list_inventory_movements(
        period_start: Start,
        period_end: End,
        with_lines: Annotated[bool, Field(description="Also each movement's rows (items, quantities, amounts)")] = False,
        date_type: Annotated[Literal["document", "changed"], Field(description="Filter on document date, or on when it was last changed")] = "document",
        limit: Limit = 200,
    ) -> dict:
        """Inventory movements (laoliikumised) in a period: receipts, issues and transfers between
        locations."""
        rows = await merit.call_period("v2/getinvmovements", period_start, period_end,
                                       {"WithLines": with_lines, "Datetype": 0 if date_type == "document" else 1})
        return _listing([r for r in rows if r], MOVEMENT_FIELDS, limit)

    @mcp.tool(annotations=READ)
    async def merit_inventory_report(
        as_of: Annotated[date | None, Field(description="Stock on this date, YYYY-MM-DD; default today")] = None,
        item_groups: Annotated[list[str] | None, Field(description="Only these item group codes (merit_reference_list kind=item_groups)")] = None,
        location: Annotated[str | None, Field(description="Only this inventory location, code or name")] = None,
        show_zero: Annotated[bool, Field(description="Also items with zero stock")] = False,
        with_reservations: Annotated[bool, Field(description="Also quantities reserved by sales orders")] = False,
        limit: Limit = 500,
    ) -> dict:
        """Stock on hand (laoseis) per item and location on a date, with quantity, price and value."""
        payload: dict = {"RepDate": _ymd(as_of or date.today()), "ShowZero": show_zero,
                         "WithReservations": with_reservations}
        if item_groups:
            payload["ArticleGroups"] = item_groups
        if location:
            payload["Location"] = location
        rows = _list(await merit.call("v2/getinventoryreport", payload))
        out = _listing(rows, STOCK_FIELDS, limit)
        out["total_amount"] = round(sum(r.get("Amount") or 0 for r in rows), 2)
        return out

    @mcp.tool(annotations=READ)
    async def merit_customer_payment_report(
        period_start: Annotated[date, Field(description="First day, YYYY-MM-DD")],
        period_end: Annotated[date, Field(description="Last day, YYYY-MM-DD")],
        customer: Annotated[str | None, Field(description="Part of a customer's name; empty = all customers")] = None,
        customer_id: Annotated[str | None, Field(description="Exact CustomerId (merit_find_customers)")] = None,
        currency: Annotated[str | None, Field(description="Only documents in this currency, e.g. EUR")] = None,
        limit: Limit = 300,
    ) -> dict:
        """Merit's customer payment report: customers' documents in a period with total, unpaid
        amount, due date and how overdue each is."""
        if period_end < period_start:
            raise ToolError("period_end is before period_start")
        if customer_id and not GUID.match(customer_id):
            raise ToolError("customer_id must be a CustomerId (merit_find_customers)")
        # Part of a name is filtered here: Merit's CustName in the similar debt report matches only an
        # exact name and otherwise returns everyone (see merit_customer_debts).
        payload: dict = {"PeriodStart": _ymd(period_start), "PeriodEnd": _ymd(period_end)}
        if customer_id:
            payload["CustId"] = customer_id
        if currency:
            payload["CurrncyCode"] = currency.upper()  # sic, as Merit's docs spell it
        page = await merit.call("v2/getcustpaymrep", payload)
        rows = _list((page or {}).get("Data"))
        pages = 1
        while (page or {}).get("HasMore"):
            if pages >= MAX_MORE_PAGES:
                raise ToolError(f"Merit's report is longer than {MAX_MORE_PAGES} pages: narrow the period or customer")
            # The docs say only "{ Id4More from previous request }"; we send it under that name.
            page = await merit.call("v2/getmoredata", {"Id4More": page.get("Id4More")})
            rows.extend(_list((page or {}).get("Data")))
            pages += 1
        rows = [r for r in rows if _contains(r, "CustName", customer)]
        out = _listing(rows, CUST_PAYM_FIELDS, limit)
        if currency:  # rows carry no currency, so totals only make sense for one currency
            out["totals"] = {currency.upper(): {f: round(sum(r.get(f) or 0 for r in rows), 2)
                                                for f in ("TotalAmount", "UnPaidAmount")}}
        return out

    @mcp.tool(annotations=READ)
    async def merit_sales_prices(
        kind: Annotated[Literal["price_list", "discounts", "customer_price"], Field(
            description="price_list: all sales prices set per item; discounts: all discount rules; "
                        "customer_price: the price and discount valid for one customer, item and date")],
        search: Annotated[str | None, Field(description="price_list/discounts: only rows whose item, group or name contains this")] = None,
        item_code: Annotated[str | None, Field(description="customer_price: the item code (required)")] = None,
        customer_id: Annotated[str | None, Field(description="customer_price: CustomerId (or give customer_reg_no or customer_name)")] = None,
        customer_reg_no: Annotated[str | None, Field(description="customer_price: the customer's registry code")] = None,
        customer_name: Annotated[str | None, Field(description="customer_price: the customer's exact name")] = None,
        on_date: Annotated[date | None, Field(description="customer_price: price valid on this date; default today")] = None,
        currency: Annotated[str | None, Field(description="customer_price: currency; default the company's")] = None,
        unit: Annotated[str | None, Field(description="customer_price: unit of measure")] = None,
        limit: Limit = 300,
    ) -> dict:
        """Sales prices and discounts kept in Merit: the price lists, the discount rules, or the
        price one customer gets for an item on a date."""
        if kind == "customer_price":
            if not item_code:
                raise ToolError("customer_price needs item_code")
            if not (customer_id or customer_reg_no or customer_name):
                raise ToolError("customer_price needs customer_id, customer_reg_no or customer_name")
            payload: dict = {"ItemCode": item_code, "DocDate": _ymd(on_date or date.today())}
            if customer_id:
                payload["CustomerId"] = customer_id
            if customer_name:
                payload["CustName"] = customer_name
            if customer_reg_no:
                payload["CustRegno"] = customer_reg_no
            if currency:
                payload["CurrencyCode"] = currency.upper()
            if unit:
                payload["UOMName"] = unit
            return compact(await merit.call("v2/getprice", payload))
        path, fields, keys = (("v2/getprices", PRICE_FIELDS, ("ItemCode", "ItemName", "TGroupName")) if kind == "price_list"
                              else ("v2/getdiscounts", DISCOUNT_FIELDS, ("GroupCode", "GroupName", "TGroupName")))
        rows = [_no_unset_dates(r) for r in _list(await merit.call(path, {}))]
        if search:
            rows = [r for r in rows if search.lower() in " ".join(str(r.get(k) or "") for k in keys).lower()]
        return _listing(rows, fields, limit)

    @mcp.tool(annotations=READ)
    async def merit_list_fixed_assets(
        search: Annotated[str | None, Field(description="Only assets whose name, inventory number or group contains this")] = None,
        limit: Limit = 300,
    ) -> dict:
        """Fixed assets (põhivara): acquisition, depreciation so far, remaining value, location and
        responsible person. Amounts in EUR."""
        rows = [_no_unset_dates(r) for r in _list(await merit.call("v2/getfixassets", {}))]
        if search:
            rows = [r for r in rows if search.lower()
                    in f"{r.get('Name') or ''} {r.get('InventaryNo') or ''} {r.get('FAFroupName') or ''}".lower()]
        out = _listing(rows, ASSET_FIELDS, limit)
        out["totals"] = {f: round(sum(r.get(f) or 0 for r in rows), 2) for f in ("AcqAmount", "TotalDeprAmount", "RemAmount")}
        return out

    async def bank_id(bank: str) -> str:
        banks = _list(await merit.call("v2/banks", None, method="GET"))
        if GUID.match(bank.strip()):
            hits = [b for b in banks if (b.get("BankId") or "").lower() == bank.strip().lower()]
        else:
            needle = bank.strip().lower().replace(" ", "")
            hits = [b for b in banks if needle in f"{b.get('Name') or ''}|{b.get('Description') or ''}".lower().replace(" ", "")
                    or needle in (b.get("IBANCode") or "").lower().replace(" ", "")]
        if len(hits) != 1:
            options = [f"{b.get('BankId')} = {b.get('Name')} {b.get('IBANCode') or ''} {b.get('CurrencyCode') or ''}".strip()
                       for b in banks]
            raise ToolError(f"{'no' if not hits else 'more than one'} bank account matches {bank!r}; pass one of: {options}")
        return hits[0]["BankId"]

    @mcp.tool(annotations=READ)
    async def merit_list_bank_entries(
        bank: Annotated[str, Field(description="BankId, or part of the bank account's name or IBAN (merit_reference_list kind=banks)")],
        period_start: Start,
        period_end: End,
        kind: Annotated[Literal["income", "expense", "statement_imports"], Field(
            description="income / expense: payments in or out of this account booked in Merit, with the documents each "
                        "settled; statement_imports: bank statement rows imported into Merit")] = "income",
        date_type: Annotated[Literal["document", "created"], Field(
            description="income / expense: filter on document date or on when it was entered; statement_imports always use the booking date")] = "document",
        with_lines: Annotated[bool, Field(description="statement_imports: also the lines of each imported row")] = False,
        limit: Limit = 200,
    ) -> dict:
        """One bank account's payments booked in Merit (income or expense), or the bank statement
        rows imported into it. Amounts in each row's own currency."""
        bid = await bank_id(bank)
        rows: list[dict] = []
        for s, e in _chunks(period_start, period_end):
            if kind == "statement_imports":
                params = {"bankId": bid, "bookingDateFrom": _iso(s), "bookingDateTo": _iso(e),
                          "withLines": "true" if with_lines else "false"}
                part = await merit.call("v2/PaymentImports", None, method="GET", params=params)
            else:
                prefix = "docDate" if date_type == "document" else "created"
                params = {f"{prefix}From": _iso(s), f"{prefix}To": _iso(e)}
                path = f"v2/Banks/{bid}/{'IncomePayments' if kind == 'income' else 'ExpensePayments'}"
                part = await merit.call(path, None, method="GET", params=params)
            rows.extend(_list(part))
        fields = IMPORT_FIELDS if kind == "statement_imports" else BANK_PAYMENT_FIELDS
        if kind == "statement_imports" and with_lines:
            # The docs do not say what withLines adds, so keep every field Merit returns.
            fields += tuple(dict.fromkeys(k for r in rows for k in r if k not in fields))
        return _listing(rows, fields, limit, ("Amount",))
