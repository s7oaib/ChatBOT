# Response Library

Approved Q&A remains the separate canonical business authority. Response Library
shows protected built-in SYSTEM templates and operator-managed CUSTOM records in
the existing `learned_knowledge` store. Counts include all lifecycle states; the
type, status, source, topic, and priority filters control the visible records.

System identity and triggers are read-only. Edit Response, Preview, Test, and
History retain the existing override behavior. System templates cannot be
disabled, archived, or deleted through this editor.

Custom records support full field editing at the same ID. ACTIVE records can be
disabled or archived; DISABLED records can be activated or archived; ARCHIVED
records can be viewed, restored, or permanently deleted. Reactivation and version
restore recheck normalized question/alias conflicts. Disabled and archived
records remain excluded from all existing customer-answer accessors.

Permanent deletion requires archive plus two explicit operator confirmations.
The server checks the confirmed question and lifecycle under a SQLite write lock
and blocks known conversation, triage, assisted-resolution, auto-resolution, and
RAG/routing audit references. Only the record's own aliases and version history
are removed with it. The legacy Admin delete API applies the same safeguards.

## CSV management

- Export Custom Q&A produces an editable UTF-8 BOM CSV. With no custom records,
  a header-only file is valid. Aliases are JSON string arrays.
- Export Full Library is an audit export of SYSTEM and CUSTOM records, excluding
  Approved Q&A. Protected system rows are explicitly non-importable.
- Download CSV Template provides the Custom format and a harmless example.
- Import Custom Q&A requires upload, dry-run review, and confirmation. Dry-run
  writes nothing. Confirmation validates again under a write lock and applies
  the whole file transactionally; any ERROR or CONFLICT blocks all writes.
- Blank IDs create new records. Existing numeric Custom IDs update that exact
  record. Unknown, malformed, and protected IDs are errors. Semantic similarity
  never selects an update target. Identical blank-ID rows may be skipped as
  UNCHANGED; they never update an existing record.
- Dry-run reports NEW, UPDATE, UNCHANGED, CONFLICT, ERROR, row number, target ID,
  and conflict source. Repeated imports of the same export make no changes.
- Metadata columns are accepted but ignored. Original provenance, timestamps,
  usage, and other server-maintained values cannot be supplied by the CSV.
- Formula-like cell values receive an apostrophe prefix for Excel. Original
  leading apostrophes are doubled. Import reverses this escaping, preserving
  Unicode, commas, quotes, line breaks, and literal formula-like text.

Custom questions and aliases are checked against all Custom rows, protected
system triggers, and current effective ACTIVE Approved Q&A questions and accepted
variations. A conflict returns a clear source and cannot be bypassed by warning
confirmation. All routes retain existing Admin authentication and CSRF policy.

No schema migration, duplicate cleanup, system-template retirement, business
fact changes, source-mapping changes, or production configuration changes are
part of this feature. Production acceptance uses exports and dry-run only.
