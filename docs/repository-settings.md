# Repository settings

The built-in deployment supports one control-plane process and one journal
writer. CI runs these jobs on pushes:

- `check`
- `images`
- `kind-e2e`

Future release tags should be annotated and, where the organization supports
it, signed. The v1.0.1 tag is historical and must not be recreated or moved.

This CI setup does not provide API identity, multi-user authorization, or
multi-replica journal-writer safety.
