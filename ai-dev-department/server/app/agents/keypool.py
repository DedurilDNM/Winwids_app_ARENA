"""The common key pool («котёл»): every model call takes the freest key.

- lease():   available keys of the provider -> fewest calls in flight -> least recently
             used. All keys limited -> wait for the next one to come back (no error).
- release(): a rate limit / empty credits -> the key is «limited» and leaves the pool;
             a rejected key -> «invalid»; the call goes on with another key.
- checker:   limited keys are re-checked in the background every 5 minutes (sooner if
             the provider said when: Retry-After), invalid ones every hour; a key that
             passes is «available» again.
- complete(): one model call with rotation and retries of temporary failures.

Secrets live only in memory while a call runs; they are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from app.agents import llm
from app.agents.llm import (INVALID, KEY_PROBLEMS, NO_CREDITS, OK, RATE_LIMITED, REQUEST, TRANSIENT, LLMError,
                            LLMResponse, Request, short_reason)
from app.agents.providers import Provider, get_provider
from app.config import get_settings
from app.models import ApiKey, utcnow

logger = logging.getLogger(__name__)


@dataclass
class Lease:
    key_id: int
    user_id: int
    provider: Provider
    secret: str
    base_url: str | None
    model: str
    tail: str
    label: str


def _setting(name: str, default):
    return getattr(get_settings(), name, default)


class KeyPool:
    def __init__(self, session_factory=None):
        self._session_factory = session_factory
        self._busy: dict[int, int] = defaultdict(int)
        self._last_lease: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        # async (user_id, event: dict) -> None: «limited», «recovered», «rotation», «waiting»
        self.on_event = None

    @property
    def sessions(self):
        if self._session_factory is None:
            from app.db import SessionLocal

            self._session_factory = SessionLocal
        return self._session_factory

    async def _emit(self, user_id: int, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            await self.on_event(user_id, event)
        except Exception:
            logger.debug("key pool event callback failed", exc_info=True)

    def busy(self, key_id: int) -> int:
        return self._busy.get(key_id, 0)

    # ------------------------------------------------------------ lease / release

    async def _try_lease(self, user_id: int, provider_id: str, model: str) -> Lease | str:
        """A lease, or why there is none: "none" (no usable key at all) | "wait"."""
        from app.security import decrypt_str

        limit = int(_setting("KEYPOOL_MAX_PARALLEL_PER_KEY", 4) or 0)
        async with self._lock:
            async with self.sessions() as db:
                keys = list((await db.execute(select(ApiKey).where(
                    ApiKey.user_id == user_id, ApiKey.provider == provider_id))).scalars())
            usable = [k for k in keys if k.status == "available"]
            free = [k for k in usable if not limit or self._busy[k.id] < limit]
            if not free:
                return "wait" if (usable or any(k.status == "limited" for k in keys)) else "none"
            key = min(free, key=lambda k: (self._busy[k.id], self._last_lease.get(k.id, 0.0), k.id))
            self._busy[key.id] += 1
            self._last_lease[key.id] = time.monotonic()
            return Lease(key.id, user_id, get_provider(key.provider), decrypt_str(key.key_encrypted), key.base_url,
                         model, key.key_tail, key.label)

    async def lease(self, user_id: int, provider_id: str, model: str, *, max_wait: float | None = None,
                    should_continue=None, on_wait=None) -> Lease:
        get_provider(provider_id)  # unknown provider -> ValueError
        max_wait = float(max_wait if max_wait is not None else _setting("KEYPOOL_MAX_WAIT_SECONDS", 1800))
        started, told = time.monotonic(), False
        while True:
            got = await self._try_lease(user_id, provider_id, model)
            if isinstance(got, Lease):
                return got
            if got == "none":
                raise LLMError(REQUEST, f"No usable {provider_id} key in the pool: add a key or re-check the keys")
            if time.monotonic() - started > max_wait:
                raise LLMError(RATE_LIMITED, f"Every {provider_id} key is limited; waited {max_wait:.0f}s")
            if should_continue is not None and not await should_continue():
                raise asyncio.CancelledError("stopped while waiting for a free key")
            if not told:
                told = True
                logger.info("Key pool: every %s key is busy or limited; waiting for a free one", provider_id)
                await self._emit(user_id, {"type": "keys_waiting", "provider": provider_id})
                if on_wait is not None:
                    try:
                        await on_wait(provider_id)
                    except Exception:
                        pass
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def release(self, lease: Lease, outcome: str, *, status: int | None = None,
                      retry_after: float | None = None, tokens: tuple[int, int] = (0, 0)) -> None:
        async with self._lock:
            self._busy[lease.key_id] = max(0, self._busy[lease.key_id] - 1)
        now = utcnow()
        event = None
        async with self.sessions() as db:
            key = await db.get(ApiKey, lease.key_id)
            if key is None:
                return
            key.last_used_at = now
            key.last_model = lease.model or key.last_model
            if outcome == OK:
                key.requests = (key.requests or 0) + 1
                key.tokens_in = (key.tokens_in or 0) + int(tokens[0] or 0)
                key.tokens_out = (key.tokens_out or 0) + int(tokens[1] or 0)
            else:
                key.errors = (key.errors or 0) + 1
            if outcome in (RATE_LIMITED, NO_CREDITS) and key.status != "disabled":
                check = float(_setting("KEYPOOL_CHECK_SECONDS", 300))
                wait = retry_after if (outcome == RATE_LIMITED and retry_after and 0 < retry_after < check) else check
                key.status, key.limited_at = "limited", now
                key.next_check_at = now + timedelta(seconds=wait)
                key.status_reason = short_reason(outcome, status)
                event = {"type": "key_limited", "key_id": key.id, "provider": key.provider, "tail": key.key_tail,
                         "label": key.label, "reason": key.status_reason}
            elif outcome == INVALID and key.status != "disabled":
                key.status = "invalid"
                key.next_check_at = now + timedelta(seconds=float(_setting("KEYPOOL_INVALID_CHECK_SECONDS", 3600)))
                key.status_reason = short_reason(outcome, status)
                event = {"type": "key_invalid", "key_id": key.id, "provider": key.provider, "tail": key.key_tail,
                         "label": key.label, "reason": key.status_reason}
            await db.commit()
        self._changed.set()
        if event:
            logger.info("Key pool: %s key …%s -> %s (%s)", event["provider"], event["tail"],
                        event["type"], event["reason"])
            await self._emit(lease.user_id, event)

    # ------------------------------------------------------------ one call with rotation

    async def complete(self, user_id: int, provider_id: str, req: Request, *, should_continue=None,
                       on_wait=None, on_rotate=None) -> LLMResponse:
        transient = 0
        previous: Lease | None = None
        while True:
            lease = await self.lease(user_id, provider_id, req.model, should_continue=should_continue, on_wait=on_wait)
            if previous is not None and previous.key_id != lease.key_id:
                event = {"type": "key_rotation", "provider": provider_id, "from_tail": previous.tail,
                         "to_tail": lease.tail, "from_label": previous.label, "to_label": lease.label}
                await self._emit(user_id, event)
                if on_rotate is not None:
                    try:
                        await on_rotate(event)
                    except Exception:
                        pass
            client = llm.make_client(lease.provider, lease.secret, lease.base_url)
            try:
                resp = await client.create(req)
            except LLMError as exc:
                await self.release(lease, exc.outcome, status=exc.status, retry_after=exc.retry_after)
                if exc.outcome in KEY_PROBLEMS:
                    previous = lease
                    continue  # straight to another key
                if exc.outcome == TRANSIENT and transient < 3:
                    transient += 1
                    previous = lease
                    await asyncio.sleep(min(30.0, exc.retry_after or 2.0 * transient))
                    continue
                raise
            except BaseException:
                await self.release(lease, TRANSIENT)
                raise
            finally:
                try:
                    await client.close()
                except Exception:
                    pass
            await self.release(lease, OK, tokens=(resp.tokens_in, resp.tokens_out))
            return resp

    # ------------------------------------------------------------ checks

    async def check_key(self, key_id: int, *, model: str | None = None) -> str:
        """Probe one key now; update its status. Returns the outcome."""
        from app.security import decrypt_str

        async with self.sessions() as db:
            key = await db.get(ApiKey, key_id)
            if key is None or key.status == "disabled":
                return "disabled"
            provider = get_provider(key.provider)
            secret, base_url, user_id = decrypt_str(key.key_encrypted), key.base_url, key.user_id
            probe_model = model or (key.last_model if key.status == "limited" else None)
            was = key.status
        client = llm.make_client(provider, secret, base_url)
        status = None
        try:
            await client.probe(probe_model)
            outcome = OK
        except LLMError as exc:
            outcome, status = exc.outcome, exc.status
            if outcome == REQUEST and probe_model:  # e.g. the model was removed: the key itself is fine
                outcome = OK
        except Exception:
            outcome = TRANSIENT
        finally:
            try:
                await client.close()
            except Exception:
                pass
        now = utcnow()
        event = None
        async with self.sessions() as db:
            key = await db.get(ApiKey, key_id)
            if key is None or key.status == "disabled":
                return "disabled"
            key.last_checked_at = now
            if outcome == OK:
                key.status, key.status_reason, key.next_check_at = "available", None, None
                if was != "available":
                    event = {"type": "key_recovered", "key_id": key.id, "provider": key.provider,
                             "tail": key.key_tail, "label": key.label}
            elif outcome in (RATE_LIMITED, NO_CREDITS):
                key.status = "limited"
                key.limited_at = key.limited_at or now
                key.status_reason = short_reason(outcome, status)
                key.next_check_at = now + timedelta(seconds=float(_setting("KEYPOOL_CHECK_SECONDS", 300)))
            elif outcome == INVALID:
                key.status, key.status_reason = "invalid", short_reason(outcome, status)
                key.next_check_at = now + timedelta(seconds=float(_setting("KEYPOOL_INVALID_CHECK_SECONDS", 3600)))
            else:  # temporary trouble: look again at the next regular check
                key.next_check_at = now + timedelta(seconds=float(_setting("KEYPOOL_CHECK_SECONDS", 300)))
            await db.commit()
        if event:
            self._changed.set()
            logger.info("Key pool: %s key …%s is available again", event["provider"], event["tail"])
            await self._emit(user_id, event)
        return outcome

    async def check_due(self) -> int:
        """Check every limited / invalid key whose check time has come. Returns how many."""
        now = utcnow()
        async with self.sessions() as db:
            ids = list((await db.execute(select(ApiKey.id).where(
                ApiKey.status.in_(("limited", "invalid")),
                (ApiKey.next_check_at.is_(None)) | (ApiKey.next_check_at <= now)))).scalars())
        for key_id in ids:
            try:
                await self.check_key(key_id)
            except Exception:
                logger.warning("Key check failed for key %s", key_id, exc_info=True)
        return len(ids)

    async def run_checker(self) -> None:
        """Background loop (started with the app)."""
        tick = float(_setting("KEYPOOL_TICK_SECONDS", 15))
        while True:
            try:
                await self.check_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Key pool checker tick failed", exc_info=True)
            await asyncio.sleep(tick)


key_pool = KeyPool()


def next_check_in(key: ApiKey) -> float | None:
    if key.status not in ("limited", "invalid") or key.next_check_at is None:
        return None
    return max(0.0, (key.next_check_at - utcnow()).total_seconds())
