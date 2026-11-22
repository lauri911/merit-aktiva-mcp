"""Whose books a server holds, for when several Merit companies are connected side by side.

Only with MERIT_COMPANY_NAME set:
- every write tool takes a required `company` argument and refuses a call whose company is not this
  server's, so a change meant for another company is not written here;
- every write tool's preview, result and refusal names the company first;
- with MERIT_ASK_COMPANY=true, Claude is told to ask the user which company a change is for when
  they have not said, instead of taking it from this server's name.
"""

import re
import unicodedata
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.server.transforms import Transform
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.tools.tool_transform import forward
from pydantic import Field

# Legal forms, as written at the start or end of a name (accents and dots removed).
LEGAL_FORMS = {"ou", "as", "mtu", "sa", "fie", "tu", "uu", "ab", "oy", "oyj", "sia", "uab", "ltd", "llc",
               "inc", "gmbh", "bv", "sl", "sro", "spzoo", "plc", "ооо", "ао", "пао", "зао", "оао"}


def _parts(name: str) -> tuple[str, frozenset[str]]:
    """(the name without its legal form, the legal form): case, accents and punctuation ignored,
    letters of any script kept."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    plain = re.sub(r"\bsp\s*z\s*oo\b", "spzoo", plain.casefold().replace(".", ""))
    words = re.findall(r"[^\W_]+", plain)
    forms = set()
    while words and words[0] in LEGAL_FORMS:
        forms.add(words.pop(0))
    while words and words[-1] in LEGAL_FORMS:
        forms.add(words.pop())
    return " ".join(words), frozenset(forms)


def same_company(a: str, b: str) -> bool:
    """The same name, ignoring case, accents and punctuation. The legal form (OÜ, AS, Ltd ...) may be
    left out on either side, but two different legal forms are two different companies."""
    (name_a, form_a), (name_b, form_b) = _parts(a), _parts(b)
    return bool(name_a) and name_a == name_b and (not form_a or not form_b or form_a == form_b)


def is_write(tool: Tool | None) -> bool:
    return bool(tool and tool.annotations and tool.annotations.destructive_hint)


def instructions(company: str, ask: bool, allow_write: bool) -> str:
    text = (f"These are the books of {company}, and only theirs: for another company, use that company's "
            "own Merit server, or tell the user it is not connected.")
    if not allow_write:
        return text + "\n\n"
    text += (" Every write tool takes `company`: the company the change is for; a call for any other "
             "company is refused.\n")
    if ask:
        text += (f"Before any write, the user must have named the company in this conversation. If they "
                 f"have not, ask them which company it is for (show the dry_run preview) instead of "
                 f"assuming {company} because this server is connected.\n")
    return text + "\n"


class CompanyArgument(Transform):
    """Adds the required `company` argument to every write tool and refuses other companies."""

    def __init__(self, company: str, ask: bool):
        self.company = company
        description = ("The company whose books this change is for, as the user means it. Refused unless "
                       "it is this server's company.")
        if ask:
            description += (" Take it from what the user said in this conversation; if they have not named "
                            "the company, ask them first. Do not fill it in from this server's name or instructions.")

        async def check(company: Annotated[str, Field(description=description)] = "", **kwargs):
            if not company.strip():
                raise ToolError("`company` is required: which company's books is this change for? Nothing was written.")
            if not same_company(company, self.company):
                raise ToolError(
                    f"wrong company: this call is for {company!r}, but this server holds the books of "
                    f"{self.company}. Nothing was written. Use {company}'s own Merit server; if the user "
                    f"meant {self.company}, confirm it with them first.")
            return await forward(**kwargs)

        self.check = check
        self.made: dict[str, tuple[Tool, Tool]] = {}  # tool name → (original, with company)

    def _with_company(self, tool: Tool | None) -> Tool | None:
        if not is_write(tool):
            return tool
        made = self.made.get(tool.name)
        if made is None or made[0] is not tool:
            changed = Tool.from_tool(tool, transform_fn=self.check)
            schema = dict(changed.parameters)
            schema["required"] = ["company", *[r for r in schema.get("required", []) if r != "company"]]
            made = self.made[tool.name] = (tool, changed.model_copy(update={"parameters": schema}))
        return made[1]

    async def list_tools(self, tools):
        return [self._with_company(t) for t in tools]

    async def get_tool(self, name, call_next, *, version=None):
        return self._with_company(await call_next(name, version=version))


class CompanyOnWrites(Middleware):
    """Puts "company" first in every write tool's preview and result, and in front of its refusals,
    so the user sees whose books a change goes into before confirming it."""

    def __init__(self, company: str, mcp: FastMCP):
        self.company = company
        self.mcp = mcp
        self.writes: dict[str, bool] = {}

    async def _is_write(self, name: str) -> bool:
        if name not in self.writes:
            self.writes[name] = is_write(await self.mcp.get_tool(name))
        return self.writes[name]

    async def on_call_tool(self, context, call_next):
        write = await self._is_write(context.message.name)
        try:
            result = await call_next(context)
        except ToolError as e:
            if write:
                raise ToolError(f"{self.company}: {e}") from e
            raise
        if not write or result.is_error or not isinstance(result.structured_content, dict):
            return result
        named = ToolResult(structured_content={"company": self.company, **result.structured_content},
                           meta=result.meta)
        named.content += [c for c in result.content if c.type != "text"]  # keep any files
        return named


def apply(mcp: FastMCP, company: str, ask: bool) -> None:
    mcp.add_transform(CompanyArgument(company, ask))
    mcp.add_middleware(CompanyOnWrites(company, mcp))
