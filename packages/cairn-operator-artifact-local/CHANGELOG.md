# Changelog

## 0.1.0

- Initial release — reference operator pack #3 of Phase 2.
- Two operators: `storage/put` (external_write) + `storage/get` (read_only).
- Local filesystem under `CAIRN_ARTIFACTS_DIR` (default `~/.cairn/artifacts`).
- Per-artifact-name subdir for collision isolation.
- Content-addressed by default: SHA-256 hex of payload is the key when none supplied.
- File:// URIs as the addressable identifier.
- Mock mode for both operators.
