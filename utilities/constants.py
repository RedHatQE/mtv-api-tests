MTV_OPERATOR_NAME: str = "mtv-operator"

# Exception messages are truncated to this length when keying must-gather deduplication.
# Messages differing only past the limit are treated as the same failure, which over-collects rather than suppresses.
MUST_GATHER_DEDUP_MESSAGE_LIMIT: int = 200
