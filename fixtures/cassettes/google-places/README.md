# google-places cassettes

**These are synthetic, not recorded.** Live Places calls are billed and need
approval (AGENTS.md §0), so lane A3 hand-built these payloads from the response
shape documented in the `google-places-api` skill rather than recording them.

They are correct in shape, not in content. Ratings, phone numbers and place IDs
are plausible fillers. Re-record them against the live API at the next approved
recording session — a stale or invented cassette hides upstream schema drift,
which is the failure mode cassettes otherwise protect against.

## What each one covers

| Request | Covers |
|---|---|
| Text Search `restaurants in Lisbon` | full record, thin record, `PRICE_LEVEL_FREE`, and one record with null geometry that must be dropped |
| Text Search `Time Out Market, Lisbon` | geocode with a locality prior — one clear winner |
| Text Search `Starbucks` | geocode with no locality prior — three equally good chain matches |
| Place Details `ChIJc7cVJl40GQ0RA0RiA1YkFRk` | bare (non-`places.`-prefixed) field mask |
| Nearby Search | reverse geocode |

Every record carries both `name` (the resource path `places/PLACE_ID`) and
`displayName` (the title), so a normalizer that reads the legacy field fails in
the tests rather than in production.

## Filenames

The filename is `request_fingerprint()` from `app/shared/cassettes.py` — method,
base URL, credential-stripped params, and the request body. The body matters
here: Text Search is a POST with no query parameters, so without it every
Places query would collapse onto one file. Changing a request body means a new
filename — regenerate rather than editing a file in place.

## Before recording these live

`X-Goog-Api-Key` is **not** in the trunk's `_CREDENTIAL_NAMES`, so a recorded
cassette writes the Places key into `request.headers` verbatim — and these files
are committed. Raised with the trunk lane. Do not run a live record session for
this provider until that redaction covers the header.
