# Examples

Show application startup `init`, ordinary model loading and attribute/container changes, then lifecycle
`shutdown`. Business methods rely on internal synchronization. Use `EmbeddedField(PetStats)` for the
usual typed independent default. Explicit fields need no `_autosave` entry. Keep examples and README
consistent with the current operator behavior and avoid exposing manager bookkeeping in business code.

Measure performance with real MongoDB and disclose workload and server settings; payload savings alone
do not establish database performance. Do not run benchmarks as routine tests of typing or documentation.
