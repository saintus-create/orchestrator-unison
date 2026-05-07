"""
Agent personas and orchestration helpers.
All calls route through the Emergent universal LLM key via emergentintegrations.
"""
from __future__ import annotations

import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from emergentintegrations.llm.chat import LlmChat, UserMessage

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


def _key() -> str:
    return os.environ.get("EMERGENT_LLM_KEY", "")


AGENTS: dict[str, dict[str, str]] = {
    "designer": {
        "name": "Designer",
        "role": "Translates a design specification into concrete UI structure, component list, and visual notes.",
        "system": (
            "You are the Designer agent. Given a product or feature specification, "
            "produce a concise design brief: the top three user flows, the component "
            "inventory, the visual tone in plain language, and any accessibility "
            "considerations. Avoid filler. Use proper sentences, sentence case, no all caps."
        ),
    },
    "coder": {
        "name": "Coder",
        "role": "Produces an implementation plan and concrete code stubs.",
        "system": (
            "You are the Coder agent. Given a design brief, produce a numbered "
            "implementation plan, the file tree to create, and the most important "
            "code stubs in fenced code blocks. Be terse and technical.\n\n"
            "PREFERRED COMPONENTS:\n"
            "For visual representations of connected services or agents, use the 'OrbitingCircles' component from '@/components/ui/orbiting-circles'.\n"
            "Usage example:\n"
            "import { OrbitingCircles } from '@/components/ui/orbiting-circles';\n"
            "// Use with Icons from the Icons object: Icons.whatsapp, Icons.notion, etc.\n"
            "<OrbitingCircles radius={100} duration={20}><Icons.whatsapp /></OrbitingCircles>\n\n"
            "No marketing tone."
        ),
    },
    "qa": {
        "name": "QA",
        "role": "Reviews the plan and code for risks, gaps, and untested paths.",
        "system": (
            "You are the QA agent. Review the proposed plan and code for missing "
            "tests, edge cases, accessibility regressions, and integration risks. "
            "Output a short numbered list of concerns and recommended tests."
        ),
    },
    "deployer": {
        "name": "Deployer",
        "role": "Outlines deployment, environment, and rollout steps.",
        "system": (
            "You are the Deployer agent. Outline the deployment steps, the required "
            "environment variables, the rollout strategy, and the rollback plan. Be concrete."
        ),
    },
    "sorter": {
        "name": "Sorter",
        "role": "Classifies and reformats documentation files (Fumadocs MDX) for a docs site.",
        "system": (
            "You are the Sorter agent for a Fumadocs documentation site. "
            "Given the contents of a markdown or MDX file, decide: "
            "(1) the best top-level category from a small fixed taxonomy, "
            "(2) a slug-style filename in kebab-case ending in .mdx, "
            "(3) a short title in sentence case, "
            "(4) a one sentence description, "
            "(5) a short cleaned excerpt of the first 80 words. "
            "Reply with strict JSON only. Schema: "
            '{"category": str, "filename": str, "title": str, "description": str, "excerpt": str}. '
            "No prose, no fences, no commentary."
        ),
    },
    "auditor": {
        "name": "Auditor",
        "role": "Reviews recent system actions and surfaces issues.",
        "system": (
            "You are the Auditor agent. Given a JSON list of recent system events, "
            "produce a short audit report with: a one paragraph summary, a numbered "
            "list of any anomalies or risks, and a numbered list of recommended actions. "
            "Use sentence case headings. No filler."
        ),
    },
}


async def call_agent(
    agent_key: str,
    prompt: str,
    session_id: str,
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL,
) -> str:
    """Run a single agent persona against a prompt and return text output."""
    if agent_key not in AGENTS:
        raise ValueError(f"Unknown agent: {agent_key}")
    if not _key():
        raise RuntimeError("EMERGENT_LLM_KEY not configured")

    persona = AGENTS[agent_key]
    chat = LlmChat(
        api_key=_key(),
        session_id=f"{session_id}:{agent_key}",
        system_message=persona["system"],
    ).with_model(provider, model)

    response = await chat.send_message(UserMessage(text=prompt))
    return response if isinstance(response, str) else str(response)


async def run_pipeline(spec: str, session_id: str) -> dict[str, str]:
    """
    Run the four product agents. Designer first; then Coder, QA, Deployer
    are run in parallel against the design brief.
    """
    design_brief = await call_agent("designer", spec, session_id)

    coder_prompt = f"Specification:\n{spec}\n\nDesign brief from the Designer agent:\n{design_brief}"
    qa_prompt = f"Specification:\n{spec}\n\nDesign brief:\n{design_brief}"
    deployer_prompt = f"Specification:\n{spec}\n\nDesign brief:\n{design_brief}"

    coder_out, qa_out, deployer_out = await asyncio.gather(
        call_agent("coder", coder_prompt, session_id),
        call_agent("qa", qa_prompt, session_id),
        call_agent("deployer", deployer_prompt, session_id),
    )

    return {
        "designer": design_brief,
        "coder": coder_out,
        "qa": qa_out,
        "deployer": deployer_out,
    }


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # remove leading fence (```json or ```)
        first_newline = t.find("\n")
        if first_newline != -1:
            t = t[first_newline + 1 :]
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()


async def sort_document(filename: str, content: str, session_id: str) -> dict[str, Any]:
    """
    Ask the Sorter agent to classify and clean one document.
    Returns a dict with category, filename, title, description, excerpt.
    """
    truncated = content[:6000]
    prompt = (
        f"Original filename: {filename}\n\n"
        f"File contents (truncated to 6000 chars):\n{truncated}\n\n"
        "Categorize this Fumadocs documentation file. Reply with JSON only."
    )
    raw = await call_agent("sorter", prompt, session_id)
    cleaned = _strip_code_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # fall back: try to find a JSON object in the response
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                parsed = {}
        else:
            parsed = {}

    return {
        "category": parsed.get("category", "uncategorized"),
        "filename": parsed.get("filename", filename.lower().replace(" ", "-")),
        "title": parsed.get("title", filename),
        "description": parsed.get("description", ""),
        "excerpt": parsed.get("excerpt", ""),
    }


def build_frontmatter(meta: dict[str, Any]) -> str:
    """Build a Fumadocs-friendly frontmatter block from sorter output."""
    title = (meta.get("title") or "Untitled").replace('"', "'")
    description = (meta.get("description") or "").replace('"', "'")
    return (
        "---\n"
        f'title: "{title}"\n'
        f'description: "{description}"\n'
        "---\n"
    )


async def run_audit(events: list[dict[str, Any]], session_id: str) -> str:
    """Run the Auditor over recent system events."""
    payload = json.dumps(events[:50], default=str, indent=2)
    prompt = f"Recent system events (most recent first):\n{payload}"
    return await call_agent("auditor", prompt, session_id)


async def call_custom_agent(
    system_message: str,
    prompt: str,
    session_id: str,
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL,
) -> str:
    """Run a custom (user-defined) agent persona with an arbitrary system message."""
    if not _key():
        raise RuntimeError("EMERGENT_LLM_KEY not configured")
    chat = LlmChat(
        api_key=_key(),
        session_id=session_id,
        system_message=system_message or "You are a helpful, terse assistant.",
    ).with_model(provider, model)
    response = await chat.send_message(UserMessage(text=prompt))
    return response if isinstance(response, str) else str(response)
