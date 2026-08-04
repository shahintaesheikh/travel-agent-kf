# serpapi_hotels cassettes — lane A2

**Hand-authored, not recorded.** Live recording costs money and requires
approval (AGENTS.md §0), so these were written from the `serpapi-google-hotels`
skill's documented shapes. No Google Hotels call was made during the S0.1 spike
(PRD Appendix D covers flights and Airbnb only), so unlike the Airbnb
cassettes, nothing here has been checked against a live response.

**Re-record before trusting the normalizer against production data.** If a real
response disagrees with these shapes, surface the discrepancy rather than
quietly reshaping the normalizer around it.

Filenames are the `args_hash` computed by `CassetteTransport` over
`{url, method, params, body}`. `api_key` is the fixed sentinel `REPLAY` under
`PROVIDER_MODE=replay` so the hashes are identical on every machine.

`body` joined the digest when the trunk added POST support to the transport,
which changed every filename here — these are GETs with no body, but `None`
still participates. A hand-authored cassette has no `request` block to match
on, so the only way to re-derive a name is to fingerprint the request the
adapter actually builds. If a rename ever looks needed again, spy on
`request_fingerprint` rather than guessing the params dict by hand.

| Cassette | Covers |
|---|---|
| `6838a850…` | Tokyo search — exercises `ads[]` exclusion, `rate_per_night` vs `total_rate`, a property with no price, and a property with no link |
| `9727fdc6…` | Exact-name query returning property details at the top level (`hotels_results_state`) |
| `b8802451…` | `property_token` details lookup — `resolve()` and `reprice()` |
