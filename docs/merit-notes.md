# Merit Aktiva API: what the docs don't tell you

Found while building and testing this server against live Merit (November 2026). The tools already
handle all of it; this is for anyone changing them, or wondering why a tool refuses something.

## General

- **Licence.** The API answers HTTP 401 ("API kasutamiseks on vajalik kehtiv Merit Aktiva PRO või
  Premium litsents") unless the company has a PRO or Premium licence.
- **Rate limit.** 100 calls a minute. A 429 often says `Retry-After: 0` while the window is still
  full; `X-RateLimit-Reset` (unix time) is the reliable value. The client waits for it (up to a
  minute, 6 tries).
- **Periods.** List endpoints take at most 3 months per call (some less: `GetGLBatchesFull` 31 days,
  `getperinvoices` 180 days); the tools split longer periods.
- **No sandbox.** Merit's FAQ: "Just create a test company."
- **Error pages.** A payload Merit can't handle comes back as HTTP 500 with a .NET
  `NullReferenceException`, or as 200 with an HTML error page, rather than a validation message.

## Reading

- `getinvoices2`'s `InvNo` matches only an exact invoice number.
- `getcustdebtrep`'s `CustName` matches only an exact customer name and, when none matches, returns
  every customer's debts; the tool filters by part of the name itself.
- `getcustpaymrep` sends dates as `/Date(ms)/` (UTC midnight of the day) and pages with `HasMore` /
  `Id4More` through `getmoredata`.
- `getpayments` takes a `BankId` filter.
- `v2/Banks/{id}/IncomePayments`, `.../ExpensePayments` and `v2/PaymentImports` are GET, with
  yyyy-MM-dd filters in the query string.
- `getcustomers` answers HTTP 500 when `WithComments` is combined with a filter.
- `getvendors` does not return a vendor's language, payer name or e-invoice settings, and
  `getcustomers` not e-invoice settings, payer name, show-balance or grouping, so those can't be
  checked after an update (the tools say so); vendor language is not offered at all.
- `v2/accounts`: `IsParent` is 1 on every account, so it says nothing about group accounts.
- `getdimensions` lists a dimension only once it has values.
- Purchase invoices waiting for approval are visible only through `GetPOrders`, and can't be changed
  or deleted through the API.

## Writing

- **Invoices can't be edited.** An unpaid, unsent one is deleted and re-created with the same number;
  anything else gets a credit invoice.
- **Invoice numbers.** Merit doesn't hand out the next number; the tools use the highest
  `<year>NNN` number + 1 (override with `invoice_no`). `offer2inv` numbers invoices by Merit's own
  numbering setting instead.
- **Rounding.** Each row is rounded to cents, then VAT is summed per rate; send totals computed the
  same way or Merit refuses ("rows do not sum up").
- **Customer discounts.** `sendoffer` and `sendperinvoice` apply the customer's discount from Merit's
  discount list even when a row says 0%; `sendinvoice` doesn't. The offer and recurring-invoice
  tools ask `getprice` for the discount first, so the preview shows the real amount.
- **Offers.** `offer2inv` invoices only approved offers ("Arveks saab teha ainult kinnitatud dokumente").
- **Payments.** Part payments of an invoice work (Merit's FAQ says otherwise). A payment with a
  `BankId` is booked on that account even when the account has no IBAN (the docs say "cash register").
  Some banks keep one IBAN for several currencies, each a separate account in Merit (LHV's EUR and
  USD accounts, for example): the tools then send only the `BankId`, and a statement import picks
  the account by the statement's currency.
- **Items.** `senditems` answers HTTP 500 for units whose code equals their name (`tk`, `kg`, `km`,
  `ltr` in a new company); `kuu`, `tund` and units made through the API work.
- **Prices and discounts.** `sendprices` can't find items whose code has a capital letter
  ("Puudub artikkel koodiga …"); `senddiscounts` can. `senddiscounts` answers HTTP 500 to
  yyyyMMddHHmmss dates; yyyyMMdd works.
- **Dimensions.** A new dimension gets the `Id` you send as its DimId (Merit refuses one that is taken).
- **Stock.** Merit doesn't advance a stock's document counter for movements made through the API;
  the tool continues after the highest number used.
- **Fixed assets** need a location and a responsible person, and the API can't create either: add
  them in Merit (Seadistused → Põhivarade asukohad / vastutajad).
- **XML imports** (`sendcamt53`, `sendinvoicexml`, `sendpurchorderxml`): the body is the XML as a JSON
  string; the answer is a one-item list. A sales e-invoice needs `vatId` on its VAT blocks, row
  numbers and a buyer address, or Merit answers 500.
- **Sending by email** needs a sender email in Merit's e-mail settings (Seadistused →
  E-posti seadistused): otherwise "Müügiarve seadistustes saataja e-mail täitmata". Emails come from
  arve@merit.ee with the invoice PDF attached.
- **Meter readings** (`sendindvalues`) answered HTTP 500 for every recurring invoice tried; how a
  recurring invoice row is set up for readings is not known yet.
