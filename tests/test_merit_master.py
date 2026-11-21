"""Master data writes (merit_master.py): each goes through the approval gate, sends exactly the
documented payload, and refuses codes or names that already exist."""

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from merit_aktiva_mcp import merit_master, merit_write

VAT24 = {"Id": "11111111-1111-1111-1111-111111111111", "Code": "24", "Name": "24% käibemaks", "TaxPct": 24.0,
         "NonActive": False}
VAT0 = {"Id": "22222222-2222-2222-2222-222222222222", "Code": "0", "Name": "0% käibemaks", "TaxPct": 0.0,
        "NonActive": False}
ITEM = {"ItemId": "33333333-3333-3333-3333-333333333333", "Code": "CONS", "Name": "Consulting", "SalesPrice": 100.0,
        "DiscountPct": 0.0, "SalesAccountCode": "3000", "VatTaxName": "24% käibemaks", "ItemGroupName": "Services",
        "UnitofMeasureName": "h"}


class FakeMerit:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.items = [dict(ITEM), {"ItemId": "99999999-9999-9999-9999-999999999999", "Code": "mat", "Name": "Materjal"}]
        self.answers = {
            "v2/taxes": [VAT24, VAT0],
            "v2/accounts": [{"Code": "3000", "Name": "Müügitulu"}, {"Code": "4000", "Name": "Kulud"},
                            {"Code": "9999", "Name": "Old", "NonActive": True}],
            "v2/getitemgroups": [{"Code": "SRV", "Name": "Services", "Id": "g1"}, {"Code": "LIC", "Name": "Licences", "Id": "g2"}],
            "v2/getcustomergroups": [{"Code": "VIP", "Name": "VIP clients", "Id": "c1"}],
            "v2/getvendorgroups": [],
            "v2/units": [{"Code": "tundi", "Name": "h"}, {"Code": "tk", "Name": "tk"}],
            "v2/getdimensions": [{"DimId": 1, "DimName": "Projekt", "Id": "d1", "Code": "P1", "Name": "Alpha",
                                  "EndDate": "1901-01-01T00:00:00"}],
            "v2/getprices": [{"ItemCode": "mat", "TGroupName": "VIP clients", "CurrencyCode": "EUR", "Price0": 90,
                              "StartDate": "2026-01-01T00:00:00", "EndDate": "1901-01-01T00:00:00"}],
            "v2/getdiscounts": [],
            "v2/senditems": [{"ItemId": "44444444-4444-4444-4444-444444444444", "Code": "AUDIT"}],
            "v1/updateitem": "OK",
            "v2/senditemgroups": [{"Code": "HW", "Id": "55555555-5555-5555-5555-555555555555"}],
            "v2/sendcustomergroup": {"Code": "NEW", "Name": "New ones", "Id": "66666666-6666-6666-6666-666666666666"},
            "v2/senduom": "77777777-7777-7777-7777-777777777777",
            "v2/sendtax": {"CreatedTaxId": "88888888-8888-8888-8888-888888888888"},
            "v2/sendprices": "OK",
            "v2/senddiscounts": "OK",
            "v2/senddimvalues": "OK",
            "v1/getcustomers": [{"CustomerId": "c-1", "Name": "Acme OÜ"}],
        }

    async def call(self, path, payload=None, method="POST", text_ok=False):
        self.calls.append((path, payload))
        if path == "v1/getitems":
            if payload.get("Id"):
                return [i for i in self.items if i["ItemId"] == payload["Id"]]
            return [i for i in self.items if payload.get("Code", "").lower() in i["Code"].lower()]
        if path == "v1/updateitem":
            item = next(i for i in self.items if i["ItemId"] == payload["Id"])
            item.update({"Code": payload.get("Code", item["Code"]), "Name": payload.get("Description", item["Name"]),
                         "SalesPrice": payload.get("SalesPrice", item.get("SalesPrice"))})
        if path == "v2/senditems":
            self.items.append({"ItemId": "44444444-4444-4444-4444-444444444444", "Code": "AUDIT", "Name": "Audit"})
        if path == "v2/senddimvalues":
            for d in payload["Dimensions"]:
                self.answers["v2/getdimensions"].append({"DimId": d["DimId"], "Code": d["DimValueCode"]})
        return self.answers[path]

    def writes(self) -> list[tuple[str, object]]:
        return [(p, b) for p, b in self.calls if "send" in p or "update" in p]


@pytest.fixture
def env():
    fake = FakeMerit()
    books = merit_write.Books(fake)
    mcp = FastMCP("test")
    merit_master.register_master(mcp, fake, books, None)

    def call(tool, **args):
        async def run():
            async with Client(mcp) as client:
                return (await client.call_tool(tool, args)).structured_content
        return asyncio.run(run())

    return SimpleNamespace(fake=fake, books=books, call=call)


def twice(env, tool, **args):
    """A dry run, which must write nothing, then the real call."""
    first = env.call(tool, **args, dry_run=True)
    assert first["written"] is False and first["dry_run"] is True
    assert env.fake.writes() == []
    return first, env.call(tool, **args)


def test_create_item_refuses_units_merit_cannot_take(env):
    # Live 2026-11-21: senditems answers HTTP 500 for units whose code equals their name (tk, kg, km, ltr).
    with pytest.raises(ToolError, match="Merit fails to create an item with unit 'tk'"):
        env.call("merit_create_items", items=[{"code": "NEW", "description": "New", "unit": "tk"}])
    with pytest.raises(ToolError, match="no unit 'pcs'"):
        env.call("merit_create_items", items=[{"code": "NEW", "description": "New", "unit": "pcs"}])


def test_create_item_sends_documented_payload_and_sets_price(env):
    env.books._cache["items"] = (9e18, {"CONS": ITEM})
    item = {"code": "AUDIT", "description": "Audit", "type": "service", "vat": "24", "group_code": "srv",
            "sales_account": "3000", "unit": "h", "sales_price": 250}
    first, out = twice(env, "merit_create_items", items=[item])
    assert first["preview"]["new_items"][0]["vat"] == "24% käibemaks (24%)"
    assert out["written"] is True and out["items"][0]["code"] == "AUDIT"
    writes = env.fake.writes()
    assert writes[0] == ("v2/senditems", {"Items": [{"Type": 2, "Usage": 3, "Code": "AUDIT", "Description": "Audit",
                                                     "TaxId": VAT24["Id"], "ItemGrCode": "SRV", "UOMName": "h",
                                                     "SalesAccCode": "3000"}]})
    assert writes[1][0] == "v1/updateitem" and writes[1][1]["SalesPrice"] == 250
    assert "items" not in env.books._cache


def test_create_item_refuses_existing_code_and_notes_a_shared_name(env):
    with pytest.raises(ToolError, match="already exists"):
        env.call("merit_create_items", items=[{"code": "cons", "description": "New"}])
    first = env.call("merit_create_items", items=[{"code": "NEW", "description": "consulting"}], dry_run=True)
    assert "already has the name" in first["preview"]["notes"][0]
    with pytest.raises(ToolError, match="not active"):
        env.call("merit_create_items", items=[{"code": "NEW", "description": "New", "sales_account": "9999"}])


def test_update_item_resends_current_values_with_the_change(env):
    first, out = twice(env, "merit_update_item", item_code="CONS",
                       changes={"sales_price": 120, "description": "Consulting hour"})
    assert first["preview"]["changes"]["SalesPrice"] == {"from": 100.0, "to": 120.0}
    assert env.fake.writes() == [("v1/updateitem", {
        "Code": "CONS", "Description": "Consulting hour", "SalesPrice": 120.0, "DiscountPct": 0.0,
        "SalesAccountCode": "3000", "TaxId": VAT24["Id"], "ItemGrCode": "SRV", "Id": ITEM["ItemId"]})]
    assert out == {"written": True, "item_id": ITEM["ItemId"], "verified": True}


def test_update_item_refuses_taken_code(env):
    env.fake.items.append({"ItemId": "x", "Code": "OTHER", "Name": "Other"})
    with pytest.raises(ToolError, match="already has code"):
        env.call("merit_update_item", item_code="CONS", changes={"code": "other"})


def test_create_groups(env):
    _, out = twice(env, "merit_create_group", kind="item", code="HW", name="Hardware")
    assert env.fake.writes() == [("v2/senditemgroups", {"ItemGroups": [{"Code": "HW", "Name": "Hardware"}]})]
    assert out["group_id"] == "55555555-5555-5555-5555-555555555555"
    env.fake.calls.clear()
    twice(env, "merit_create_group", kind="customer", code="NEW", name="New ones")
    assert env.fake.writes() == [("v2/sendcustomergroup", {"Code": "NEW", "Name": "New ones"})]
    with pytest.raises(ToolError, match="already exists"):
        env.call("merit_create_group", kind="customer", code="X", name="vip clients")


def test_create_unit(env):
    _, out = twice(env, "merit_create_unit", name="kuu", name_en="month")
    assert env.fake.writes() == [("v2/senduom", {"Name": "kuu", "NameEN": "month"})]
    assert out["unit_id"] == "77777777-7777-7777-7777-777777777777"
    with pytest.raises(ToolError, match="already exists"):
        env.call("merit_create_unit", name="H")


def test_dimension_values_add_and_close(env):
    _, out = twice(env, "merit_send_dimension_values", dimension_id=1,
                   values=[{"code": "P2", "name": "Beta"}, {"code": "p1", "end_date": "2026-12-31"}])
    assert env.fake.writes() == [("v2/senddimvalues", {"Dimensions": [
        {"DimId": 1, "DimValueCode": "P2", "DimValueName": "Beta"},
        {"DimId": 1, "DimValueCode": "p1", "DimValueId": "d1", "DimValueName": "Alpha", "EndDate": "20261231"}]})]
    assert out["verified"] is True
    with pytest.raises(ToolError, match="already has value"):
        env.call("merit_send_dimension_values", dimension_id=1, values=[{"code": "P1", "name": "Alpha again"}])


def test_create_tax_and_invalidate_cache(env):
    _, out = twice(env, "merit_create_tax", country_code="fi", tax_pct=25.5, code="FI25.5", name="Soome OSS 25,5%")
    assert env.fake.writes() == [("v2/sendtax", {"Code": "FI25.5", "Name": "Soome OSS 25,5%", "TaxPct": 25.5,
                                                 "TaxType": 12, "CountryCode": "FI"})]
    assert out["tax_id"] == "88888888-8888-8888-8888-888888888888"
    assert "taxes" not in env.books._cache
    with pytest.raises(ToolError, match="already exists"):
        env.call("merit_create_tax", country_code="FI", tax_pct=24, code="24", name="x")
    with pytest.raises(ToolError, match="other EU countries"):
        env.call("merit_create_tax", country_code="EE", tax_pct=24, code="X", name="x")


def test_sales_price_for_customer_and_overlap_refused(env):
    twice(env, "merit_create_sales_prices", prices=[{"item_code": "mat", "price": 95, "target": "customer",
                                                       "customer_id": "c-1", "start_date": "2026-12-01"}])
    assert env.fake.writes() == [("v2/sendprices", {"Prices": [
        {"TargetGroup": 1, "TargetName": "Acme OÜ", "ItemCode": "mat", "Price": 95.0, "CurrencyCode": "EUR",
         "DiscountAllowed": True, "StartDate": "20261201"}]})]
    with pytest.raises(ToolError, match="capital letters"):  # Merit's sendprices cannot find such codes
        env.call("merit_create_sales_prices", prices=[{"item_code": "CONS", "price": 95, "target": "customer",
                                                       "customer_id": "c-1"}])
    with pytest.raises(ToolError, match="already has price"):
        env.call("merit_create_sales_prices", prices=[{"item_code": "mat", "price": 80, "target": "customer_group",
                                                       "customer_group_code": "VIP", "start_date": "2027-01-01"}])


def test_discount_on_item_group(env):
    twice(env, "merit_create_discounts", discounts=[{"discount_pct": 10, "target": "customer_group",
                                                     "customer_group_code": "vip", "item_group_code": "LIC",
                                                     "start_date": "2026-12-01", "end_date": "2026-12-31"}])
    assert env.fake.writes() == [("v2/senddiscounts", {"Discounts": [
        {"TargetGroup": 2, "TargetName": "VIP clients", "GroupType": 2, "GroupCode": "LIC", "DiscountPrc": 10.0,
         "StartDate": "20261201", "EndDate": "20261231"}]})]
    with pytest.raises(ToolError, match="exactly one"):
        env.call("merit_create_discounts", discounts=[{"discount_pct": 5, "target": "customer", "customer_id": "c-1"}])


