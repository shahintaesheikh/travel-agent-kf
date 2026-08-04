# omkar_airbnb cassettes — lane A2

**Hand-authored, not recorded.** Live recording costs money and requires
approval (AGENTS.md §0). Field shapes come from the observed S0.1 spike
responses (PRD Appendix D) plus the `omkar-airbnb` skill; the spike confirmed
the dated `booking_url`, the null `nightly_rate` on 2027 dates, and the HTTP 500
on dated details, so the traps these cover are real rather than assumed.

Two values are deliberately synthetic and worth knowing about:

- `1447073736891652317`'s breakdown label reads `"4 nights x $99.99"` against an
  amount of `229.68`. The real response's label agreed with its amount, which
  would let a label-parsing bug pass. They disagree here on purpose.
- Listing `222` reproduces the vendor's own discount sample
  (`361.43` charge, `30.46` discount, `nightly_rate: 331`).

Filenames are the `args_hash` computed by `CassetteTransport` over
`{url, method, params, body}`. The `API-Key` header is not part of the hash.

`body` joined the digest when the trunk added POST support to the transport,
which changed every filename here — these are GETs with no body, but `None`
still participates. A hand-authored cassette has no `request` block to match
on, so the only way to re-derive a name is to fingerprint the request the
adapter actually builds. If a rename ever looks needed again, spy on
`request_fingerprint` rather than guessing the params dict by hand.

| Cassette | Covers |
|---|---|
| `05996404…` | Dated Tokyo search — the mislabeled `nightly_rate`, a null-price listing, and a `booking_url` whose dates did not land |
| `3f5bb87e…` | Dated details → HTTP 500, the known upstream failure |
| `7152c382…` | Undated details → 200, the fallback path (`dates_applied=False`) |
| `c1627b3a…` | Dated details that succeeds and reports `is_available: false` with a reason |
