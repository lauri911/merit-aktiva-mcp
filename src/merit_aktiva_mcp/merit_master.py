"""Write tools for Merit's master data: items, groups, units, dimensions, OSS taxes, sales prices
and discounts. Registered only when MERIT_ALLOW_WRITE=true; dry_run=true returns the preview without writing.

Every create first reads Merit's existing list and refuses a code or name that is already there,
so nothing is created twice. Merit has no API to delete any of these.
"""

from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from .merit import Merit, _ymd, compact
from .merit_write import GUID, WRITE, DryRun, Books, _gate
from .files import Files


ITEM_TYPE = {"stock": 1, "service": 2, "goods": 3}
ITEM_USAGE = {"sales": 1, "purchases": 2, "both": 3}
NO_DATE = "1901-01-01"  # Merit's start or end date for "not set"
# v1/updateitem field -> the v1/getitems field that holds its current value.
ITEM_UPDATE_FROM = {"Code": "Code", "Description": "Name", "SalesPrice": "SalesPrice", "DiscountPct": "DiscountPct",
                    "EANCode": "EANCode", "NameEN": "NameEN", "LastPurchasePrice": "LastPurchasePrice",
                    "SalesAccountCode": "SalesAccountCode", "InventoryAccountCode": "InventoryAccountCode",
                    "ItemCostAccountCode": "ItemCostAccountCode"}
ITEM_CHANGE_MAP = {"code": "Code", "description": "Description", "sales_price": "SalesPrice",
                   "discount_pct": "DiscountPct", "ean_code": "EANCode", "name_en": "NameEN",
                   "last_purchase_price": "LastPurchasePrice", "sales_account": "SalesAccountCode",
                   "inventory_account": "InventoryAccountCode", "cost_account": "ItemCostAccountCode"}


# ---------------------------------------------------------------------------- input models

class NewItem(BaseModel):
    code: str = Field(max_length=20, description="Item code; must not exist yet")
    description: str = Field(max_length=100, description="Item name as it shows on invoices; must not exist yet")
    type: Literal["service", "goods", "stock"] = Field(
        "service", description="service; goods (not tracked in stock); stock (a stock item, needs unit)")
    usage: Literal["sales", "purchases", "both"] = Field("both", description="Which documents may use it")
    unit: str | None = Field(None, max_length=64, description="Unit of measure, e.g. 'tk', 'h', 'kuu'; required for stock")
    vat: str | None = Field(None, description="Default VAT: a rate like '24' or a tax Id (merit_reference_list kind=taxes)")
    group_code: str | None = Field(None, description="Item group code; must exist")
    sales_account: str | None = Field(None, description="Revenue account code")
    purchase_account: str | None = Field(None, description="Expense account code")
    inventory_account: str | None = Field(None, description="Inventory account code (stock items)")
    cost_account: str | None = Field(None, description="Cost of goods sold account code (stock items)")
    description_en: str | None = Field(None, max_length=100)
    ean_code: str | None = None
    location_code: str | None = Field(None, max_length=20, description="Default stock; only when several stocks are used")
    sales_price: float | None = Field(None, ge=0, description="Default sales price without VAT; set right after "
                                                               "creating (Merit's create call has no price)")


class ItemChanges(BaseModel):
    """Only the fields given are changed. Unit, type, usage and the purchase account cannot be
    changed through Merit's API."""
    code: str | None = Field(None, max_length=20, description="New code; must not exist yet")
    description: str | None = Field(None, max_length=100)
    sales_price: float | None = Field(None, ge=0, description="Default sales price without VAT")
    discount_pct: float | None = Field(None, ge=0, le=100)
    vat: str | None = Field(None, description="A rate like '24' or a tax Id (merit_reference_list kind=taxes)")
    group_code: str | None = Field(None, description="Item group code; must exist")
    sales_account: str | None = None
    inventory_account: str | None = None
    cost_account: str | None = None
    ean_code: str | None = Field(None, max_length=13)
    name_en: str | None = Field(None, max_length=100)
    last_purchase_price: float | None = Field(None, ge=0)


class DimValue(BaseModel):
    code: str = Field(description="Value code. A code the dimension already has only gets its end_date changed")
    name: str | None = Field(None, description="Required for a new value")
    end_date: date | None = Field(None, description="Last day the value can be used; required to close an existing one")


class PriceRow(BaseModel):
    item_code: str = Field(description="Existing item code")
    price: float = Field(ge=0, description="Unit price WITHOUT VAT")
    target: Literal["customer", "customer_group", "campaign"] = Field(
        description="Who gets the price: one customer, a customer group, or everyone in the period (campaign)")
    customer_id: str | None = Field(None, description="CustomerId, for target=customer")
    customer_group_code: str | None = Field(None, description="Group code, for target=customer_group")
    campaign_name: str | None = Field(None, description="Optional name, for target=campaign")
    currency: str = "EUR"
    discount_allowed: bool = Field(True, description="May discounts still apply on top of this price")
    start_date: date | None = Field(None, description="Default today")
    end_date: date | None = Field(None, description="Default: no end")


class DiscountRow(BaseModel):
    discount_pct: float = Field(gt=0, le=100)
    target: Literal["customer", "customer_group"]
    customer_id: str | None = Field(None, description="CustomerId, for target=customer")
    customer_group_code: str | None = Field(None, description="Group code, for target=customer_group")
    item_code: str | None = Field(None, description="Discount on this item (give this or item_group_code)")
    item_group_code: str | None = Field(None, description="Discount on every item in this group")
    start_date: date | None = Field(None, description="Default today")
    end_date: date | None = Field(None, description="Default: no end")


# ---------------------------------------------------------------------------- helpers

def _rows_of(res: Any) -> list[dict]:
    return [res] if isinstance(res, dict) else [r for r in (res or []) if isinstance(r, dict)]


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def _day(value: str | None) -> date | None:
    """A Merit date, or None for its 1901-01-01 'not set'."""
    if not value or value.startswith(NO_DATE):
        return None
    return date.fromisoformat(value[:10])


def _overlaps(s1: date | None, e1: date | None, s2: date | None, e2: date | None) -> bool:
    return (s1 or date.min) <= (e2 or date.max) and (s2 or date.min) <= (e1 or date.max)


def _ok(res: Any, what: str) -> None:
    text = res if isinstance(res, str) else str(res)
    if text.strip().strip('"').upper() != "OK":
        raise ToolError(f"Merit did not confirm the {what}: {text[:300]}")


def register_master(mcp: FastMCP, merit: Merit, books: Books, files: Files | None = None) -> None:  # noqa: ARG001 (no files here)

    # ------------------------------------------------------------ lookups

    async def accounts() -> dict[str, dict]:
        return {a["Code"]: a for a in _rows_of(await merit.call("v2/accounts", None, method="GET"))}

    async def check_accounts(codes: dict[str, str | None]) -> None:
        given = {k: v for k, v in codes.items() if v}
        if not given:
            return
        known = await accounts()
        for what, code in given.items():
            a = known.get(code)
            if a is None:
                raise ToolError(f"{what} {code!r} is not in the chart of accounts (merit_reference_list kind=accounts)")
            if a.get("NonActive"):
                raise ToolError(f"{what} {code!r} ({a.get('Name')}) is not active")

    async def groups(kind: str) -> list[dict]:
        path = {"item": "v2/getitemgroups", "customer": "v2/getcustomergroups", "vendor": "v2/getvendorgroups"}[kind]
        return _rows_of(await merit.call(path, {}))

    async def group(kind: str, code: str) -> dict:
        for g in await groups(kind):
            if _same(g.get("Code"), code):
                return g
        raise ToolError(f"no {kind} group with code {code!r} (merit_reference_list kind={kind}_groups)")

    async def all_items() -> list[dict]:
        return _rows_of(await merit.call("v1/getitems", {}))

    async def item_by_code(code: str) -> dict:
        rows = _rows_of(await merit.call("v1/getitems", {"Code": code}))  # broad match
        hits = [r for r in rows if r.get("Code") == code] or [r for r in rows if _same(r.get("Code"), code)]
        if len(hits) != 1:
            raise ToolError(f"no item with code {code!r} (merit_reference_list kind=items)")
        return hits[0]

    async def customer_name(customer_id: str | None) -> str:
        if not customer_id:
            raise ToolError("target=customer needs customer_id (merit_find_customers)")
        return (await books.customer(customer_id))["Name"]

    def forget_items() -> None:
        books._cache.pop("items", None)

    # ------------------------------------------------------------ items

    async def item_record(item: dict, changes: dict) -> tuple[dict, list[str]]:
        """The full v1/updateitem record: the item's current values with the changes on top.
        Current VAT and group come back from getitems as names, so they are matched to an Id and a code."""
        notes: list[str] = []
        record = {k: item.get(src) for k, src in ITEM_UPDATE_FROM.items() if item.get(src) is not None}
        if "TaxId" not in changes and item.get("VatTaxName"):
            hits = [t for t in await books.taxes() if _same(t.get("Name"), item["VatTaxName"])]
            if len(hits) == 1:
                record["TaxId"] = hits[0]["Id"]
            else:
                notes.append(f"current VAT {item['VatTaxName']!r} does not match one tax, so it is not sent")
        if "ItemGrCode" not in changes and item.get("ItemGroupName"):
            hits = [g for g in await groups("item") if _same(g.get("Name"), item["ItemGroupName"])]
            if len(hits) == 1:
                record["ItemGrCode"] = hits[0]["Code"]
            else:
                notes.append(f"current group {item['ItemGroupName']!r} does not match one group, so it is not sent")
        record.update(changes)
        record["Id"] = item["ItemId"]
        return record, notes

    async def update_item_in_merit(record: dict) -> None:
        res = await merit.call("v1/updateitem", record, text_ok=True)
        if isinstance(res, str) and ("api-" in res.lower() or "error" in res.lower()):
            raise ToolError(f"Merit did not accept the item change: {res[:300]}")

    @mcp.tool(annotations=WRITE)
    async def merit_create_items(
        items: Annotated[list[NewItem], Field(min_length=1, max_length=50)],
        dry_run: DryRun = False,
    ) -> dict:
        """Create items (artiklid) in Merit with their accounts, VAT, group and unit. Refused if a code
        or name already exists. Merit cannot delete items through the API. merit_create_sales_invoice
        can also create a bare item on the fly; this is for setting one up properly.
        dry_run=true returns the preview only."""
        existing = await all_items()
        codes = [i.code.strip().lower() for i in items]
        if len(set(codes)) != len(codes):
            raise ToolError("the same code is given twice")
        notes = []
        for i in items:
            for e in existing:
                if _same(e.get("Code"), i.code):
                    raise ToolError(f"item {e.get('Code')!r} ({e.get('Name')}) already exists in Merit; use it, or "
                                    "merit_update_item to change it")
                if _same(e.get("Name"), i.description):
                    notes.append(f"{i.code}: item {e.get('Code')!r} already has the name {e.get('Name')!r}")
        units = _rows_of(await merit.call("v2/units", None, method="GET")) if any(i.unit for i in items) else []
        rows, summary, prices = [], [], {}
        for i in items:
            if i.type == "stock" and not i.unit:
                raise ToolError(f"{i.code}: a stock item needs a unit")
            if i.unit:
                u = next((u for u in units if _same(u.get("Name"), i.unit) or _same(u.get("Code"), i.unit)), None)
                if u is None:
                    raise ToolError(f"{i.code}: no unit {i.unit!r} in Merit; units: {[u.get('Name') for u in units]} "
                                    "(merit_create_unit adds one)")
                if u.get("Code") and _same(u.get("Code"), u.get("Name")):
                    # Seen live 2026-11-21: senditems answers HTTP 500 for tk, kg, km and ltr (code equal to
                    # name), while kuu (code kuud), tund (code tundi) and units made through the API work.
                    raise ToolError(f"{i.code}: Merit fails to create an item with unit {u.get('Name')!r} through "
                                    "the API (a Merit fault with units whose code equals their name). Create the "
                                    "item without a unit and set it in Merit, or use another unit.")
            await check_accounts({"sales_account": i.sales_account, "purchase_account": i.purchase_account,
                                  "inventory_account": i.inventory_account, "cost_account": i.cost_account})
            row: dict = {"Type": ITEM_TYPE[i.type], "Usage": ITEM_USAGE[i.usage], "Code": i.code,
                         "Description": i.description}
            shown: dict = {"code": i.code, "name": i.description, "type": i.type, "usage": i.usage}
            if i.vat:
                tax = await books.tax(i.vat)
                row["TaxId"] = tax["Id"]
                shown["vat"] = f"{tax['Name'].strip()} ({tax['TaxPct']:g}%)"
            if i.group_code:
                g = await group("item", i.group_code)
                row["ItemGrCode"] = g["Code"]
                shown["group"] = f"{g['Code']} {g.get('Name', '')}".strip()
            for field, key in (("unit", "UOMName"), ("ean_code", "EANCode"), ("description_en", "DescriptionEN"),
                               ("location_code", "DefLocationCode"), ("sales_account", "SalesAccCode"),
                               ("purchase_account", "PurchaseAccCode"), ("inventory_account", "InventoryAccCode"),
                               ("cost_account", "CostAccCode")):
                if getattr(i, field):
                    row[key] = getattr(i, field)
                    shown[field] = getattr(i, field)
            if i.sales_price is not None:
                prices[i.code] = i.sales_price
                shown["sales_price"] = i.sales_price
            rows.append(row)
            summary.append(shown)
        payload = {"Items": rows}
        state = {"items": payload, "sales_prices": prices}
        if (gate := _gate("create_items", state, dry_run, {"new_items": summary, "notes": notes})) is not None:
            return gate
        res = _rows_of(await merit.call("v2/senditems", payload))
        forget_items()
        made = {r.get("Code"): r.get("ItemId") for r in res if r.get("ItemId")}
        missing = [r["Code"] for r in rows if r["Code"] not in made]
        if missing:
            raise ToolError(f"Merit did not confirm items {missing}: {str(res)[:300]}")
        out: dict = {"written": True, "items": [{"code": c, "item_id": i} for c, i in made.items()]}
        failed = []
        for code, price in prices.items():
            try:
                fresh = _rows_of(await merit.call("v1/getitems", {"Id": made[code]}))
                record, _ = await item_record(fresh[0], {"SalesPrice": price})
                await update_item_in_merit(record)
            except Exception as e:  # the item exists by now; say so instead of failing the whole call
                failed.append(f"{code}: {e}")
        if failed:
            out["warning"] = f"Items were created, but setting the sales price failed: {failed}. Use merit_update_item."
        return out

    @mcp.tool(annotations=WRITE)
    async def merit_update_item(
        item_code: Annotated[str, Field(description="The item's current code (merit_reference_list kind=items)")],
        changes: ItemChanges,
        dry_run: DryRun = False,
    ) -> dict:
        """Change an item: name, code, sales price, discount, VAT, group, accounts. Fields not given
        stay as they are; invoices already made keep their rows. dry_run=true shows old and new values."""
        item = await item_by_code(item_code)
        fields = changes.model_dump()
        diff = {ITEM_CHANGE_MAP[k]: v for k, v in fields.items() if v is not None and k in ITEM_CHANGE_MAP}
        shown: dict = {ITEM_CHANGE_MAP[k]: {"from": item.get(ITEM_UPDATE_FROM[ITEM_CHANGE_MAP[k]]), "to": v}
                       for k, v in fields.items() if v is not None and k in ITEM_CHANGE_MAP}
        expect = {ITEM_UPDATE_FROM[k]: v for k, v in diff.items()}  # getitems field -> value after the change
        if changes.code is not None and not _same(changes.code, item["Code"]):
            if any(_same(e.get("Code"), changes.code) for e in await all_items()):
                raise ToolError(f"another item already has code {changes.code!r}")
        if changes.vat is not None:
            tax = await books.tax(changes.vat)
            diff["TaxId"] = tax["Id"]
            shown["VAT"] = {"from": item.get("VatTaxName"), "to": tax["Name"].strip()}
            expect["VatTaxName"] = tax["Name"].strip()
        if changes.group_code is not None:
            g = await group("item", changes.group_code)
            diff["ItemGrCode"] = g["Code"]
            shown["group"] = {"from": item.get("ItemGroupName"), "to": g.get("Name")}
            expect["ItemGroupName"] = g.get("Name")
        if not diff:
            raise ToolError("no changes given")
        await check_accounts({"sales_account": changes.sales_account, "inventory_account": changes.inventory_account,
                              "cost_account": changes.cost_account})
        record, notes = await item_record(item, diff)
        preview = {"item": item["Code"], "name": item.get("Name"), "changes": shown, "notes": notes}
        if (gate := _gate("update_item", record, dry_run, preview)) is not None:
            return gate
        await update_item_in_merit(record)
        forget_items()
        after = _rows_of(await merit.call("v1/getitems", {"Id": item["ItemId"]}))
        after = after[0] if after else {}
        wrong = {k: after.get(k) for k, v in expect.items()
                 if not (_same(str(after.get(k)), str(v)) or (isinstance(v, float) and after.get(k) == v))}
        return {"written": True, "item_id": item["ItemId"],
                **({"warning": f"Merit shows different values for: {wrong}"} if wrong else {"verified": True})}

    # ------------------------------------------------------------ groups and units

    @mcp.tool(annotations=WRITE)
    async def merit_create_group(
        kind: Annotated[Literal["item", "customer", "vendor"], Field(description="Which kind of group")],
        code: Annotated[str, Field(min_length=1, max_length=20)],
        name: Annotated[str, Field(min_length=1, max_length=64)],
        dry_run: DryRun = False,
    ) -> dict:
        """Create an item, customer or vendor group. Refused if the code or name exists. dry_run=true returns the preview only."""
        for g in await groups(kind):
            if _same(g.get("Code"), code) or _same(g.get("Name"), name):
                raise ToolError(f"{kind} group {g.get('Code')!r} ({g.get('Name')}) already exists")
        if kind == "item":
            path, payload = "v2/senditemgroups", {"ItemGroups": [{"Code": code, "Name": name}]}
        else:
            path = "v2/sendcustomergroup" if kind == "customer" else "v2/sendvendorgroup"
            payload = {"Code": code, "Name": name}
        if (gate := _gate("create_group", {"path": path, "payload": payload}, dry_run,
                          {"new_group": {"kind": kind, "code": code, "name": name}})) is not None:
            return gate
        res = _rows_of(await merit.call(path, payload))
        made = [r for r in res if r.get("Id")]
        if not made:
            raise ToolError(f"Merit did not confirm the group: {str(res)[:300]}")
        return {"written": True, "kind": kind, "group_id": made[0]["Id"], "code": code, "name": name}

    @mcp.tool(annotations=WRITE)
    async def merit_create_unit(
        name: Annotated[str, Field(min_length=1, max_length=64, description="Unit as it shows on invoices, e.g. 'tk', 'kuu'")],
        name_plural: str | None = None,
        name_en: str | None = None,
        name_plural_en: str | None = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Create a unit of measure. Refused if it exists. dry_run=true returns the preview only."""
        for u in _rows_of(await merit.call("v2/units", None, method="GET")):
            if _same(u.get("Name"), name) or _same(u.get("Code"), name):
                raise ToolError(f"unit {u.get('Name')!r} already exists")
        payload = compact({"Name": name, "NamePlural": name_plural, "NameEN": name_en, "NamePluralEN": name_plural_en})
        if (gate := _gate("create_unit", payload, dry_run, {"new_unit": payload})) is not None:
            return gate
        res = await merit.call("v2/senduom", payload, text_ok=True)
        text = res if isinstance(res, str) else str(res)
        if "api-uomexsist" in text:
            raise ToolError(f"Merit says unit {name!r} already exists (api-uomexsist); nothing was created")
        unit_id = res.get("Id") if isinstance(res, dict) else text.strip().strip('"')
        if not unit_id or not GUID.match(str(unit_id)):
            raise ToolError(f"Merit did not confirm the unit: {text[:300]}")
        return {"written": True, "unit": name, "unit_id": unit_id}

    # ------------------------------------------------------------ dimensions

    async def dimension_rows() -> list[dict]:
        return _rows_of(await merit.call("v2/getdimensions", {"AllValues": True}))

    @mcp.tool(annotations=WRITE)
    async def merit_create_dimension(
        name: Annotated[str, Field(min_length=1, max_length=50, description="e.g. 'Projekt'")],
        type: Annotated[Literal["detail", "summary"], Field(description="detail: values are used on documents; "
                                                                        "summary: groups other dimensions")] = "detail",
        parent_dimension_id: Annotated[int | None, Field(description="DimId of the general dimension it belongs to, if any")] = None,
        debit_positive: Annotated[bool, Field(description="Amounts in debit are shown as positive")] = True,
        dimension_id: Annotated[int | None, Field(ge=1, description="DimId to give it; default: one above the highest "
                                                                      "DimId Merit lists")] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Create a dimension (e.g. project or cost centre); add its values with
        merit_send_dimension_values. Refused if a dimension of that name has values already. dry_run=true returns the preview only."""
        rows = await dimension_rows()
        clash = next((r for r in rows if _same(r.get("DimName"), name)), None)
        if clash:
            raise ToolError(f"dimension {clash['DimName']!r} already exists (DimId {clash.get('DimId')})")
        if parent_dimension_id is not None and not any(r.get("DimId") == parent_dimension_id for r in rows):
            raise ToolError(f"no dimension with DimId {parent_dimension_id} (merit_reference_list kind=dimensions)")
        # The new dimension's DimId is the Id we send (checked live 2026-11-21): Merit lists a dimension only
        # once it has values, so without it there would be no way to find the DimId to add values to.
        # Merit refuses an Id that is taken ("Dimensioon Id = N on juba ...").
        new_id = dimension_id or max([r.get("DimId") or 0 for r in rows] + [0]) + 1
        if any(r.get("DimId") == new_id for r in rows):
            raise ToolError(f"DimId {new_id} is already used; pick another dimension_id")
        dim = {"Id": new_id, "Name": name, "Type": 1 if type == "detail" else 2, "PosNeg": debit_positive}
        if parent_dimension_id is not None:
            dim["GenId"] = parent_dimension_id
        payload = {"Dimensions": [dim]}
        preview = {"new_dimension": {"dimension_id": new_id, "name": name, "type": type,
                                     "parent_dimension_id": parent_dimension_id, "debit_positive": debit_positive},
                   "note": "Merit's dimension list only shows dimensions that have values, so one without values "
                           "could not be checked for a duplicate name or DimId; Merit refuses a DimId that is taken"}
        if (gate := _gate("create_dimension", payload, dry_run, preview)) is not None:
            return gate
        try:
            res = await merit.call("v2/senddimensions", payload, text_ok=True)
        except ToolError as e:
            if "on juba" in str(e):
                raise ToolError(f"DimId {new_id} is already used by a dimension without values; call again with "
                                f"dimension_id={new_id + 1} (nothing was created)") from None
            raise
        text = res if isinstance(res, str) else str(res)
        if res is None or "api-" in text.lower() or "error" in text.lower():
            raise ToolError(f"Merit did not confirm the dimension: {text[:300]}")
        return {"written": True, "dimension": name, "dimension_id": new_id, "merit_answer": res,
                "next": f"Add its values with merit_send_dimension_values dimension_id={new_id}."}

    @mcp.tool(annotations=WRITE)
    async def merit_send_dimension_values(
        dimension_id: Annotated[int, Field(description="DimId (merit_reference_list kind=dimensions)")],
        values: Annotated[list[DimValue], Field(min_length=1, max_length=100)],
        dry_run: DryRun = False,
    ) -> dict:
        """Add values to a dimension (e.g. a new project), or close existing ones by giving their
        code with an end_date. A new code needs a name. Merit cannot rename or delete values
        through the API. dry_run=true returns the preview only."""
        rows = await dimension_rows()
        mine = [r for r in rows if r.get("DimId") == dimension_id]
        dim_name = mine[0].get("DimName") if mine else None
        if len({v.code.strip().lower() for v in values}) != len(values):
            raise ToolError("the same code is given twice")
        out, shown = [], []
        for v in values:
            old = next((r for r in mine if _same(r.get("Code"), v.code)), None)
            row: dict = {"DimId": dimension_id, "DimValueCode": v.code}
            if old:
                if v.end_date is None:
                    raise ToolError(f"{dim_name} already has value {old.get('Code')!r} ({old.get('Name')}); "
                                    "give an end_date to close it, or use a new code")
                if v.name and not _same(v.name, old.get("Name")):
                    raise ToolError(f"value {old.get('Code')!r} exists as {old.get('Name')!r}; Merit can only change "
                                    "its end date, not its name")
                row.update({"DimValueId": old["Id"], "DimValueName": old.get("Name"), "EndDate": _ymd(v.end_date)})
                shown.append({"code": old.get("Code"), "name": old.get("Name"), "change": "end date",
                              "from": str(_day(old.get("EndDate")) or "no end"), "to": v.end_date.isoformat()})
            else:
                if not v.name:
                    raise ToolError(f"new value {v.code!r} needs a name")
                row["DimValueName"] = v.name
                if v.end_date:
                    row["EndDate"] = _ymd(v.end_date)
                shown.append({"code": v.code, "name": v.name, "change": "new value",
                              "end_date": v.end_date.isoformat() if v.end_date else None})
            out.append(row)
        payload = {"Dimensions": out}
        preview = {"dimension": dim_name or f"DimId {dimension_id}", "values": shown}
        if not mine:
            preview["warning"] = (f"Merit lists no values for DimId {dimension_id}, so it could not be checked that "
                                  "this dimension exists. Make sure it is the right one.")
        if (gate := _gate("send_dimension_values", payload, dry_run, preview)) is not None:
            return gate
        res = await merit.call("v2/senddimvalues", payload, text_ok=True)
        text = res if isinstance(res, str) else str(res)
        if "api-" in text.lower() or "error" in text.lower():
            raise ToolError(f"Merit did not accept the dimension values: {text[:300]}")
        after = [r for r in await dimension_rows() if r.get("DimId") == dimension_id]
        missing = [v.code for v in values if not any(_same(r.get("Code"), v.code) for r in after)]
        return {"written": True, "dimension_id": dimension_id, "values": [v.code for v in values],
                **({"warning": f"Merit does not list these values afterwards: {missing}"} if missing else {"verified": True})}

    # ------------------------------------------------------------ taxes

    @mcp.tool(annotations=WRITE)
    async def merit_create_tax(
        country_code: Annotated[str, Field(min_length=2, max_length=2, description="EU country the OSS rate is for, e.g. 'FI'")],
        tax_pct: Annotated[float, Field(ge=0, le=100, description="That country's VAT rate, e.g. 25.5")],
        code: Annotated[str, Field(min_length=1, max_length=16)],
        name: Annotated[str, Field(min_length=1, max_length=40)],
        name_en: Annotated[str | None, Field(max_length=150)] = None,
        dry_run: DryRun = False,
    ) -> dict:
        """Create a VAT rate for OSS sales to consumers in another EU country (the only kind Merit's
        API can create). Refused if the code or name exists. dry_run=true returns the preview only."""
        country = country_code.upper()
        if country == "EE":
            raise ToolError("OSS rates are for other EU countries; Estonian rates are set up in Merit's VAT settings")
        taxes = await books.taxes()
        for t in taxes:
            if _same(t.get("Code"), code) or _same(t.get("Name"), name):
                raise ToolError(f"tax {t.get('Code')!r} ({(t.get('Name') or '').strip()}) already exists (Id {t.get('Id')})")
        similar = [f"{t.get('Code')} {(t.get('Name') or '').strip()}" for t in taxes
                   if _same(t.get("CountryCode"), country) and t.get("TaxPct") == tax_pct and not t.get("NonActive")]
        payload = compact({"Code": code, "Name": name, "NameEN": name_en, "TaxPct": tax_pct, "TaxType": 12,
                           "CountryCode": country})
        preview = {"new_tax": {"code": code, "name": name, "name_en": name_en, "rate_pct": tax_pct, "country": country,
                               "type": "OSS sale to the EU"},
                   "warning": f"{country} already has an active {tax_pct:g}% rate: {similar}" if similar else None}
        if (gate := _gate("create_tax", payload, dry_run, preview)) is not None:
            return gate
        res = await merit.call("v2/sendtax", payload, text_ok=True)
        books._cache.pop("taxes", None)
        if isinstance(res, dict):
            tax_id = res.get("CreatedTaxId") or res.get("Id")
        else:  # documented as "CreatedTaxId= <guid>"
            tax_id = str(res or "").split("=")[-1].strip().strip('"')
        if not tax_id or not GUID.match(tax_id):
            raise ToolError(f"Merit did not confirm the tax: {str(res)[:300]}")
        return {"written": True, "tax_id": tax_id, "code": code, "name": name}

    # ------------------------------------------------------------ prices and discounts

    async def target_name(target: str, customer_id: str | None, group_code: str | None, campaign: str | None = None) -> str:
        if target == "customer":
            return await customer_name(customer_id)
        if target == "customer_group":
            if not group_code:
                raise ToolError("target=customer_group needs customer_group_code (merit_reference_list kind=customer_groups)")
            return (await group("customer", group_code))["Name"]
        return campaign or ""

    @mcp.tool(annotations=WRITE)
    async def merit_create_sales_prices(
        prices: Annotated[list[PriceRow], Field(min_length=1, max_length=100)],
        dry_run: DryRun = False,
    ) -> dict:
        """Set special sales prices for a customer, a customer group or a campaign period, used on
        new invoices instead of the item's default price. Refused if the same item already has a
        price for that target and currency in an overlapping period. dry_run=true returns the preview only."""
        today = date.today()
        known = await books.items(require=[p.item_code for p in prices])
        existing = _rows_of(await merit.call("v2/getprices", {}))
        rows, shown = [], []
        for p in prices:
            if p.item_code not in known:
                raise ToolError(f"no item with code {p.item_code!r} (merit_reference_list kind=items)")
            if p.item_code != p.item_code.lower():
                # Seen live 2026-11-21: sendprices answers "Puudub artikkel koodiga X" for every item whose code
                # has a capital letter (lc… and mat work, Mx… and T… do not); senddiscounts finds them.
                raise ToolError(f"Merit's API cannot set a price for item {p.item_code!r}: it does not find item codes "
                                "with capital letters (a Merit fault). Set this price in Merit.")
            start = p.start_date or today
            if p.end_date and p.end_date < start:
                raise ToolError(f"{p.item_code}: end_date is before start_date")
            who = await target_name(p.target, p.customer_id, p.customer_group_code, p.campaign_name)
            currency = p.currency.upper()
            for e in existing:
                if (_same(e.get("ItemCode"), p.item_code) and _same(e.get("TGroupName"), who)
                        and _same(e.get("CurrencyCode") or currency, currency)
                        and _overlaps(start, p.end_date, _day(e.get("StartDate")), _day(e.get("EndDate")))):
                    raise ToolError(f"{p.item_code} already has price {e.get('Price0')} {e.get('CurrencyCode')} for "
                                    f"{who or 'the campaign'} from {_day(e.get('StartDate'))} to "
                                    f"{_day(e.get('EndDate')) or 'no end'}; change it in Merit or pick another period")
            row = {"TargetGroup": {"campaign": 0, "customer": 1, "customer_group": 2}[p.target], "TargetName": who,
                   "ItemCode": p.item_code, "Price": p.price, "CurrencyCode": currency,
                   "DiscountAllowed": p.discount_allowed, "StartDate": _ymd(start)}
            if p.end_date:
                row["EndDate"] = _ymd(p.end_date)
            rows.append(row)
            shown.append({"item": p.item_code, "item_name": known[p.item_code].get("Name"), "price": p.price,
                          "currency": currency, "for": f"{p.target.replace('_', ' ')} {who}".strip(),
                          "from": start.isoformat(), "to": p.end_date.isoformat() if p.end_date else "no end",
                          "discounts_allowed": p.discount_allowed})
        payload = {"Prices": rows}
        if (gate := _gate("create_sales_prices", payload, dry_run, {"prices": shown})) is not None:
            return gate
        _ok(await merit.call("v2/sendprices", payload, text_ok=True), "prices")
        return {"written": True, "prices": len(rows)}

    @mcp.tool(annotations=WRITE)
    async def merit_create_discounts(
        discounts: Annotated[list[DiscountRow], Field(min_length=1, max_length=100)],
        dry_run: DryRun = False,
    ) -> dict:
        """Set discount percentages for a customer or customer group, on one item or a whole item
        group. Refused if the same discount target already has one in an overlapping period. dry_run=true returns the preview only."""
        today = date.today()
        existing = _rows_of(await merit.call("v2/getdiscounts", {}))
        rows, shown = [], []
        for d in discounts:
            if bool(d.item_code) == bool(d.item_group_code):
                raise ToolError("give exactly one of item_code or item_group_code")
            if d.item_code:
                on, kind = (await item_by_code(d.item_code))["Code"], 1
                on_name = f"item {on}"
            else:
                on, kind = (await group("item", d.item_group_code))["Code"], 2
                on_name = f"item group {on}"
            start = d.start_date or today
            if d.end_date and d.end_date < start:
                raise ToolError(f"{on}: end_date is before start_date")
            who = await target_name(d.target, d.customer_id, d.customer_group_code)
            for e in existing:
                if (_same(e.get("GroupCode"), on) and _same(e.get("TGroupName"), who)
                        and _overlaps(start, d.end_date, _day(e.get("StartDate")), _day(e.get("EndDate")))):
                    raise ToolError(f"{who} already has a {e.get('Discount0')}% discount on {on_name} from "
                                    f"{_day(e.get('StartDate'))} to {_day(e.get('EndDate')) or 'no end'}")
            row = {"TargetGroup": 1 if d.target == "customer" else 2, "TargetName": who, "GroupType": kind,
                   "GroupCode": on, "DiscountPrc": d.discount_pct, "StartDate": _ymd(start)}
            if d.end_date:
                row["EndDate"] = _ymd(d.end_date)
            rows.append(row)
            shown.append({"discount_pct": d.discount_pct, "on": on_name, "for": f"{d.target.replace('_', ' ')} {who}",
                          "from": start.isoformat(), "to": d.end_date.isoformat() if d.end_date else "no end"})
        payload = {"Discounts": rows}
        if (gate := _gate("create_discounts", payload, dry_run, {"discounts": shown})) is not None:
            return gate
        _ok(await merit.call("v2/senddiscounts", payload, text_ok=True), "discounts")
        return {"written": True, "discounts": len(rows)}
