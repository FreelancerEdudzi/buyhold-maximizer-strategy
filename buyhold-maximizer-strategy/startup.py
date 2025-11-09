#!/usr/bin/env python3
"""Entry point for the Buy-and-Hold Maximizer strategy."""

import os
import sys

BASE_PATH = os.path.join(os.path.dirname(__file__), "..", "base-bot-template")
if not os.path.exists(BASE_PATH):
    BASE_PATH = "/app/base"

sys.path.insert(0, BASE_PATH)

import buyhold_maximizer  # noqa: F401  # registers the strategy
from universal_bot import UniversalBot


def main() -> None:
    print("🚀 Starting Buy-and-Hold Maximizer strategy...")
    bot = UniversalBot()
    bot.run()


if __name__ == "__main__":
    main()
