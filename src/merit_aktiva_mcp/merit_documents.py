"""Write tools for Merit documents other than sales and purchase invoices and payments: sales
offers, recurring invoices and their meter readings, general ledger entries, inventory movements,
fixed assets and Estonian e-invoice (standard 1.2) XML imports. Registered only when ALLOW_WRITE=true.

Like every write tool, dry_run=true returns the preview without writing.

Merit's docs leave several things open (noted where they matter): the answers of sendglbatch,
SendInvMovement and sendpurchorderxml are not documented, so a write without an id in the answer is
confirmed by finding the new document in Merit; updateoffer names its date field "DeriveryDate".
"""

import base64
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from .merit import Merit, _ymd, compact
from .merit_write import (CUSTOMER_MAP, GUID, WRITE, DryRun, Books, NewCustomer, SalesRow, _gate,
                          _money, _rows)
from .files import Files


OFFER_TYPES = {"quote": 1, "sales_order": 2, "prepayment_invoice": 3}
OFFER_STATUS = {"created": 1, "sent": 2, "approved": 3, "rejected": 4, "comment_received": 5, "canceled": 7}
OFFER_STATUS_NAME = {1: "created", 2: "sent", 3: "approved", 4: "rejected", 5: "comment received",
                     6: "invoice created", 7: "canceled"}
INVOICED = 6
CYCLES = {"month": 1, "quarter": 2, "year": 3, "week": 4}
# Merit: 2 current period, 3 next period, 4 "for the next period", 5 previous period, 6 "current period,
# less number of cycles". 4 and 6 are not explained, so only the three clear ones are offered.
PERIODS = {"current": 2, "next": 3, "previous": 5}
MOVEMENT_TYPES = {"in": 1, "out": 2, "transfer": 3}
MOVEMENT_PREFIX = {"in": ("InBPrefix", "InBNextNo"), "out": ("OutBPrefix", "OutBNextNo"),
                   "transfer": ("Loc2LocPrefix", "Loc2LocNextNo")}
DEPRECIATION = {"linear": 1, "residual": 3}
MAX_XML = 10 * 1024 * 1024


# ---------------------------------------------------------------------------- input models

class GLLine(BaseModel):
    account: str = Field(description="Account code (merit_reference_list kind=accounts); must be active")
    debit: float = Field(0, ge=0, description="Debit amount; give debit or credit, not both")
    credit: float = Field(0, ge=0, description="Credit amount")
    memo: str | None = Field(None, max_length=150, description="Line text")
    department: str | None = Field(None, description="Department code (merit_reference_list kind=departments)")


class MovementRow(BaseModel):
    item_code: str = Field(description="Existing item code (merit_reference_list kind=items)")
    quantity: float = Field(gt=0)
    unit_cost: float | None = Field(None, ge=0, description="Cost per unit without VAT; required for type=in")
    unit: str | None = Field(None, description="Unit name, e.g. 'tk'; default: the item's own")
    gl_account: str | None = Field(None, description="Counter account code; default: Merit's setting")


class MeterReading(BaseModel):
    customer_id: str | None = Field(None, description="CustomerId of the recurring invoice's customer")
    address: str | None = Field(None, description="Or the recurring invoice's address, exactly as Merit has it")
    end_value: float | None = Field(None, description="Meter reading at the end of the period")
    quantity: float | None = Field(None, description="Or the consumption itself (then end_value is not used)")


class NewFixedAsset(BaseModel):
    inventory_no: str = Field(description="Inventory number; must not exist yet")
    name: str
    group: str = Field(description="Fixed asset group name as in Merit; its accounts come from the group")
    depreciation_pct: float = Field(ge=0, le=100, description="Yearly depreciation rate, %")
    depreciation_method: Literal["linear", "residual"] = "linear"
    department: str | None = Field(None, description="Department code or name")
    responsible_person: str = Field(description="Name from Merit's responsible employees list (merit_reference_list "
                                                "kind=fixed_asset_responsible); Merit requires one")
    location: str = Field(description="Name from Merit's fixed asset locations list (merit_reference_list "
                                      "kind=fixed_asset_locations); Merit requires one")
    comment: str | None = None
    subsidy_amount: float | None = Field(None, ge=0, description="Grant received for the asset")


# ---------------------------------------------------------------------------- helpers

def _as_list(rows: Any) -> list[dict]:
    return [rows] if isinstance(rows, dict) else [r for r in (rows or []) if isinstance(r, dict)]


def _id_from(res: Any, *keys: str) -> str | None:
    """The new document's id in Merit's answer, if it gives one. sendinvoicexml and sendpurchorderxml
    answer with a one-item list (seen live 2026-11-21)."""
    if isinstance(res, list) and len(res) == 1:
        res = res[0]
    if isinstance(res, dict):
        for k in keys:
            if res.get(k):
                return str(res[k])
    if isinstance(res, str) and GUID.match(res.strip().strip('"')):
        return res.strip().strip('"')
    return None


def _day(value: Any) -> date | None:
    """A Merit date ("2026-11-21T00:00:00" or "20261121") as a date."""
    if not value:
        return None
    s = str(value)
    try:
        return date.fromisoformat(s[:10]) if "-" in s else date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _next_no(numbers: list[str]) -> str | None:
    nums = [n for n in numbers if n and n.isdigit()]
    if not nums:
        return None
    top = max(nums, key=int)
    return str(int(top) + 1).zfill(len(top))


async def _customer(books: Books, customer_id: str | None, new_customer: NewCustomer | None):
    """Merit's Customer object, preview lines, payment days and notes, as merit_write.build_sales does it."""
    if bool(customer_id) == bool(new_customer):
        raise ToolError("give exactly one of customer_id or new_customer")
    if customer_id:
        card = await books.customer(customer_id)
        who = {"customer": card["Name"], "customer_id": card["CustomerId"], "email": card.get("Email"),
               "reg_no": card.get("RegNo")}
        return {"Id": card["CustomerId"]}, who, card.get("PaymentDeadLine"), []
    await books.check_new_party("v1/getcustomers", new_customer.name, new_customer.reg_no, "customer")
    fields = new_customer.model_dump()
    if fields["private_or_foreign"] is None:
        fields["private_or_foreign"] = new_customer.country_code.upper() != "EE"
    if fields["invoice_language"] is None:
        fields["invoice_language"] = "ET" if new_customer.country_code.upper() == "EE" else "EN"
    if new_customer.country_code.upper() != "EE" and not new_customer.city:
        raise ToolError("city is required for a customer outside Estonia")
    card = await books.card_fields("customer", fields, CUSTOMER_MAP)
    return (card, {"customer": new_customer.name, "new_customer_card": card}, new_customer.payment_days,
            [f"new customer card {new_customer.name!r} will be created"])


async def _with_customer_discounts(merit: Merit, rows: list[SalesRow], customer_id: str, day: date,
                                   notes: list[str]) -> list[SalesRow]:
    """Rows with the discount Merit's price list gives this customer, where the row has none."""
    found: dict[str, float] = {}
    out = []
    for r in rows:
        if not r.discount_pct:
            if r.item_code not in found:
                try:
                    price = await merit.call("v2/getprice", {"ItemCode": r.item_code, "CustomerId": customer_id,
                                                             "DocDate": _ymd(day)})
                    found[r.item_code] = float((price or {}).get("DiscountPct") or 0)
                except ToolError:  # a new item has no price list entry
                    found[r.item_code] = 0.0
            if found[r.item_code]:
                notes.append(f"Merit applies this customer's {found[r.item_code]:g}% discount on {r.item_code}")
                r = r.model_copy(update={"discount_pct": found[r.item_code]})
        out.append(r)
    return out


def _offer_brief(o: dict) -> dict:
    return compact({
        "offer_no": o.get("OfferNo"), "type": {v: k for k, v in OFFER_TYPES.items()}.get(o.get("DocType")),
        "status": OFFER_STATUS_NAME.get(o.get("DocStatus"), o.get("DocStatus")), "customer": o.get("CustomerName"),
        "date": o.get("DocDate"), "expires": o.get("ExpireDate"), "delivery_date": o.get("DeliveryDate"),
        "currency": o.get("CurrencyCode"), "total_without_vat": o.get("TotalAmount"), "vat": o.get("TaxAmount"),
        "header_comment": o.get("HComment"), "footer_comment": o.get("FComment"),
        "rows": [{"item": r.get("ArticleCode"), "description": r.get("Description"), "quantity": r.get("Quantity"),
                  "unit_price": r.get("Price"), "net": r.get("AmountExclVat")} for r in o.get("OfferRow") or []]})


# ---------------------------------------------------------------------------- e-invoice XML

def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _find(el: ET.Element | None, *path: str) -> ET.Element | None:
    for name in path:
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == name), None)
    return el


def _text(el: ET.Element | None, *path: str) -> str | None:
    found = _find(el, *path)
    return found.text.strip() if found is not None and found.text and found.text.strip() else None


def _einvoice(name: str, data: bytes) -> tuple[str, dict, list[ET.Element]]:
    """The XML as text, a summary of its one invoice, and its item rows. Refuses anything that is not
    a single-invoice Estonian e-invoice."""
    if len(data) > MAX_XML:
        raise ToolError(f"{name} is {len(data):,} bytes; an e-invoice over {MAX_XML // 1024 // 1024} MB is refused")
    raw = data.removeprefix(b"\xef\xbb\xbf")
    if re.search(rb"<!(DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        raise ToolError(f"{name} declares a DOCTYPE or entities; an e-invoice has neither, so it is refused")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ToolError(f"{name} is not valid XML: {e}") from None
    if _local(root.tag) != "E_Invoice":
        raise ToolError(f"{name} is not an Estonian e-invoice: its root element is <{_local(root.tag)}>, not <E_Invoice>")
    invoices = [c for c in root if _local(c.tag) == "Invoice"]
    if len(invoices) != 1:
        raise ToolError(f"{name} holds {len(invoices)} invoices; import one invoice per file")
    inv = invoices[0]
    kind = _find(inv, "InvoiceInformation", "Type")
    entries = [e for e in inv.iter() if _local(e.tag) == "ItemEntry"]
    info = {
        "invoice_no": _text(inv, "InvoiceInformation", "InvoiceNumber"),
        "type": {"DEB": "invoice", "CRE": "credit invoice"}.get(kind.get("type"), kind.get("type")) if kind is not None else None,
        "date": _text(inv, "InvoiceInformation", "InvoiceDate"), "due_date": _text(inv, "InvoiceInformation", "DueDate"),
        "seller": _text(inv, "InvoiceParties", "SellerParty", "Name"),
        "seller_reg_no": _text(inv, "InvoiceParties", "SellerParty", "RegNumber"),
        "seller_vat_no": _text(inv, "InvoiceParties", "SellerParty", "VATRegNumber"),
        "buyer": _text(inv, "InvoiceParties", "BuyerParty", "Name"),
        "buyer_reg_no": _text(inv, "InvoiceParties", "BuyerParty", "RegNumber"),
        "currency": _text(inv, "InvoiceSumGroup", "Currency"),
        "total_without_vat": _text(inv, "InvoiceSumGroup", "InvoiceSum"),
        "vat": _text(inv, "InvoiceSumGroup", "TotalVATSum"), "total_with_vat": _text(inv, "InvoiceSumGroup", "TotalSum"),
        "rows": [{"description": _text(e, "Description"), "quantity": _text(e, "ItemDetailInfo", "ItemAmount"),
                  "unit_price": _text(e, "ItemDetailInfo", "ItemPrice"), "net": _text(e, "ItemSum"),
                  "vat_rate": _text(e, "VAT", "VATRate")} for e in entries[:50]],
        "row_count": len(entries),
    }
    if not info["invoice_no"]:
        raise ToolError(f"{name} has no InvoiceNumber")
    m = re.match(rb"<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)", raw)
    try:
        text = raw.decode(m.group(1).decode() if m else "utf-8")
    except (LookupError, UnicodeDecodeError) as e:
        raise ToolError(f"{name} cannot be read as text: {e}") from None
    return text, info, entries


# ---------------------------------------------------------------------------- tools

def register_documents(mcp: FastMCP, merit: Merit, books: Books, files: Files | None) -> None:

    async def accounts() -> dict[str, dict]:
        return {a["Code"]: a for a in _as_list(await merit.call("v2/accounts", None, method="GET")) if a.get("Code")}

    async def find_account(code: str, known: dict[str, dict]) -> dict:
        a = known.get(code)
        if a is None:
            raise ToolError(f"account {code!r} does not exist in Merit (merit_reference_list kind=accounts)")
        if a.get("NonActive"):
            raise ToolError(f"account {code} {a.get('Name')!r} is not active")
        # Not checked: IsParent. Merit sets it to 1 on every account (222 of 222 in the test company,
        # 2026-11-21) and books entries on them all, so it says nothing about group accounts.
        return a

    async def departments() -> list[dict]:
        return _as_list(await merit.call("v2/departments", None, method="GET"))

    async def find_department(value: str, known: list[dict]) -> dict:
        v = value.strip().lower()
        hits = [d for d in known if v in ((d.get("Code") or "").strip().lower(), (d.get("Name") or "").strip().lower())]
        if not hits:
            raise ToolError(f"no department {value!r} in Merit (merit_reference_list kind=departments)")
        if hits[0].get("NonActive"):
            raise ToolError(f"department {value!r} is not active")
        return hits[0]

    def pdf(upload_id: str | None, path: str | None) -> tuple[dict, str]:
        name, data = files.get(upload_id, path, (".pdf",))
        if not data.startswith(b"%PDF"):
            raise ToolError(f"{name} is not a PDF; Merit only takes PDF attachments")
        name = name if name.lower().endswith(".pdf") else name + ".pdf"
        return {"FileName": name, "FileContent": base64.b64encode(data).decode()}, f"{name} ({len(data):,} bytes)"

    # ------------------------------------------------------------ sales offers

    async def offer(offer_id: str) -> dict:
        o = await merit.call("v2/getoffer", {"Id": offer_id})
        if not isinstance(o, dict) or not (o.get("OfferNo") or o.get("OfferId")):
            raise ToolError(f"no sales offer {offer_id}")
        return o

    @mcp.tool(annotations=WRITE)
    async def merit_create_sales_offer(
        rows: list[SalesRow],
        customer_id: Annotated[str | None, Field(description="Existing customer's Id (merit_find_customers)")] = None,
        new_customer: Annotated[NewCustomer | None, Field(description="Only for a customer not yet in Merit")] = None,
        doc_type: Annotated[Literal["quote", "sales_order", "prepayment_invoice"],
                            Field(description="quote (pakkumine), sales order (tellimus) or prepayment invoice "
                                              "(ettemaksuarve)")] = "quote",
        offer_no: Annotated[str | None, Field(description="Default: the highest numeric offer number of the last "
                                                          "year + 1")] = None,
        doc_date: Annotated[date | None, Field(description="Default today")] = None,
        expire_date: Annotated[date | None, Field(description="Valid until (for an order or prepayment invoice: the "
                                                              "due date). Default: 30 days for a quote, else the "
                                                              "customer's payment term")] = None,
        delivery_date: date | None = None,
        currency: Annotated[str | None, Field(description="Default EUR")] = None,
        header_comment: Annotated[str | None, Field(description="Text above the rows; default: from the customer card")] = None,
        footer_comment: Annotated[str | None, Field(description="Text below the rows; default: from the customer card")] = None,
        contact_info: str | None = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Create a sales offer (quote, sales order or prepayment invoice) in Merit, status
        'created'. Nothing is booked or sent. Prices are without VAT; VAT is counted as on sales
        invoices. dry_run=true returns the preview only."""
        today = date.today()
        doc = doc_date or today
        customer, who, days, notes = await _customer(books, customer_id, new_customer)
        if customer_id:
            # sendoffer applies the customer's discount from Merit's discount list to rows without one, even
            # when DiscountPct 0 is sent (seen live 2026-11-21: 124.00 previewed, 117.80 booked with 5% off).
            # Ask Merit which discount it will use, so the preview shows the real amount.
            rows = await _with_customer_discounts(merit, rows, customer_id, doc, notes)
        lines, tax_amount, summary, vat_rates, net, vat, row_notes = await _rows(books, rows, sales=True)
        recent = await merit.call_period("v2/getoffers", today - timedelta(days=365), today, {"DateType": 0, "UnPaid": False})
        numbers = [r.get("OfferNo") or "" for r in recent]
        if offer_no:
            if any(n.strip().lower() == offer_no.strip().lower() for n in numbers):
                raise ToolError(f"offer number {offer_no} is already used in Merit; choose another")
            number = offer_no
        else:
            number = _next_no(numbers)
            if not number:
                raise ToolError("no numeric offer numbers in Merit in the last year to continue from; give offer_no")
        expires = expire_date or (doc + timedelta(days=30 if doc_type == "quote" else (days if days is not None else 14)))
        payload = {"Customer": customer, "DocDate": _ymd(doc), "ExpireDate": _ymd(expires), "OfferNo": number,
                   "DocType": OFFER_TYPES[doc_type], "DocStatus": OFFER_STATUS["created"],
                   "CurrencyCode": (currency or "EUR").upper(), "OfferRow": lines, "TotalAmount": float(net),
                   "RoundingAmount": 0, "TaxAmount": tax_amount}
        if delivery_date:
            payload["DeliveryDate"] = _ymd(delivery_date)
        if header_comment is not None:
            payload["HComment"] = header_comment
        if footer_comment is not None:
            payload["FComment"] = footer_comment
        if contact_info:
            payload["ContactInfo"] = contact_info
        preview = {**who, "type": doc_type, "offer_no": number,
                   "offer_no_note": None if offer_no else "next number after the highest in Merit",
                   "date": doc.isoformat(), "valid_until": expires.isoformat(),
                   "delivery_date": delivery_date.isoformat() if delivery_date else None, "currency": payload["CurrencyCode"],
                   "rows": summary, "vat_by_rate": vat_rates, "total_without_vat": float(net), "vat": float(vat),
                   "total_with_vat": float(net + vat), "header_comment": header_comment,
                   "footer_comment": footer_comment, "contact_info": contact_info, "notes": notes + row_notes}
        if (gate := _gate("create_sales_offer", payload, dry_run, preview)) is not None:
            return gate
        res = await merit.call("v2/sendoffer", payload)
        offer_id = _id_from(res, "InvoiceId", "OfferId", "Id")
        if not offer_id:
            raise ToolError(f"Merit did not confirm the offer: {str(res)[:300]}")
        r = res if isinstance(res, dict) else {}
        out = {"written": True, "offer_id": offer_id, "offer_no": r.get("InvoiceNo") or number,
               "customer_id": r.get("CustomerId"), "new_customer": r.get("NewCustomer")}
        o = await offer(offer_id)
        expected = _money(net + vat)
        if o.get("TotalAmount") is not None:
            total = _money(Decimal(str(o["TotalAmount"])) + Decimal(str(o.get("TaxAmount") or 0)))
            out["total_with_vat_in_merit"] = float(total)
            if total != expected:
                out["warning"] = f"Merit's total {total} differs from the previewed {expected}; check the offer."
        return out

    @mcp.tool(annotations=WRITE)
    async def merit_update_sales_offer(
        offer_id: Annotated[str, Field(description="The offer's Id (SIHId in Merit's offer list)")],
        status: Annotated[Literal["created", "sent", "approved", "rejected", "comment_received", "canceled"] | None,
                          Field(description="New status; 'invoice created' comes only from merit_invoice_from_offer")] = None,
        status_comment: Annotated[str | None, Field(description="Required for comment_received")] = None,
        delivery_date: date | None = None,
        header_comment: str | None = None,
        footer_comment: str | None = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Change a sales offer's status, delivery date or header/footer text. Merit cannot change
        an offer's rows or customer. Refused for an offer already turned into an invoice. dry_run=true
        shows the offer and what changes."""
        o = await offer(offer_id)
        fields = {"DeliveryDate": delivery_date, "HComment": header_comment, "FComment": footer_comment}
        if status is None and all(v is None for v in fields.values()):
            raise ToolError("nothing to change: give status, delivery_date, header_comment or footer_comment")
        if o.get("DocStatus") == INVOICED:
            raise ToolError(f"offer {o.get('OfferNo')} was already turned into an invoice; change the invoice instead")
        if status == "comment_received" and not status_comment:
            raise ToolError("status_comment is required for comment_received")
        changes, calls = {}, []
        if any(v is not None for v in fields.values()):
            # updateoffer finds the offer by OfferNo and the docs do not say what an omitted field does, so the
            # current values go along. Its docs spell the date field "DeriveryDate": both spellings are sent.
            delivery = delivery_date or _day(o.get("DeliveryDate"))
            upd = {"Id": offer_id, "OfferNo": o["OfferNo"],
                   "HComment": header_comment if header_comment is not None else o.get("HComment") or "",
                   "FComment": footer_comment if footer_comment is not None else o.get("FComment") or ""}
            if delivery:
                upd["DeliveryDate"] = upd["DeriveryDate"] = _ymd(delivery)
            calls.append(("v2/updateoffer", upd))
            for k, v in fields.items():
                if v is not None:
                    changes[k] = {"from": o.get(k), "to": v.isoformat() if isinstance(v, date) else v}
        if status is not None:
            st = {"Id": offer_id, "NewStatus": OFFER_STATUS[status]}
            if status_comment:
                st["Comment"] = status_comment
            calls.append(("v2/setofferstatus", st))
            changes["status"] = {"from": OFFER_STATUS_NAME.get(o.get("DocStatus"), o.get("DocStatus")), "to": status,
                                 "comment": status_comment}
        preview = {"offer": _offer_brief(o), "changes": changes}
        if (gate := _gate("update_sales_offer", {"offer": o, "calls": calls}, dry_run, preview)) is not None:
            return gate
        for path, payload in calls:
            await merit.call(path, payload, text_ok=True)
        after = await offer(offer_id)
        wrong = {}
        if status is not None and after.get("DocStatus") != OFFER_STATUS[status]:
            wrong["status"] = OFFER_STATUS_NAME.get(after.get("DocStatus"), after.get("DocStatus"))
        for k in ("HComment", "FComment"):
            if fields[k] is not None and (after.get(k) or "") != fields[k]:
                wrong[k] = after.get(k)
        if delivery_date and _day(after.get("DeliveryDate")) != delivery_date:
            wrong["DeliveryDate"] = after.get("DeliveryDate")
        return {"written": True, "offer_no": o.get("OfferNo"),
                **({"warning": f"Merit shows different values for: {wrong}"} if wrong else {"verified": True})}

    @mcp.tool(annotations=WRITE)
    async def merit_invoice_from_offer(
        offer_id: Annotated[str, Field(description="The offer's Id (SIHId in Merit's offer list)")],
        dry_run: DryRun = False,
    ) -> dict:
        """Turn an approved sales offer into a booked sales invoice with the same customer and rows.
        Merit numbers the invoice itself (its own numbering setting). Not sent: that is
        merit_send_sales_invoice. Merit only invoices approved offers: set the status to approved
        with merit_update_sales_offer first. dry_run=true shows the offer."""
        o = await offer(offer_id)
        st = o.get("DocStatus")
        if st != OFFER_STATUS["approved"]:
            # Merit: "Arveks saab teha ainult kinnitatud dokumente." (only approved ones; seen live 2026-11-21)
            raise ToolError(f"offer {o.get('OfferNo')} is {OFFER_STATUS_NAME.get(st, st)}; Merit only makes an invoice "
                            "from an approved offer"
                            + ("" if st == INVOICED else ". If the customer approved it, set status=approved with "
                                                         "merit_update_sales_offer first"))
        preview = {"offer": _offer_brief(o), "invoice_no": "given by Merit from its own numbering, which may differ "
                                                             "from the <year>NNN numbers merit_create_sales_invoice uses"}
        if (gate := _gate("invoice_from_offer", {"offer": o}, dry_run, preview)) is not None:
            return gate
        res = await merit.call("v2/offer2inv", {"Id": offer_id})
        invoice_id = _id_from(res, "InvoiceId")
        if not invoice_id:
            raise ToolError(f"Merit did not confirm the invoice: {str(res)[:300]}")
        h = (await books.sales_invoice(invoice_id)).get("Header") or {}
        r = res if isinstance(res, dict) else {}
        out = {"written": True, "invoice_id": invoice_id, "invoice_no": r.get("InvoiceNo") or h.get("InvoiceNo"),
               "ref_no": r.get("RefNo"), "from_offer": o.get("OfferNo"), "total_with_vat_in_merit": h.get("TotalSum")}
        if h.get("TotalSum") is not None and o.get("TotalAmount") is not None:
            expected = _money(Decimal(str(o["TotalAmount"])) + Decimal(str(o.get("TaxAmount") or 0)))
            if _money(h["TotalSum"]) != expected:
                out["warning"] = f"The invoice total {h['TotalSum']} differs from the offer's {expected}; check it."
        return out

    # ------------------------------------------------------------ recurring invoices

    async def recurring_contracts() -> list[dict]:
        return _as_list(await merit.call("v2/getpershaddress", {}))

    @mcp.tool(annotations=WRITE)
    async def merit_create_recurring_invoice(
        rows: list[SalesRow],
        contract_no: Annotated[str, Field(max_length=35, description="The recurring invoice's (contract's) number")],
        start_date: date,
        cycle: Annotated[Literal["month", "quarter", "year", "week"], Field(description="How often an invoice is made")] = "month",
        period: Annotated[Literal["current", "next", "previous"],
                          Field(description="Which period each invoice is for, relative to its date")] = "current",
        next_date: Annotated[date | None, Field(description="Date of the first invoice; default start_date")] = None,
        end_date: Annotated[date | None, Field(description="Last day of the contract; default: open-ended")] = None,
        customer_id: Annotated[str | None, Field(description="Existing customer's Id (merit_find_customers)")] = None,
        new_customer: Annotated[NewCustomer | None, Field(description="Only for a customer not yet in Merit")] = None,
        currency: Annotated[str | None, Field(description="Default EUR")] = None,
        header_comment: str | None = None,
        footer_comment: str | None = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Create a recurring invoice (perioodiline arve) in Merit: a contract from which Merit
        makes a sales invoice every cycle. Prices are without VAT. Refused if a running recurring
        invoice has the same number. dry_run=true returns the preview only."""
        if end_date and end_date < start_date:
            raise ToolError("end_date is before start_date")
        first = next_date or start_date
        if first < start_date or (end_date and first > end_date):
            raise ToolError("next_date must be within the contract's dates")
        customer, who, _days, notes = await _customer(books, customer_id, new_customer)
        if customer_id:  # Merit likely applies stored customer discounts here as sendoffer does
            rows = await _with_customer_discounts(merit, rows, customer_id, first, notes)
        clash = [c for c in await recurring_contracts()
                 if (c.get("InvoiceNo") or "").strip().lower() == contract_no.strip().lower()]
        if clash:
            raise ToolError(f"a running recurring invoice already has number {contract_no} (Id {clash[0].get('Id')})")
        lines, tax_amount, summary, vat_rates, net, vat, row_notes = await _rows(books, rows, sales=True)
        payload = {"Customer": customer, "InvoiceNo": contract_no, "StartDate": _ymd(start_date), "NextDate": _ymd(first),
                   "Cycle": CYCLES[cycle], "Period": PERIODS[period], "CurrencyCode": (currency or "EUR").upper(),
                   "PriceInclVat": False, "InvoiceRow": lines, "TaxAmount": tax_amount, "TotalAmount": float(net),
                   "TotalSum": float(net + vat), "RoundingAmount": 0}
        if end_date:
            payload["EndDate"] = _ymd(end_date)
        if header_comment is not None:
            payload["HComment"] = header_comment
        if footer_comment is not None:
            payload["FComment"] = footer_comment
        preview = {**who, "contract_no": contract_no, "every": cycle, "invoice_is_for": f"the {period} period",
                   "start_date": start_date.isoformat(), "first_invoice": first.isoformat(),
                   "end_date": end_date.isoformat() if end_date else "open-ended", "currency": payload["CurrencyCode"],
                   "rows_per_invoice": summary, "vat_by_rate": vat_rates, "total_without_vat": float(net),
                   "vat": float(vat), "total_with_vat": float(net + vat), "header_comment": header_comment,
                   "footer_comment": footer_comment, "notes": notes + row_notes}
        if (gate := _gate("create_recurring_invoice", payload, dry_run, preview)) is not None:
            return gate
        res = await merit.call("v2/sendperinvoice", payload, text_ok=True)
        new_id = _id_from(res, "InvoiceId", "SIHId", "Id")
        if not new_id:
            # The answer is not documented: confirm by finding the contract number among running ones.
            found = [c for c in await recurring_contracts() if (c.get("InvoiceNo") or "").strip() == contract_no.strip()]
            if not found:
                raise ToolError(f"Merit did not confirm the recurring invoice: {str(res)[:300]}. Check Merit before trying again.")
            new_id = found[0].get("Id")
        return {"written": True, "recurring_invoice_id": new_id, "contract_no": contract_no,
                "customer_id": res.get("CustomerId") if isinstance(res, dict) else None}

    @mcp.tool(annotations=WRITE)
    async def merit_send_meter_readings(
        item_code: Annotated[str, Field(description="The metered item's code on the recurring invoices")],
        readings: Annotated[list[MeterReading], Field(min_length=1)],
        dry_run: DryRun = False,
    ) -> dict:
        """Enter meter readings (näidud) or consumption for an item on recurring invoices; Merit
        recalculates the quantities and amounts of the matching rows. dry_run=true returns the preview only."""
        if item_code not in await books.items(require=[item_code]):
            raise ToolError(f"item code {item_code!r} does not exist in Merit (merit_reference_list kind=items)")
        addresses = None
        values, shown = [], []
        for r in readings:
            if bool(r.customer_id) == bool(r.address):
                raise ToolError("each reading needs exactly one of customer_id or address")
            if (r.end_value is None) == (r.quantity is None):
                raise ToolError("each reading needs exactly one of end_value or quantity")
            v: dict = {}
            line: dict = {}
            if r.customer_id:
                card = await books.customer(r.customer_id)
                v["CustomerId"] = card["CustomerId"]
                line["customer"] = card["Name"]
            else:
                if addresses is None:
                    addresses = {}
                    for c in await recurring_contracts():
                        addresses.setdefault((c.get("Address") or "").strip().lower(), []).append(c)
                found = addresses.get(r.address.strip().lower())
                if not found:
                    raise ToolError(f"no running recurring invoice has the address {r.address!r}")
                v["Address"] = found[0]["Address"]
                line["address"] = found[0]["Address"]
                line["contracts"] = [c.get("InvoiceNo") for c in found]  # Merit applies it to every one
            if r.quantity is not None:
                v["Quantity"] = r.quantity
                line["quantity"] = r.quantity
            else:
                v["EndValue"] = r.end_value
                line["end_value"] = r.end_value
            values.append(v)
            shown.append(line)
        payload = {"ArtCode": item_code, "IndValue": values}
        if (gate := _gate("send_meter_readings", payload, dry_run, {"item": item_code, "readings": shown})) is not None:
            return gate
        try:
            res = await merit.call("v1/sendindvalues", payload, text_ok=True)
        except ToolError as e:
            if "HTTP 500" in str(e):
                # Seen live 2026-11-21: NullReferenceException for a running contract with the item on it.
                raise ToolError("Merit failed to take the readings (HTTP 500, nothing changed). It does this when "
                                "the recurring invoice's row is not set up for meter readings in Merit; set that up "
                                "in Merit or change the row quantities there.") from None
            raise
        text = res if isinstance(res, str) else str(res)
        # Merit answers in Estonian: "Näidud kanti üle <n> lepingu <m> reale ja kogused ning summad arvutati uuesti."
        counts = re.search(r"üle\s+(\d+)\D+?(\d+)", text)
        if not counts:
            raise ToolError(f"Merit's answer is not the expected confirmation: {text[:300]}. Check the recurring "
                            "invoices in Merit before trying again.")
        out = {"written": True, "contracts": int(counts.group(1)), "rows": int(counts.group(2)), "merit_says": text}
        if int(counts.group(2)) == 0:
            out["warning"] = "Merit matched no recurring invoice row; check the item code, customers or addresses."
        return out

    # ------------------------------------------------------------ general ledger

    @mcp.tool(annotations=WRITE)
    async def merit_create_gl_entry(
        doc_no: Annotated[str, Field(max_length=35, description="Document number of the entry")],
        entry_date: date,
        lines: Annotated[list[GLLine], Field(min_length=2, description="Debit and credit lines; they must balance")],
        currency: Annotated[str | None, Field(description="Default EUR")] = None,
        currency_rate: Annotated[float | None, Field(gt=0, description="Default: the ECB rate of the date")] = None,
        attachment_upload_id: Annotated[str | None, Field(description="A PDF for the entry. Server mode: upload id from create_upload_link")] = None,
        attachment_path: Annotated[str | None, Field(description="A PDF for the entry. On your own computer: the file's path")] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Create a manual general ledger entry (pearaamatu kanne) in Merit. Debits must equal
        credits; accounts must exist and be active. Lines carry no VAT. Refused if an entry with the
        same document number is within 90 days of the date. dry_run=true shows the lines."""
        known = await accounts()
        deps = None
        rows, shown = [], []
        debit, credit = Decimal(0), Decimal(0)
        for ln in lines:
            d, c = _money(ln.debit), _money(ln.credit)
            if (d > 0) == (c > 0):
                raise ToolError(f"line on account {ln.account}: give a debit or a credit amount, not both or neither")
            a = await find_account(ln.account.strip(), known)
            row: dict = {"AccountCode": a["Code"], "Debit": float(d), "Credit": float(c)}
            if ln.memo:
                row["Memo"] = ln.memo
            if ln.department:
                deps = deps if deps is not None else await departments()
                row["DepartmentCode"] = (await find_department(ln.department, deps))["Code"]
            rows.append(row)
            shown.append(compact({"account": a["Code"], "account_name": a.get("Name"), "debit": float(d) or None,
                                  "credit": float(c) or None, "memo": ln.memo, "department": row.get("DepartmentCode")}))
            debit += d
            credit += c
        if debit != credit:
            raise ToolError(f"the entry does not balance: debits {debit}, credits {credit} (difference {debit - credit})")
        near = await merit.call_period("v1/getglbatches", entry_date - timedelta(days=90), entry_date + timedelta(days=90),
                                       {"DateType": 0})
        same = [b for b in near if (b.get("Document") or "").strip().lower() == doc_no.strip().lower()]
        if same:
            raise ToolError(f"an entry with document number {doc_no} is already in Merit ({same[0].get('BatchCode')}"
                            f"-{same[0].get('No')}, {str(same[0].get('BatchDate'))[:10]}); not creating a duplicate")
        payload: dict = {"DocNo": doc_no, "BatchDate": _ymd(entry_date), "CurrencyCode": (currency or "EUR").upper(),
                         "EntryRow": rows}
        if currency_rate:
            payload["CurrencyRate"] = currency_rate
        file_note = None
        if attachment_upload_id or attachment_path:
            payload["Attachment"], file_note = pdf(attachment_upload_id, attachment_path)
        preview = {"doc_no": doc_no, "date": entry_date.isoformat(), "currency": payload["CurrencyCode"],
                   "currency_rate": currency_rate, "lines": shown, "total_debit": float(debit),
                   "total_credit": float(credit), "attachment": file_note}
        if (gate := _gate("create_gl_entry", payload, dry_run, preview)) is not None:
            return gate
        res = await merit.call("v1/sendglbatch", payload, text_ok=True)
        batch_id = _id_from(res, "GLBId", "BatchId", "Id")
        if not batch_id:
            # The answer is not documented: confirm by finding the entry on its date.
            found = [b for b in await merit.call_period("v1/getglbatches", entry_date, entry_date, {"DateType": 0})
                     if (b.get("Document") or "").strip() == doc_no.strip()]
            if not found:
                raise ToolError(f"Merit did not confirm the entry: {str(res)[:300]}. Check Merit before trying again.")
            batch_id = found[0].get("GLBId")
        return {"written": True, "gl_batch_id": batch_id, "doc_no": doc_no, "total": float(debit)}

    # ------------------------------------------------------------ inventory

    @mcp.tool(annotations=WRITE)
    async def merit_create_inventory_movement(
        movement: Annotated[Literal["in", "out", "transfer"],
                            Field(description="in: into a stock; out: written off a stock; transfer: between stocks")],
        location: Annotated[str, Field(description="Stock (location) code; for a transfer, where the goods leave")],
        rows: Annotated[list[MovementRow], Field(min_length=1)],
        to_location: Annotated[str | None, Field(description="Destination stock code; only for a transfer")] = None,
        doc_no: Annotated[str | None, Field(description="Default: the stock's next number in Merit")] = None,
        doc_date: Annotated[date | None, Field(description="Default today")] = None,
        department: Annotated[str | None, Field(description="Department code")] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Record a stock movement (laoliikumine) in Merit: goods into a stock, out of it, or
        between stocks. Items and stocks must exist. dry_run=true returns the preview only."""
        doc = doc_date or date.today()
        locations = {(l.get("Code") or "").strip().lower(): l for l in _as_list(await merit.call("v2/getlocations", {}))}

        def stock(code: str) -> dict:
            l = locations.get(code.strip().lower())
            if l is None:
                raise ToolError(f"no stock with code {code!r}; Merit has {sorted(x['Code'] for x in locations.values())}")
            if l.get("NonActive") or l.get("NonActine"):
                raise ToolError(f"stock {code} {l.get('Name')!r} is not active")
            return l

        src = stock(location)
        dst = None
        if movement == "transfer":
            if not to_location:
                raise ToolError("a transfer needs to_location")
            dst = stock(to_location)
            if dst["Code"] == src["Code"]:
                raise ToolError("to_location is the same stock as location")
        elif to_location:
            raise ToolError("to_location is only for a transfer")
        items = await books.items(require=[r.item_code for r in rows])
        known = None
        lines, shown = [], []
        for r in rows:
            item = items.get(r.item_code)
            if item is None:
                raise ToolError(f"item code {r.item_code!r} does not exist in Merit (merit_reference_list kind=items)")
            if movement == "in" and r.unit_cost is None:
                raise ToolError(f"row {r.item_code}: unit_cost is required for goods coming in")
            line: dict = {"ArticleCode": item["Code"], "Quantity": r.quantity}
            if r.unit_cost is not None:
                line["ItemUnitCost"] = r.unit_cost
            if r.unit:
                line["UOMName"] = r.unit
            if r.gl_account:
                known = known if known is not None else await accounts()
                line["GLAccountCode"] = (await find_account(r.gl_account.strip(), known))["Code"]
            lines.append(line)
            shown.append(compact({"item": item["Code"], "name": item.get("Name"), "quantity": r.quantity,
                                  "unit": r.unit or item.get("UnitofMeasureName"), "unit_cost": r.unit_cost,
                                  "cost": float(_money(r.quantity * r.unit_cost)) if r.unit_cost is not None else None,
                                  "account": line.get("GLAccountCode")}))
        # From the start of the year (and at least 90 days back): Merit does not advance the stock's counter
        # for API movements, so the numbers used so far decide the next one.
        since = min(doc - timedelta(days=90), date(doc.year, 1, 1))
        near = await merit.call_period("v2/getinvmovements", since, doc + timedelta(days=90),
                                       {"WithLines": False, "Datetype": 0})
        number = doc_no
        if not number:
            prefix, next_no = MOVEMENT_PREFIX[movement]
            if src.get(next_no) is None:
                raise ToolError(f"stock {src['Code']} has no next {movement} number in Merit; give doc_no")
            # Merit does not advance the stock's counter for movements made through the API (seen live
            # 2026-11-21: ST-1 offered again), so continue after the highest number already used.
            pre = src.get(prefix) or ""
            used = [int(n[len(pre):]) for n in ((m.get("DocNo") or "").strip() for m in near)
                    if n.startswith(pre) and n[len(pre):].isdigit()]
            number = f"{pre}{max([int(src[next_no])] + [u + 1 for u in used])}"
        if any((m.get("DocNo") or "").strip().lower() == number.strip().lower() for m in near):
            raise ToolError(f"an inventory movement numbered {number} is already in Merit; give another doc_no")
        payload: dict = {"DocDate": _ymd(doc), "DocNo": number, "Location1Code": src["Code"],
                         "Type": MOVEMENT_TYPES[movement], "Rows": lines}
        if dst:
            payload["Location2Code"] = dst["Code"]
        if department:
            payload["DepartmentCode"] = (await find_department(department, await departments()))["Code"]
        preview = {"movement": movement, "doc_no": number, "doc_no_note": None if doc_no else "the stock's next number in Merit",
                   "date": doc.isoformat(), "stock": f"{src['Code']} {src.get('Name') or ''}".strip(),
                   "to": f"{dst['Code']} {dst.get('Name') or ''}".strip() if dst else None,
                   "department": payload.get("DepartmentCode"), "rows": shown}
        if (gate := _gate("create_inventory_movement", payload, dry_run, preview)) is not None:
            return gate
        res = await merit.call("v2/SendInvMovement", payload, text_ok=True)
        movement_id = _id_from(res, "DocumentId", "Id")
        if not movement_id:
            # The answer is not documented: confirm by finding the movement on its date.
            found = [m for m in await merit.call_period("v2/getinvmovements", doc, doc, {"WithLines": False, "Datetype": 0})
                     if (m.get("DocNo") or "").strip() == number.strip()]
            if not found:
                raise ToolError(f"Merit did not confirm the movement: {str(res)[:300]}. Check Merit before trying again.")
            movement_id = found[0].get("DocumentId")
        return {"written": True, "movement_id": movement_id, "doc_no": number}

    # ------------------------------------------------------------ fixed assets

    @mcp.tool(annotations=WRITE)
    async def merit_create_fixed_assets(
        assets: Annotated[list[NewFixedAsset], Field(min_length=1, max_length=50)],
        dry_run: DryRun = False,
    ) -> dict:
        """Add fixed asset cards (põhivara) in Merit. This only makes the cards: the cost comes from
        the purchase invoice or ledger entry that acquires the asset. Inventory numbers must be new;
        responsible persons, locations and departments must exist. dry_run=true returns the preview only."""
        existing = _as_list(await merit.call("v2/getfixassets", {}))
        taken = {(a.get("InventaryNo") or "").strip().lower(): a for a in existing}
        groups = {(a.get("FAFroupName") or "").strip().lower() for a in existing}
        people = {(p.get("Name") or "").strip().lower(): p for p in _as_list(await merit.call("v2/getfaresppersons", {}))}
        places = {(p.get("Name") or "").strip().lower(): p for p in _as_list(await merit.call("v2/getfalocations", {}))}
        # Merit refuses an asset without a location or a responsible person ("Puudub PV asukoht / vastutaja
        # nimega .", seen live 2026-11-21), and the API cannot create either.
        if not places or not people:
            raise ToolError(f"Merit has no fixed asset {'locations' if not places else 'responsible people'}, and it "
                            "refuses an asset without one. Add them in Merit (Seadistused → Põhivarade asukohad / "
                            "Põhivarade vastutajad); the API cannot create them.")
        deps = None
        seen: set[str] = set()
        cards, notes = [], []
        for a in assets:
            key = a.inventory_no.strip().lower()
            if key in taken:
                raise ToolError(f"inventory number {a.inventory_no} is already used by {taken[key].get('Name')!r}")
            if key in seen:
                raise ToolError(f"inventory number {a.inventory_no} is given twice")
            seen.add(key)
            card: dict = {"InventaryNo": a.inventory_no, "Name": a.name, "FAFroupName": a.group,
                          "DeprCalcMethod": DEPRECIATION[a.depreciation_method], "DeprPct": a.depreciation_pct}
            if a.group.strip().lower() not in groups:
                notes.append(f"group {a.group!r} is not used by any asset in Merit yet; check that it exists there "
                             "(Merit refuses an unknown group)")
            if a.responsible_person:  # required, so always given
                p = people.get(a.responsible_person.strip().lower())
                if p is None:
                    raise ToolError(f"no responsible employee {a.responsible_person!r}; Merit has {sorted(x['Name'] for x in people.values())}")
                card["RespPersonName"] = p["Name"]
            if a.location:
                p = places.get(a.location.strip().lower())
                if p is None:
                    raise ToolError(f"no fixed asset location {a.location!r}; Merit has {sorted(x['Name'] for x in places.values())}")
                card["Loc2name"] = p["Name"]
            if a.department:
                deps = deps if deps is not None else await departments()
                card["DepartName"] = (await find_department(a.department, deps))["Name"]
            if a.comment:
                card["Comment"] = a.comment
            if a.subsidy_amount:
                card["SubsidAmount"] = a.subsidy_amount
            cards.append(card)
        payload = {"FixedAssets": cards}
        preview = {"new_fixed_assets": [compact({"inventory_no": c["InventaryNo"], "name": c["Name"], "group": c["FAFroupName"],
                                                 "depreciation": f"{c['DeprPct']:g}% a year, {a.depreciation_method}",
                                                 "responsible": c.get("RespPersonName"), "location": c.get("Loc2name"),
                                                 "department": c.get("DepartName"), "comment": c.get("Comment"),
                                                 "subsidy": c.get("SubsidAmount")})
                                        for c, a in zip(cards, assets)],
                   "notes": sorted(set(notes))}
        if (gate := _gate("create_fixed_assets", payload, dry_run, preview)) is not None:
            return gate
        res = await merit.call("v2/sendfixedassets", payload)
        made = [r for r in _as_list(res) if r.get("Id")]
        if len(made) != len(cards):
            raise ToolError(f"Merit confirmed {len(made)} of {len(cards)} fixed assets: {str(res)[:300]}. Check Merit "
                            "before trying again.")
        return {"written": True, "fixed_assets": [{"id": r["Id"], "inventory_no": r.get("InventaryNo"),
                                                   "name": r.get("Name")} for r in made]}

    # ------------------------------------------------------------ e-invoice XML

    @mcp.tool(annotations=WRITE)
    async def merit_import_einvoice(
        direction: Annotated[Literal["sales", "purchase"],
                             Field(description="sales: our own invoice, booked as a sales invoice; purchase: a vendor's "
                                               "invoice, entered to wait for approval in Merit")],
        upload_id: Annotated[str | None, Field(description="The XML file. Server mode: upload id from create_upload_link")] = None,
        file_path: Annotated[str | None, Field(description="The XML file. On your own computer: the file's path")] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Import an Estonian e-invoice (standard 1.2 XML, one invoice per file) into Merit. A sales
        invoice is booked (not sent); a purchase invoice waits for approval in Merit, where it is
        corrected or deleted. Refuses an invoice number already in Merit. dry_run=true shows what the
        file holds."""
        name, data = files.get(upload_id, file_path, (".xml",))
        text, info, entries = _einvoice(name, data)
        notes: list[str] = []
        if direction == "sales":
            for e in entries:
                if not any(_local(c.tag).lower() in ("sellerproductid", "ean") and (c.text or "").strip() for c in e):
                    raise ToolError(f"row {_text(e, 'Description')!r} has no SellerProductId or EAN; Merit needs one "
                                    "on every row of a sales invoice to find the item")
            same = [r for r in _as_list(await merit.call("v2/getinvoices2", {"InvNo": info["invoice_no"]}))
                    if (r.get("InvoiceNo") or "").strip().lower() == info["invoice_no"].strip().lower()]
            if same:
                raise ToolError(f"sales invoice {info['invoice_no']} is already in Merit (SIHId {same[0].get('SIHId')})")
            if info.get("buyer_reg_no"):
                cards = _as_list(await merit.call("v1/getcustomers", {"RegNo": info["buyer_reg_no"]}))
                notes.append(f"customer card: {cards[0].get('Name')!r}" if cards else
                             f"no customer card with registry code {info['buyer_reg_no']}; Merit takes the buyer from the file")
        else:
            today = date.today()
            cards = _as_list(await merit.call("v1/getvendors", {"RegNo": info["seller_reg_no"]})) if info.get("seller_reg_no") else []
            if not cards and info.get("seller"):
                # A card without a registry code (often a foreign vendor) is found by its exact name.
                cards = [c for c in _as_list(await merit.call("v1/getvendors", {"Name": info["seller"]}))
                         if (c.get("Name") or "").strip().lower() == info["seller"].strip().lower()]
            if cards:
                ids = {c.get("VendorId") for c in cards}
                recent = await merit.call_period("v2/getpurchorders", today - timedelta(days=365), today)
                pending = await books.pending_purchases(today - timedelta(days=365), today)
                dupes = [p for p in recent + pending if ids & {p.get("VendorId"), p.get("ReceiverId")}
                         and (p.get("BillNo") or "").strip().lower() == info["invoice_no"].strip().lower()]
                if dupes:
                    raise ToolError(f"{cards[0].get('Name')} bill {info['invoice_no']} is already in Merit (PIHId "
                                    f"{dupes[0].get('PIHId')}); not importing a duplicate")
                notes.append(f"vendor card: {cards[0].get('Name')!r}")
            else:
                notes.append(f"no vendor card with registry code {info.get('seller_reg_no')} or name {info.get('seller')!r}; "
                             "Merit takes the seller from the file (no duplicate check was possible)")
        preview = {"direction": direction, "file": f"{name} ({len(data):,} bytes)", **info,
                   "booking": "booked as a sales invoice (not sent)" if direction == "sales"
                              else "waits for approval in Merit (Ostuarved → kinnitamata)",
                   "notes": notes}
        action = f"import_{direction}_einvoice"
        if (gate := _gate(action, {"direction": direction, "xml": text}, dry_run, preview)) is not None:
            return gate
        # The docs show the payload as the XML in quotes: it is sent as one JSON string.
        if direction == "sales":
            res = await merit.call("v2/sendinvoicexml", text)
            if isinstance(res, list) and len(res) == 1:
                res = res[0]
            if isinstance(res, dict) and res.get("ErrMsg"):
                raise ToolError(f"Merit refused the e-invoice: {res['ErrMsg']}")
            invoice_id = _id_from(res, "InvoiceId")
            if not invoice_id:
                raise ToolError(f"Merit did not confirm the invoice: {str(res)[:300]}")
            h = (await books.sales_invoice(invoice_id)).get("Header") or {}
            r = res if isinstance(res, dict) else {}
            out = {"written": True, "invoice_id": invoice_id, "invoice_no": r.get("InvoiceNo"),
                   "customer_id": r.get("CustomerId"), "ref_no": r.get("RefNo"),
                   "total_with_vat_in_merit": h.get("TotalSum")}
            if h.get("TotalSum") is not None and info.get("total_with_vat"):
                try:
                    if _money(h["TotalSum"]) != _money(Decimal(info["total_with_vat"])):
                        out["warning"] = f"Merit's total {h['TotalSum']} differs from the file's {info['total_with_vat']}."
                except ArithmeticError:
                    pass
            return out
        res = await merit.call("v2/sendpurchorderxml", text, text_ok=True)
        if isinstance(res, list) and len(res) == 1:
            res = res[0]
        if isinstance(res, dict) and (res.get("ErrMsg") or res.get("Error")):
            raise ToolError(f"Merit refused the e-invoice: {res.get('ErrMsg') or res.get('Error')}")
        bill_id = _id_from(res, "BillId", "PIHId", "Id")
        if not bill_id and not (isinstance(res, str) and res.strip().strip('"').upper() == "OK"):
            raise ToolError(f"Merit's answer is not a confirmation: {str(res)[:300]}. Check the purchase invoices "
                            "waiting for approval in Merit before trying again.")
        return {"written": True, "purchase_invoice_id": bill_id, "bill_no": info["invoice_no"],
                "vendor": info.get("seller"), "waiting_for_approval": True,
                "note": "Waiting for approval in Merit; correct or delete it there."}
