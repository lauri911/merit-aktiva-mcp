"""Customer and vendor cards (merit_write.py): the extra card fields map to Merit's names, dimension
values and groups are resolved and checked, an update keeps the card's group, and fields Merit's card
list does not show are not claimed as verified."""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from merit_aktiva_mcp import merit_write

CUST = "c0000000-0000-0000-0000-000000000001"
VEND = "v0000000-0000-0000-0000-000000000001"
DIM_VALUE = "d0000000-0000-0000-0000-000000000001"


class FakeMerit:
    def __init__(self):
        self.calls = []
        self.customer = {"CustomerId": CUST, "Name": "Acme OÜ", "CountryCode": "EE", "RegNo": "10000001",
                         "CustomerGroupName": "VIP clients", "Email": "arved@acme.ee"}
        self.vendor = {"VendorId": VEND, "Name": "Tarnija OÜ", "CountryCode": "EE", "VatAccountable": True,
                       "VendorGroupName": "IT"}

    async def call(self, path, payload=None, method="POST", text_ok=False):
        self.calls.append((path, payload))
        if path == "v1/getcustomers":
            return [self.customer] if payload.get("Id") == CUST else []
        if path == "v1/getvendors":
            return [self.vendor] if payload.get("Id") == VEND else []
        if path == "v2/getcustomergroups":
            return [{"Code": "VIP", "Name": "VIP clients"}, {"Code": "STD", "Name": "Standard"}]
        if path == "v2/getvendorgroups":
            return [{"Code": "IT", "Name": "IT"}]
        if path == "v2/getdimensions":
            return [{"DimId": 1, "DimName": "Projekt", "Id": DIM_VALUE, "Code": "P1", "Name": "Alpha"},
                    {"DimId": 1, "DimName": "Projekt", "Id": "closed", "Code": "P0", "Name": "Old", "NonActive": True}]
        if path == "v2/sendcustomer":
            return {"Id": "new-id", "Name": payload["Name"]}
        if path == "v1/updatecustomer":
            self.customer.update({k: v for k, v in payload.items() if k in ("Email",)})
            return "OK"
        if path == "v2/updatevendor":
            return "OK"
        raise AssertionError(path)

    def sent(self, path):
        return [b for p, b in self.calls if p == path]


def twice(fake, tool, args):
    """A dry run that writes nothing, then the real call."""
    mcp = FastMCP("test")
    merit_write.register_write(mcp, fake, None, merit_write.Books(fake))

    async def run():
        async with Client(mcp) as c:
            first = (await c.call_tool(tool, {**args, "dry_run": True})).structured_content
            assert first["written"] is False and not [p for p, _ in fake.calls if "send" in p or "update" in p]
            second = (await c.call_tool(tool, args)).structured_content
            return first, second
    return asyncio.run(run())


def test_new_customer_sends_group_dimensions_and_einvoice_fields():
    fake = FakeMerit()
    _, out = twice(fake, "merit_create_customer", {"customer": {
        "name": "Uus OÜ", "reg_no": "10000002", "group_code": "std", "ref_no_base": "77", "einvoice_operator": 3,
        "payer_name": "Ema OÜ", "bank_account": "EE001", "dimensions": [{"dim_id": 1, "value_code": "p1"}]}})
    sent = fake.sent("v2/sendcustomer")[0]
    assert sent["CustGrCode"] == "STD" and sent["RefNoBase"] == "77" and sent["EInvOperator"] == 3
    assert sent["PayerReceiverName"] == "Ema OÜ" and sent["BankAccount"] == "EE001"
    assert sent["Dimensions"] == [{"DimId": 1, "DimValueId": DIM_VALUE, "DimCode": "P1"}]
    assert out["customer_id"] == "new-id"


def test_unknown_group_or_closed_dimension_value_is_refused():
    with pytest.raises(ToolError, match="no customer group"):
        twice(FakeMerit(), "merit_create_customer", {"customer": {"name": "Uus OÜ", "group_code": "nope"}})
    with pytest.raises(ToolError, match="no single active value"):
        twice(FakeMerit(), "merit_create_customer", {"customer": {
            "name": "Uus OÜ", "dimensions": [{"dim_id": 1, "value_code": "P0"}]}})


def test_customer_update_keeps_the_group_and_reports_unverifiable_fields():
    fake = FakeMerit()
    _, out = twice(fake, "merit_update_customer", {"customer_id": CUST, "changes": {
        "email": "uus@acme.ee", "einvoice_operator": 2}})
    sent = fake.sent("v1/updatecustomer")[0]
    assert sent["CustGrCode"] == "VIP"  # the card shows only the group's name
    assert sent["Email"] == "uus@acme.ee" and sent["EInvOperator"] == 2
    assert out["verified"] is False and "EInvOperator" in out["not_verifiable"]


def test_vendor_update_uses_updatevendor_names():
    fake = FakeMerit()
    twice(fake, "merit_update_vendor", {"vendor_id": VEND, "changes": {"payer_name": "Saaja", "ref_no": "123"}})
    sent = fake.sent("v2/updatevendor")[0]
    assert sent["PayerReceiverName"] == "Saaja" and sent["ReferenceNo"] == "123" and sent["VendGrCode"] == "IT"
    assert "ReceiverName" not in sent and "RefNoBase" not in sent
