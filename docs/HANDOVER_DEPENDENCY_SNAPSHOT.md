# Validated handover dependency environment

This is the validated handover environment snapshot, not a requirements lock.

Evidence: successful [final handover CI run 36931567553](https://github.com/s7oaib/ChatBOT/actions/runs/36931567553), exact runtime `b2d2b485c6c4e8b7c35d6908b75ce489a251d4bd`, 3410 passed, zero failed. Ubuntu 24.04.5, CPython **3.11.16** (setup-python log).

The workflow did not emit `pip freeze`; exact versions below are machine-extracted from that run's successful pip installation log, not inferred from local packages or minimum constraints. All direct runtime dependencies are included. `uvicorn[standard]` extras were installed. No requirements changes or upgrades were introduced by this handover.

| Direct runtime dependency | Validated version |
| --- | --- |
| fastapi | 0.142.2 |
| uvicorn | 0.54.0 |
| httpx | 0.28.1 |
| python-dotenv | 1.2.4 |
| slowapi | 0.1.10 |
| argon2-cffi | 25.1.0 |
| python-multipart | 0.0.32 |
| pillow | 12.3.0 |
| pypdfium2 | 5.13.0 |

## Installed environment packages from CI

```text
annotated-doc==0.0.5
annotated-types==0.8.0
anyio==4.15.1
argon2-cffi==25.1.0
argon2-cffi-bindings==26.1.0
certifi==2026.7.22
cffi==2.1.1
click==8.5.0
deprecated==1.3.1
fastapi==0.142.2
flake8==7.4.1
h11==0.16.0
httpcore==1.0.9
httptools==0.8.0
httpx==0.28.1
idna==3.20
iniconfig==2.3.0
limits==5.8.0
mccabe==0.7.0
opentelemetry-api==1.45.0
packaging==26.3
pillow==12.3.0
pluggy==1.6.0
pycodestyle==2.15.0
pycparser==3.0
pydantic==2.13.5
pydantic-core==2.46.5
pyflakes==4.0.1
pygments==2.21.0
pypdfium2==5.13.0
pytest==9.1.1
pytest-asyncio==1.4.0
python-dotenv==1.2.4
python-multipart==0.0.32
pyyaml==6.0.3
slowapi==0.1.10
starlette==1.7.0
typing-extensions==4.16.0
typing-inspection==0.4.4
uvicorn==0.54.0
uvloop==0.23.0
watchfiles==1.3.0
websockets==17.1
wrapt==2.5.0
```

This records successful-install lines including test tools; it is not a complete freeze of preinstalled runner packages. Critical lint and schema checks passed in this same environment. Existing Starlette/httpx deprecation warning remains; do not replace dependencies solely to remove it during handover. Future dependency changes require independent impact review and validation.
