"""Automatic model choice: which models are available, which are strongest.

1. Collect the models of every provider that has a working key in the pool.
2. Score each one without spending tokens: the price per output token where the
   service tells it (the best proxy for strength across vendors), and the name
   (pro/ultra/large/opus vs mini/nano/flash/lite, the parameter count, reasoning).
   Embedding / speech / image / moderation models and models without tool calling
   are left out.
3. Check the best candidates for real: a tiny request that must call a tool. A model
   that does not answer or cannot use tools is dropped.
4. The strongest working model leads (Orchestrator); the other roles get the next
   ones in descending strength. Each role also gets the next models as fallbacks, so
   it keeps working when every key of its provider is limited.

The same model offered by several providers counts once (its other offers become
fallbacks for that role).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re

from sqlalchemy import select

from app.agents import llm
from app.agents.keypool import key_pool
from app.agents.llm import LLMError, Request
from app.agents.providers import get_provider
from app.models import AgentRole, ApiKey

logger = logging.getLogger(__name__)

# the order in which roles get models: the lead first, then by how much depends on the role
ROLE_ORDER = ("Orchestrator", "Architect", "Coder", "Reviewer", "Integrator", "Tester")

_EXCLUDE = re.compile(r"embed|whisper|tts|speech|audio|transcri|dall-?e|image|imagen|vision-only|moderat|guard|"
                      r"rerank|bge-|clip|sora|veo|stable-diffusion|flux|ocr|realtime|search-preview", re.I)
# whole words of the name (so «mini» is not found inside «gemini»)
_UP = {"opus": 14, "ultra": 12, "pro": 8, "large": 6, "sonnet": 6, "max": 4, "plus": 3}
_DOWN = {"nano": 18, "tiny": 18, "micro": 14, "mini": 10, "small": 10, "lite": 10, "haiku": 8, "instant": 8,
         "flash": 6, "vl": 2, "vision": 2}
_REASONING = re.compile(r"(^|[/\-_.])(r1|r2|o1|o3|o4|qwq)([\-_.:]|$)|reason|thinking", re.I)


def base_name(model_id: str) -> str:
    """The model without the vendor prefix and the free tag: one model, several offers."""
    name = model_id.lower().split("/")[-1]
    return re.sub(r":(free|beta|extended|nitro|floor)$", "", name)


def score(model_id: str, meta: dict | None = None) -> float | None:
    """Higher = stronger. None = not a chat model that can work as an agent."""
    meta = meta or {}
    name = model_id.lower()
    if _EXCLUDE.search(name):
        return None
    kind = (meta.get("kind") or "").lower()
    if kind and not any(k in kind for k in ("chat", "language", "text", "llm", "code")):
        return None
    if meta.get("tools") is False:
        return None
    context = meta.get("context")
    if context and context < 8000:
        return None
    s = 50.0
    price = meta.get("price_out")
    if price and price > 0:
        s += 12 * math.log10(price + 0.05)  # $75/1M -> +22, $15 -> +14, $2 -> +4, $0.5 -> -3.5
    short = name.split("/")[-1]
    words = set(re.split(r"[-_./:\s]+", short))
    s += max((bonus for word, bonus in _UP.items() if word in words), default=0)
    s -= min(24, sum(malus for word, malus in _DOWN.items() if word in words))
    sizes = [float(x) for x in re.findall(r"(?<![a-z])(\d+(?:\.\d+)?)b(?![a-z])", short)]
    if sizes:
        s += 6 * math.log10(max(sizes)) - 9  # 70b -> +2, 8b -> -3.6, 405b -> +6.7
    if _REASONING.search(short):
        s += 4
    if "coder" in short or "code" in short:
        s += 2
    version = re.search(r"(\d+(?:\.\d+)?)", re.sub(r"\d+(?:\.\d+)?b(?![a-z])", "", short))
    if version:
        s += min(6.0, float(version.group(1))) * 0.8
    if name.endswith(":free"):
        s -= 3  # the same model, but heavily rate-limited
    if context:
        s += 2 if context >= 200_000 else (-4 if context < 16_000 else 0)
    return round(s, 2)


async def collect(db, user_id: int) -> list[dict]:
    """Candidates from every provider with a working key: [{provider, model, score, meta}] (best first)."""
    from app.security import decrypt_str

    keys = list((await db.execute(select(ApiKey).where(ApiKey.user_id == user_id,
                                                       ApiKey.status.in_(("available", "limited"))))).scalars())
    by_provider: dict[str, ApiKey] = {}
    for k in sorted(keys, key=lambda k: k.status != "available"):
        by_provider.setdefault(k.provider, k)
    found: list[dict] = []

    async def one(provider_id: str, key: ApiKey) -> None:
        client = llm.make_client(get_provider(provider_id), decrypt_str(key.key_encrypted), key.base_url)
        try:
            metas = await client.models_meta()
        except LLMError as exc:
            logger.info("Model picker: %s did not list its models (%s)", provider_id, exc.status or exc.outcome)
            return
        except Exception:
            logger.info("Model picker: %s model list failed", provider_id, exc_info=True)
            return
        finally:
            try:
                await client.close()
            except Exception:
                pass
        for meta in metas:
            value = score(meta["id"], meta)
            if value is not None:
                found.append({"provider": provider_id, "model": meta["id"], "score": value,
                              "price_out": meta.get("price_out"), "tools": meta.get("tools")})

    await asyncio.gather(*(one(p, k) for p, k in by_provider.items()))
    found.sort(key=lambda c: (-c["score"], c["model"].endswith(":free"), c["provider"], c["model"]))
    return found


PROBE_TOOL = {"name": "report_ready", "description": "Call this to report that you are ready.",
              "input_schema": {"type": "object", "properties": {"ready": {"type": "boolean"}},
                               "required": ["ready"]}}


async def probe(user_id: int, provider: str, model: str, *, timeout: float = 60) -> tuple[bool, str]:
    """(works, why): the model must answer and call a tool."""
    req = Request(model=model, max_tokens=300, tools=[PROBE_TOOL],
                  messages=[{"role": "user", "content": "Call the report_ready tool with ready=true. Do not write text."}])
    try:
        resp = await asyncio.wait_for(key_pool.complete(user_id, provider, req, wait_limit=5), timeout)
    except asyncio.TimeoutError:
        return False, "no answer in time"
    except LLMError as exc:
        return False, llm.short_reason(exc.outcome, exc.status)
    except Exception as exc:
        return False, type(exc).__name__
    if any(c["name"] == "report_ready" for c in resp.tool_calls):
        return True, "ok"
    return False, "answers, but does not call tools"


async def pick(db, user_id: int, *, check: bool = True, max_checks: int = 14, parallel: int = 4,
               on_progress=None) -> dict:
    """Rank the available models and assign them to roles. Returns {ranking, assignments}."""
    candidates = await collect(db, user_id)
    groups: dict[str, list[dict]] = {}
    for c in candidates:
        groups.setdefault(base_name(c["model"]), []).append(c)
    ordered = sorted(groups.values(), key=lambda g: -g[0]["score"])
    working: list[list[dict]] = []
    ranking: list[dict] = []
    sem = asyncio.Semaphore(parallel)

    async def check_group(group: list[dict]) -> list[dict]:
        ok_offers = []
        for offer in group[:3]:  # the same model at up to three providers
            async with sem:
                works, why = await probe(user_id, offer["provider"], offer["model"]) if check else (True, "not checked")
            offer["check"] = why
            if works:
                ok_offers.append(offer)
                if check:
                    break  # one working offer is enough to rank it; the rest stay fallbacks
        rest = [o for o in group if o not in ok_offers and o.get("check") in (None, "ok", "not checked")]
        return ok_offers + rest if ok_offers else []

    need = len(ROLE_ORDER)
    index = 0
    while index < len(ordered) and len(working) < need and index < max_checks:
        batch = ordered[index: index + parallel]
        index += len(batch)
        results = await asyncio.gather(*(check_group(g) for g in batch))
        for group, result in zip(batch, results):
            ranking.append({"model": group[0]["model"], "provider": group[0]["provider"], "score": group[0]["score"],
                            "price_out": group[0]["price_out"], "offers": len(group),
                            "works": bool(result), "check": group[0].get("check")})
            if result:
                working.append(result)
        if on_progress is not None:
            await on_progress(len(working), index)
    for group in ordered[index:index + 20]:  # the rest, unchecked, for the UI
        ranking.append({"model": group[0]["model"], "provider": group[0]["provider"], "score": group[0]["score"],
                        "price_out": group[0]["price_out"], "offers": len(group), "works": None, "check": None})
    working.sort(key=lambda g: -g[0]["score"])
    assignments = []
    for i, role in enumerate(ROLE_ORDER):
        if not working:
            break
        g = working[min(i, len(working) - 1)]
        primary = g[0]
        fallbacks = [{"provider": o["provider"], "model": o["model"]} for o in g[1:3]]
        for other in working[min(i, len(working) - 1) + 1:]:  # then the next weaker models
            if len(fallbacks) >= 3:
                break
            fallbacks.append({"provider": other[0]["provider"], "model": other[0]["model"]})
        assignments.append({"role": role, "provider": primary["provider"], "model": primary["model"],
                            "score": primary["score"], "fallbacks": fallbacks})
    return {"ranking": ranking, "assignments": assignments, "candidates": len(candidates)}


async def apply(db, user_id: int, assignments: list[dict]) -> None:
    """Save the chosen models on the roles; the user's tools, skills and instructions stay, and a
    role whose model the user chose by hand keeps it."""
    saved = {r.role: r for r in (await db.execute(select(AgentRole).where(AgentRole.user_id == user_id))).scalars()}
    for a in assignments:
        r = saved.get(a["role"])
        if r is not None and not r.auto and r.model:
            continue  # chosen by hand: the picker never overrides it
        if r is None:
            r = AgentRole(user_id=user_id, role=a["role"], provider=a["provider"], model=a["model"])
            db.add(r)
        r.provider, r.model, r.fallbacks, r.auto = a["provider"], a["model"], a["fallbacks"], True
    await db.commit()
