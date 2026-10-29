"""Идемпотентный seed: запускается при каждом старте api."""

import asyncio


async def seed() -> None:
    return None


if __name__ == "__main__":
    asyncio.run(seed())
