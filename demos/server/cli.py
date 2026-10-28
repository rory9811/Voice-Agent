"""Serve the real model experience on a local HTTP endpoint."""

import argparse
import sys

import uvicorn

from .app import create_app
from .resources import fetch_model
from demos.model.settings import model_token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8032)
    parser.add_argument("--model-server", required=True, help="Model server URL, e.g. http://10.194.99.8:8031")
    parser.add_argument("--config", required=True)
    parser.add_argument("--demo-config", help="Demo deployment JSON for frontend settings")
    args = parser.parse_args()
    try:
        model = fetch_model(args.model_server, model_token())
    except RuntimeError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    uvicorn.run(
        create_app(
            settings_path=args.config,
            demo_path=args.demo_config,
            model_type=model.model_type,
            tokenizer=model.tokenizer,
            model_server_url=args.model_server,
        ),
        host=args.host,
        port=args.port,
        ws_max_size=512 * 1024,
    )


if __name__ == "__main__":
    main()
