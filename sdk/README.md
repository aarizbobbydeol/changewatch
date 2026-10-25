# ChangeWatch Python SDK

Reports errors, slow requests and a request count per minute to a self-hosted
[ChangeWatch](https://github.com/aarizbobbydeol/changewatch) server, so it can tell you within
minutes whether a deploy broke something. No dependencies, Python 3.9 or newer.

```bash
pip install "git+https://github.com/aarizbobbydeol/changewatch.git#subdirectory=sdk"
```

```python
import os
import changewatch
from changewatch import ASGIMiddleware
from fastapi import FastAPI

cw = changewatch.init(
    api_key=os.environ["CHANGEWATCH_API_KEY"],   # cw_ingest_...
    release=os.environ.get("GIT_SHA"),           # ties events to the deploy
    endpoint="https://changewatch.example.com",
)

app = FastAPI()
app.add_middleware(ASGIMiddleware, client=cw)   # errors, latency, heartbeats
```

Optional: `capture_logs=True` attaches the app's last 50 log lines (WARNING and above, or
`log_level=`) to each error; `capture_logged_errors=True` also reports exceptions that are caught
and only logged with `logger.exception(...)`.

The SDK never raises into your app and never blocks a request: events go into a bounded queue
that a background thread sends in batches. Secrets and personal data (emails, IPs, tokens) are
removed before anything leaves the process. `CHANGEWATCH_DISABLED=1` turns it off.
