"""Write tools for Merit Aktiva: sales invoices, customers, purchase invoices and vendors.
Registered only when MERIT_ALLOW_WRITE=true.

A write tool writes when called. With dry_run=true it only prepares: it resolves the customer or
vendor, VAT rates and items, computes the totals the way Merit does (each row rounded, VAT summed
per rate) and returns that preview, writing nothing. When to show the user a preview first is the
agent's call. The checks that stop wrong writes always run: duplicate numbers, paid or sent
documents, VAT from vendors not registered for VAT, and a read-back of the totals after writing.

Merit has no way to edit an invoice: a correction of one that is not paid or sent is a delete
and a re-create with the same number; anything paid or sent gets a credit invoice instead.
Every write is logged.
"""

import base64
import json
import logging
import re
import time
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, Field

from .merit import Merit, _chunks, compact
from .files import Files

log = logging.getLogger(__name__)

WRITE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
STANDARD_VAT = re.compile(r"^\d+% [Kk]äibemaks$")  # "24% käibemaks": the plain rate, not a special regime
NOT_EMAILED = "1901-01-01"  # Merit's EmailSent for "never"

DryRun = Annotated[bool, Field(
    description="true: only return a preview of what would be written; nothing is written")]


# ---------------------------------------------------------------------------- helpers

def _money(x: Decimal | float) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _dt(d: date) -> str:
    return d.strftime("%Y%m%d") + "000000"


def _gate(action: str, payload: Any, dry_run: bool, preview: dict) -> dict | None:
    """The preview to return for a dry run, or None when the write should happen now (logged)."""
    if dry_run:
        return {"preview": compact(preview), "written": False, "dry_run": True}
    log.info("WRITE %s by %s: %s", action, _who(), json.dumps(payload, default=str, ensure_ascii=False)[:2000])
    return None


def _who() -> str:
    """The signed-in person on a server (so a team can see who made a change), else "local"."""
    try:
        token = get_access_token()
    except Exception:  # no request context (local mode, tests)
        token = None
    claims = (getattr(token, "claims", None) or {}) if token else {}
    return claims.get("preferred_username") or claims.get("upn") or claims.get("email") or "local"


def _is_sent(h: dict) -> bool:
    sent = h.get("EmailSent") or ""
    return bool(h.get("EInvSent")) or (bool(sent) and not sent.startswith(NOT_EMAILED))


# ---------------------------------------------------------------------------- input models

class SalesRow(BaseModel):
    item_code: str = Field(description="Item code in Merit (merit_reference_list kind=items); an unknown code creates a new item")
    description: str = Field(max_length=150, description="Row text on the invoice")
    quantity: float = 1
    price: float = Field(description="Unit price WITHOUT VAT, in the invoice currency")
    vat: str = Field(description="VAT rate as a number, e.g. '24', '13', '9', '0' (the standard rate of that size), "
                                 "or a tax Id from merit_reference_list kind=taxes for special regimes, e.g. "
                                 "'Teenuse käive ei ole Eestis' for services sold abroad")
    unit: str | None = Field(None, description="Unit, e.g. 'tk', 'h', 'kuu'")
    discount_pct: float = 0
    gl_account: str | None = Field(None, description="Revenue account code; default: the item's own")
    new_item_type: Literal["service", "goods"] = Field("service", description="Only used when item_code is new")


class PurchaseRow(BaseModel):
    item_code: str = Field(description="Existing item code in Merit (merit_reference_list kind=items); must exist")
    description: str = Field(max_length=100)
    quantity: float = 1
    price: float = Field(description="Unit price WITHOUT VAT, in the invoice currency")
    vat: str = Field(description="VAT rate as a number ('24', '22', '9', '0') or a tax Id from merit_reference_list kind=taxes")
    gl_account: str | None = Field(None, description="Expense account code; default: the item's own")


CONTACT_DESCRIPTION = "Who invoices are addressed to (a name on the invoice)"


class DimensionRef(BaseModel):
    dim_id: int = Field(description="DimId of the dimension (merit_reference_list kind=dimensions)")
    value_code: str = Field(description="Code of the dimension value")


EInvOperator = Literal[1, 2, 3, 4]
EINV_DESCRIPTION = ("E-invoice channel: 1 none, 2 e-invoices to the bank through Omniva, 3 bank (full e-invoice), "
                    "4 bank (limited e-invoice)")


class NewCustomer(BaseModel):
    name: str = Field(max_length=150, description="Must not exist yet in Merit")
    country_code: str = Field("EE", min_length=2, max_length=2)
    reg_no: str | None = Field(None, description="Company registry code (isikukood for a private person, optional)")
    vat_reg_no: str | None = None
    private_or_foreign: bool | None = Field(
        None, description="Merit's 'not a TD customer': true for private persons and foreign companies, "
                          "false for Estonian companies. Default: true unless country_code is EE.")
    email: str | None = Field(None, description="Where invoices are emailed")
    phone: str | None = None
    address: str | None = None
    city: str | None = Field(None, description="Required when the country is not EE")
    postal_code: str | None = None
    contact: str | None = Field(None, description=CONTACT_DESCRIPTION)
    invoice_language: Literal["ET", "EN", "RU", "FI"] | None = Field(None, description="Default: ET for EE, else EN")
    payment_days: int | None = Field(None, ge=0, le=365, description="Payment term; default: Merit's setting")
    currency: str | None = Field(None, description="Default invoice currency, e.g. EUR")
    bank_account: str | None = Field(None, description="The customer's own bank account")
    group_code: str | None = Field(None, description="Customer group code (merit_reference_list kind=customer_groups)")
    ref_no_base: str | None = Field(None, max_length=36, description="Base for the customer's reference numbers")
    overdue_charge_pct: float | None = Field(None, ge=0, description="Overdue interest % per day; default: Merit's setting")
    county: str | None = None
    phone2: str | None = None
    homepage: str | None = None
    einvoice_operator: EInvOperator | None = Field(None, description=EINV_DESCRIPTION)
    einvoice_payment_id: str | None = Field(None, max_length=20, description="E-invoice payment agreement id (EInvPaymId)")
    payer_name: str | None = Field(None, max_length=150, description="Payer name, when someone else pays the invoices")
    gln_code: str | None = Field(None, max_length=10)
    party_code: str | None = Field(None, max_length=20)
    show_balance: bool | None = Field(None, description="Show the customer's balance on invoices")
    group_invoices: bool | None = Field(None, description="Group the customer's invoices (GroupInv)")
    invoice_bank_id: str | None = Field(None, description="BankId whose account is printed on this customer's invoices "
                                                          "(merit_reference_list kind=banks)")
    dimensions: list[DimensionRef] | None = Field(None, description="Default dimension values for the card")


class CustomerChanges(BaseModel):
    """Only the fields given are changed; everything else on the card stays as it is."""
    name: str | None = None
    country_code: str | None = None
    reg_no: str | None = None
    vat_reg_no: str | None = None
    private_or_foreign: bool | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    postal_code: str | None = None
    contact: str | None = Field(None, description=CONTACT_DESCRIPTION)
    invoice_language: Literal["ET", "EN", "RU", "FI"] | None = None
    payment_days: int | None = Field(None, ge=0, le=365)
    currency: str | None = None
    bank_account: str | None = None
    group_code: str | None = Field(None, description="Customer group code (merit_reference_list kind=customer_groups)")
    ref_no_base: str | None = Field(None, max_length=36, description="Base for the customer's reference numbers")
    overdue_charge_pct: float | None = Field(None, ge=0, description="Overdue interest % per day; default: Merit's setting")
    county: str | None = None
    phone2: str | None = None
    homepage: str | None = None
    einvoice_operator: EInvOperator | None = Field(None, description=EINV_DESCRIPTION)
    einvoice_payment_id: str | None = Field(None, max_length=20, description="E-invoice payment agreement id (EInvPaymId)")
    payer_name: str | None = Field(None, max_length=150, description="Payer name, when someone else pays the invoices")
    gln_code: str | None = Field(None, max_length=10)
    party_code: str | None = Field(None, max_length=20)
    show_balance: bool | None = Field(None, description="Show the customer's balance on invoices")
    group_invoices: bool | None = Field(None, description="Group the customer's invoices (GroupInv)")
    invoice_bank_id: str | None = Field(None, description="BankId whose account is printed on this customer's invoices "
                                                          "(merit_reference_list kind=banks)")
    dimensions: list[DimensionRef] | None = Field(None, description="Default dimension values for the card")


class NewVendor(BaseModel):
    name: str = Field(max_length=150, description="Must not exist yet in Merit")
    country_code: str = Field("EE", min_length=2, max_length=2)
    reg_no: str | None = None
    vat_reg_no: str | None = None
    vat_accountable: bool = Field(description="Is the vendor VAT-registered (käibemaksukohustuslane)?")
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = Field(None, description="Required when the country is not EE")
    postal_code: str | None = None
    bank_account: str | None = Field(None, description="IBAN")
    payment_days: int | None = Field(None, ge=0, le=365)
    currency: str | None = None
    group_code: str | None = Field(None, description="Vendor group code (merit_reference_list kind=vendor_groups)")
    swift_bic: str | None = Field(None, max_length=30)
    receiver_name: str | None = Field(None, max_length=150, description="Payee name on payments, if not the vendor's name")
    ref_no_base: str | None = Field(None, max_length=36, description="Reference number for payments to the vendor")
    overdue_charge_pct: float | None = Field(None, ge=0)
    county: str | None = None
    phone2: str | None = None
    homepage: str | None = None
    dimensions: list[DimensionRef] | None = Field(None, description="Default dimension values for the card")


class VendorChanges(BaseModel):
    """Only the fields given are changed; everything else on the card stays as it is. Merit's API cannot
    change a vendor's payment term or currency: do that in Merit."""
    name: str | None = None
    country_code: str | None = None
    reg_no: str | None = None
    vat_reg_no: str | None = None
    vat_accountable: bool | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    postal_code: str | None = None
    bank_account: str | None = None
    phone2: str | None = None
    group_code: str | None = Field(None, description="Vendor group code (merit_reference_list kind=vendor_groups)")
    ref_no: str | None = Field(None, max_length=36, description="Reference number for payments to the vendor")
    payer_name: str | None = Field(None, max_length=150, description="Payee name on payments, if not the vendor's name")
    dimensions: list[DimensionRef] | None = Field(None, description="Default dimension values; replaces the card's")


CUSTOMER_MAP = {"name": "Name", "country_code": "CountryCode", "reg_no": "RegNo", "vat_reg_no": "VatRegNo",
                "private_or_foreign": "NotTDCustomer", "email": "Email", "phone": "PhoneNo", "address": "Address",
                "city": "City", "postal_code": "PostalCode", "contact": "Contact", "invoice_language": "SalesInvLang",
                "payment_days": "PaymentDeadLine", "currency": "CurrencyCode", "bank_account": "BankAccount",
                "group_code": "CustGrCode", "ref_no_base": "RefNoBase", "overdue_charge_pct": "OverDueCharge",
                "county": "County", "phone2": "PhoneNo2", "homepage": "HomePage", "einvoice_operator": "EInvOperator",
                "einvoice_payment_id": "EInvPaymId", "payer_name": "PayerReceiverName", "gln_code": "GLNCode",
                "party_code": "PartyCode", "show_balance": "ShowBalance", "group_invoices": "GroupInv",
                "invoice_bank_id": "BankOnSalesInvoice"}
VENDOR_MAP = {"name": "Name", "country_code": "CountryCode", "reg_no": "RegNo", "vat_reg_no": "VatRegNo",
              "vat_accountable": "VatAccountable", "email": "Email", "phone": "PhoneNo", "address": "Address",
              "city": "City", "postal_code": "PostalCode", "bank_account": "BankAccount",
              "payment_days": "PaymentDeadLine", "currency": "CurrencyCode", "group_code": "VendGrCode",
              "swift_bic": "SWIFT_BIC", "receiver_name": "ReceiverName", "ref_no_base": "RefNoBase",
              "overdue_charge_pct": "OverDueCharge", "county": "County", "phone2": "PhoneNo2", "homepage": "HomePage"}
# v2/updatevendor names some fields differently from v2/sendvendor.
VENDOR_UPDATE_MAP = {**{k: v for k, v in VENDOR_MAP.items() if k not in ("payment_days", "currency", "swift_bic",
                                                                       "receiver_name", "ref_no_base",
                                                                       "overdue_charge_pct", "county", "homepage")},
                     "ref_no": "ReferenceNo", "payer_name": "PayerReceiverName"}
# Fields v1/updatecustomer and v2/updatevendor accept, as getcustomers / getvendors name them.
CUSTOMER_UPDATABLE = ("Name", "CountryCode", "Address", "City", "PostalCode", "PhoneNo", "PhoneNo2", "Email", "RegNo",
                      "VatRegNo", "SalesInvLang", "RefNoBase", "BankAccount", "Contact", "PaymentDeadLine",
                      "NotTDCustomer", "CurrencyCode", "County", "HomePage", "GLNCode", "PartyCode", "EInvOperator",
                      "EInvPaymId", "PayerReceiverName", "ShowBalance", "GroupInv", "BankOnSalesInvoice", "ApixEInv")
VENDOR_UPDATABLE = ("Name", "CountryCode", "Address", "City", "PostalCode", "PhoneNo", "PhoneNo2", "Email", "RegNo",
                    "VatRegNo", "VatAccountable", "BankAccount", "ReferenceNo")


def _read_back(diff: dict, after: dict) -> dict:
    """Compare the changes with the card as Merit shows it afterwards. Fields Merit's card list does
    not return (dimensions, e-invoice settings, …) cannot be checked, and are named as such."""
    wrong = {k: after.get(k) for k, v in diff.items() if k in after and after.get(k) != v}
    unchecked = sorted(k for k in diff if k not in after)
    out: dict = {"warning": f"Merit shows different values for: {wrong}"} if wrong else {"verified": not unchecked}
    if unchecked:
        out["not_verifiable"] = f"Merit's card list does not show {unchecked}; check them in Merit if they matter"
    return out


def _card(fields: dict, mapping: dict) -> dict:
    return {mapping[k]: v for k, v in fields.items() if v is not None and k in mapping}


# ---------------------------------------------------------------------------- Merit lookups

class Books:
    """Lookups the write tools share, with a short cache for the slow-changing lists."""

    def __init__(self, merit: Merit):
        self.m = merit
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _cached(self, key: str, fetch) -> Any:
        hit = self._cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        value = await fetch()
        self._cache[key] = (time.time() + 600, value)
        return value

    async def taxes(self) -> list[dict]:
        return await self._cached("taxes", lambda: self.m.call("v2/taxes", None, method="GET"))

    async def items(self, require: list[str] | tuple = ()) -> dict[str, dict]:
        """Items by code. Fetched again when a required code is missing from the cached list: a sales
        invoice or e-invoice can create an item in Merit meanwhile."""
        async def fetch():
            return {i["Code"]: i for i in await self.m.call("v1/getitems", {}) or []}
        items = await self._cached("items", fetch)
        if any(c not in items for c in require):
            self._cache.pop("items", None)
            items = await self._cached("items", fetch)
        return items

    async def tax(self, vat: str) -> dict:
        taxes = [t for t in await self.taxes() if not t.get("NonActive")]
        vat = vat.strip()
        if GUID.match(vat):
            for t in taxes:
                if t["Id"].lower() == vat.lower():
                    return t
            raise ToolError(f"no active tax with Id {vat} (merit_reference_list kind=taxes)")
        try:
            pct = float(vat.rstrip("%").replace(",", "."))
        except ValueError:
            raise ToolError(f"vat must be a rate like '24' or a tax Id, not {vat!r}") from None
        hits = [t for t in taxes if t["TaxPct"] == pct and STANDARD_VAT.match(t["Name"].strip())]
        if len(hits) == 1:
            return hits[0]
        options = [f"{t['Id']} = {t['Name']} ({t['TaxPct']}%)" for t in taxes if t["TaxPct"] == pct]
        raise ToolError(f"no single standard {pct:g}% VAT; pass one of these tax Ids instead: {options}")

    async def dimensions(self, refs: list | None) -> list[dict] | None:
        """Merit's Dimensions objects for [{dim_id, value_code}], refusing unknown or closed values."""
        if not refs:
            return None
        rows = await self.m.call("v2/getdimensions", {"AllValues": False}) or []
        out = []
        for ref in refs:
            hits = [r for r in rows if r.get("DimId") == ref.dim_id and (r.get("Code") or "").strip().lower()
                    == ref.value_code.strip().lower() and not r.get("NonActive")]
            if len(hits) != 1:
                raise ToolError(f"no single active value {ref.value_code!r} in dimension {ref.dim_id} "
                                "(merit_reference_list kind=dimensions)")
            out.append({"DimId": ref.dim_id, "DimValueId": hits[0]["Id"], "DimCode": hits[0]["Code"]})
        return out

    async def group_code(self, kind: Literal["customer", "vendor"], *, code: str | None = None,
                         name: str | None = None) -> str | None:
        """Check a group code exists, or find the code of a group by name (cards only show the name)."""
        if not code and not name:
            return None
        groups = await self.m.call(f"v2/get{kind}groups", {}) or []
        for g in groups:
            if (code and (g.get("Code") or "").strip().lower() == code.strip().lower()) or \
                    (not code and (g.get("Name") or "").strip().lower() == name.strip().lower()):
                return g.get("Code")
        if code:
            raise ToolError(f"no {kind} group with code {code!r} (merit_reference_list kind={kind}_groups)")
        return None

    async def card_fields(self, kind: Literal["customer", "vendor"], fields: dict, mapping: dict) -> dict:
        """A card's Merit fields from model fields, with the group and dimensions checked and resolved."""
        refs = fields.pop("dimensions", None)
        if fields.get("group_code"):
            fields["group_code"] = await self.group_code(kind, code=fields["group_code"])
        card = _card(fields, mapping)
        if refs:
            card["Dimensions"] = await self.dimensions([DimensionRef(**r) if isinstance(r, dict) else r for r in refs])
        return card

    async def customer(self, customer_id: str) -> dict:
        found = await self.m.call("v1/getcustomers", {"Id": customer_id})
        found = [found] if isinstance(found, dict) else (found or [])
        if not found:
            raise ToolError(f"no customer with Id {customer_id} (merit_find_customers)")
        return found[0]

    async def vendor(self, vendor_id: str) -> dict:
        found = await self.m.call("v1/getvendors", {"Id": vendor_id})
        found = [found] if isinstance(found, dict) else (found or [])
        if not found:
            raise ToolError(f"no vendor with Id {vendor_id} (merit_find_vendors)")
        return found[0]

    async def check_new_party(self, path: str, name: str, reg_no: str | None, kind: str) -> None:
        """Merit silently reuses an existing card with the same name, so refuse up front."""
        clashes = []
        for payload in ({"Name": name}, {"RegNo": reg_no} if reg_no else None):
            if payload:
                rows = await self.m.call(path, payload)
                rows = [rows] if isinstance(rows, dict) else (rows or [])
                clashes += [r for r in rows if r.get("Name", "").strip().lower() == name.strip().lower()
                            or (reg_no and (r.get("RegNo") or "").strip() == reg_no.strip())]
        if clashes:
            c = clashes[0]
            raise ToolError(f"{kind} already exists in Merit: {c.get('Name')!r} (Id {c.get(kind.capitalize() + 'Id')}, "
                            f"reg no {c.get('RegNo')}). Use that Id instead of creating a new one.")

    async def next_invoice_no(self, today: date) -> str:
        """Merit cannot hand out numbers: take the highest this-year number (2026001 style) + 1."""
        rows = await self.m.call_period("v2/getinvoices", date(today.year, 1, 1), today)
        prefix = str(today.year)
        nums = [r["InvoiceNo"] for r in rows if r["InvoiceNo"].isdigit() and r["InvoiceNo"].startswith(prefix)
                and len(r["InvoiceNo"]) <= 8]
        if not nums:
            return prefix + "001"
        top = max(nums, key=int)
        return str(int(top) + 1).zfill(len(top))

    async def sales_invoice(self, invoice_id: str) -> dict:
        return await self.m.call("v2/getinvoice", {"Id": invoice_id, "AddAttachment": False})

    async def purchase_invoice(self, invoice_id: str) -> dict:
        try:
            return await self.m.call("v2/getpurchorder", {"Id": invoice_id, "SkipAttachment": True})
        except ToolError:
            today = date.today()
            if any(p.get("PIHId") == invoice_id for p in await self.pending_purchases(today - timedelta(days=365), today)):
                raise ToolError("This purchase invoice is waiting for approval in Merit. The API cannot change or delete "
                                "those: approve, correct or delete it in Merit's list of purchase invoices waiting "
                                "for approval.") from None
            raise

    async def pending_purchases(self, start: date, end: date) -> list[dict]:
        """Purchase invoices entered with wait_for_approval and not yet approved."""
        rows: list[dict] = []
        for s, e in _chunks(start, end):
            part = await self.m.call("v2/GetPOrders", {"PeriodStart": s.strftime("%Y%m%d"), "PeriodEnd": e.strftime("%Y%m%d"),
                                                       "DateFilter": 1, "StatusFilter": 0, "WithAttachments": False})
            rows.extend(p for p in ([part] if isinstance(part, dict) else (part or [])) if p)
        return rows


async def _rows(books: Books, rows: list, sales: bool) -> tuple[list[dict], list[dict], list[dict], list[dict], Decimal, Decimal, list[str]]:
    """Merit invoice rows, TaxAmount list, readable rows and VAT per rate, net and VAT totals, notes.
    Each row is rounded on its own, then VAT is summed per tax, which is how Merit counts."""
    if not rows:
        raise ToolError("at least one row is needed")
    if len(rows) > 500:
        raise ToolError("Merit allows at most 500 rows per document")
    items = await books.items(require=[r.item_code for r in rows])
    out, summary, notes = [], [], []
    vat_by_tax: dict[str, Decimal] = {}
    tax_names: dict[str, str] = {}
    net_total = Decimal(0)
    for r in rows:
        tax = await books.tax(r.vat)
        qty, price = Decimal(str(r.quantity)), Decimal(str(r.price))
        disc_pct = Decimal(str(getattr(r, "discount_pct", 0) or 0))
        disc = qty * price * disc_pct / 100
        net = _money(qty * price - disc)
        vat = _money(net * Decimal(str(tax["TaxPct"])) / 100)
        net_total += net
        vat_by_tax[tax["Id"]] = vat_by_tax.get(tax["Id"], Decimal(0)) + vat
        tax_names[tax["Id"]] = f"{tax['Name'].strip()} ({tax['TaxPct']:g}%)"
        item: dict = {"Code": r.item_code, "Description": r.description}
        known = items.get(r.item_code)
        if known is None:
            if not sales:
                raise ToolError(f"item code {r.item_code!r} does not exist in Merit; purchase rows need an existing "
                                "item (merit_reference_list kind=items)")
            item["Type"] = 2 if r.new_item_type == "service" else 3
            notes.append(f"new item {r.item_code!r} will be created as a {r.new_item_type}")
        if getattr(r, "unit", None):
            item["UOMName"] = r.unit
        row: dict = {"Item": item, "Quantity": float(qty), "Price": float(price), "TaxId": tax["Id"]}
        if sales:
            row["DiscountPct"] = float(disc_pct)
            row["DiscountAmount"] = float(_money(disc))
        if r.gl_account:
            row["GLAccountCode"] = r.gl_account
        out.append(row)
        summary.append({"item": r.item_code, "description": r.description, "quantity": float(qty), "unit_price": float(price),
                        "discount_pct": float(disc_pct) or None, "net": float(net), "vat": float(vat),
                        "vat_rate": tax_names[tax["Id"]], "account": r.gl_account})
    tax_amount = [{"TaxId": t, "Amount": float(_money(v))} for t, v in vat_by_tax.items()]
    vat_total = sum((_money(v) for v in vat_by_tax.values()), Decimal(0))
    summary_tax = [{"vat_rate": tax_names[t], "amount": float(_money(v))} for t, v in vat_by_tax.items()]
    return out, tax_amount, summary, summary_tax, net_total, vat_total, notes


# ---------------------------------------------------------------------------- tools

def register_write(mcp: FastMCP, merit: Merit, files: Files | None, books: Books | None = None) -> None:
    books = books or Books(merit)

    # ------------------------------------------------------------ sales invoices

    async def build_sales(customer_id, new_customer, rows, invoice_no, doc_date, due_date, currency,
                          header_comment, footer_comment, today=None):
        today = today or date.today()
        doc = doc_date or today
        notes: list[str] = []
        if bool(customer_id) == bool(new_customer):
            raise ToolError("give exactly one of customer_id or new_customer")
        if customer_id:
            card = await books.customer(customer_id)
            customer = {"Id": card["CustomerId"]}
            who = {"customer": card["Name"], "customer_id": card["CustomerId"], "email": card.get("Email"),
                   "reg_no": card.get("RegNo")}
            days = card.get("PaymentDeadLine")
        else:
            await books.check_new_party("v1/getcustomers", new_customer.name, new_customer.reg_no, "customer")
            fields = new_customer.model_dump()
            if fields["private_or_foreign"] is None:
                fields["private_or_foreign"] = new_customer.country_code.upper() != "EE"
            if fields["invoice_language"] is None:
                fields["invoice_language"] = "ET" if new_customer.country_code.upper() == "EE" else "EN"
            if new_customer.country_code.upper() != "EE" and not new_customer.city:
                raise ToolError("city is required for a customer outside Estonia")
            customer = await books.card_fields("customer", fields, CUSTOMER_MAP)
            who = {"customer": new_customer.name, "new_customer_card": customer}
            notes.append(f"new customer card {new_customer.name!r} will be created")
            days = new_customer.payment_days
        lines, tax_amount, summary, vat_rates, net, vat, row_notes = await _rows(books, rows, sales=True)
        number = invoice_no or await books.next_invoice_no(today)
        due = due_date or (doc + timedelta(days=days if days is not None else 14))
        payload = {"Customer": customer, "DocDate": _dt(doc), "DueDate": _dt(due), "TransactionDate": _dt(doc),
                   "InvoiceNo": number, "CurrencyCode": (currency or "EUR").upper(), "InvoiceRow": lines,
                   "TotalAmount": float(net), "RoundingAmount": 0, "TaxAmount": tax_amount}
        if header_comment is not None:
            payload["HComment"] = header_comment
        if footer_comment is not None:
            payload["FComment"] = footer_comment
        preview = {**who, "invoice_no": number, "invoice_no_note": None if invoice_no else "next free number in Merit",
                   "date": doc.isoformat(), "due_date": due.isoformat(), "currency": payload["CurrencyCode"],
                   "rows": summary, "vat_by_rate": vat_rates, "total_without_vat": float(net), "vat": float(vat),
                   "total_with_vat": float(net + vat), "header_comment": header_comment, "footer_comment": footer_comment, "notes": notes + row_notes}
        return payload, preview

    async def create_sales(payload: dict) -> dict:
        res = await merit.call("v2/sendinvoice", payload)
        if not isinstance(res, dict) or not res.get("InvoiceId"):
            raise ToolError(f"Merit did not confirm the invoice: {str(res)[:300]}")
        inv = await books.sales_invoice(res["InvoiceId"])
        h = inv.get("Header") or {}
        expected = _money(payload["TotalAmount"] + sum(t["Amount"] for t in payload["TaxAmount"]))
        out = {"written": True, "invoice_id": res["InvoiceId"], "invoice_no": res.get("InvoiceNo"),
               "ref_no": res.get("RefNo"), "customer_id": res.get("CustomerId"), "new_customer": res.get("NewCustomer"),
               "total_with_vat_in_merit": h.get("TotalSum"), "batch": h.get("BatchInfo")}
        if h.get("TotalSum") is not None and _money(h["TotalSum"]) != expected:
            out["warning"] = f"Merit's total {h['TotalSum']} differs from the previewed {expected}; check the invoice."
        return out

    @mcp.tool(annotations=WRITE)
    async def merit_create_sales_invoice(
        rows: list[SalesRow],
        customer_id: Annotated[str | None, Field(description="Existing customer's Id (merit_find_customers)")] = None,
        new_customer: Annotated[NewCustomer | None, Field(description="Only for a customer not yet in Merit")] = None,
        invoice_no: Annotated[str | None, Field(description="Default: the next free number")] = None,
        doc_date: Annotated[date | None, Field(description="Invoice date; default today")] = None,
        due_date: Annotated[date | None, Field(description="Default: date + the customer's payment term")] = None,
        currency: Annotated[str | None, Field(description="Default EUR; other currencies use the ECB rate of the date")] = None,
        header_comment: Annotated[str | None, Field(description="Text above the rows; default: from the customer card")] = None,
        footer_comment: Annotated[str | None, Field(description="Text below the rows; default: from the customer card")] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Create a sales invoice (müügiarve) in Merit. It is booked but NOT sent: sending is
        merit_send_sales_invoice. Prices are without VAT. dry_run=true returns the preview only."""
        payload, preview = await build_sales(customer_id, new_customer, rows, invoice_no, doc_date, due_date, currency,
                                             header_comment, footer_comment)
        if (gate := _gate("create_sales_invoice", payload, dry_run, preview)) is not None:
            return gate
        return await create_sales(payload)

    async def deletable_sales(invoice_id: str) -> dict:
        inv = await books.sales_invoice(invoice_id)
        h = inv.get("Header") or {}
        if not h:
            raise ToolError(f"no sales invoice {invoice_id}")
        if h.get("Paid") or (h.get("PaidAmount") or 0) != 0 or inv.get("Payments"):
            raise ToolError(f"invoice {h.get('InvoiceNo')} has payments, so it cannot be deleted or replaced; "
                            "use merit_create_credit_invoice")
        if _is_sent(h):
            raise ToolError(f"invoice {h.get('InvoiceNo')} was already sent to the customer, so it cannot be deleted "
                            "or replaced; use merit_create_credit_invoice")
        return inv

    def sales_brief(inv: dict) -> dict:
        h = inv.get("Header") or {}
        return compact({"invoice_no": h.get("InvoiceNo"), "customer": h.get("CustomerName"), "date": h.get("DocumentDate"),
                        "currency": h.get("CurrencyCode"), "total_with_vat": h.get("TotalSum"),
                        "rows": [{"item": l.get("ArticleCode"), "description": l.get("Description"),
                                  "quantity": l.get("Quantity"), "unit_price": l.get("Price")} for l in inv.get("Lines") or []]})

    @mcp.tool(annotations=WRITE)
    async def merit_delete_sales_invoice(
        invoice_id: Annotated[str, Field(description="SIHId (merit_list_sales_invoices)")],
        dry_run: DryRun = False,
    ) -> dict:
        """Delete a sales invoice that has no payments and was never sent. Paid or sent invoices
        are refused: correct those with merit_create_credit_invoice. dry_run=true returns the preview only."""
        inv = await deletable_sales(invoice_id)
        state = {"id": invoice_id, "changed": (inv.get("Header") or {}).get("ChangedDate")}
        if (gate := _gate("delete_sales_invoice", state, dry_run, {"delete": sales_brief(inv)})) is not None:
            return gate
        await merit.call(f"v2/deleteinvoice/{invoice_id}", {}, text_ok=True)
        return {"written": True, "deleted_invoice": sales_brief(inv)}

    @mcp.tool(annotations=WRITE)
    async def merit_replace_sales_invoice(
        invoice_id: Annotated[str, Field(description="SIHId of the invoice to correct")],
        rows: Annotated[list[SalesRow], Field(description="ALL rows of the corrected invoice, not only the changed ones")],
        customer_id: Annotated[str | None, Field(description="Default: the original customer")] = None,
        doc_date: Annotated[date | None, Field(description="Default: the original date")] = None,
        due_date: Annotated[date | None, Field(description="Default: the original due date")] = None,
        currency: Annotated[str | None, Field(description="Default: the original currency")] = None,
        header_comment: str | None = None,
        footer_comment: str | None = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Correct a sales invoice that has no payments and was never sent: Merit cannot edit
        invoices, so it is deleted and created again with the same number. Paid or sent invoices
        are refused (use merit_create_credit_invoice). dry_run=true previews old and new."""
        inv = await deletable_sales(invoice_id)
        h = inv["Header"]
        doc = doc_date or date.fromisoformat(h["DocumentDate"][:10])
        due = due_date or date.fromisoformat(h["DueDate"][:10])
        payload, preview = await build_sales(customer_id or h["CustomerId"], None, rows, h["InvoiceNo"], doc, due,
                                             currency or h.get("CurrencyCode"), header_comment, footer_comment)
        state = {"replace": invoice_id, "changed": h.get("ChangedDate"), "new": payload}
        if (gate := _gate("replace_sales_invoice", state, dry_run,
                          {"old": sales_brief(inv), "new": preview})) is not None:
            return gate
        await merit.call(f"v2/deleteinvoice/{invoice_id}", {}, text_ok=True)
        try:
            return {**await create_sales(payload), "replaced": sales_brief(inv)}
        except Exception as e:
            log.error("REPLACE FAILED after delete of %s: %s", invoice_id, e)
            raise ToolError(f"The old invoice {h['InvoiceNo']} WAS DELETED, but creating the corrected one failed: {e}. "
                            f"Tell the user at once. Old invoice for re-creating it: {json.dumps(sales_brief(inv), ensure_ascii=False)}")

    @mcp.tool(annotations=WRITE)
    async def merit_create_credit_invoice(
        invoice_id: Annotated[str, Field(description="SIHId of the invoice to credit")],
        reason: Annotated[str | None, Field(description="Shown above the rows, after the reference to the original")] = None,
        doc_date: Annotated[date | None, Field(description="Default today")] = None,
        invoice_no: Annotated[str | None, Field(description="Default: the next free number")] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Create a credit invoice (kreeditarve) that cancels a whole sales invoice: the same rows
        with negative quantities, a new number, and a reference to the original in the text. Use it
        for invoices that are paid or already sent. It is not sent automatically. dry_run=true returns the preview only."""
        inv = await books.sales_invoice(invoice_id)
        h = inv.get("Header") or {}
        if not h:
            raise ToolError(f"no sales invoice {invoice_id}")
        if (h.get("TotalSum") or 0) <= 0:
            raise ToolError(f"invoice {h.get('InvoiceNo')} is not a positive invoice, so there is nothing to credit")
        today = date.today()
        doc = doc_date or today
        lines, vat_by_tax, net = [], {}, Decimal(0)
        for l in inv.get("Lines") or []:
            amount = -_money(l.get("AmountExclVat") or 0)
            vat = -_money(l.get("VatAmount") or 0)
            row = {"Item": {"Code": l["ArticleCode"], "Description": l.get("Description") or l["ArticleCode"]},
                   "Quantity": -float(l.get("Quantity") or 0), "Price": float(l.get("Price") or 0),
                   "TaxId": l["TaxId"], "DiscountPct": float(l.get("DiscountPct") or 0),
                   "DiscountAmount": -float(l.get("DiscountAmount") or 0)}
            if l.get("AccountCode"):
                row["GLAccountCode"] = l["AccountCode"]
            if l.get("UOMName"):
                row["Item"]["UOMName"] = l["UOMName"]
            lines.append(row)
            net += amount
            vat_by_tax[l["TaxId"]] = vat_by_tax.get(l["TaxId"], Decimal(0)) + vat
        number = invoice_no or await books.next_invoice_no(today)
        text = f"Kreeditarve arvele {h['InvoiceNo']} / Credit note for invoice {h['InvoiceNo']}"
        payload = {"Customer": {"Id": h["CustomerId"]}, "DocDate": _dt(doc), "DueDate": _dt(doc),
                   "TransactionDate": _dt(doc), "InvoiceNo": number, "CurrencyCode": h.get("CurrencyCode") or "EUR",
                   "InvoiceRow": lines, "TotalAmount": float(net), "RoundingAmount": 0,
                   "TaxAmount": [{"TaxId": t, "Amount": float(v)} for t, v in vat_by_tax.items()],
                   "HComment": text + (f"\n{reason}" if reason else "")}
        if h.get("CurrencyRate") and payload["CurrencyCode"] != "EUR":
            payload["CurrencyRate"] = h["CurrencyRate"]
        vat_total = sum(vat_by_tax.values(), Decimal(0))
        preview = {"credits": sales_brief(inv), "credit_invoice_no": number, "date": doc.isoformat(),
                   "total_without_vat": float(net), "vat": float(vat_total), "total_with_vat": float(net + vat_total),
                   "header_comment": payload["HComment"]}
        if (gate := _gate("create_credit_invoice", {"of": invoice_id, "new": payload}, dry_run, preview)) is not None:
            return gate
        return {**await create_sales(payload), "credits_invoice": h["InvoiceNo"]}

    @mcp.tool(annotations=WRITE)
    async def merit_send_sales_invoice(
        invoice_id: Annotated[str, Field(description="SIHId (merit_list_sales_invoices)")],
        via: Annotated[Literal["email", "einvoice"], Field(description="email: to the address on the customer card; "
                                                                        "einvoice: through the customer's e-invoice operator")] = "email",
        without_prices: Annotated[bool, Field(description="Send as a delivery note, without prices")] = False,
        dry_run: DryRun = False,
    ) -> dict:
        """SEND a sales invoice to the customer from Merit. Cannot be undone. Only when the user
        explicitly asked to send this invoice. dry_run=true shows the recipient and the invoice."""
        inv = await books.sales_invoice(invoice_id)
        h = inv.get("Header") or {}
        if not h:
            raise ToolError(f"no sales invoice {invoice_id}")
        card = await books.customer(h["CustomerId"])
        if via == "email" and not card.get("Email"):
            raise ToolError(f"customer {card['Name']!r} has no email on the card; add one with merit_update_customer")
        preview = {"send": sales_brief(inv), "via": via,
                   "to": card.get("Email") if via == "email" else "the customer's e-invoice address",
                   "without_prices": without_prices,
                   "warning": "This invoice was already sent before." if _is_sent(h) else None}
        state = {"id": invoice_id, "via": via, "np": without_prices, "to": card.get("Email"), "changed": h.get("ChangedDate")}
        if (gate := _gate("send_sales_invoice", state, dry_run, preview)) is not None:
            return gate
        path = "v2/sendinvoicebyemail" if via == "email" else "v2/sendinvoiceaseinv"
        try:
            res = await merit.call(path, {"Id": invoice_id, "DelivNote": without_prices}, text_ok=True)
        except ToolError as e:
            if "saataja e-mail" in str(e):  # seen live 2026-11-21 in a company without the setting
                raise ToolError("Nothing was sent: Merit has no sender email in its sales invoice settings "
                                "(Seadistused → Müügiarve seaded). Add it in Merit, then try again.") from None
            raise
        text = res if isinstance(res, str) else json.dumps(res)
        if "api-noeinv" in text:
            raise ToolError("The customer cannot receive e-invoices (api-noeinv). Nothing was sent; use via=email.")
        if text.strip().strip('"').upper() != "OK":
            raise ToolError(f"Merit did not confirm sending: {text[:300]}")
        return {"written": True, "sent": True, "invoice_no": h.get("InvoiceNo"), "via": via, "to": preview["to"]}

    # ------------------------------------------------------------ customers

    @mcp.tool(annotations=WRITE)
    async def merit_create_customer(customer: NewCustomer, dry_run: DryRun = False) -> dict:
        """Add a customer card in Merit. Refused if a customer with the same name or registry
        code exists. dry_run=true returns the preview only."""
        await books.check_new_party("v1/getcustomers", customer.name, customer.reg_no, "customer")
        fields = customer.model_dump()
        if fields["private_or_foreign"] is None:
            fields["private_or_foreign"] = customer.country_code.upper() != "EE"
        if fields["invoice_language"] is None:
            fields["invoice_language"] = "ET" if customer.country_code.upper() == "EE" else "EN"
        if customer.country_code.upper() != "EE" and not customer.city:
            raise ToolError("city is required for a customer outside Estonia")
        payload = await books.card_fields("customer", fields, CUSTOMER_MAP)
        if (gate := _gate("create_customer", payload, dry_run, {"new_customer": payload})) is not None:
            return gate
        res = await merit.call("v2/sendcustomer", payload)
        if not isinstance(res, dict) or not res.get("Id"):
            raise ToolError(f"Merit did not confirm the customer: {str(res)[:300]}")
        return {"written": True, "customer_id": res["Id"], "name": res.get("Name")}

    @mcp.tool(annotations=WRITE)
    async def merit_update_customer(
        customer_id: Annotated[str, Field(description="CustomerId (merit_find_customers)")],
        changes: CustomerChanges,
        dry_run: DryRun = False,
    ) -> dict:
        """Change fields on a customer card; fields not given stay as they are. Invoices already
        made keep their old details. dry_run=true shows old and new values."""
        card = await books.customer(customer_id)
        diff = await books.card_fields("customer", changes.model_dump(), CUSTOMER_MAP)
        if not diff:
            raise ToolError("no changes given")
        record = {k: card.get(k) for k in CUSTOMER_UPDATABLE if card.get(k) is not None}
        # The card shows only the group's name; send its code so the update keeps the group.
        if "CustGrCode" not in diff and card.get("CustomerGroupName"):
            if code := await books.group_code("customer", name=card["CustomerGroupName"]):
                record["CustGrCode"] = code
        record.update(diff)
        record["Id"] = customer_id
        if "OverdueCharge" in card:
            record["OverDueCharge"] = card["OverdueCharge"]
        preview = {"customer": card["Name"], "changes": {k: {"from": card.get(k), "to": v} for k, v in diff.items()}}
        if "CustGrCode" in diff:
            preview["changes"]["CustGrCode"]["from"] = card.get("CustomerGroupName")
        if (gate := _gate("update_customer", record, dry_run, preview)) is not None:
            return gate
        await merit.call("v1/updatecustomer", record, text_ok=True)
        after = await books.customer(customer_id)
        return {"written": True, "customer_id": customer_id, **_read_back(diff, after)}

    # ------------------------------------------------------------ vendors

    @mcp.tool(annotations=WRITE)
    async def merit_create_vendor(vendor: NewVendor, dry_run: DryRun = False) -> dict:
        """Add a vendor (supplier) card in Merit. Refused if a vendor with the same name or registry
        code exists. dry_run=true returns the preview only."""
        await books.check_new_party("v1/getvendors", vendor.name, vendor.reg_no, "vendor")
        if vendor.country_code.upper() != "EE" and not vendor.city:
            raise ToolError("city is required for a vendor outside Estonia")
        payload = {**await books.card_fields("vendor", vendor.model_dump(), VENDOR_MAP), "VendorType": 1}
        if (gate := _gate("create_vendor", payload, dry_run, {"new_vendor": payload})) is not None:
            return gate
        res = await merit.call("v2/sendvendor", payload)
        if not isinstance(res, dict) or not res.get("Id"):
            raise ToolError(f"Merit did not confirm the vendor: {str(res)[:300]}")
        return {"written": True, "vendor_id": res["Id"], "name": res.get("Name")}

    @mcp.tool(annotations=WRITE)
    async def merit_update_vendor(
        vendor_id: Annotated[str, Field(description="VendorId (merit_find_vendors)")],
        changes: VendorChanges,
        dry_run: DryRun = False,
    ) -> dict:
        """Change fields on a vendor card; fields not given stay as they are. dry_run=true shows old
        and new values."""
        card = await books.vendor(vendor_id)
        diff = await books.card_fields("vendor", changes.model_dump(), VENDOR_UPDATE_MAP)
        if not diff:
            raise ToolError("no changes given")
        record = {k: card.get(k) for k in VENDOR_UPDATABLE if card.get(k) is not None}
        if "VendGrCode" not in diff and card.get("VendorGroupName"):
            if code := await books.group_code("vendor", name=card["VendorGroupName"]):
                record["VendGrCode"] = code
        record.update(diff)
        record["Id"] = vendor_id
        preview = {"vendor": card["Name"], "changes": {k: {"from": card.get(k), "to": v} for k, v in diff.items()}}
        if "VendGrCode" in diff:
            preview["changes"]["VendGrCode"]["from"] = card.get("VendorGroupName")
        if (gate := _gate("update_vendor", record, dry_run, preview)) is not None:
            return gate
        await merit.call("v2/updatevendor", record, text_ok=True)
        after = await books.vendor(vendor_id)
        return {"written": True, "vendor_id": vendor_id, **_read_back(diff, after)}

    # ------------------------------------------------------------ purchase invoices

    async def build_purchase(vendor_id, new_vendor, bill_no, rows, doc_date, due_date, currency, comment,
                             attachment_upload_id, wait_for_approval, replacing: str | None = None, attachment_path: str | None = None,
                             keep_attachment: dict | None = None):
        today = date.today()
        doc = doc_date or today
        notes: list[str] = []
        if bool(vendor_id) == bool(new_vendor):
            raise ToolError("give exactly one of vendor_id or new_vendor")
        if vendor_id:
            card = await books.vendor(vendor_id)
            vendor = {"Id": card["VendorId"]}
            who = {"vendor": card["Name"], "vendor_id": card["VendorId"], "reg_no": card.get("RegNo")}
            days = card.get("PaymentDeadLine")
            vat_registered = card.get("VatAccountable") is not False
            # The same bill twice from one vendor is almost always a duplicate.
            recent = await merit.call_period("v2/getpurchorders", today - timedelta(days=365), today)
            pending = await books.pending_purchases(today - timedelta(days=365), today)
            dupes = [p for p in recent + pending if card["VendorId"] in (p.get("VendorId"), p.get("ReceiverId"))
                     and p.get("PIHId") != replacing
                     and (p.get("BillNo") or "").strip().lower() == bill_no.strip().lower()]
            if dupes:
                raise ToolError(f"{card['Name']} bill {bill_no} is already in Merit (PIHId {dupes[0]['PIHId']}, "
                                f"{dupes[0].get('DocumentDate', '')[:10]}); not creating a duplicate")
        else:
            await books.check_new_party("v1/getvendors", new_vendor.name, new_vendor.reg_no, "vendor")
            if new_vendor.country_code.upper() != "EE" and not new_vendor.city:
                raise ToolError("city is required for a vendor outside Estonia")
            vendor = await books.card_fields("vendor", new_vendor.model_dump(), VENDOR_MAP)
            who = {"vendor": new_vendor.name, "new_vendor_card": vendor}
            notes.append(f"new vendor card {new_vendor.name!r} will be created")
            days = new_vendor.payment_days
            vat_registered = new_vendor.vat_accountable
        if not vat_registered:
            for r in rows:
                if (await books.tax(r.vat))["TaxPct"] > 0:
                    raise ToolError(f"{who['vendor']} is not VAT-registered, so its invoice has no deductible VAT and "
                                    "Merit would book the VAT as part of the cost. Use vat '0' with the full price, "
                                    "or mark the vendor VAT-registered first (merit_update_vendor) if that is wrong.")
        lines, tax_amount, summary, vat_rates, net, vat, _ = await _rows(books, rows, sales=False)
        due = due_date or (doc + timedelta(days=days if days is not None else 14))
        payload = {"Vendor": vendor, "DocDate": _dt(doc), "DueDate": _dt(due), "TransactionDate": _dt(doc),
                   "BillNo": bill_no, "CurrencyCode": (currency or "EUR").upper(), "InvoiceRow": lines,
                   "TotalAmount": float(net), "RoundingAmount": 0, "TaxAmount": tax_amount}
        if comment:
            payload["HComment"] = comment
        file_note = None
        if attachment_upload_id or attachment_path:
            name, data = files.get(attachment_upload_id, attachment_path, (".pdf",))
            if not data.startswith(b"%PDF"):
                raise ToolError(f"{name} is not a PDF; Merit only takes PDF attachments")
            payload["Attachment"] = {"FileName": name if name.lower().endswith(".pdf") else name + ".pdf",
                                     "FileContent": base64.b64encode(data).decode()}
            file_note = f"{payload['Attachment']['FileName']} ({len(data):,} bytes)"
        elif keep_attachment and keep_attachment.get("FileContent"):
            payload["Attachment"] = {"FileName": keep_attachment.get("Filename") or "invoice.pdf",
                                     "FileContent": keep_attachment["FileContent"]}
            file_note = f"{payload['Attachment']['FileName']} (kept from the original)"
        preview = {**who, "bill_no": bill_no, "date": doc.isoformat(), "due_date": due.isoformat(),
                   "currency": payload["CurrencyCode"], "rows": summary, "vat_by_rate": vat_rates, "total_without_vat": float(net),
                   "vat": float(vat), "total_with_vat": float(net + vat), "comment": comment, "attachment": file_note,
                   "booking": "waits for approval in Merit (Ostuarved → kinnitamata)" if wait_for_approval
                              else "booked immediately into the ledger",
                   "notes": notes}
        return payload, preview

    async def create_purchase(payload: dict, wait_for_approval: bool) -> dict:
        path = "v2/sendpurchorder" if wait_for_approval else "v2/sendpurchinvoice"
        res = await merit.call(path, payload)
        if not isinstance(res, dict) or not res.get("BillId"):
            raise ToolError(f"Merit did not confirm the purchase invoice: {str(res)[:300]}")
        out = {"written": True, "purchase_invoice_id": res["BillId"], "bill_no": res.get("BillNo"),
               "vendor_id": res.get("VendorId"), "batch": res.get("BatchInfo") or res.get("BatcInfo"),
               "waiting_for_approval": wait_for_approval}
        if not wait_for_approval:
            h = (await books.purchase_invoice(res["BillId"])).get("Header") or {}
            expected = _money(payload["TotalAmount"] + sum(t["Amount"] for t in payload["TaxAmount"]))
            out["total_with_vat_in_merit"] = h.get("TotalSum")
            if h.get("TotalSum") is not None and _money(h["TotalSum"]) != expected:
                out["warning"] = f"Merit's total {h['TotalSum']} differs from the previewed {expected}; check the invoice."
        else:
            out["note"] = ("Waiting for approval in Merit. Once there, it can only be corrected or deleted in Merit "
                           "itself, not through these tools.")
        return out

    @mcp.tool(annotations=WRITE)
    async def merit_create_purchase_invoice(
        bill_no: Annotated[str, Field(description="The vendor's own invoice number")],
        rows: list[PurchaseRow],
        vendor_id: Annotated[str | None, Field(description="Existing vendor's Id (merit_find_vendors)")] = None,
        new_vendor: Annotated[NewVendor | None, Field(description="Only for a vendor not yet in Merit")] = None,
        doc_date: Annotated[date | None, Field(description="The vendor invoice's date; default today")] = None,
        due_date: Annotated[date | None, Field(description="Default: date + the vendor's payment term")] = None,
        currency: Annotated[str | None, Field(description="Default EUR")] = None,
        comment: str | None = None,
        attachment_upload_id: Annotated[str | None, Field(description="The vendor's PDF. Server mode: upload id from create_upload_link")] = None,
        attachment_path: Annotated[str | None, Field(description="The vendor's PDF. On your own computer: the file's path")] = None,
        wait_for_approval: Annotated[bool, Field(description="true (default): it waits for approval in Merit; "
                                                             "false: booked into the ledger at once")] = True,
        dry_run: DryRun = False,
    ) -> dict:
        """Enter a purchase invoice (ostuarve) from a vendor. By default it waits for approval in
        Merit instead of being booked; once there, corrections and deletion happen in Merit itself.
        Refuses a bill number the vendor already has in Merit, and VAT from a vendor that is not
        VAT-registered.
        Prices are without VAT. dry_run=true returns the preview only."""
        payload, preview = await build_purchase(vendor_id, new_vendor, bill_no, rows, doc_date, due_date, currency,
                                                comment, attachment_upload_id, wait_for_approval,
                                                attachment_path=attachment_path)
        state = {"approval": wait_for_approval, "payload": payload}
        if (gate := _gate("create_purchase_invoice", state, dry_run, preview)) is not None:
            return gate
        return await create_purchase(payload, wait_for_approval)

    async def deletable_purchase(invoice_id: str) -> dict:
        inv = await books.purchase_invoice(invoice_id)
        h = inv.get("Header") or {}
        if not h:
            raise ToolError(f"no purchase invoice {invoice_id}")
        if h.get("Paid") or (h.get("PaidAmount") or 0) != 0 or inv.get("Payments"):
            raise ToolError(f"purchase invoice {h.get('BillNo')} from {h.get('VendorName')} has payments, so it "
                            "cannot be deleted or replaced")
        return inv

    def purchase_brief(inv: dict) -> dict:
        h = inv.get("Header") or {}
        return compact({"vendor": h.get("VendorName"), "bill_no": h.get("BillNo"), "date": h.get("DocumentDate"),
                        "currency": h.get("CurrencyCode"), "total_with_vat": h.get("TotalSum"), "batch": h.get("BatchInfo"),
                        "rows": [{"item": l.get("ArticleCode"), "description": l.get("Description"),
                                  "quantity": l.get("Quantity"), "unit_price": l.get("Price")} for l in inv.get("Lines") or []]})

    @mcp.tool(annotations=WRITE)
    async def merit_delete_purchase_invoice(
        invoice_id: Annotated[str, Field(description="PIHId (merit_list_purchase_invoices)")],
        dry_run: DryRun = False,
    ) -> dict:
        """Delete a booked purchase invoice that has no payments. Invoices still waiting for approval
        can only be deleted in Merit itself. dry_run=true returns the preview only."""
        inv = await deletable_purchase(invoice_id)
        state = {"id": invoice_id, "changed": (inv.get("Header") or {}).get("ChangedDate")}
        if (gate := _gate("delete_purchase_invoice", state, dry_run, {"delete": purchase_brief(inv)})) is not None:
            return gate
        await merit.call(f"v2/deletepurchinvoice/{invoice_id}", {}, text_ok=True)
        return {"written": True, "deleted_purchase_invoice": purchase_brief(inv)}

    @mcp.tool(annotations=WRITE)
    async def merit_replace_purchase_invoice(
        invoice_id: Annotated[str, Field(description="PIHId of the purchase invoice to correct")],
        rows: Annotated[list[PurchaseRow], Field(description="ALL rows of the corrected invoice")],
        bill_no: Annotated[str | None, Field(description="Default: the original bill number")] = None,
        doc_date: Annotated[date | None, Field(description="Default: the original date")] = None,
        due_date: Annotated[date | None, Field(description="Default: the original due date")] = None,
        currency: Annotated[str | None, Field(description="Default: the original currency")] = None,
        comment: str | None = None,
        attachment_upload_id: Annotated[str | None, Field(description="New PDF (Server mode: upload id from create_upload_link); without a new "
                                                                       "file the original is kept")] = None,
        attachment_path: Annotated[str | None, Field(description="New PDF (On your own computer: the file's path)")] = None,
        wait_for_approval: bool = True,
        dry_run: DryRun = False,
    ) -> dict:
        """Correct a booked purchase invoice that has no payments (ones waiting for approval are
        corrected in Merit itself): Merit cannot edit invoices, so it is
        deleted and entered again (same vendor; the original PDF is carried over unless a new one
        is given). dry_run=true previews old and new."""
        inv = await deletable_purchase(invoice_id)
        h = inv["Header"]
        doc = doc_date or date.fromisoformat(h["DocumentDate"][:10])
        due = due_date or date.fromisoformat(h["DueDate"][:10])
        original_file = None
        if not (attachment_upload_id or attachment_path) and h.get("FileExists"):
            full = await merit.call("v2/getpurchorder", {"Id": invoice_id, "SkipAttachment": False})
            original_file = full.get("Attachment")
        payload, preview = await build_purchase(h["VendorId"], None, bill_no or h["BillNo"], rows, doc, due,
                                                currency or h.get("CurrencyCode"), comment, attachment_upload_id,
                                                wait_for_approval, replacing=invoice_id, keep_attachment=original_file, attachment_path=attachment_path)
        state = {"replace": invoice_id, "changed": h.get("ChangedDate"), "approval": wait_for_approval, "new": payload}
        if (gate := _gate("replace_purchase_invoice", state, dry_run,
                          {"old": purchase_brief(inv), "new": preview})) is not None:
            return gate
        await merit.call(f"v2/deletepurchinvoice/{invoice_id}", {}, text_ok=True)
        try:
            return {**await create_purchase(payload, wait_for_approval), "replaced": purchase_brief(inv)}
        except Exception as e:
            log.error("REPLACE FAILED after delete of purchase %s: %s", invoice_id, e)
            raise ToolError(f"The old purchase invoice {h['BillNo']} WAS DELETED, but entering the corrected one "
                            f"failed: {e}. Tell the user at once. Old invoice: {json.dumps(purchase_brief(inv), ensure_ascii=False)}")
