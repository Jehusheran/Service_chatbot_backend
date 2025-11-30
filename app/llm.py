# app/llm.py
"""
LLM adapter for Service Chatbot.

Primary preference: Zoho SalesIQ LLM (if SALESIQ_API_URL + SALESIQ_API_KEY configured)
Fallback: OpenAI (if OPENAI_API_KEY configured)

Design goals:
 - Minimal assumptions about the exact SalesIQ HTTP contract (make it easy to adapt)
 - Async-friendly (wrap blocking calls with asyncio.to_thread)
 - Provide the same convenient helpers used across the app:
     - call_chat(messages, ...)
     - embed_texts(texts, ...)
     - generate_bot_suggestions(context, n)
     - summarize_short_sentences(text, n)
     - simple_chunk_and_summarize(text, ...)
     - summarize_messages_for_range(messages, n_sentences)
 - Graceful fallbacks and safe defaults for hackathon/demo usage.
"""

from __future__ import annotations
import os
import json
import time
import math
import asyncio
from typing import List, Tuple, Dict, Any, Optional
from asyncio import to_thread
from dotenv import load_dotenv
import logging
import requests

load_dotenv()
logger = logging.getLogger("llm")
logger.setLevel(logging.INFO)

# Primary SalesIQ settings (user must provide)
SALESIQ_API_URL = os.getenv("SALESIQ_API_URL, “https://salesiq.zoho.in")  # e.g. "https://api.zoho.com/salesiq/generate"
SALESIQ_API_KEY = os.getenv("SALESIQ_API_KEY, MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAiIQwaAqcSWA4Gury1volPwsO5U6UT0mB551G+I74jzgcLW+qs2z2NtyNq4NaoeaVh4/NwfyLi2W+FXzHJ3WjBOZgESXZTI8tWV+2jOjo8xJK/OGSPlT2IBNNunGLlKju7LXVZ411xHBzX53UtA/o1ixCPDq9EtJNqRA+VmA2CGY+RRnOqSea/u5dfcUqYnjpme1ti6dr5OSP4G9IPUCQk9xff1wKhWyO8xwvV5Mo3JLEF/wgwpjHYFzhWxHQzN4KFL+TvLvfe89XhsjwsgMK/qJcfgFqFhZ2OPFgoSsIVHLbFYBArpitvIt+LbDsp2OUpv/ANpGZ3ZAINDBio7YGUQIDAQAB")
SALESIQ_MODEL = os.getenv("SALESIQ_MODEL", "salesiq-default-model")
SALESIQ_EMBEDDING_URL = os.getenv("SALESIQ_EMBEDDING_URL")  # optional endpoint for embeddings

# Optional OpenAI fallback (if SalesIQ not configured or lacks a given capability)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
DEFAULT_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))

# If openai is available, import it; we won't require it if SalesIQ is configured and used exclusively
try:
    import openai  # type: ignore
    if OPENAI_API_KEY:
        openai.api_key = OPENAI_API_KEY
except Exception:
    openai = None  # type: ignore

# -------------------------
# HTTP helpers for SalesIQ
# -------------------------
def _call_salesiq_api_sync(payload: Dict[str, Any], url: Optional[str] = None, api_key: Optional[str] = None, timeout: int = 30) -> Dict[str, Any]:
    """
    Generic synchronous POST to SalesIQ API.
    Expect JSON response. This function is intentionally generic — adapt to your exact SalesIQ contract.
    """
    if not (url or SALESIQ_API_URL):
        raise RuntimeError("SalesIQ API URL not configured (SALESIQ_API_URL)")
    if not (api_key or SALESIQ_API_KEY):
        # allow unauthenticated use in development if you run a local mock endpoint
        logger.debug("No SalesIQ API key configured; attempting unauthenticated call (dev only).")
    headers = {
        "Content-Type": "application/json",
    }
    if api_key or SALESIQ_API_KEY:
        headers["Authorization"] = f"Bearer {api_key or SALESIQ_API_KEY}"

    final_url = url or SALESIQ_API_URL
    try:
        resp = requests.post(final_url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.exception("SalesIQ API call failed: %s", e)
        raise

async def _call_salesiq_api(payload: Dict[str, Any], url: Optional[str] = None, api_key: Optional[str] = None, timeout: int = 30) -> Dict[str, Any]:
    return await to_thread(_call_salesiq_api_sync, payload, url, api_key, timeout)

# -------------------------
# Embeddings via SalesIQ (optional) or OpenAI fallback
# -------------------------
async def embed_texts(texts: List[str], model: Optional[str] = None) -> List[List[float]]:
    """
    Return embeddings for a list of texts.
    Preference order:
      1. SALESIQ_EMBEDDING_URL (if provided)
      2. OpenAI embeddings (if OPENAI_API_KEY available)
    If neither available, raises RuntimeError.
    """
    # Try SalesIQ embedding endpoint if configured
    if SALESIQ_EMBEDDING_URL:
        payload = {
            "model": model or SALESIQ_MODEL,
            "inputs": texts,
        }
        try:
            resp = await _call_salesiq_api(payload, url=SALESIQ_EMBEDDING_URL)
            # Try common response shapes. Adapt this to your provider if needed.
            if isinstance(resp, dict) and resp.get("embeddings"):
                return resp["embeddings"]
            if isinstance(resp, dict) and resp.get("data"):
                # e.g. {"data": [{"embedding": [...]}, ...]}
                return [item.get("embedding") or item.get("vector") for item in resp["data"]]
        except Exception:
            logger.exception("SalesIQ embedding call failed; falling back to OpenAI if available.")

    # Fallback to OpenAI embeddings
    if openai and OPENAI_API_KEY:
        def _sync_openai_embed():
            out = openai.Embedding.create(model=model or OPENAI_EMBEDDING_MODEL, input=texts)
            return [d["embedding"] for d in out["data"]]
        return await to_thread(_sync_openai_embed)

    raise RuntimeError("No embeddings provider configured (set SALESIQ_EMBEDDING_URL or OPENAI_API_KEY)")

# -------------------------
# Primary chat/call wrapper
# -------------------------
async def call_chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    max_tokens: Optional[int] = 512,
    temperature: Optional[float] = None,
    stop: Optional[List[str]] = None,
) -> str:
    """
    messages: list of {"role":"system"|"user"|"assistant", "content": "..."}
    Behavior:
      - If SalesIQ configured -> send a flexible payload to SALESIQ_API_URL (model, messages)
      - Else if OpenAI available -> call OpenAI ChatCompletion.create
      - Returns assistant text (string). Raises on fatal failure.
    """
    temperature = DEFAULT_TEMPERATURE if temperature is None else temperature
    model = model or SALESIQ_MODEL

    # Try SalesIQ first
    if SALESIQ_API_URL:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stop": stop,
        }
        try:
            resp = await _call_salesiq_api(payload)
            # Parse a few common response shapes:
            # 1) {"output": "text..."}
            if isinstance(resp, dict):
                if "output" in resp and isinstance(resp["output"], str):
                    return resp["output"]
                # 2) {"choices":[{"message":{"content":"..."}}]}
                choices = resp.get("choices")
                if isinstance(choices, list) and len(choices) > 0:
                    first = choices[0]
                    # try message.content
                    msg = first.get("message") or first.get("delta") or first
                    if isinstance(msg, dict):
                        # message.content or text
                        if "content" in msg:
                            return msg["content"]
                        if "text" in msg:
                            return msg["text"]
                    # fallback to string representation
                    return str(first)
            # fallback: stringify entire response
            return json.dumps(resp)[:max_tokens or 512]
        except Exception:
            logger.exception("SalesIQ chat call failed; will attempt OpenAI fallback if available.")

    # Fallback: OpenAI
    if openai and OPENAI_API_KEY:
        def _sync_openai_chat():
            resp = openai.ChatCompletion.create(
                model=model or OPENAI_CHAT_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stop=stop,
            )
            choice = resp.choices[0]
            if hasattr(choice, "message") and getattr(choice.message, "content", None):
                return choice.message.content
            return getattr(choice, "text", "") or resp.choices[0].get("text", "")
        return await to_thread(_sync_openai_chat)

    raise RuntimeError("No LLM provider configured (set SALESIQ_API_URL + SALESIQ_API_KEY or OPENAI_API_KEY)")

# -------------------------
# High-level helpers
# -------------------------
async def generate_bot_suggestions(context: str, n: int = 1) -> List[str]:
    system = "You are a helpful customer-support assistant. Provide brief, professional reply suggestions."
    user = f"Context:\n{context}\n\nReturn up to {n} concise suggested replies, each on its own line."

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        out = await call_chat(messages, max_tokens=200, temperature=0.15)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if not lines:
            return ["Thanks — we'll check and get back to you shortly."]
        return lines[:n]
    except Exception:
        logger.exception("generate_bot_suggestions failed")
        return ["Thanks — we will investigate and follow up shortly."][:n]


async def summarize_short_sentences(text: str, n: int = 3) -> Tuple[List[str], Dict[str, Any]]:
    system = (
        "You are an assistant that summarizes customer-agent chat histories into short, high-level "
        "theoretical sentences. Produce exactly {n} independent sentences. Each sentence should be "
        "concise (10-25 words), abstract, and actionable — focusing on causes, effects, risks, or "
        "recommendations rather than chat details. Do NOT include quotes or personal identifiers. "
        "Use present-tense, professional tone. After the sentences, output a JSON object on its own "
        "line with keys: topics (list of strings), sentiment (one of positive/neutral/negative/mixed)."
    ).replace("{n}", str(n))

    user = f"Conversation:\n{text}\n\nReturn exactly {n} short sentences, each on its own line, followed by the JSON metadata on its own line."

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    try:
        raw = await call_chat(messages, max_tokens=512, temperature=0.15)
        lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]

        sentences = lines[:n]
        meta = {}
        if len(lines) > n:
            meta_text = "\n".join(lines[n:]).strip()
            try:
                meta = json.loads(meta_text)
            except Exception:
                idx = meta_text.rfind("{")
                if idx != -1:
                    try:
                        meta = json.loads(meta_text[idx:])
                    except Exception:
                        meta = {}
        if not sentences:
            sentences = ["Customer raised repeated issues requiring escalation.", "Agent responses improved response time but didn't close the issue.", "Recommend manager intervention."][:n]
        return sentences, meta or {}
    except Exception:
        logger.exception("summarize_short_sentences failed")
        return ([
            "Customer reports repeated unresolved issues requiring escalation.",
            "Agent provided clarifications but could not resolve technical blockers.",
            "Recommend manager review and prioritized follow-up."
        ][:n], {"topics": ["escalation", "follow-up"], "sentiment": "mixed"})


# -------------------------
# Chunking helpers and reduce
# -------------------------
def _chunk_text_by_chars(text: str, max_chars: int = 4000) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    lines = text.splitlines(keepends=True)
    chunks = []
    cur = []
    cur_len = 0
    for ln in lines:
        ln_len = len(ln)
        if cur_len + ln_len > max_chars and cur:
            chunks.append("".join(cur))
            cur = []
            cur_len = 0
        cur.append(ln)
        cur_len += ln_len
    if cur:
        chunks.append("".join(cur))
    return chunks


async def simple_chunk_and_summarize(text: str, sentences_per_chunk: int = 2, final_sentences: int = 3) -> Tuple[List[str], Dict[str, Any]]:
    max_chars = 4000
    chunks = _chunk_text_by_chars(text, max_chars=max_chars)
    chunk_summaries = []
    topics_acc: List[str] = []
    sentiments = []

    for c in chunks:
        sents, meta = await summarize_short_sentences(c, n=sentences_per_chunk)
        chunk_summaries.append(" ".join(sents))
        if isinstance(meta, dict):
            if meta.get("topics"):
                topics_acc.extend(meta.get("topics", []))
            if meta.get("sentiment"):
                sentiments.append(meta.get("sentiment"))

    combined_text = "\n".join(chunk_summaries)
    final_sents, final_meta = await summarize_short_sentences(combined_text, n=final_sentences)

    final_topics = list(dict.fromkeys((final_meta.get("topics") or []) + topics_acc))
    final_sentiment = final_meta.get("sentiment") or (sentiments[-1] if sentiments else "mixed")

    return final_sents, {"topics": final_topics, "sentiment": final_sentiment}


# -------------------------
# Utility: cosine similarity
# -------------------------
def cosine_sim(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norma = math.sqrt(sum(x * x for x in a))
    normb = math.sqrt(sum(y * y for y in b))
    if norma == 0 or normb == 0:
        return 0.0
    return dot / (norma * normb)


# -------------------------
# Convenience: summarize messages for range
# -------------------------
async def summarize_messages_for_range(messages: List[Dict[str, Any]], n_sentences: int = 3) -> Tuple[List[str], Dict[str, Any]]:
    lines = []
    for m in messages:
        ts = m.get("created_at")
        sender = m.get("sender", "")
        text = m.get("message", "")
        if isinstance(ts, str):
            lines.append(f"{ts} {sender}: {text}")
        else:
            lines.append(f"{text}" if not ts else f"{ts.isoformat()} {sender}: {text}")
    convo = "\n".join(lines)
    if len(convo) > 12000:
        return await simple_chunk_and_summarize(convo, sentences_per_chunk=2, final_sentences=n_sentences)
    return await summarize_short_sentences(convo, n=n_sentences)
