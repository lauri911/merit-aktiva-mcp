# Safety

The write tools make real bookkeeping entries in your Merit company. A wrong invoice, payment or
ledger entry misstates your books until someone corrects it, and an invoice sent by email cannot be
recalled. This is what protects you.

**Writing is off by default.** Without `MERIT_ALLOW_WRITE=true` (or "Allow writing" in the Claude
Desktop extension) the write tools don't exist; Claude can only read.

**Your Claude app asks first.** The write tools are marked as changing data, so Claude Desktop and
claude.ai ask before each call unless you choose "Always allow" for that tool. Leave them on asking
if you want to see every change.

**Dry runs.** Every write tool takes `dry_run=true`: it does every lookup and check and returns
exactly what would be written (customer, rows, VAT, totals, the number it would get), and writes
nothing. Claude is told to use it whenever you haven't clearly asked for that exact change.

**There is no separate approval step.** Deciding when to check with you is Claude's job, the same
as for anything else it does on your behalf. If you want a person to approve every write, keep
your Claude app asking before each call.

**Checks that always run.** The tools refuse what Merit itself would let through:
- a bill number the vendor already has in Merit (booked or waiting for approval), or a sales invoice
  number already used;
- a new customer or vendor whose name or registry code exists (Merit would silently reuse the old card);
- paying more than an invoice's unpaid amount, or in another currency, unless explicitly allowed;
- deleting or replacing an invoice that has payments or was already sent (credit it instead);
- VAT on a bill from a vendor not registered for VAT (Merit would book it as cost);
- a ledger entry whose debits and credits don't balance;
- and after writing, totals are read back from Merit and compared with what was meant.

**Logs.** Every write is logged (`WRITE <action>: <payload>`) to the server's log (stderr in local
mode, the container log on a server).

**Your API key.** It gives full access to the company's books through the API. In local mode it
sits in your Claude configuration (the Desktop extension stores it in your system's keychain);
on a server, keep it in a secrets file readable only by the server. Anyone or any program that can
read it can use Merit's API directly, with or without this server. Merit lets you delete a key and
make a new one under Seadistused → API seadistus.

**Files in local mode.** With writing on, tools that take a file (a vendor's PDF, a bank
statement, an e-invoice) accept `file_path`, a path on your computer. Claude can then read any
`.pdf` or `.xml` file your user account can read and send it to Merit as an attachment or import.
Keep Claude asking before write calls if that matters to you, and remember that invoice texts and
files are written by other people: the server tells Claude never to follow instructions inside them.

**Server mode sign-in.** Only the email addresses in `ALLOWED_EMAILS` get in, checked on every
request, on top of whatever your Microsoft or Google app allows.
