import asyncio
import logging

import wavelink

from cogs.utils.lavalink_server import LavalinkSettings, load_lavalink_settings

log = logging.getLogger(__name__)


async def connect_lavalink(bot, credentials: dict) -> None:
    settings = load_lavalink_settings(credentials)
    if not settings.enabled:
        log.info("Lavalink is disabled in config; skipping Wavelink connection.")
        return

    if wavelink.Pool.nodes:
        log.debug("Wavelink already connected; skipping.")
        return

    uri = f"http://{settings.host}:{settings.port}"
    nodes = [wavelink.Node(uri=uri, password=settings.password)]
    await wavelink.Pool.connect(nodes=nodes, client=bot, cache_capacity=100)
    log.info("Connected to Lavalink at %s", uri)


async def connect_lavalink_with_retry(bot, credentials: dict) -> bool:
    """Try to connect Wavelink until Lavalink is up or the configured timeout elapses."""
    settings = load_lavalink_settings(credentials)
    if not settings.enabled:
        return False

    timeout = settings.startup_timeout_seconds
    for attempt in range(1, timeout + 1):
        try:
            await connect_lavalink(bot, credentials)
            return True
        except Exception as e:
            if attempt == 1 or attempt % 10 == 0:
                log.info("Waiting for Lavalink (%s/%ss): %s", attempt, timeout, e)
            await asyncio.sleep(1)

    log.error("Gave up connecting to Lavalink after %ss", timeout)
    return False


async def reconnect_lavalink(bot, credentials: dict) -> None:
    """Disconnect and reconnect Wavelink (e.g. after Lavalink restart)."""
    try:
        await wavelink.Pool.close()
    except Exception:
        pass
    await connect_lavalink(bot, credentials)


def get_connection_settings(credentials: dict) -> LavalinkSettings:
    return load_lavalink_settings(credentials)