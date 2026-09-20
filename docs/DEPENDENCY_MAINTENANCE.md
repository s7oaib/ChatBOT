# Dependency maintenance record

## Known test warning

The focused and full local test runs currently emit one non-blocking warning:

`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.`

Evidence:

- FastAPI `TestClient` re-exports Starlette's test client.
- The installed Starlette test client prefers the upstream-compatible
  `httpx2` package and falls back to `httpx` with the warning.
- Production application code uses `httpx` directly for provider and Meta
  calls; this warning is from the test-client compatibility layer.

The repository currently declares `httpx>=0.27.0` and does not declare
`httpx2`. No broad dependency upgrade or unverified replacement was made in
this maintenance pass. Do not suppress the warning or convert it to xfail.

## Follow-up operator/dependency action

When the dependency publisher and the project's supported FastAPI/Starlette
combination are verified, evaluate a small test-dependency change in an
isolated branch. Confirm that the replacement supports the current
`TestClient` behavior and the full suite before changing `requirements.txt`.
