# Application package

Keep production application code in this package. Module boundaries are kept
flat intentionally: endpoint, responder, database, channel adapters, menu
operations, and monitoring have direct, stable imports used by the FastAPI
entry point and test suite. New domain capabilities should be added as focused
modules here rather than scripts or root-level Python files.
