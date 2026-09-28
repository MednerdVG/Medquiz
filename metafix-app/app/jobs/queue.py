"""Background jobs. With REDIS_URL set (and rq installed) work goes to the RQ worker;
otherwise it runs inline, which is what tests and single-process dev use."""
from __future__ import annotations

import os
from typing import Callable


def enqueue(fn: Callable, *args, **kwargs):
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            from redis import Redis
            from rq import Queue
            return Queue("metafix", connection=Redis.from_url(url)).enqueue(fn, *args, **kwargs)
        except ImportError:  # pragma: no cover
            pass
    return fn(*args, **kwargs)
