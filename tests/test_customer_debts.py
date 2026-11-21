"""merit_customer_debts filters by part of the customer's name, as its description says.

Merit's getcustdebtrep takes CustName as an exact (case-insensitive) customer name, and when no
customer has exactly that name it returns every customer's debts (checked against live Merit).
"""

import asyncio

from fastmcp import Client, FastMCP

from merit_aktiva_mcp import merit

DEBTS = [
    {"PartnerName": "Põhjala Laen LTD", "DocNo": "2026049", "CurrencyCode": "EUR", "UnPaidAmount": 1545.51},
    {"PartnerName": "Kask ja Kuusk OÜ", "DocNo": "2026046", "CurrencyCode": "EUR", "UnPaidAmount": 3265.34},
    {"PartnerName": "Savanna Credit (Pty) Ltd", "DocNo": "2026063", "CurrencyCode": "ZAR", "UnPaidAmount": 3495},
]
CUSTOMERS = ["Põhjala Laen LTD", "Kask ja Kuusk OÜ", "Savanna Credit (Pty) Ltd", "Nordlink AB"]


class FakeMerit:
    """getcustdebtrep as Merit answers it: CustName narrows the report only on an exact name."""

    async def call(self, path, payload=None, method="POST", text_ok=False):
        assert path == "v1/getcustdebtrep"
        name = payload["CustName"].lower()
        if any(c.lower() == name for c in CUSTOMERS):
            return [r for r in DEBTS if r["PartnerName"].lower() == name]
        return list(DEBTS)


def debts(**args) -> dict:
    mcp = FastMCP("test")
    merit.register(mcp, FakeMerit())

    async def run() -> dict:
        async with Client(mcp) as client:
            return (await client.call_tool("merit_customer_debts", args)).structured_content

    return asyncio.run(run())


def test_part_of_a_name_returns_only_that_customer():
    out = debts(customer="põhjala laen")
    assert [r["PartnerName"] for r in out["rows"]] == ["Põhjala Laen LTD"]
    assert out["totals_by_currency"] == {"EUR": {"UnPaidAmount": 1545.51}}


def test_customer_without_debts_returns_nothing():
    out = debts(customer="Nordlink")
    assert out["count"] == 0
    assert out["rows"] == []


def test_exact_name_still_works():
    assert [r["PartnerName"] for r in debts(customer="Põhjala Laen LTD")["rows"]] == ["Põhjala Laen LTD"]


def test_no_customer_returns_everyone():
    assert debts()["count"] == len(DEBTS)
