"""Opt-in public evidence collection. Never invents queries from private context."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
import time
from urllib.parse import urlencode

from .network import fetch, validate_url
from .monitor import RunCancelled


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
    def handle_data(self, text):
        if not self.skip and text.strip():
            self.parts.append(text.strip())


class Research:
    def __init__(self, config, reader=fetch):
        self.config, self.reader = config, reader

    def collect(self, options, cancel_event=None):
        result = {"enabled": options.get("web_research") is True, "sources": [], "errors": [], "requests": 0, "bytes": 0}
        if not result["enabled"]:
            return result
        queries = options.get("public_queries", [])
        urls = options.get("public_urls", [])
        if not isinstance(queries, list) or len(queries) > 3 or not all(isinstance(q, str) and 0 < len(q) <= 300 for q in queries):
            raise ValueError("公开检索词最多3条，每条1–300字")
        if not isinstance(urls, list) or len(urls) > 5:
            raise ValueError("公开页面最多5条")
        urls = list(dict.fromkeys(urls))
        for url in urls:
            validate_url(url)
        end = time.monotonic() + 60
        def read(spec):
            if cancel_event is not None and cancel_event.is_set():
                raise RunCancelled("Research cancelled")
            remaining = end - time.monotonic()
            if remaining <= 0 or result["requests"] >= 8:
                raise TimeoutError("research_budget_exhausted")
            result["requests"] += 1
            value = self.reader(spec, timeout=min(15, remaining), cancel_event=cancel_event)
            result["bytes"] += value["bytes"]
            return value
        for query in queries:
            if not os.environ.get(self.config.research.get("api_key_env", "BRAVE_SEARCH_API_KEY")):
                result["errors"].append({"query": query, "reason": "search_credential_unavailable"})
                continue
            try:
                raw = read({"url": "https://api.search.brave.com/res/v1/web/search?" + urlencode({"q": query, "count": 3}),
                    "api_key_env": self.config.research.get("api_key_env", "BRAVE_SEARCH_API_KEY"), "key_header": "X-Subscription-Token", "key_prefix": ""})
                data = json.loads(raw["text"])
                hits = data.get("web", {}).get("results", [])
                if not isinstance(hits, list):
                    raise ValueError("invalid_search_response")
                for hit in hits[:3]:
                    url = hit.get("url")
                    validate_url(url)
                    result["sources"].append({"kind": "search_snippet", "url": url, "title": str(hit.get("title", ""))[:300],
                        "excerpt": str(hit.get("description", ""))[:1000], "query": query, "collected_at": datetime.now(timezone.utc).isoformat(),
                        "response_sha256": raw["sha256"], "untrusted": True})
                    if url not in urls and len(urls) < 5:
                        urls.append(url)
            except (ValueError, OSError, TimeoutError) as exc:
                result["errors"].append({"query": query, "reason": str(exc)[:200]})
        for url in urls:
            try:
                raw = read({"url": url})
                if raw["content_type"] in {"text/html", "application/xhtml+xml"}:
                    parser = PageText()
                    parser.feed(raw["text"])
                    excerpt = " ".join(parser.parts)
                elif raw["content_type"] in {"text/plain", "text/markdown", "application/json"}:
                    excerpt = raw["text"]
                else:
                    raise ValueError("unsupported_content_type")
                result["sources"].append({"kind": "page", "url": url, "excerpt": excerpt[:8000], "bytes": raw["bytes"],
                    "response_sha256": raw["sha256"], "collected_at": datetime.now(timezone.utc).isoformat(), "untrusted": True})
            except (ValueError, OSError, TimeoutError) as exc:
                result["errors"].append({"url": url, "reason": str(exc)[:200]})
        if not queries and not urls:
            result["errors"].append({"reason": "no_public_queries_or_urls"})
        return result
