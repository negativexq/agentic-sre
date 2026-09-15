# ITBench E9 provenance errata

The historical E9 manifest records runtime source SHA
`96c3168e1f4990794032abf38a0de5c139f2b5fc`. Git resolves the matching source
commit to `96c3168757cb9832c966dca68106169acc769651`.

This is a provenance-recording defect. It does not change historical E9
predictions, scores, ledgers, or validity classification. The correspondence
was reconstructed by resolving the recorded prefix against repository history
and comparing the E9 runtime files at the resulting commit.

Future manifests must collect Git HEAD, relevant content hashes, dirty-path
status, and the complete benchmark code bundle automatically before provider
transport; a manually typed runtime SHA is insufficient.
