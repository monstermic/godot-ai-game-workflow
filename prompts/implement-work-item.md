# Implement one work item

Load the packet from `aigame context WI-#### --json`. Restate its goal, risks, non-goals, and required evidence. If any required capability or decision is missing, emit `needs_human`. Otherwise implement only the claimed scope test-first, run deterministic and runtime validation, and checkpoint a `RunResult` before opening a PR.
