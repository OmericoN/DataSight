"""One bounded, grounded completion through the configured DataSight gateway."""

import json
import time

from datasight.application.extraction_engine import LOCATE, Block, anchor_passage
from datasight.domain.extraction import LocatedPassages
from datasight.infrastructure.llm.client import GatewayClient, payload_for


def main() -> None:
    source = "We analyzed the Alpha cohort dataset."
    block = Block(0, len(source))
    attempts: list[dict] = []
    client = GatewayClient()
    try:
        status = client.status()
        if not status["available"]:
            raise RuntimeError(status["message"])
        started = time.monotonic()
        result = client.complete(
            payload_for(LOCATE, {"block_id": block.block_id, "text": source}, LocatedPassages),
            LocatedPassages, attempts.append,
            lambda value: [anchor_passage(source, block, passage) for passage in value.passages],
        )
        if not result.passages:
            raise AssertionError("Smoke completion omitted the explicit dataset reference")
        print(json.dumps({"model": status["model"], "passages": len(result.passages),
                          "elapsed_seconds": round(time.monotonic() - started, 3),
                          "attempts": len(attempts), "http_status": attempts[-1]["http_status"],
                          "usage": {key: attempts[-1]["response"].get("usage", {}).get(key)
                                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")}}))
    finally:
        client.close()


if __name__ == "__main__":
    main()
