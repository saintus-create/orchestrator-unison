"""
Emergent Multi-Agent Orchestrator backend.

- FastAPI with /api prefix.
- Mongo via MONGO_URL.
- LLM calls via emergentintegrations (Claude Sonnet 4.5).
- GitHub: real API when a PAT is provided per session, otherwise stubbed.
"""
from __future__ import annotations

import os
import uuid
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, APIRouter, HTTPException, UploadFile, File, Form
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, ConfigDict

from agents import (
    AGENTS,
    run_pipeline,
    sort_document,
    build_frontmatter,
    run_audit,
    call_agent,
    call_custom_agent,
)
from router import route as router_route

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("orchestrator")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

app = FastAPI(title="Emergent Multi-Agent Orchestrator")
api = APIRouter(prefix="/api")


# --------------------------------------------------------------------- helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def log_activity(kind: str, agent: Optional[str], summary: str, meta: Optional[dict] = None) -> None:
    doc = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "agent": agent,
        "summary": summary,
        "meta": meta or {},
        "timestamp": now_iso(),
    }
    await db.activity.insert_one(doc)


# ---------------------------------------------------------------------- models
class AgentInfo(BaseModel):
    key: str
    name: str
    role: str
    status: str = "idle"
    last_run_at: Optional[str] = None


class ProjectCreate(BaseModel):
    name: str
    spec: str


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    spec: str
    status: str = "draft"
    output: Optional[dict] = None
    created_at: str
    updated_at: str


class GithubConnect(BaseModel):
    session_id: str
    token: str


class CustomAgentCreate(BaseModel):
    key: str
    name: str
    role: str
    system_message: str


class ConsoleSessionCreate(BaseModel):
    title: Optional[str] = None
    agent_key: str = "designer"


class ConsoleMessageCreate(BaseModel):
    content: str
    agent_key: Optional[str] = None  # override per-message if desired


class McpInvoke(BaseModel):
    tool: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatAttachment(BaseModel):
    filename: str
    content: str


class ChatSendPayload(BaseModel):
    content: str
    attachments: list[ChatAttachment] = Field(default_factory=list)


class SortFilePayload(BaseModel):
    filename: str
    content: str


class SortJobCreate(BaseModel):
    name: str
    files: list[SortFilePayload]


# ----------------------------------------------------------------- agents API
@api.get("/agents", response_model=list[AgentInfo])
async def list_agents() -> list[AgentInfo]:
    runs = {
        d["agent_key"]: d["last_run_at"]
        async for d in db.agent_runs.find({}, {"_id": 0})
    }
    out: list[AgentInfo] = []
    for key, persona in AGENTS.items():
        out.append(
            AgentInfo(
                key=key,
                name=persona["name"],
                role=persona["role"],
                status="idle",
                last_run_at=runs.get(key),
            )
        )
    custom = await db.custom_agents.find({}, {"_id": 0}).to_list(200)
    for c in custom:
        out.append(
            AgentInfo(
                key=c["key"],
                name=c["name"],
                role=c["role"],
                status="idle",
                last_run_at=runs.get(c["key"]),
            )
        )
    return out


@api.post("/agents", response_model=AgentInfo)
async def create_custom_agent(payload: CustomAgentCreate) -> AgentInfo:
    key = payload.key.strip().lower().replace(" ", "-")
    if not key or key in AGENTS:
        raise HTTPException(400, "Key is empty or collides with a built-in agent.")
    if await db.custom_agents.find_one({"key": key}):
        raise HTTPException(400, "An agent with that key already exists.")
    doc = {
        "key": key,
        "name": payload.name.strip() or key,
        "role": payload.role.strip(),
        "system_message": payload.system_message.strip(),
        "created_at": now_iso(),
    }
    await db.custom_agents.insert_one(dict(doc))
    await log_activity("agent.created", key, f"Custom agent created: {doc['name']}")
    return AgentInfo(key=key, name=doc["name"], role=doc["role"])


@api.delete("/agents/{key}")
async def delete_custom_agent(key: str) -> dict[str, str]:
    if key in AGENTS:
        raise HTTPException(400, "Built-in agents cannot be deleted.")
    res = await db.custom_agents.delete_one({"key": key})
    if res.deleted_count == 0:
        raise HTTPException(404, "Agent not found.")
    await log_activity("agent.deleted", key, f"Custom agent deleted: {key}")
    return {"status": "deleted"}


async def _resolve_agent(key: str) -> tuple[str, str]:
    """Return (system_message, display_name) for either built-in or custom agent."""
    if key in AGENTS:
        return AGENTS[key]["system"], AGENTS[key]["name"]
    custom = await db.custom_agents.find_one({"key": key}, {"_id": 0})
    if not custom:
        raise HTTPException(404, f"Unknown agent: {key}")
    return custom["system_message"], custom["name"]


# ----------------------------------------------------------------- projects API
@api.post("/projects", response_model=Project)
async def create_project(payload: ProjectCreate) -> Project:
    doc = {
        "id": str(uuid.uuid4()),
        "name": payload.name,
        "spec": payload.spec,
        "status": "draft",
        "output": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    await db.projects.insert_one(dict(doc))
    await log_activity("project.created", None, f"Project created: {payload.name}")
    return Project(**doc)


@api.get("/projects", response_model=list[Project])
async def list_projects() -> list[Project]:
    docs = await db.projects.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return [Project(**d) for d in docs]


@api.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str) -> Project:
    doc = await db.projects.find_one({"id": project_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Project not found")
    return Project(**doc)


@api.post("/projects/{project_id}/run", response_model=Project)
async def run_project(project_id: str) -> Project:
    doc = await db.projects.find_one({"id": project_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Project not found")

    await db.projects.update_one(
        {"id": project_id}, {"$set": {"status": "running", "updated_at": now_iso()}}
    )
    await log_activity("project.run", None, f"Pipeline started for {doc['name']}")

    try:
        result = await run_pipeline(doc["spec"], session_id=project_id)
        ts = now_iso()
        for key in ("designer", "coder", "qa", "deployer"):
            await db.agent_runs.update_one(
                {"agent_key": key},
                {"$set": {"agent_key": key, "last_run_at": ts}},
                upsert=True,
            )
            await log_activity(
                "agent.run", key, f"{AGENTS[key]['name']} agent completed for {doc['name']}"
            )
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"status": "completed", "output": result, "updated_at": ts}},
        )
        doc.update({"status": "completed", "output": result, "updated_at": ts})
    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline failed")
        ts = now_iso()
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"status": "failed", "output": {"error": str(exc)}, "updated_at": ts}},
        )
        await log_activity("project.failed", None, f"Pipeline failed: {exc}")
        raise HTTPException(500, f"Pipeline failed: {exc}") from exc

    return Project(**doc)


# --------------------------------------------------------------- file sorter API
@api.post("/sort/jobs")
async def create_sort_job(payload: SortJobCreate) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    files = [
        {"filename": f.filename, "content": f.content, "result": None, "status": "pending"}
        for f in payload.files
    ]
    doc = {
        "id": job_id,
        "name": payload.name,
        "status": "pending",
        "files": files,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    await db.sort_jobs.insert_one(dict(doc))
    await log_activity(
        "sort.job_created",
        "sorter",
        f"Sort job created: {payload.name} ({len(files)} files)",
    )
    return {k: v for k, v in doc.items() if k != "_id"}


@api.post("/sort/upload")
async def upload_sort_files(
    name: str = Form(...),
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    payload_files: list[SortFilePayload] = []
    for upload in files:
        raw = await upload.read()
        try:
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            text = ""
        payload_files.append(SortFilePayload(filename=upload.filename or "untitled.md", content=text))
    return await create_sort_job(SortJobCreate(name=name, files=payload_files))


@api.get("/sort/jobs")
async def list_sort_jobs() -> list[dict]:
    docs = await db.sort_jobs.find({}, {"_id": 0}).sort("created_at", -1).to_list(100)
    # collapse files for the list view
    summary = []
    for d in docs:
        summary.append(
            {
                "id": d["id"],
                "name": d["name"],
                "status": d["status"],
                "file_count": len(d.get("files", [])),
                "created_at": d["created_at"],
            }
        )
    return summary


@api.get("/sort/jobs/{job_id}")
async def get_sort_job(job_id: str) -> dict[str, Any]:
    doc = await db.sort_jobs.find_one({"id": job_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Sort job not found")
    return doc


@api.post("/sort/jobs/{job_id}/run")
async def run_sort_job(job_id: str) -> dict[str, Any]:
    doc = await db.sort_jobs.find_one({"id": job_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Sort job not found")

    await db.sort_jobs.update_one(
        {"id": job_id}, {"$set": {"status": "running", "updated_at": now_iso()}}
    )

    async def process(file_entry: dict) -> dict:
        try:
            meta = await sort_document(
                file_entry["filename"], file_entry["content"], session_id=job_id
            )
            front = build_frontmatter(meta)
            body = file_entry["content"]
            # strip an existing simple frontmatter block, if any
            if body.startswith("---"):
                end = body.find("---", 3)
                if end != -1:
                    body = body[end + 3 :].lstrip("\n")
            new_content = front + "\n" + body
            return {
                **file_entry,
                "status": "done",
                "result": {**meta, "new_content": new_content},
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("sort failed for %s", file_entry["filename"])
            return {**file_entry, "status": "failed", "result": {"error": str(exc)}}

    processed = await asyncio.gather(*(process(f) for f in doc["files"]))
    ts = now_iso()
    await db.sort_jobs.update_one(
        {"id": job_id},
        {"$set": {"status": "completed", "files": processed, "updated_at": ts}},
    )
    await db.agent_runs.update_one(
        {"agent_key": "sorter"},
        {"$set": {"agent_key": "sorter", "last_run_at": ts}},
        upsert=True,
    )
    await log_activity(
        "sort.completed",
        "sorter",
        f"Sorted {len(processed)} files for job {doc['name']}",
    )
    updated = await db.sort_jobs.find_one({"id": job_id}, {"_id": 0})
    return updated


# ----------------------------------------------------------------- github API
@api.post("/github/connect")
async def github_connect(payload: GithubConnect) -> dict[str, Any]:
    # validate token by hitting /user
    async with httpx.AsyncClient(timeout=15.0) as cx:
        r = await cx.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {payload.token}", "Accept": "application/vnd.github+json"},
        )
    if r.status_code != 200:
        raise HTTPException(400, f"GitHub rejected the token: {r.status_code}")
    user = r.json()
    await db.github_sessions.update_one(
        {"session_id": payload.session_id},
        {"$set": {
            "session_id": payload.session_id,
            "token": payload.token,
            "login": user.get("login"),
            "avatar_url": user.get("avatar_url"),
            "connected_at": now_iso(),
        }},
        upsert=True,
    )
    await log_activity("github.connected", None, f"Connected GitHub as {user.get('login')}")
    return {"login": user.get("login"), "avatar_url": user.get("avatar_url")}


async def _gh_token(session_id: str) -> Optional[str]:
    s = await db.github_sessions.find_one({"session_id": session_id}, {"_id": 0})
    return s["token"] if s else None


@api.get("/github/status")
async def github_status(session_id: str) -> dict[str, Any]:
    s = await db.github_sessions.find_one({"session_id": session_id}, {"_id": 0, "token": 0})
    if not s:
        return {"connected": False}
    return {"connected": True, **s}


@api.get("/github/repos")
async def github_repos(session_id: str) -> list[dict]:
    token = await _gh_token(session_id)
    if not token:
        # stub repos so the UI is usable without a token
        return [
            {"name": "fumadocs-site", "full_name": "you/fumadocs-site", "private": False, "stub": True},
            {"name": "internal-tools", "full_name": "you/internal-tools", "private": True, "stub": True},
        ]
    async with httpx.AsyncClient(timeout=20.0) as cx:
        r = await cx.get(
            "https://api.github.com/user/repos?per_page=50&sort=updated",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return [
        {"name": d["name"], "full_name": d["full_name"], "private": d["private"], "stub": False}
        for d in r.json()
    ]


@api.get("/github/repos/{owner}/{repo}/tree")
async def github_repo_tree(owner: str, repo: str, session_id: str) -> dict[str, Any]:
    token = await _gh_token(session_id)
    if not token:
        return {
            "stub": True,
            "tree": [
                {"path": "content/docs/index.mdx", "type": "blob"},
                {"path": "content/docs/getting-started.mdx", "type": "blob"},
                {"path": "content/docs/concepts/agents.mdx", "type": "blob"},
            ],
        }
    async with httpx.AsyncClient(timeout=20.0) as cx:
        r = await cx.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return {"stub": False, "tree": r.json().get("tree", [])}


# -------------------------------------------------------------------- audit API
@api.post("/audit/run")
async def audit_run() -> dict[str, Any]:
    events = await db.activity.find({}, {"_id": 0}).sort("timestamp", -1).to_list(50)
    if not events:
        report = "No activity has been recorded yet, so there is nothing to audit."
    else:
        report = await run_audit(events, session_id="self-audit")
    ts = now_iso()
    audit_doc = {
        "id": str(uuid.uuid4()),
        "report": report,
        "events_reviewed": len(events),
        "created_at": ts,
    }
    await db.audits.insert_one(dict(audit_doc))
    await db.agent_runs.update_one(
        {"agent_key": "auditor"},
        {"$set": {"agent_key": "auditor", "last_run_at": ts}},
        upsert=True,
    )
    await log_activity("audit.completed", "auditor", f"Self audit completed over {len(events)} events")
    return {k: v for k, v in audit_doc.items() if k != "_id"}


@api.get("/audit")
async def list_audits() -> list[dict]:
    docs = await db.audits.find({}, {"_id": 0}).sort("created_at", -1).to_list(50)
    return docs


# ----------------------------------------------------------------- activity API
@api.get("/activity")
async def list_activity(limit: int = 100) -> list[dict]:
    docs = await db.activity.find({}, {"_id": 0}).sort("timestamp", -1).to_list(min(limit, 500))
    return docs


# ---------------------------------------------------------------- console API
@api.post("/console/sessions")
async def create_console_session(payload: ConsoleSessionCreate) -> dict[str, Any]:
    _, name = await _resolve_agent(payload.agent_key)
    doc = {
        "id": str(uuid.uuid4()),
        "title": payload.title or f"Conversation with {name}",
        "agent_key": payload.agent_key,
        "messages": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    await db.console_sessions.insert_one(dict(doc))
    return {k: v for k, v in doc.items() if k != "_id"}


@api.get("/console/sessions")
async def list_console_sessions() -> list[dict]:
    docs = await db.console_sessions.find(
        {}, {"_id": 0, "messages": 0}
    ).sort("updated_at", -1).to_list(100)
    return docs


@api.get("/console/sessions/{session_id}")
async def get_console_session(session_id: str) -> dict[str, Any]:
    doc = await db.console_sessions.find_one({"id": session_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Session not found")
    return doc


@api.delete("/console/sessions/{session_id}")
async def delete_console_session(session_id: str) -> dict[str, str]:
    res = await db.console_sessions.delete_one({"id": session_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Session not found")
    return {"status": "deleted"}


@api.post("/console/sessions/{session_id}/messages")
async def send_console_message(session_id: str, payload: ConsoleMessageCreate) -> dict[str, Any]:
    doc = await db.console_sessions.find_one({"id": session_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Session not found")

    agent_key = payload.agent_key or doc["agent_key"]
    system_message, agent_name = await _resolve_agent(agent_key)

    history = doc.get("messages", [])
    # Build a flat prompt that includes prior turns. emergentintegrations'
    # LlmChat carries history within a session_id, but to keep the call
    # idempotent across processes we reconstruct a transcript explicitly.
    transcript_parts = []
    for m in history:
        role = "User" if m["role"] == "user" else "Assistant"
        transcript_parts.append(f"{role}: {m['content']}")
    transcript_parts.append(f"User: {payload.content}")
    composite_prompt = "\n\n".join(transcript_parts)

    try:
        reply = await call_custom_agent(
            system_message=system_message,
            prompt=composite_prompt,
            session_id=f"console:{session_id}",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Agent call failed: {exc}") from exc

    ts = now_iso()
    new_messages = history + [
        {"role": "user", "content": payload.content, "timestamp": ts},
        {
            "role": "assistant",
            "content": reply,
            "agent_key": agent_key,
            "agent_name": agent_name,
            "timestamp": now_iso(),
        },
    ]
    await db.console_sessions.update_one(
        {"id": session_id},
        {"$set": {"messages": new_messages, "updated_at": now_iso(), "agent_key": agent_key}},
    )
    await db.agent_runs.update_one(
        {"agent_key": agent_key},
        {"$set": {"agent_key": agent_key, "last_run_at": ts}},
        upsert=True,
    )
    await log_activity("console.message", agent_key, f"Message sent to {agent_name}")
    updated = await db.console_sessions.find_one({"id": session_id}, {"_id": 0})
    return updated


# ----------------------------------------------------------------------- chat
@api.post("/chat/sessions")
async def create_chat_session() -> dict[str, Any]:
    doc = {
        "id": str(uuid.uuid4()),
        "title": "New conversation",
        "messages": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    await db.chat_sessions.insert_one(dict(doc))
    return {k: v for k, v in doc.items() if k != "_id"}


@api.get("/chat/sessions")
async def list_chat_sessions() -> list[dict]:
    docs = await db.chat_sessions.find(
        {}, {"_id": 0, "messages": 0}
    ).sort("updated_at", -1).to_list(100)
    return docs


@api.get("/chat/sessions/{session_id}")
async def get_chat_session(session_id: str) -> dict[str, Any]:
    doc = await db.chat_sessions.find_one({"id": session_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Session not found")
    return doc


@api.delete("/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str) -> dict[str, str]:
    res = await db.chat_sessions.delete_one({"id": session_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Session not found")
    return {"status": "deleted"}


def _format_pipeline_result(result: dict[str, str]) -> str:
    """Render the pipeline output as a single readable text block."""
    parts = []
    labels = {
        "designer": "Design brief",
        "coder": "Implementation plan",
        "qa": "QA review",
        "deployer": "Deployment plan",
    }
    for key in ("designer", "coder", "qa", "deployer"):
        if key in result:
            parts.append(f"### {labels[key]}\n\n{result[key]}")
    return "\n\n".join(parts)


async def _finish_pipeline_in_chat(session_id: str, message_index: int, project_id: str) -> None:
    """Background task: run the pipeline, then update the assistant message in place."""
    try:
        ran = await run_project(project_id)
        rendered = _format_pipeline_result(ran.output or {})
        new_content = rendered or "Pipeline completed but produced no output."
        artifact = {
            "kind": "project",
            "project": ran.model_dump(),
            "rendered": rendered,
            "status": "completed",
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("background pipeline failed")
        new_content = f"Pipeline failed: {exc}"
        artifact = {
            "kind": "project",
            "project_id": project_id,
            "status": "failed",
            "error": str(exc),
        }

    await db.chat_sessions.update_one(
        {"id": session_id},
        {
            "$set": {
                f"messages.{message_index}.content": new_content,
                f"messages.{message_index}.artifact": artifact,
                "updated_at": now_iso(),
            }
        },
    )


@api.post("/chat/sessions/{session_id}/send")
async def chat_send(session_id: str, payload: ChatSendPayload) -> dict[str, Any]:
    doc = await db.chat_sessions.find_one({"id": session_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Session not found")

    history = doc.get("messages", [])
    attachments = [a.model_dump() for a in payload.attachments]

    # 1. Ask the router what to do.
    decision = await router_route(
        user_message=payload.content,
        history=history,
        attachments=attachments,
        session_id=session_id,
    )
    action = decision["action"]
    reply_intro = decision.get("reply", "").strip()
    artifact: dict[str, Any] | None = None

    # 2. Execute the chosen action and assemble the reply.
    if action == "sort" and attachments:
        files = [
            SortFilePayload(filename=a["filename"], content=a["content"])
            for a in attachments
        ]
        job = await create_sort_job(
            SortJobCreate(name=payload.content[:60] or "Chat sort job", files=files)
        )
        ran = await run_sort_job(job["id"])
        artifact = {"kind": "sort_job", "job": ran}
        full_reply = reply_intro or (
            f"I sorted {len(ran['files'])} file"
            f"{'s' if len(ran['files']) != 1 else ''} for you. The results are below."
        )
    elif action == "build":
        spec = decision.get("spec") or payload.content
        project = await create_project(
            ProjectCreate(name=payload.content[:60] or "Chat project", spec=spec)
        )
        artifact = {
            "kind": "project",
            "project_id": project.id,
            "project": project.model_dump(),
            "status": "running",
        }
        full_reply = (
            reply_intro
            or "I am running the design, code, QA, and deployment agents in parallel. This usually takes a couple of minutes; the output will appear here when it is ready."
        )
        # The pipeline is too slow for a synchronous request; finish it in the
        # background and patch the assistant message when complete.
        # message_index is computed below after we save the placeholder.
        # We schedule the task after the DB write to know the exact index.
    elif action == "audit":
        result = await audit_run()
        artifact = {"kind": "audit", "audit": result}
        full_reply = (reply_intro + "\n\n" + result["report"]).strip() if reply_intro else result["report"]
    else:
        # plain conversational reply: ask the Designer agent (the most generalist
        # of our personas) to answer directly. Reuse the router's reply if it
        # already produced one of substance.
        if reply_intro and len(reply_intro) > 40:
            full_reply = reply_intro
        else:
            full_reply = await call_custom_agent(
                system_message=AGENTS["designer"]["system"],
                prompt=payload.content,
                session_id=f"chat:{session_id}",
            )

    ts = now_iso()
    new_messages = history + [
        {
            "role": "user",
            "content": payload.content,
            "attachments": [a["filename"] for a in attachments],
            "timestamp": ts,
        },
        {
            "role": "assistant",
            "content": full_reply,
            "action": action,
            "artifact": artifact,
            "timestamp": now_iso(),
        },
    ]
    title = doc.get("title") or "New conversation"
    if title == "New conversation":
        title = payload.content.strip()[:60] or "New conversation"

    await db.chat_sessions.update_one(
        {"id": session_id},
        {"$set": {"messages": new_messages, "updated_at": now_iso(), "title": title}},
    )
    if action == "build" and artifact and artifact.get("status") == "running":
        # The assistant message we just saved is the last one; len-1.
        asyncio.create_task(
            _finish_pipeline_in_chat(session_id, len(new_messages) - 1, artifact["project_id"])
        )
    await log_activity("chat.message", None, f"Chat: {action} — {title}")
    updated = await db.chat_sessions.find_one({"id": session_id}, {"_id": 0})
    return updated
