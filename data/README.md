# Runtime seed data

- `menu.json`: current structured menu and static image references.
- `qa_templates.json`: approved customer-facing answers and matching triggers.

Change these through the admin review/publish workflow when possible. They are
the factual source of truth for customer replies; do not place credentials,
customer data, or uploaded weekly flyers in this directory.

The `images/sondesh_addon_01.jpeg` through `images/sondesh_addon_03.jpeg`
files are the three client-provided, approved Sondesh add-on images from
2026-09-08. They are preserved byte-for-byte under stable filenames and are
sent in that order only for the à la carte add-on flow.
