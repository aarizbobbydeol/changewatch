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

The SDK never raises into your app and never blocks a request: events go into a bounded queue
that a background thread sends in batches. Secrets and personal data (emails, IPs, tokens) are
removed before anything leaves the process. `CHANGEWATCH_DISABLED=1` turns it off.
