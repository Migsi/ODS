#!/usr/bin/env python3
"""Fixed client for the root-owned Pixel model transition."""
import re
import sys

from pixel_access_client import request_access


HEX = re.compile(r"[a-f0-9]{64}\Z")


def execute(action, transaction_id=None, outcome=None, *, request=request_access):
    if action == "begin" and transaction_id is None and outcome is None:
        status, body = request("model-begin")
        if (status != 200 or set(body) != {"status", "transaction_id"}
                or body.get("status") != "held"
                or type(body.get("transaction_id")) is not str
                or not HEX.fullmatch(body["transaction_id"])):
            raise RuntimeError(body.get("error", "model-transition-begin-failed"))
        return body["transaction_id"]
    if (action == "finish" and type(transaction_id) is str and HEX.fullmatch(transaction_id)
            and outcome in ("applied", "rolled-back")):
        status, body = request("model-finish", {
            "transaction_id": transaction_id, "outcome": outcome})
        if status != 200 or body != {"status": "released", "outcome": outcome}:
            raise RuntimeError(body.get("error", "model-transition-finish-failed"))
        return body["status"]
    raise ValueError("invalid model transition request")


def main(argv):
    if argv == ["begin"]:
        print(execute("begin"))
        return 0
    if len(argv) == 4 and argv[0] == "finish" and argv[1] == "--transaction" and argv[3] in ("applied", "rolled-back"):
        execute("finish", argv[2], argv[3])
        return 0
    raise SystemExit("usage: pixel_model_transition.py begin | finish --transaction HEX64 applied|rolled-back")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
