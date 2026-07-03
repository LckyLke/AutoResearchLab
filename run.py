#!/usr/bin/env python3
"""Start the AutoResearchLab server.

Usage:  python run.py [--host 127.0.0.1] [--port 8321]
"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoResearchLab web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8321)
    args = parser.parse_args()
    print(f"AutoResearchLab → http://{args.host}:{args.port}")
    uvicorn.run("autoresearch.server:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
