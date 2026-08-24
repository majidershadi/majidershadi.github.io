# SC4S operational examples

These files accompany the [SC4S production guide](../../articles/sc4s-production-guide/).

They are sanitized reference material, not a drop-in production configuration. They use documentation-only network ranges and placeholders instead of credentials. Before deployment, compare every option and metadata key with the documentation and `splunk_metadata.csv.example` shipped in your exact SC4S release.

The shell scripts are intentionally conservative:

- `troubleshoot-sc4s.sh` performs read-only diagnostics.
- `macvlan-example.sh` creates a Docker network and must not be run unchanged.

Never commit a real HEC token.
