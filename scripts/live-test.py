"""Run every write tool against YOUR Merit TEST company, through the real server code.

    export MERIT_API_ID=... MERIT_API_KEY=... MERIT_LIVE_TEST_BANK="Test Bank"
    uv run python scripts/live-test.py --yes-this-is-a-test-company

Merit has no sandbox: make a separate company in Merit for testing (Merit's FAQ: "Just create a
test company"), give it a PRO or Premium licence and an API key, and add a bank account whose name
exists in no real company of yours; put that name in MERIT_LIVE_TEST_BANK. The script stops unless
that bank account exists, so it cannot run against a company that lacks it.

Every write tool is called first with dry_run=true (which must write nothing) and then for real.
This creates customers, vendors, items, invoices, payments, offers, ledger entries and more in the
test company, named with a run id so runs do not collide. Nothing is sent by email or e-invoice.
Prints one line per step and a summary; exit code 1 if any step failed.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import traceback
from datetime import date
from pathlib import Path

from fastmcp import Client

from merit_aktiva_mcp import config, main as server
from merit_aktiva_mcp.merit import Merit

TEST_BANK = os.environ.get("MERIT_LIVE_TEST_BANK", "").strip()
RUN = time.strftime("%m%d%H%M")
TODAY = date.today().isoformat()
TMP = Path(tempfile.mkdtemp(prefix="merit-live-test-"))

results: list[tuple[str, bool, str]] = []


def main() -> None:
    if "--yes-this-is-a-test-company" not in sys.argv[1:] or not TEST_BANK:
        sys.exit(__doc__)
    asyncio.run(run())
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} steps passed")
    for name, _, msg in failed:
        print(f"  FAILED {name}: {msg[:400]}")
    sys.exit(1 if failed else 0)


def save(name: str, data: bytes) -> str:
    path = TMP / name
    path.write_bytes(data)
    return str(path)


async def run() -> None:
    cfg = config.load("stdio")
    m = Merit(cfg.base_url, cfg.api_id, cfg.api_key)
    banks = await m.call("v2/banks", None, method="GET") or []
    if not any((b.get("Name") or "").strip() == TEST_BANK for b in banks):
        sys.exit(f"STOP: no bank account {TEST_BANK!r} in this company, so it is not the test company")
    import dataclasses
    mcp = server.build(dataclasses.replace(cfg, allow_write=True))

    async with Client(mcp) as c:
        async def read(name, tool, args):
            return await call(name, tool, args, write=False)

        async def write(name, tool, args):
            return await call(name, tool, args, write=True)

        async def refused(name, tool, args, expect, approve=False):
            """A refusal the tool must give: before writing anything (checked with a dry run), or with
            approve=True, when the call reaches Merit (a known Merit limitation the tool explains)."""
            try:
                await c.call_tool(tool, args if approve else {**args, "dry_run": True})
                results.append((name, False, "was not refused"))
                print(f"FAIL {name}: was not refused")
            except Exception as e:
                ok = expect in str(e)
                results.append((name, ok, str(e)))
                print(f"{'ok  ' if ok else 'FAIL'} {name} (refused): {str(e)[:200]}")

        async def call(name, tool, args, write):
            try:
                if write:
                    dry = (await c.call_tool(tool, {**args, "dry_run": True})).structured_content
                    if not dry or dry.get("written") is not False or not dry.get("dry_run"):
                        raise RuntimeError(f"the dry run did not stop at a preview: {dry}")
                r = await c.call_tool(tool, args)
                out = r.structured_content
                if write:
                    if not out or not out.get("written"):
                        raise RuntimeError(f"not written: {out}")
                if out is None:  # list or text results
                    out = {"content": [getattr(x, "text", "") for x in r.content]}
                text = json.dumps(out, ensure_ascii=False, default=str)
                results.append((name, True, text))
                print(f"ok   {name}: {text[:300]}")
                return out
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                results.append((name, False, msg))
                print(f"FAIL {name}: {msg[:600]}")
                if not hasattr(e, "args") or "ToolError" not in type(e).__name__:
                    traceback.print_exc(limit=2)
                return None

        # ------------------------------------------------ cards
        cust = await write("create customer", "merit_create_customer", {"customer": {
            "name": f"Test Klient {RUN}", "reg_no": f"7{RUN}", "email": "test@example.com", "payment_days": 7,
            "address": "Test 1", "city": "Tallinn"}})
        cid = (cust or {}).get("customer_id")
        await write("update customer", "merit_update_customer", {"customer_id": cid, "changes": {
            "phone": "5550000", "ref_no_base": "77", "einvoice_operator": 1}})
        vend = await write("create vendor", "merit_create_vendor", {"vendor": {
            "name": f"Test Tarnija {RUN}", "reg_no": f"8{RUN}", "vat_accountable": True,
            "bank_account": "EE382200221020145685", "receiver_name": "Saaja Test"}})
        vid = (vend or {}).get("vendor_id")
        await write("update vendor", "merit_update_vendor", {"vendor_id": vid, "changes": {
            "phone": "5551111", "ref_no": "1234561", "payer_name": "Saaja Uus"}})

        # ------------------------------------------------ master data
        await write("create unit", "merit_create_unit", {"name": f"tu{RUN[-4:]}"})
        for kind in ("item", "customer", "vendor"):
            await write(f"create {kind} group", "merit_create_group",
                        {"kind": kind, "code": f"G{kind[0].upper()}{RUN}", "name": f"Test {kind} group {RUN}"})
        svc, stock = f"t{RUN}", f"s{RUN}"  # lowercase: Merit's sendprices cannot find codes with capitals
        await write("create items", "merit_create_items", {"items": [
            {"code": svc, "description": f"Test teenus {RUN}", "type": "service", "vat": "24", "unit": "kuu",
             "sales_account": "3000", "sales_price": 50},
            {"code": stock, "description": f"Test kaup {RUN}", "type": "stock", "unit": "kuu", "vat": "24"}]})
        await refused("item with unit tk", "merit_create_items", {"items": [
            {"code": f"X{RUN}", "description": f"X {RUN}", "unit": "tk"}]}, "Merit fails to create an item")
        await write("update item", "merit_update_item", {"item_code": svc, "changes": {"sales_price": 60}})
        dim = await write("create dimension", "merit_create_dimension", {"name": f"Projekt {RUN}"})
        print(f"     (senddimensions answered {json.dumps((dim or {}).get('merit_answer'))[:200]})")
        dim_id = (dim or {}).get("dimension_id")
        if dim_id is not None:
            await write("add dimension values", "merit_send_dimension_values",
                        {"dimension_id": dim_id, "values": [{"code": f"P{RUN}", "name": "Test projekt"}]})
        else:
            results.append(("add dimension values", False, "no DimId for the new dimension"))
            print("FAIL add dimension values: no DimId for the new dimension")
        await write("create OSS tax", "merit_create_tax", {"country_code": "FI", "tax_pct": 25.5,
                                                            "code": f"FI{RUN[-4:]}", "name": f"FI OSS {RUN}"})
        if cid:
            await write("sales prices", "merit_create_sales_prices", {"prices": [
                {"item_code": svc, "price": 45, "target": "customer", "customer_id": cid}]})
            await write("discounts", "merit_create_discounts", {"discounts": [
                {"discount_pct": 5, "target": "customer", "customer_id": cid, "item_code": svc}]})
        await read("price list", "merit_sales_prices", {"kind": "price_list"})
        await read("customer price", "merit_sales_prices", {"kind": "customer_price", "item_code": svc,
                                                            "customer_id": cid})

        # ------------------------------------------------ sales invoices
        row = {"item_code": svc, "description": "Test rida", "quantity": 2, "price": 50, "vat": "24"}
        a = await write("create sales invoice", "merit_create_sales_invoice", {"customer_id": cid, "rows": [row]})
        a2 = await write("replace sales invoice", "merit_replace_sales_invoice", {
            "invoice_id": (a or {}).get("invoice_id"), "rows": [{**row, "quantity": 3}]})
        b = await write("create invoice to delete", "merit_create_sales_invoice", {"customer_id": cid, "rows": [row]})
        await write("delete sales invoice", "merit_delete_sales_invoice", {"invoice_id": (b or {}).get("invoice_id")})
        inv_id = (a2 or {}).get("invoice_id")
        await read("get sales invoice + pdf", "merit_get_sales_invoice", {"id": inv_id, "include_pdf": True})

        # ------------------------------------------------ payments of a sales invoice (186.00)
        part = await write("part payment of sales invoice", "merit_record_payment", {
            "kind": "sales_invoice", "bank": TEST_BANK, "document_id": inv_id, "amount": 86})
        rest = await write("rest of the sales invoice", "merit_record_payment", {
            "kind": "sales_invoice", "bank": TEST_BANK, "document_id": inv_id,
            "amount": 100 if part else 186})
        pays = await read("payments list", "merit_list_payments", {"period_start": TODAY, "period_end": TODAY})
        last = next((r for r in (pays or {}).get("rows", []) if r.get("CounterPartName") == f"Test Klient {RUN}"), None)
        if last:
            await write("delete payment", "merit_delete_payment", {"payment_id": last["PHId"], "payment_date": TODAY})
        await write("credit invoice", "merit_create_credit_invoice", {"invoice_id": inv_id, "reason": "live test"})

        # ------------------------------------------------ purchase invoices
        prow = {"item_code": "telefon", "description": "Test kulu", "quantity": 1, "price": 100, "vat": "24"}
        p1 = await write("create purchase invoice (booked)", "merit_create_purchase_invoice", {
            "vendor_id": vid, "bill_no": f"B{RUN}-1", "rows": [prow], "wait_for_approval": False})
        p1b = await write("replace purchase invoice", "merit_replace_purchase_invoice", {
            "invoice_id": (p1 or {}).get("purchase_invoice_id"), "rows": [{**prow, "price": 110}],
            "wait_for_approval": False})
        await write("create purchase invoice (waiting)", "merit_create_purchase_invoice", {
            "vendor_id": vid, "bill_no": f"B{RUN}-2", "rows": [prow]})
        await read("waiting purchase invoices", "merit_list_purchase_invoices", {
            "period_start": TODAY, "period_end": TODAY, "status": "waiting_approval"})
        p3 = await write("purchase invoice to delete", "merit_create_purchase_invoice", {
            "vendor_id": vid, "bill_no": f"B{RUN}-3", "rows": [prow], "wait_for_approval": False})
        await write("delete purchase invoice", "merit_delete_purchase_invoice", {
            "invoice_id": (p3 or {}).get("purchase_invoice_id")})
        await write("payment of purchase invoice", "merit_record_payment", {
            "kind": "purchase_invoice", "bank": TEST_BANK, "document_id": (p1b or {}).get("purchase_invoice_id"),
            "amount": 136.4})

        # ------------------------------------------------ other bank entries
        await write("other expense", "merit_record_bank_transaction", {
            "direction": "expense", "bank": TEST_BANK, "lines": [{"account": "4010", "amount": 2.5, "description": "Test"}],
            "document_number": f"E{RUN}"})
        await write("other income", "merit_record_bank_transaction", {
            "direction": "income", "bank": TEST_BANK, "lines": [{"account": "1000", "amount": 3}]})
        await write("customer prepayment", "merit_record_prepayment", {
            "party": "customer", "party_id": cid, "bank": TEST_BANK, "amount": 20})
        await read("bank income list", "merit_list_bank_entries", {
            "bank": TEST_BANK, "period_start": TODAY, "period_end": TODAY, "kind": "income"})

        # ------------------------------------------------ settlement: a sales invoice against a purchase invoice
        s = await write("invoice for settlement", "merit_create_sales_invoice", {"customer_id": cid, "rows": [row]})
        p4 = await write("bill for settlement", "merit_create_purchase_invoice", {
            "vendor_id": vid, "bill_no": f"B{RUN}-4", "rows": [prow], "wait_for_approval": False})
        await write("settlement", "merit_record_settlement", {
            "customer_lines": [{"invoice_id": (s or {}).get("invoice_id"), "amount": 124}],
            "vendor_lines": [{"invoice_id": (p4 or {}).get("purchase_invoice_id"), "amount": 124}]})

        # ------------------------------------------------ offers
        q = await write("create quote", "merit_create_sales_offer", {"customer_id": cid, "rows": [row],
                                                                    "offer_no": f"Q{RUN}"})
        qid = (q or {}).get("offer_id")
        await read("get offer", "merit_get_sales_offer", {"id": qid})
        await write("update offer", "merit_update_sales_offer", {"offer_id": qid, "status": "sent",
                                                                  "header_comment": "Live test"})
        await refused("invoice from a sent offer", "merit_invoice_from_offer", {"offer_id": qid}, "approved offer")
        await write("approve offer", "merit_update_sales_offer", {"offer_id": qid, "status": "approved"})
        await write("invoice from offer", "merit_invoice_from_offer", {"offer_id": qid})
        pre = await write("create prepayment invoice", "merit_create_sales_offer", {
            "customer_id": cid, "rows": [row], "doc_type": "prepayment_invoice", "offer_no": f"E{RUN}"})
        await write("payment of offer", "merit_record_payment", {
            "kind": "sales_offer", "bank": TEST_BANK, "document_id": (pre or {}).get("offer_id"),
            "amount": (pre or {}).get("total_with_vat_in_merit") or 124})
        await read("offers list", "merit_list_sales_offers", {"period_start": TODAY, "period_end": TODAY})

        # ------------------------------------------------ recurring invoices
        await write("create recurring invoice", "merit_create_recurring_invoice", {
            "customer_id": cid, "contract_no": f"R{RUN}", "start_date": "2026-12-01", "rows": [row]})
        await read("recurring list", "merit_list_recurring_invoices", {"period_start": "2026-12-01",
                                                                       "period_end": "2026-12-31"})
        # Merit answers HTTP 500 unless the contract row is set up for readings in its web UI.
        await refused("meter readings", "merit_send_meter_readings", {
            "item_code": svc, "readings": [{"customer_id": cid, "quantity": 4}]}, "meter readings", approve=True)

        # ------------------------------------------------ ledger, stock, fixed assets
        await refused("GL entry that does not balance", "merit_create_gl_entry", {
            "doc_no": f"K{RUN}", "entry_date": TODAY,
            "lines": [{"account": "4010", "debit": 10}, {"account": "1000", "credit": 9}]}, "does not balance")
        accounts = await m.call("v2/accounts", None, method="GET")
        expense = next(a["Code"] for a in accounts if a["Code"].startswith("4") and not a.get("NonActive"))
        await write("GL entry", "merit_create_gl_entry", {"doc_no": f"K{RUN}", "entry_date": TODAY, "lines": [
            {"account": expense, "debit": 10, "memo": "Live test"}, {"account": "1000", "credit": 10}]})
        await read("GL with lines", "merit_list_gl_batches", {"period_start": TODAY, "period_end": TODAY,
                                                              "with_lines": True, "limit": 3})
        await write("stock in", "merit_create_inventory_movement", {
            "movement": "in", "location": "1", "rows": [{"item_code": stock, "quantity": 5, "unit_cost": 10}]})
        await write("stock out", "merit_create_inventory_movement", {
            "movement": "out", "location": "1", "rows": [{"item_code": stock, "quantity": 1}]})
        await read("movements", "merit_list_inventory_movements", {"period_start": TODAY, "period_end": TODAY,
                                                                   "with_lines": True})
        await read("stock report", "merit_inventory_report", {"as_of": TODAY})
        locations = await m.call("v2/getfalocations", {}) or []
        people = await m.call("v2/getfaresppersons", {}) or []
        asset = {"inventory_no": f"FA{RUN}", "name": "Test arvuti", "group": "Arvutid ja arvutisüsteemid",
                 "depreciation_pct": 33, "location": (locations[0]["Name"] if locations else "Kontor"),
                 "responsible_person": (people[0]["Name"] if people else "Keegi")}
        if locations and people:
            await write("fixed asset", "merit_create_fixed_assets", {"assets": [asset]})
        else:  # the API cannot create them: add a location and a responsible person in Merit
            await refused("fixed asset without location or responsible", "merit_create_fixed_assets",
                          {"assets": [asset]}, "refuses an asset without one")
        await read("fixed assets", "merit_list_fixed_assets", {})

        # ------------------------------------------------ e-invoice XML
        sales_xml = save("sales.xml", einvoice(f"X{RUN}", seller=("API Test", "12345678"),
                                                     buyer=(f"Test Klient {RUN}", f"7{RUN}"), product=svc))
        await write("import sales e-invoice", "merit_import_einvoice", {"direction": "sales", "file_path": sales_xml})
        purch_xml = save("purchase.xml", einvoice(f"Y{RUN}", seller=(f"Test Tarnija {RUN}", f"8{RUN}"),
                                                        buyer=("API Test", "12345678"), product="telefon"))
        await write("import purchase e-invoice", "merit_import_einvoice", {"direction": "purchase",
                                                                           "file_path": purch_xml})

        # ------------------------------------------------ bank statement (needs an IBAN on the test bank)
        bank = next(b for b in await m.call("v2/banks", None, method="GET") if b["Name"].strip() == TEST_BANK)
        if bank.get("IBANCode"):
            statement = save("statement.xml", camt(bank["IBANCode"].replace(" ", "")))
            await write("import bank statement", "merit_import_bank_statement", {"file_path": statement})
        else:
            results.append(("import bank statement", False, f"{TEST_BANK} has no IBAN in Merit"))
            print(f"FAIL import bank statement: {TEST_BANK} has no IBAN in Merit")
    await m.close()


def camt(iban: str) -> bytes:
    entry = """<Ntry><Amt Ccy="EUR">{amt}</Amt><CdtDbtInd>{ind}</CdtDbtInd><Sts>BOOK</Sts>
<BookgDt><Dt>{day}</Dt></BookgDt><ValDt><Dt>{day}</Dt></ValDt><AcctSvcrRef>{ref}</AcctSvcrRef>
<NtryDtls><TxDtls><Refs><AcctSvcrRef>{ref}</AcctSvcrRef></Refs><RmtInf><Ustrd>{text}</Ustrd></RmtInf></TxDtls></NtryDtls>
</Ntry>"""
    rows = (entry.format(amt="12.40", ind="CRDT", day=TODAY, ref=f"{RUN}A", text="Live test income")
            + entry.format(amt="1.50", ind="DBIT", day=TODAY, ref=f"{RUN}B", text="Teenustasu"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt>
<GrpHdr><MsgId>T{RUN}</MsgId><CreDtTm>{TODAY}T10:00:00</CreDtTm></GrpHdr>
<Stmt><Id>S{RUN}</Id><CreDtTm>{TODAY}T10:00:00</CreDtTm>
<FrToDt><FrDtTm>{TODAY}T00:00:00</FrDtTm><ToDtTm>{TODAY}T23:59:59</ToDtTm></FrToDt>
<Acct><Id><IBAN>{iban}</IBAN></Id><Ccy>EUR</Ccy></Acct>{rows}</Stmt></BkToCstmrStmt></Document>""".encode()


def einvoice(number: str, seller: tuple[str, str], buyer: tuple[str, str], product: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<E_Invoice>
<Header><Date>{TODAY}</Date><FileId>{number}</FileId><Version>1.2</Version></Header>
<Invoice invoiceId="{number}" regNumber="{buyer[1]}" sellerRegnumber="{seller[1]}">
<InvoiceParties>
<SellerParty><Name>{seller[0]}</Name><RegNumber>{seller[1]}</RegNumber></SellerParty>
<BuyerParty><Name>{buyer[0]}</Name><RegNumber>{buyer[1]}</RegNumber><ContactData><LegalAddress>
<PostalAddress1>Test 1</PostalAddress1><City>Tallinn</City><Country>EE</Country></LegalAddress></ContactData></BuyerParty>
</InvoiceParties>
<InvoiceInformation><Type type="DEB"/><DocumentName>Arve</DocumentName><InvoiceNumber>{number}</InvoiceNumber>
<InvoiceDate>{TODAY}</InvoiceDate><DueDate>{TODAY}</DueDate></InvoiceInformation>
<InvoiceSumGroup><InvoiceSum>10.00</InvoiceSum><VAT vatId="TAX"><SumBeforeVAT>10.00</SumBeforeVAT><VATRate>24</VATRate>
<VATSum>2.40</VATSum></VAT><TotalVATSum>2.40</TotalVATSum><TotalSum>12.40</TotalSum><TotalToPay>12.40</TotalToPay>
<Currency>EUR</Currency></InvoiceSumGroup>
<InvoiceItem><InvoiceItemGroup><ItemEntry><RowNo>1</RowNo><SellerProductId>{product}</SellerProductId>
<Description>Test</Description><ItemDetailInfo><ItemUnit>kuu</ItemUnit><ItemAmount>1</ItemAmount><ItemPrice>10.00</ItemPrice></ItemDetailInfo>
<ItemSum>10.00</ItemSum><VAT vatId="TAX"><SumBeforeVAT>10.00</SumBeforeVAT><VATRate>24</VATRate><VATSum>2.40</VATSum></VAT>
<ItemTotal>12.40</ItemTotal></ItemEntry></InvoiceItemGroup></InvoiceItem>
<PaymentInfo><Currency>EUR</Currency><PaymentRefId>{number}</PaymentRefId><PaymentDescription>Arve {number}</PaymentDescription>
<Payable>YES</Payable><PayDueDate>{TODAY}</PayDueDate><PaymentTotalSum>12.40</PaymentTotalSum>
<PayerName>{buyer[0]}</PayerName><PaymentId>{number}</PaymentId><PayToAccount>EE382200221020145685</PayToAccount>
<PayToName>{seller[0]}</PayToName></PaymentInfo>
</Invoice>
<Footer><TotalNumberInvoices>1</TotalNumberInvoices><TotalAmount>12.40</TotalAmount></Footer>
</E_Invoice>
""".encode()


if __name__ == "__main__":
    main()
