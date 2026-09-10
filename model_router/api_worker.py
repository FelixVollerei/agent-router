"""Private child process for bounded HTTP transport. No credentials written to disk."""
import json
import sys

from .providers import _http_completion


def main():
    try:
        payload = json.loads(sys.stdin.read(1_000_001))
        result = _http_completion(**payload)
        print(json.dumps({"result": result}), flush=True)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:500]}"}), flush=True)


if __name__ == "__main__":
    main()
