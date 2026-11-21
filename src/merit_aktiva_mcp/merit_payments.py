"""Write tools for payments in Merit Aktiva: payments of invoices and offers, other bank income
and expenses, prepayments, settlements (offsetting) and bank statement imports.
Registered only when ALLOW_WRITE=true.

These record payments in Merit's books; they move no money. A wrong one still misstates what
customers owe and what is owed to vendors, so every tool is strict: the bank account is resolved
from Merit's own list (ambiguity is refused), the document being paid is looked up and shown in
the preview, and paying more than is unpaid, paying in another currency or paying something
already paid is refused unless the user explicitly allows it. Like every write tool, dry_run=true returns the preview without writing.
"""

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from .merit import Merit, _ymd, compact
from .merit_write import GUID, WRITE, DryRun, Books, _dt, _gate, _money
from .files import Files

log = logging.getLogger(__name__)


LOCAL_CURRENCY = "EUR"
CENT = Decimal("0.005")  # tolerance when comparing amounts rounded to cents
MAX_STATEMENT_BYTES = 20 * 1024 * 1024
CAMT_VERSIONS = ("camt.053.001.02", "camt.053.001.10")  # what Merit's sendcamt53 says it supports
OFFER_STATUS = {1: "created", 2: "sent", 3: "approved", 4: "rejected", 5: "comment received", 6: "invoice created",
                7: "canceled"}
PAYMENT_DIRECTION = {1: "from customer", 2: "to vendor", 3: "other income", 4: "other expense"}

Bank = Annotated[str, Field(description="The bank account in Merit: its name (or part of it), IBAN or BankId "
                                        "(merit_reference_list kind=banks). Must match exactly one account")]
PaymentDate = Annotated[date | None, Field(description="Date of the payment, YYYY-MM-DD; default today; not in the future")]


def _dec(x: Any) -> Decimal:
    return Decimal(str(x or 0))


def _iban(s: str | None) -> str:
    return re.sub(r"\s", "", s or "").upper()


def _date(value: Any) -> date | None:
    """Merit dates come as 2026-11-20T00:00:00 or 20261120."""
    s = str(value or "")
    try:
        return date.fromisoformat(s[:10]) if "-" in s[:10] else date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _when(payment_date: date | None) -> date:
    d = payment_date or date.today()
    if d > date.today():
        raise ToolError(f"payment date {d} is in the future; record a payment once it has happened")
    return d


def _guid(value: str, what: str) -> str:
    value = value.strip()
    if not GUID.match(value):
        raise ToolError(f"{what} must be a Merit Id (a GUID), not {value!r}")
    return value


# ---------------------------------------------------------------------------- input models

class BankLine(BaseModel):
    account: str = Field(description="General ledger account code the money comes from or goes to "
                                     "(merit_reference_list kind=accounts), e.g. a bank fee or interest account")
    amount: float = Field(gt=0, description="Amount, positive, in the payment currency")
    description: str | None = Field(None, max_length=150)


class SettlementLine(BaseModel):
    invoice_id: str = Field(description="customer_lines: SIHId of a sales or credit invoice (merit_list_sales_invoices); "
                                        "vendor_lines: PIHId of a purchase invoice (merit_list_purchase_invoices)")
    amount: float = Field(description="Amount offset against this document: positive for an invoice, negative for a "
                                      "credit invoice; at most its unpaid amount")


# ---------------------------------------------------------------------------- tools

def register_payments(mcp: FastMCP, merit: Merit, books: Books, files: Files | None) -> None:

    async def find_bank(ref: str) -> dict:
        """Exactly one bank account from Merit's list, by BankId, IBAN or name."""
        banks = await merit.call("v2/banks", None, method="GET") or []
        ref = ref.strip()
        if not ref:
            raise ToolError("bank is required")
        if GUID.match(ref):
            hits = [b for b in banks if (b.get("BankId") or "").lower() == ref.lower()]
        else:
            hits = [b for b in banks if _iban(ref) and _iban(b.get("IBANCode")) == _iban(ref)]
            if not hits:
                hits = [b for b in banks if ref.lower() in f"{b.get('Name') or ''} {b.get('Description') or ''}".lower()]
                exact = [b for b in hits if (b.get("Name") or "").strip().lower() == ref.lower()]
                hits = exact if len(exact) == 1 else hits
        if len(hits) == 1:
            return hits[0]
        options = [f"{b.get('Name')} ({b.get('IBANCode') or 'no IBAN'}, {b.get('CurrencyCode') or '?'}, BankId "
                   f"{b.get('BankId')})" for b in (hits or banks)]
        raise ToolError(("bank matches more than one account" if hits else f"no bank account matches {ref!r}")
                        + f"; pass the IBAN or BankId of one of: {options}")

    def bank_brief(b: dict) -> dict:
        return compact({"name": b.get("Name"), "iban": b.get("IBANCode"), "currency": b.get("CurrencyCode"),
                        "gl_account": b.get("AccountCode"), "bank_id": b.get("BankId")})

    async def bank_fields(b: dict) -> dict:
        # Merit's docs say a bank payment without an IBAN goes to the cash register, but with a BankId it is
        # booked on that account (seen live 2026-11-21, a bank with no IBAN). The IBAN goes along only when no
        # other account has it: LHV's EUR and USD accounts share one, and Merit matches payment methods by IBAN.
        fields = {"BankId": b["BankId"]}
        iban = _iban(b.get("IBANCode"))
        if iban and iban != "-":
            banks = await merit.call("v2/banks", None, method="GET") or []
            if sum(_iban(x.get("IBANCode")) == iban for x in banks) == 1:
                fields["IBAN"] = iban
        return fields

    def same_currency(bank_row: dict, currency: str) -> None:
        bank_cur = (bank_row.get("CurrencyCode") or "").upper()
        if bank_cur and bank_cur != currency:
            raise ToolError(f"{bank_row.get('Name')} is a {bank_cur} account, the payment is in {currency}")

    # ------------------------------------------------------------ the document being paid

    async def sales_doc(invoice_id: str | None, invoice_no: str | None) -> dict:
        if not invoice_id:
            if not invoice_no:
                raise ToolError("give document_id (SIHId) or invoice_no")
            found = await merit.call("v2/getinvoices2", {"InvNo": invoice_no})
            found = [found] if isinstance(found, dict) else (found or [])
            found = [f for f in found if (f.get("InvoiceNo") or "").strip() == invoice_no.strip()]
            if len(found) != 1:
                raise ToolError(f"{'no' if not found else 'more than one'} sales invoice number {invoice_no!r}; "
                                "pass document_id (SIHId from merit_list_sales_invoices)")
            invoice_id = found[0]["SIHId"]
        inv = await books.sales_invoice(_guid(invoice_id, "document_id"))
        h = (inv or {}).get("Header") or {}
        if not h:
            raise ToolError(f"no sales invoice {invoice_id}")
        paid = h.get("PaidAmount")
        if paid is None:
            paid = sum(_dec(p.get("Amount")) for p in inv.get("Payments") or [])
        return {"id": invoice_id, "what": "sales invoice", "number": h.get("InvoiceNo"), "party": h.get("CustomerName"),
                "party_label": "customer", "date": h.get("DocumentDate"), "due": h.get("DueDate"),
                "currency": (h.get("CurrencyCode") or LOCAL_CURRENCY).upper(), "total": _dec(h.get("TotalSum")),
                "paid": _dec(paid), "is_paid": bool(h.get("Paid")), "ref_no": h.get("ReferenceNo"),
                "changed": h.get("ChangedDate")}

    async def purchase_doc(invoice_id: str | None) -> dict:
        if not invoice_id:
            raise ToolError("give document_id: the purchase invoice's PIHId (merit_list_purchase_invoices); bill "
                            "numbers are not unique across vendors")
        inv = await books.purchase_invoice(_guid(invoice_id, "document_id"))
        h = (inv or {}).get("Header") or {}
        if not h:
            raise ToolError(f"no purchase invoice {invoice_id}")
        paid = h.get("PaidAmount")
        if paid is None:
            paid = sum(_dec(p.get("Amount")) for p in inv.get("Payments") or [])
        return {"id": invoice_id, "what": "purchase invoice", "number": h.get("BillNo"), "party": h.get("VendorName"),
                "party_label": "vendor", "date": h.get("DocumentDate"), "due": h.get("DueDate"),
                "currency": (h.get("CurrencyCode") or LOCAL_CURRENCY).upper(), "total": _dec(h.get("TotalSum")),
                "paid": _dec(paid), "is_paid": bool(h.get("Paid")), "ref_no": h.get("ReferenceNo"),
                "changed": h.get("ChangedDate")}

    async def offer_doc(offer_id: str | None) -> dict:
        if not offer_id:
            raise ToolError("give document_id: the sales offer's Id (SIHId in Merit's offer list)")
        offer_id = _guid(offer_id, "document_id")
        o = await merit.call("v2/getoffer", {"Id": offer_id})
        if not isinstance(o, dict) or not o.get("OfferNo"):
            raise ToolError(f"no sales offer {offer_id}")
        day = _date(o.get("DocDate"))
        if day is None:
            raise ToolError(f"cannot read the date of offer {o.get('OfferNo')}: {o.get('DocDate')!r}")
        # getoffer has no paid amount; Merit's offer list does, so find the offer there on its own date.
        rows = await merit.call("v2/getoffers", {"PeriodStart": _ymd(day), "PeriodEnd": _ymd(day), "DateType": 0,
                                                 "UnPaid": False})
        rows = [r for r in rows or [] if (r.get("SIHId") or "").lower() == offer_id.lower()]
        if not rows:
            raise ToolError(f"offer {o.get('OfferNo')} is not in Merit's offer list for {day}, so how much of it is "
                            "paid is unknown; record this payment in Merit")
        r = rows[0]
        status = r.get("DocStatus")
        if status in (4, 7):
            raise ToolError(f"offer {r.get('OfferNo')} is {OFFER_STATUS[status]}; not recording a payment for it")
        if status == 6:
            raise ToolError(f"an invoice was already made from offer {r.get('OfferNo')}; record the payment on that "
                            "sales invoice instead (kind=sales_invoice)")
        return {"id": offer_id, "what": {1: "quote", 2: "sales order", 3: "prepayment invoice"}.get(r.get("DocType"), "sales offer"),
                "number": r.get("OfferNo"), "party": r.get("CustomerName") or o.get("CustomerName"),
                "party_label": "customer", "date": r.get("DocumentDate"), "due": r.get("DueDate"),
                "currency": (r.get("CurrencyCode") or LOCAL_CURRENCY).upper(), "total": _dec(r.get("TotalSum")),
                "paid": _dec(r.get("PaidAmount")), "is_paid": bool(r.get("Paid")),
                "ref_no": r.get("ReferenceNo") or o.get("RefNo"), "changed": r.get("ChangedDate"),
                "status": OFFER_STATUS.get(status, status)}

    async def document(kind: str, document_id: str | None, invoice_no: str | None) -> dict:
        if invoice_no and kind != "sales_invoice":
            raise ToolError("invoice_no only works for sales invoices; pass document_id")
        if kind == "sales_invoice":
            return await sales_doc(document_id, invoice_no)
        if kind == "purchase_invoice":
            return await purchase_doc(document_id)
        return await offer_doc(document_id)

    def doc_brief(d: dict) -> dict:
        return compact({d["party_label"]: d["party"], "document": d["what"], "number": d["number"], "date": d.get("date"),
                        "due_date": d.get("due"), "status": d.get("status"), "currency": d["currency"],
                        "total": float(d["total"]), "paid_so_far": float(d["paid"]),
                        "unpaid": float(d["total"] - d["paid"]), "reference_no": d.get("ref_no")})

    # ------------------------------------------------------------ payments of invoices and offers

    @mcp.tool(annotations=WRITE)
    async def merit_record_payment(
        kind: Annotated[Literal["sales_invoice", "purchase_invoice", "sales_offer"],
                        Field(description="sales_invoice: money received from a customer; purchase_invoice: money paid "
                                          "to a vendor; sales_offer: a customer's payment of a quote, order or "
                                          "prepayment invoice")],
        bank: Bank,
        amount: Annotated[float, Field(gt=0, description="Amount paid, in the payment currency")],
        document_id: Annotated[str | None, Field(description="SIHId (sales invoice), PIHId (purchase invoice) or the "
                                                             "offer's Id")] = None,
        invoice_no: Annotated[str | None, Field(description="Sales invoice number, instead of document_id "
                                                            "(sales_invoice only)")] = None,
        payment_date: PaymentDate = None,
        currency: Annotated[str | None, Field(description="Default: the document's currency")] = None,
        currency_rate: Annotated[float | None, Field(gt=0, description="Rate to EUR; default: the central bank's rate "
                                                                       "of the payment date")] = None,
        allow_overpayment: Annotated[bool, Field(description="Only when the user explicitly confirmed paying more than "
                                                             "the unpaid amount")] = False,
        allow_currency_mismatch: Annotated[bool, Field(description="Only when the user explicitly confirmed paying in "
                                                                   "another currency than the document's or the bank "
                                                                   "account's")] = False,
        dry_run: DryRun = False,
    ) -> dict:
        """Record in Merit that a sales invoice, purchase invoice or sales offer was paid (whole or in
        part) through a bank account. Books a payment entry; moves no money. Refuses documents already
        paid, more than the unpaid amount and other currencies unless explicitly allowed. Undo with
        merit_delete_payment. dry_run=true returns the preview only."""
        day = _when(payment_date)
        doc = await document(kind, document_id, invoice_no)
        acct = await find_bank(bank)
        pay_cur = (currency or doc["currency"]).strip().upper()
        unpaid = doc["total"] - doc["paid"]
        amt = _money(amount)
        notes: list[str] = []
        if doc["total"] <= 0:
            raise ToolError(f"{doc['what']} {doc['number']} has total {doc['total']}: a credit invoice or empty document "
                            "is not paid this way (merit_record_settlement offsets credit invoices)")
        if doc["is_paid"] or unpaid <= CENT:
            raise ToolError(f"{doc['what']} {doc['number']} ({doc['party']}) is already paid "
                            f"({doc['paid']} of {doc['total']} {doc['currency']})")
        mismatch = []
        if pay_cur != doc["currency"]:
            mismatch.append(f"the payment is in {pay_cur} but {doc['what']} {doc['number']} is in {doc['currency']}")
        bank_cur = (acct.get("CurrencyCode") or "").upper()
        if bank_cur and bank_cur != pay_cur:
            mismatch.append(f"{acct.get('Name')} is a {bank_cur} account but the payment is in {pay_cur}")
        if mismatch and not allow_currency_mismatch:
            raise ToolError("; ".join(mismatch) + ". Check with the user; only if they confirm, call again with "
                            "allow_currency_mismatch=true")
        notes += [m + " (allowed)" for m in mismatch]
        if pay_cur == doc["currency"]:
            if amt > unpaid + CENT:
                if not allow_overpayment:
                    raise ToolError(f"{amt} {pay_cur} is more than the unpaid {_money(unpaid)} of {doc['what']} "
                                    f"{doc['number']}. Check with the user; only if they confirm the overpayment, call "
                                    "again with allow_overpayment=true")
                notes.append(f"overpayment of {_money(amt - unpaid)} {pay_cur} (allowed)")
            elif amt < unpaid - CENT:
                notes.append(f"part payment: {_money(unpaid - amt)} {pay_cur} stays unpaid")
        else:
            notes.append("the amount cannot be compared with the unpaid amount in another currency")
        if _date(doc.get("date")) and day < _date(doc["date"]):
            notes.append(f"the payment date is before the document date {doc['date'][:10]}")
        payload: dict = await bank_fields(acct)
        if kind == "purchase_invoice":
            payload.update({"VendorName": doc["party"], "BillNo": doc["number"]})
        elif kind == "sales_offer":
            payload.update({"CustomerName": doc["party"], "OfferNo": doc["number"]})
        else:
            payload.update({"CustomerName": doc["party"], "InvoiceNo": doc["number"]})
        if doc.get("ref_no"):
            payload["RefNo"] = doc["ref_no"]
        # The docs write PaymentDate as YYYYmmddHHii, but their examples of the same field elsewhere
        # (PaymDate) use 14 digits, as do Merit's other dates; send yyyyMMdd000000 like sendinvoice.
        payload.update({"PaymentDate": _dt(day), "Amount": float(amt), "CurrencyCode": pay_cur})
        if currency_rate:
            payload["CurrencyRate"] = currency_rate
        preview = {"record": f"payment {'received from' if doc['party_label'] == 'customer' else 'made to'} "
                             f"{doc['party']}", "paying": doc_brief(doc), "bank_account": bank_brief(acct),
                   "payment_date": day.isoformat(), "amount": float(amt), "currency": pay_cur,
                   "currency_rate": currency_rate, "notes": notes}
        state = {"payload": payload, "doc": doc["id"], "paid": str(doc["paid"]), "changed": doc.get("changed")}
        if (gate := _gate(f"record_payment_{kind}", state, dry_run, preview)) is not None:
            return gate
        path = {"sales_invoice": "v2/sendpayment", "purchase_invoice": "v2/sendPaymentV",
                "sales_offer": "v2/sendPaymentO"}[kind]
        res = await merit.call(path, payload, text_ok=True)
        # The docs do not say what these endpoints answer, so check the document's paid amount instead.
        after = await document(kind, doc["id"], None)
        if after["paid"] <= doc["paid"]:
            raise ToolError(f"Merit answered {str(res)[:300]!r}, but the paid amount of {doc['what']} {doc['number']} "
                            f"is still {after['paid']}. Do not try again before checking merit_list_payments for "
                            f"{day}: the payment may have been booked without being linked.")
        out = {"written": True, "paid": doc_brief(after), "merit_answer": compact(res) if res else None}
        if pay_cur == doc["currency"] and abs(after["paid"] - doc["paid"] - amt) > CENT:
            out["warning"] = (f"the paid amount went from {doc['paid']} to {after['paid']}, not up by {amt}; "
                              "check the payment in Merit")
        return out

    # ------------------------------------------------------------ delete

    @mcp.tool(annotations=WRITE)
    async def merit_delete_payment(
        payment_id: Annotated[str, Field(description="The payment's id (PHId in merit_list_payments)")],
        payment_date: Annotated[date, Field(description="The payment's date as merit_list_payments shows it, "
                                                        "YYYY-MM-DD (it is looked up within a week of this date)")],
        dry_run: DryRun = False,
    ) -> dict:
        """Delete a payment in Merit: its ledger entry goes, and the invoices it paid become unpaid
        again by that amount. Cannot be undone (record the payment again if needed). dry_run=true
        shows the payment and what it paid."""
        payment_id = _guid(payment_id, "payment_id")
        start, end = payment_date - timedelta(days=7), payment_date + timedelta(days=7)

        async def find() -> dict | None:
            rows = await merit.call_period("v2/getpayments", start, end, {"DateType": 0})
            # The docs name the id PIHId; the read tool sees PHId. Accept either.
            hits = [r for r in rows if (r.get("PHId") or r.get("PIHId") or "").lower() == payment_id.lower()]
            return hits[0] if hits else None

        row = await find()
        if row is None:
            raise ToolError(f"no payment {payment_id} dated {start} to {end} (merit_list_payments shows each payment's "
                            "PHId and DocumentDate)")
        details = row.get("PaymAPIDetails") or row.get("Details") or []
        details = [details] if isinstance(details, dict) else details
        paid_docs = [compact({"document_no": d.get("DocNo"), "document_amount": d.get("DocAmount"),
                              "paid_by_this_payment": d.get("PaidAmount"), "currency": d.get("CurrencyCode")})
                     for d in details if isinstance(d, dict)]
        brief = compact({"date": row.get("DocumentDate"), "number": row.get("DocumentNo"), "bank": row.get("BankName"),
                         "direction": PAYMENT_DIRECTION.get(row.get("Direction"), row.get("Direction")),
                         "counterparty": row.get("CounterPartName"), "currency": row.get("CurrencyCode"),
                         "amount": row.get("Amount"), "paid_documents": paid_docs})
        preview = {"delete": brief, "effect": "the documents listed become unpaid again by these amounts" if paid_docs
                   else "the payment's ledger entry is removed"}
        state = {"id": payment_id, "changed": row.get("ChangedDate"), "amount": row.get("Amount"),
                 "date": row.get("DocumentDate"), "details": paid_docs}
        if (gate := _gate("delete_payment", state, dry_run, preview)) is not None:
            return gate
        res = await merit.call(f"v2/deletepayment/{payment_id}", {}, text_ok=True)
        if await find() is not None:
            raise ToolError(f"Merit answered {str(res)[:300]!r}, but the payment is still there; nothing was deleted")
        return {"written": True, "deleted_payment": brief}

    # ------------------------------------------------------------ other income and expenses, prepayments

    @mcp.tool(annotations=WRITE)
    async def merit_record_bank_transaction(
        direction: Annotated[Literal["income", "expense"],
                             Field(description="income: money into the account; expense: money out of it")],
        bank: Bank,
        lines: Annotated[list[BankLine], Field(description="Where the money comes from or goes to, by ledger account")],
        payment_date: PaymentDate = None,
        currency: Annotated[str | None, Field(description="Default: the bank account's currency")] = None,
        currency_rate: Annotated[float | None, Field(gt=0, description="Rate to EUR; default: the central bank's")] = None,
        document_number: Annotated[str | None, Field(max_length=35, description="E.g. the bank's transaction reference")] = None,
        description: Annotated[str | None, Field(max_length=200)] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Record a bank payment that is not for an invoice, against ledger accounts: bank fees,
        interest, loan payments, taxes paid, transfers between own accounts. Lines carry no VAT: a
        cost with VAT is a purchase invoice (merit_create_purchase_invoice) paid with
        merit_record_payment. Books a ledger entry; moves no money. dry_run=true returns the preview only."""
        day = _when(payment_date)
        if not lines:
            raise ToolError("at least one line is needed")
        acct = await find_bank(bank)
        cur = (currency or acct.get("CurrencyCode") or LOCAL_CURRENCY).strip().upper()
        same_currency(acct, cur)
        accounts = {a.get("Code"): a for a in await merit.call("v2/accounts", None, method="GET") or []}
        rows, summary, total = [], [], Decimal(0)
        for l in lines:
            a = accounts.get(l.account.strip())
            if a is None:
                raise ToolError(f"no account {l.account!r} in Merit (merit_reference_list kind=accounts)")
            if a.get("NonActive"):
                raise ToolError(f"account {l.account} {a.get('Name')} is not active")
            if acct.get("AccountCode") and l.account.strip() == acct["AccountCode"]:
                raise ToolError(f"account {l.account} is {acct.get('Name')}'s own account")
            amt = _money(l.amount)
            total += amt
            row: dict = {"AccountCode": l.account.strip(), "Quantity": 1, "Price": float(amt), "Amount": float(amt)}
            if l.description:
                row["Description"] = l.description
            rows.append(row)
            summary.append(compact({"account": f"{l.account.strip()} {a.get('Name') or ''}".strip(),
                                    "amount": float(amt), "description": l.description}))
        # DocumentDate is yyyy-MM-dd on these v2/Banks endpoints, unlike Merit's other dates.
        payload: dict = {"DocumentDate": day.isoformat(), "CurrencyCode": cur, "Lines": rows}
        if currency_rate:
            payload["CurrencyRate"] = currency_rate
        if document_number:
            payload["DocumentNumber"] = document_number
        if description:
            payload["Description"] = description
        preview = {"record": f"other {direction} {'into' if direction == 'income' else 'out of'} {acct.get('Name')}",
                   "bank_account": bank_brief(acct), "date": day.isoformat(), "currency": cur, "lines": summary,
                   "total": float(total), "document_number": document_number, "description": description,
                   "currency_rate": currency_rate}
        state = {"bank": acct["BankId"], "payload": payload}
        if (gate := _gate(f"record_other_{direction}", state, dry_run, preview)) is not None:
            return gate
        path = f"v2/Banks/{acct['BankId']}/{'IncomePayments' if direction == 'income' else 'ExpensePayments'}"
        res = await merit.call(path, payload)
        if not isinstance(res, dict) or not res.get("BatchId"):
            raise ToolError(f"Merit did not confirm the payment: {str(res)[:300]}")
        return {"written": True, "batch_id": res["BatchId"], "batch": res.get("BatchInfo"), "total": float(total),
                "currency": cur}

    @mcp.tool(annotations=WRITE)
    async def merit_record_prepayment(
        party: Annotated[Literal["customer", "vendor"],
                         Field(description="customer: a prepayment received; vendor: a prepayment made")],
        party_id: Annotated[str, Field(description="CustomerId (merit_find_customers) or VendorId (merit_find_vendors)")],
        bank: Bank,
        amount: Annotated[float, Field(gt=0, description="Amount, positive, in the payment currency")],
        payment_date: PaymentDate = None,
        currency: Annotated[str | None, Field(description="Default: the bank account's currency")] = None,
        document_number: Annotated[str | None, Field(max_length=35)] = None,
        description: Annotated[str | None, Field(max_length=200)] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Record a prepayment with no invoice yet: received from a customer or paid to a vendor.
        It stays as a prepayment on the party until offset against an invoice (merit_record_settlement).
        A payment of a sales offer or prepayment invoice is merit_record_payment kind=sales_offer.
        Books a ledger entry; moves no money. dry_run=true returns the preview only."""
        day = _when(payment_date)
        party_id = _guid(party_id, "party_id")
        card = await (books.customer(party_id) if party == "customer" else books.vendor(party_id))
        acct = await find_bank(bank)
        cur = (currency or acct.get("CurrencyCode") or LOCAL_CURRENCY).strip().upper()
        same_currency(acct, cur)
        amt = _money(amount)
        payload: dict = {"DocumentDate": day.isoformat(), "CurrencyCode": cur, "Amount": float(amt)}
        if document_number:
            payload["DocumentNumber"] = document_number
        if description:
            payload["Description"] = description
        preview = {"record": f"prepayment {'received from' if party == 'customer' else 'paid to'} {card.get('Name')}",
                   party: compact({"name": card.get("Name"), "reg_no": card.get("RegNo"), "id": party_id}),
                   "bank_account": bank_brief(acct), "date": day.isoformat(), "amount": float(amt), "currency": cur,
                   "document_number": document_number, "description": description}
        state = {"bank": acct["BankId"], "party": party, "id": party_id, "payload": payload}
        if (gate := _gate("record_prepayment", state, dry_run, preview)) is not None:
            return gate
        path = f"v2/Banks/{acct['BankId']}/PrePayments/{'ForCustomer' if party == 'customer' else 'ForVendor'}/{party_id}"
        res = await merit.call(path, payload)
        if not isinstance(res, dict) or not res.get("BatchId"):
            raise ToolError(f"Merit did not confirm the prepayment: {str(res)[:300]}")
        return {"written": True, "batch_id": res["BatchId"], "batch": res.get("BatchInfo"), party: card.get("Name"),
                "amount": float(amt), "currency": cur}

    # ------------------------------------------------------------ settlement

    @mcp.tool(annotations=WRITE)
    async def merit_record_settlement(
        customer_lines: Annotated[list[SettlementLine] | None, Field(description="Sales and credit invoices to offset")] = None,
        vendor_lines: Annotated[list[SettlementLine] | None, Field(description="Purchase invoices to offset")] = None,
        doc_date: Annotated[date | None, Field(description="Date of the settlement; default today")] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Offset documents against each other without money moving (tasaarveldus): a sales invoice
        against a credit invoice, or what a company owes us against what we owe it. The customer
        lines must add up to the vendor lines (a credit invoice counts negative). All documents must
        be in one currency. dry_run=true shows every document."""
        day = _when(doc_date)
        customer_lines, vendor_lines = customer_lines or [], vendor_lines or []
        if len(customer_lines) + len(vendor_lines) < 2:
            raise ToolError("a settlement needs at least two documents")
        ids = [l.invoice_id.strip().lower() for l in customer_lines + vendor_lines]
        if len(set(ids)) != len(ids):
            raise ToolError("the same document is listed twice")
        currencies, parties, shown, snapshot = set(), set(), [], []
        sides: dict[str, list[dict]] = {"CustLines": [], "VendLines": []}
        sums = {"CustLines": Decimal(0), "VendLines": Decimal(0)}
        for side, lines in (("CustLines", customer_lines), ("VendLines", vendor_lines)):
            for l in lines:
                invoice_id = _guid(l.invoice_id, "invoice_id")
                if side == "CustLines":
                    h = ((await books.sales_invoice(invoice_id)) or {}).get("Header") or {}
                    who = (h.get("CustomerId"), h.get("CustomerName"), h.get("CustomerRegNo"), h.get("InvoiceNo"))
                    what = "sales invoice"
                else:
                    h = ((await books.purchase_invoice(invoice_id)) or {}).get("Header") or {}
                    who = (h.get("VendorId"), h.get("VendorName"), h.get("VendorRegNo"), h.get("BillNo"))
                    what = "purchase invoice"
                if not h:
                    raise ToolError(f"no {what} {invoice_id}")
                party_id, name, reg_no, number = who
                total, paid = _dec(h.get("TotalSum")), _dec(h.get("PaidAmount"))
                unpaid, amt = total - paid, _money(l.amount)
                if total < 0:
                    what = "credit " + what
                if h.get("Paid") or abs(unpaid) <= CENT:
                    raise ToolError(f"{what} {number} ({name}) has nothing unpaid")
                if amt == 0 or (amt > 0) != (unpaid > 0):
                    raise ToolError(f"{what} {number} ({name}) has {_money(unpaid)} unpaid, so its amount must be "
                                    f"{'positive' if unpaid > 0 else 'negative'}, not {amt}")
                if abs(amt) > abs(unpaid) + CENT:
                    raise ToolError(f"{amt} is more than the {_money(unpaid)} unpaid on {what} {number} ({name})")
                currencies.add((h.get("CurrencyCode") or LOCAL_CURRENCY).upper())
                parties.add(name)
                line = {"CustVendName": name, "CustVendId": party_id, "DocNo": number, "Amount": float(amt)}
                if reg_no:
                    line["CustVendRegNo"] = reg_no
                sides[side].append(line)
                sums[side] += amt
                shown.append(compact({"side": "customer" if side == "CustLines" else "vendor", "party": name,
                                      "document": what, "number": number, "date": h.get("DocumentDate"),
                                      "currency": h.get("CurrencyCode"), "total": float(total),
                                      "unpaid": float(_money(unpaid)), "offset": float(amt)}))
                snapshot.append([invoice_id, str(paid), h.get("ChangedDate")])
        if len(currencies) > 1:
            raise ToolError(f"the documents are in different currencies ({', '.join(sorted(currencies))}); a settlement "
                            "is in one currency")
        # Merit: "The total amount of the lines must equal zero", yet its own example offsets a sales
        # invoice of 6.00 against a purchase invoice of 6.00, both positive. Read as: customer lines
        # (credit invoices negative) add up to vendor lines.
        if sums["CustLines"] != sums["VendLines"]:
            raise ToolError(f"customer lines add up to {sums['CustLines']} and vendor lines to {sums['VendLines']}; "
                            "they must be equal")
        cur = currencies.pop()
        payload = {"DocDate": _ymd(day), "CurrencyCode": cur, "CustLines": sides["CustLines"],
                   "VendLines": sides["VendLines"]}
        preview = {"settlement_date": day.isoformat(), "currency": cur, "documents": shown,
                   "offset_total": float(max(sums["CustLines"], sums["VendLines"], key=abs)),
                   "notes": [f"documents of different parties: {', '.join(sorted(p for p in parties if p))}"]
                   if len(parties) > 1 else None}
        if (gate := _gate("record_settlement", {"payload": payload, "docs": snapshot}, dry_run, preview)) is not None:
            return gate
        res = await merit.call("v2/sendsettlement", payload)
        if not isinstance(res, dict) or not res.get("DocumentId"):
            raise ToolError(f"Merit did not confirm the settlement: {str(res)[:300]}")
        return {"written": True, "settlement_id": res["DocumentId"], "settlement_no": res.get("DocumentNo"),
                "documents": shown}

    # ------------------------------------------------------------ bank statement import

    def read_camt(name: str, data: bytes) -> tuple[str, str, list[dict]]:
        """The statement as text, its CAMT version and per-account summaries; refuses anything else."""
        if len(data) > MAX_STATEMENT_BYTES:
            raise ToolError(f"{name} is {len(data):,} bytes; a bank statement over {MAX_STATEMENT_BYTES:,} is not expected")
        data = data.removeprefix(b"\xef\xbb\xbf")
        if not data.lstrip().startswith(b"<"):
            raise ToolError(f"{name} is not XML; a CAMT.053 bank statement (XML) is needed")
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            raise ToolError(f"{name} declares a DOCTYPE or entities, which a CAMT.053 statement never does")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{name} is not UTF-8; export the statement as UTF-8 CAMT.053") from None
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            raise ToolError(f"{name} is not valid XML: {e}") from None
        ns = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
        version = ns.rsplit(":", 1)[-1]
        if not version.startswith("camt.053"):
            raise ToolError(f"{name} is not a CAMT.053 bank statement (its format is {version or root.tag!r})")
        if version not in CAMT_VERSIONS:
            raise ToolError(f"{name} is {version}; Merit imports only {', '.join(CAMT_VERSIONS)}")
        q = lambda path: "/".join(f"{{{ns}}}{p}" for p in path.split("/"))  # noqa: E731
        statements = []
        for st in root.iter(f"{{{ns}}}Stmt"):
            entries = st.findall(q("Ntry"))
            credits, debits, days = Decimal(0), Decimal(0), []
            for n in entries:
                amt = _dec((n.findtext(q("Amt")) or "0").strip())
                if (n.findtext(q("CdtDbtInd")) or "").strip() == "DBIT":
                    debits += amt
                else:
                    credits += amt
                d = _date(n.findtext(q("BookgDt/Dt")) or n.findtext(q("BookgDt/DtTm")))
                if d:
                    days.append(d)
            start = _date(st.findtext(q("FrToDt/FrDtTm"))) or (min(days) if days else None)
            end = _date(st.findtext(q("FrToDt/ToDtTm"))) or (max(days) if days else None)
            statements.append({"iban": _iban(st.findtext(q("Acct/Id/IBAN"))),
                               "currency": (st.findtext(q("Acct/Ccy")) or "").strip() or None,
                               "start": start, "end": end, "entries": len(entries),
                               "credits": float(credits), "debits": float(debits)})
        if not statements:
            raise ToolError(f"{name} has no statement (Stmt) in it")
        if not sum(s["entries"] for s in statements):
            raise ToolError(f"{name} has no entries to import")
        return text, version, statements

    @mcp.tool(annotations=WRITE)
    async def merit_import_bank_statement(
        upload_id: Annotated[str | None, Field(description="The CAMT.053 XML file. Server mode: upload id from create_upload_link")] = None,
        file_path: Annotated[str | None, Field(description="The CAMT.053 XML file. On your own computer: the file's path")] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Import a bank statement (CAMT.053 XML, as banks export it) into Merit: Merit creates a
        bank payment for each entry and matches what it can to invoices. Every account in the file
        must be a bank account in Merit. Importing the same statement twice can duplicate payments,
        so the preview shows how many payments Merit already has for that account and period.
        dry_run=true returns the preview only."""
        name, data = files.get(upload_id, file_path, (".xml",))
        text, version, statements = read_camt(name, data)
        banks = await merit.call("v2/banks", None, method="GET") or []
        shown, existing = [], {}
        for s in statements:
            hits = [b for b in banks if s["iban"] and _iban(b.get("IBANCode")) == s["iban"]]
            if len(hits) > 1 and s["currency"]:
                # One IBAN can hold several currencies, each its own account in Merit (e.g. LHV EUR and USD).
                hits = [b for b in hits if (b.get("CurrencyCode") or "").upper() == s["currency"].upper()]
            if len(hits) != 1:
                raise ToolError(f"{'no' if not hits else 'more than one'} bank account in Merit has IBAN "
                                f"{s['iban'] or '(none in the file)'}"
                                + (f" and currency {s['currency']}" if s["currency"] else "")
                                + ("; add it in Merit first" if not hits else ""))
            b = hits[0]
            already = None
            if s["start"] and s["end"]:
                try:
                    rows = await merit.call_period("v2/getpayments", s["start"], s["end"],
                                                   {"DateType": 0, "BankId": b["BankId"]})
                    already = len(rows)
                except ToolError:  # a period Merit cannot list in one go; the warning is only a help
                    already = None
            existing[f"{s['iban']} {s['currency'] or ''}".strip()] = already
            shown.append(compact({"account": b.get("Name"), "iban": s["iban"], "currency": s["currency"],
                                  "period": f"{s['start']} to {s['end']}" if s["start"] else None,
                                  "entries": s["entries"], "money_in": s["credits"], "money_out": s["debits"],
                                  "payments_already_in_merit_for_this_period": already}))
        notes = [f"Merit already has {n} payments on {i} in this period: if this statement (or part of it) was "
                 "imported before, its lines may be booked twice" for i, n in existing.items() if n]
        if any(sum(_iban(b.get("IBANCode")) == s["iban"] for b in banks) > 1 for s in statements):
            notes.append("This IBAN belongs to several accounts in Merit (one per currency). The account shown is "
                         "the one for the statement's currency, but Merit picks the account itself on import: "
                         "check afterwards that the payments are on it.")
        preview = {"file": f"{name} ({len(data):,} bytes, {version})", "statements": shown, "notes": notes}
        if (gate := _gate("import_bank_statement", {"xml": text, "existing": existing}, dry_run, preview)) is not None:
            return gate
        # The docs show the payload as the XML in quotes, read here as a JSON string holding the XML
        # (Merit.call JSON-encodes the str); they do not say whether a raw XML body works instead.
        res = await merit.call("v2/sendcamt53", text, text_ok=True)
        answer = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
        m = re.search(r"Imporditi\s+(\d+)", answer)  # "Imporditi X makserida (ridu kokku X )."
        if not m:
            raise ToolError(f"Merit did not confirm the import: {answer[:300]}")
        out: dict = {"written": True, "imported_payment_rows": int(m.group(1)), "merit_answer": answer.strip()}
        total = re.search(r"ridu kokku\s+(\d+)", answer)
        if total and int(total.group(1)) > int(m.group(1)):
            out["note"] = (f"Merit imported {m.group(1)} of {total.group(1)} rows; the rest were probably imported "
                           "before or could not be read. Check merit_list_payments.")
        return out
