# Approved Q&A source mapping

Instagram Story/Post/Reel mappings have response_mode CUSTOM (the additive default) or APPROVED_QA with a nullable approved_qa_id. Migration adds columns without rewriting existing operator content. Switching modes retains historical custom description; CUSTOM clears an unused QA reference. Approved mode never accepts copied QA text.

The authenticated selector reads the effective Approved Q&A authority dynamically, including current overrides and validity/archive lifecycle. Search covers question, stable ID and available variations. Preview is read-only and shows exact answer and canonical ordered asset/link slots. Save rejects missing, unknown or non-effective IDs.

A narrow generic source-reference whitelist resolves by stable ID, then uses the same canonical response/block/action path as normal Approved Q&A. Explicit product, price, payment and other independently understood intents retain normal precedence. Invalid/unavailable mapped references fail closed before custom/topic/static fallback. Strict overlay reads prevent a storage failure from silently selecting an old static mapped answer.

Home Table delivery uses QA-SM1.1 unchanged: each new inbound request gets an independent delivery; retries of the same job retain generation, positions and receipts. order_ready_handoff keeps the existing human escalation flag and action path. Canonical assets are resolved at runtime; route menu selection does not override Approved Q&A assets.

Production stays TEST_ONLY with the existing allowlist. Mapping endpoints retain existing Admin authentication and CSRF enforcement. No native MEDIA_SHARE, WhatsApp automation, provider/configuration changes or Approved Q&A wording edits are included.
