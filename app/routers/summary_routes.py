# app/routers/summary_routes.py
"""
Async summary routes (Quart Blueprint)

Features:
 - Date-range summaries (start / end ISO strings) or presets (last_7_days, last_30_days, last_year)
 - Optional agent scoping
 - Cache summaries by deterministic cache_key; store in DB via crud.save_summary
 - Force regeneration via ?force=true
 - Uses app.llm (SalesIQ primary, OpenAI fallback) for actual summarization
 - Produces short, theoretical sentences suitable for manager overviews

Routes:
 - GET /v1/summary/customer/<customer_id>
     Query params:
       - agent_id (optional)
       - start (ISO datetime, optional)
       - end   (ISO datetime, optional)
       - preset ("last_7_days"|"last_30_days"|"last_year") optional
       - sentences (int, default 3)
       - force (true|false) bypass cache
 - GET /v1/summary/customer/<customer_id>/agent/<agent_id>
     (alias to the above with agent_id path param)
"""
from __future__ import annotations
import asyncio
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone

from quart import Blueprint, request, jsonify, current_app

from ..db import get_session
from .. import crud, llm, utils

bp = Blueprint("summary_routes", __name__, url_prefix="/v1/summary")


# -------------------------
# Helpers
# -------------------------
def _parse_iso_or_none(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _preset_to_range(preset: Optional[str]) -> Optional[tuple]:
    if not preset:
        return None
    now = datetime.now(timezone.utc)
    if preset == "last_7_days":
        return (now - timedelta(days=7), now)
    if preset == "last_30_days":
        return (now - timedelta(days=30), now)
    if preset == "last_year":
        return (now - timedelta(days=365), now)
    return None


def _serialize_msg_for_cache(m: Any) -> Dict[str, Any]:
    """
    Convert Message model or dict to lightweight dict for hashing/caching/LLM.
    """
    if m is None:
        return {}
    # If SQLModel object or similar
    try:
        data = {}
        for k, v in vars(m).items():
            if k.startswith("_") or k == "_sa_instance_state":
                continue
            if hasattr(v, "isoformat"):
                try:
                    data[k] = v.isoformat()
                except Exception:
                    data[k] = str(v)
            else:
                data[k] = v
        return data
    except Exception:
        # assume dict-like
        try:
            return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in m.items()}
        except Exception:
            return {"message": str(m)}


async def _generate_summary_for_messages(messages: List[Dict[str, Any]], n_sentences: int = 3) -> Tuple[List[str], Dict[str, Any]]:
    """
    Use the llm adapter to summarize a list of message dicts into n_sentences.
    Handles chunking for very long conversations.
    Returns (sentences, meta).
    """
    convo_text = utils.export_messages_to_text(messages)
    # If very long, use chunked path
    try:
        if len(convo_text) > 12000:
            sentences, meta = await llm.simple_chunk_and_summarize(convo_text, sentences_per_chunk=2, final_sentences=n_sentences)
        else:
            sentences, meta = await llm.summarize_short_sentences(convo_text, n=n_sentences)
        # Ensure we have sentences
        if not sentences or not isinstance(sentences, list):
            raise RuntimeError("LLM returned no sentences")
        return sentences, meta or {}
    except Exception as e:
        current_app.logger.exception("LLM summarization failed: %s", e)
        # fallback safe summary
        fallback = [
            "Conversation contains multiple exchanges requiring follow-up.",
            "Agent responded but resolution status is unclear.",
            "Recommend manager review and schedule prioritized follow-up.",
        ][:n_sentences]
        return fallback, {"topics": [], "sentiment": "mixed"}


# -------------------------
# Main endpoint
# -------------------------
@bp.route("/customer/<string:customer_id>", methods=["GET"])
@bp.route("/customer/<string:customer_id>/agent/<string:agent_id>", methods=["GET"])
async def get_summary(customer_id: str, agent_id: Optional[str] = None):
    """
    Return a short LLM summary for a customer's conversation history (optionally filtered by agent)
    with caching and date-range controls.
    """
    q = request.args
    start_q = q.get("start")
    end_q = q.get("end")
    preset = q.get("preset")
    try:
        sentences = int(q.get("sentences", "3"))
    except Exception:
        sentences = 3
    force_flag = q.get("force", "false").lower() in ("1", "true", "yes")

    # Resolve date range: explicit start/end take precedence over preset
    start_dt = _parse_iso_or_none(start_q)
    end_dt = _parse_iso_or_none(end_q)
    if (start_dt is None or end_dt is None) and preset:
        pr = _preset_to_range(preset)
        if pr:
            start_dt, end_dt = pr

    # Default to last 30 days if still not set
    if start_dt is None or end_dt is None:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=30)

    # fetch messages for range
    async for session in get_session():
        try:
            msgs = await crud.get_messages(session, customer_id=customer_id, agent_id=agent_id, start=start_dt, end=end_dt)
        except Exception as e:
            current_app.logger.exception("Failed to fetch messages for summary: %s", e)
            return jsonify({"error": "failed_fetch_messages", "details": str(e)}), 500

        if not msgs:
            return jsonify({
                "customer_id": customer_id,
                "agent_id": agent_id,
                "message_count": 0,
                "sentences": [],
                "meta": {"topics": [], "sentiment": "neutral"},
                "cached": False,
            })

        # Prepare serializable messages and cache key
        serial_msgs = [_serialize_msg_for_cache(m) for m in msgs]
        cache_payload = serial_msgs + [{"range_start": start_dt.isoformat(), "range_end": end_dt.isoformat(), "agent_id": agent_id}]
        cache_key = utils.cache_key_from_messages(cache_payload)

        # derive source_hash (concatenate message ids if present)
        try:
            ids_concat = "|".join([str(m.get("message_id") or m.get("id") or "") for m in serial_msgs])
            source_hash = utils.sha256(f"{ids_concat}|{start_dt.isoformat()}|{end_dt.isoformat()}|{agent_id or ''}")
        except Exception:
            source_hash = utils.sha256(cache_key)

        # Check cache unless forced
        cached_summary = None
        if not force_flag:
            try:
                cached_summary = await crud.get_summary_by_cache_key(session, cache_key)
            except Exception as e:
                current_app.logger.debug("Summary cache lookup error: %s", e)
                cached_summary = None

        if cached_summary and not force_flag:
            return jsonify({
                "customer_id": customer_id,
                "agent_id": agent_id,
                "message_count": cached_summary.message_count or len(serial_msgs),
                "sentences": cached_summary.sentences or [],
                "meta": {"topics": cached_summary.topics or [], "sentiment": cached_summary.sentiment or "neutral", "model_meta": cached_summary.model_meta or {}},
                "cached": True,
                "cache_key": cached_summary.cache_key,
                "generated_at": cached_summary.generated_at.isoformat() if hasattr(cached_summary, "generated_at") else None,
            })

        # Generate new summary
        sentences_list, meta = await _generate_summary_for_messages(serial_msgs, n_sentences=sentences)

        # Best-effort persist summary
        try:
            saved = await crud.save_summary(
                session=session,
                customer_id=customer_id,
                agent_id=agent_id,
                range_start=start_dt,
                range_end=end_dt,
                sentences=sentences_list,
                topics=meta.get("topics") if isinstance(meta, dict) else [],
                sentiment=meta.get("sentiment") if isinstance(meta, dict) else None,
                message_count=len(serial_msgs),
                model_meta={"provider": "salesiq" if (llm.SALESIQ_API_URL) else ("openai" if getattr(llm, "OPENAI_API_KEY", None) else "none"), "notes": "generated_via_api"},
                cache_key=cache_key,
                source_hash=source_hash,
            )
            cache_key_out = saved.cache_key
            generated_at = saved.generated_at.isoformat() if hasattr(saved, "generated_at") else datetime.now(timezone.utc).isoformat()
        except Exception as e:
            current_app.logger.exception("Failed to persist summary (non-fatal): %s", e)
            cache_key_out = cache_key
            generated_at = datetime.now(timezone.utc).isoformat()

        return jsonify({
            "customer_id": customer_id,
            "agent_id": agent_id,
            "message_count": len(serial_msgs),
            "sentences": sentences_list,
            "meta": meta or {},
            "cached": False,
            "cache_key": cache_key_out,
            "generated_at": generated_at,
        })
