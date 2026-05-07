import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { toast } from "sonner";
import {
    Send,
    Plus,
    Trash2,
    Loader2,
    Paperclip,
    Sparkles,
    FileText,
    Download,
    X,
} from "lucide-react";
import { OrbitingCirclesDemo } from "../components/ui/OrbitingCirclesDemo";

const SUGGESTIONS = [
    "Sort these documentation files into proper Fumadocs structure.",
    "Build a small internal tool that turns CSV exports into a printable report.",
    "Audit the system and tell me what looks off.",
    "Create a visualization of my connected services using orbiting circles.",
];

function downloadText(filename, text) {
    const blob = new Blob([text], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

function ArtifactView({ artifact }) {
    if (!artifact) return null;
    if (artifact.kind === "sort_job") {
        const job = artifact.job;
        const failures = (job?.files || []).filter((f) => f.status === "failed").length;
        return (
            <div className="mt-3 border border-border" style={{ borderRadius: 2 }}>
                <div className="px-4 py-2 bg-secondary border-b border-border text-xs flex items-center justify-between">
                    <span>Sort job: {job.name} • {job.files.length} files</span>
                    {failures > 0 && (
                        <span className="uswds-tag" style={{ borderColor: "hsl(var(--destructive))", color: "hsl(var(--destructive))" }}>
                            {failures} failed
                        </span>
                    )}
                </div>
                <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="text-left border-b border-border">
                                <th className="py-2 px-3">Original</th>
                                <th className="py-2 px-3">Category</th>
                                <th className="py-2 px-3">Proposed filename</th>
                                <th className="py-2 px-3">Title</th>
                                <th className="py-2 px-3"></th>
                            </tr>
                        </thead>
                        <tbody>
                            {job.files.map((f, i) => {
                                const r = f.result || {};
                                const failed = f.status === "failed";
                                return (
                                    <tr key={i} className="border-b border-border last:border-b-0">
                                        <td className="py-2 px-3 max-w-[200px] truncate">{f.filename}</td>
                                        <td className="py-2 px-3">
                                            {failed ? (
                                                <span className="uswds-tag" style={{ borderColor: "hsl(var(--destructive))", color: "hsl(var(--destructive))" }}>
                                                    failed
                                                </span>
                                            ) : (
                                                <span className="uswds-tag">{r.category || "—"}</span>
                                            )}
                                        </td>
                                        <td className="py-2 px-3 max-w-[220px] truncate">
                                            {r.filename || "—"}
                                        </td>
                                        <td className="py-2 px-3 max-w-[260px] truncate">
                                            {r.title || (failed ? r.error || "see logs" : "—")}
                                        </td>
                                        <td className="py-2 px-3">
                                            {r.new_content && (
                                                <button
                                                    onClick={() =>
                                                        downloadText(
                                                            r.filename || f.filename,
                                                            r.new_content,
                                                        )
                                                    }
                                                    className="inline-flex items-center gap-1 text-uswds-blue hover:underline text-xs"
                                                >
                                                    <Download size={12} /> Download
                                                </button>
                                            )}
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            </div>
        );
    }
    if (artifact.kind === "project") {
        const status = artifact.status;
        if (status === "running") {
            return (
                <div className="mt-3 border border-border bg-secondary px-4 py-3 text-sm flex items-center gap-2" style={{ borderRadius: 2 }}>
                    <Loader2 size={14} className="animate-spin" />
                    The Designer, Coder, QA and Deployer are working. The output will appear here when it is ready.
                </div>
            );
        }
        if (status === "failed") {
            return (
                <div className="mt-3 border border-destructive bg-secondary px-4 py-3 text-sm" style={{ borderRadius: 2 }}>
                    Pipeline failed. {artifact.error}
                </div>
            );
        }
        if (status === "completed") {
            const hasOrbit = artifact.rendered?.includes("OrbitingCircles");
            return (
                <div className="mt-3">
                    {hasOrbit && (
                        <div className="mb-4 border border-border p-4 bg-white shadow-sm overflow-hidden" style={{ borderRadius: 2 }}>
                            <div className="text-xs text-muted-foreground mb-2 font-bold uppercase tracking-wider">Live Visualization</div>
                            <OrbitingCirclesDemo />
                        </div>
                    )}
                    <div className="uswds-card bg-secondary p-4 text-sm prose max-w-none prose-sm">
                        <pre className="whitespace-pre-wrap font-sans">{artifact.rendered}</pre>
                    </div>
                </div>
            );
        }
        return null;
    }
    return null;
}

export default function Chat() {
    const [sessions, setSessions] = useState([]);
    const [active, setActive] = useState(null);
    const [input, setInput] = useState("");
    const [busy, setBusy] = useState(false);
    const [staged, setStaged] = useState([]); // files staged for next message
    const [dragOver, setDragOver] = useState(false);
    const fileRef = useRef(null);
    const scrollRef = useRef(null);

    async function loadSessions() {
        const r = await api.get("/chat/sessions");
        setSessions(r.data);
    }
    useEffect(() => {
        loadSessions();
    }, []);

    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
    }, [active, busy]);

    // Poll the active session when there is a running pipeline artifact.
    useEffect(() => {
        if (!active) return undefined;
        const hasRunning = (active.messages || []).some(
            (m) => m.artifact?.kind === "project" && m.artifact?.status === "running",
        );
        if (!hasRunning) return undefined;
        const t = setInterval(async () => {
            try {
                const r = await api.get(`/chat/sessions/${active.id}`);
                setActive(r.data);
                const stillRunning = (r.data.messages || []).some(
                    (m) => m.artifact?.kind === "project" && m.artifact?.status === "running",
                );
                if (!stillRunning) {
                    clearInterval(t);
                    await loadSessions();
                }
            } catch {
                /* swallow transient errors and keep polling */
            }
        }, 3000);
        return () => clearInterval(t);
    }, [active?.id, active?.messages]);

    async function ensureSession() {
        if (active) return active;
        const r = await api.post("/chat/sessions");
        setActive(r.data);
        await loadSessions();
        return r.data;
    }

    async function openSession(id) {
        const r = await api.get(`/chat/sessions/${id}`);
        setActive(r.data);
    }

    async function removeSession(id) {
        await api.delete(`/chat/sessions/${id}`);
        if (active?.id === id) setActive(null);
        await loadSessions();
    }

    async function startFresh() {
        setActive(null);
        setStaged([]);
    }

    async function readFiles(fileList) {
        const arr = await Promise.all(
            Array.from(fileList).map(
                (f) =>
                    new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onload = () =>
                            resolve({
                                filename: f.name,
                                content: String(reader.result || ""),
                            });
                        reader.readAsText(f);
                    }),
            ),
        );
        setStaged((prev) => [...prev, ...arr]);
    }

    function onDrop(e) {
        e.preventDefault();
        setDragOver(false);
        if (e.dataTransfer.files?.length) readFiles(e.dataTransfer.files);
    }

    async function send() {
        if (!input.trim() && staged.length === 0) return;
        const session = await ensureSession();
        const text = input;
        const attachments = staged;
        setInput("");
        setStaged([]);
        setActive((cur) => ({
            ...(cur || session),
            messages: [
                ...((cur || session).messages || []),
                {
                    role: "user",
                    content: text,
                    attachments: attachments.map((a) => a.filename),
                    timestamp: new Date().toISOString(),
                },
            ],
        }));
        setBusy(true);
        try {
            const r = await api.post(`/chat/sessions/${session.id}/send`, {
                content: text,
                attachments,
            });
            setActive(r.data);
            await loadSessions();
        } catch (err) {
            toast.error(err.response?.data?.detail || err.message);
        } finally {
            setBusy(false);
        }
    }

    function onKey(e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            send();
        }
    }

    return (
        <div className="animate-fade-in" data-testid="chat-page">
            <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
                <aside className="lg:col-span-1">
                    <div className="flex items-center justify-between mb-3">
                        <h2 className="text-2xl">Conversations</h2>
                        <button
                            onClick={startFresh}
                            className="inline-flex items-center gap-1 text-sm text-uswds-blue hover:underline"
                            data-testid="chat-new-btn"
                        >
                            <Plus size={14} /> New
                        </button>
                    </div>
                    <div className="uswds-card p-0">
                        {sessions.length === 0 ? (
                            <p className="text-sm text-muted-foreground p-4">
                                No conversations yet.
                            </p>
                        ) : (
                            <ul className="divide-y divide-border max-h-[70vh] overflow-y-auto">
                                {sessions.map((s) => (
                                    <li
                                        key={s.id}
                                        className={`flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-secondary ${
                                            active?.id === s.id ? "bg-accent" : ""
                                        }`}
                                        onClick={() => openSession(s.id)}
                                        data-testid={`chat-session-${s.id}`}
                                    >
                                        <div className="text-sm font-medium truncate flex-1 mr-2">
                                            {s.title}
                                        </div>
                                        <button
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                removeSession(s.id);
                                            }}
                                            className="text-muted-foreground hover:text-destructive"
                                            data-testid={`chat-delete-${s.id}`}
                                        >
                                            <Trash2 size={14} />
                                        </button>
                                    </li>
                                ))}
                            </ul>
                        )}
                    </div>
                </aside>

                <section className="lg:col-span-3">
                    {!active && (
                        <div className="mb-6">
                            <h1 className="text-4xl">What do you want to do?</h1>
                            <p className="text-base text-muted-foreground mt-2 max-w-2xl">
                                Type plainly. Attach files when you have them.
                                The agents will sort, build, audit, or just
                                answer — whichever fits the request.
                            </p>
                            <div className="flex flex-wrap gap-2 mt-4">
                                {SUGGESTIONS.map((s) => (
                                    <button
                                        key={s}
                                        onClick={() => setInput(s)}
                                        className="uswds-tag hover:bg-accent transition-colors"
                                        data-testid="chat-suggestion"
                                    >
                                        {s}
                                    </button>
                                ))}
                            </div>
                        </div>
                    )}

                    <div
                        className="uswds-card flex flex-col"
                        style={{ minHeight: "70vh" }}
                        onDragOver={(e) => {
                            e.preventDefault();
                            setDragOver(true);
                        }}
                        onDragLeave={() => setDragOver(false)}
                        onDrop={onDrop}
                        data-testid="chat-card"
                    >
                        <div
                            ref={scrollRef}
                            className="flex-1 overflow-y-auto"
                            data-testid="chat-transcript"
                        >
                            {!active || !active.messages?.length ? (
                                <div className="h-full flex flex-col items-center justify-center text-center text-muted-foreground py-16">
                                    <Sparkles size={28} className="text-uswds-blue mb-3" />
                                    <p className="text-sm max-w-sm">
                                        Drag and drop files here, or just start
                                        typing below. Press Enter to send.
                                    </p>
                                </div>
                            ) : (
                                <ul className="space-y-5">
                                    {active.messages.map((m, i) => (
                                        <li key={i} className="flex flex-col">
                                            <div className="text-xs text-muted-foreground mb-1">
                                                {m.role === "user" ? "You" : "Orchestrator"}
                                                {m.action && m.role === "assistant" && (
                                                    <span className="uswds-tag ml-2">
                                                        {m.action}
                                                    </span>
                                                )}
                                            </div>
                                            <div
                                                className="kbd-block"
                                                style={
                                                    m.role === "user"
                                                        ? {
                                                              borderLeft: "3px solid hsl(var(--primary))",
                                                          }
                                                        : undefined
                                                }
                                            >
                                                {m.content}
                                            </div>
                                            {m.attachments?.length > 0 && (
                                                <div className="mt-2 flex flex-wrap gap-2">
                                                    {m.attachments.map((n, k) => (
                                                        <span key={k} className="uswds-tag">
                                                            <FileText size={10} /> {n}
                                                        </span>
                                                    ))}
                                                </div>
                                            )}
                                            <ArtifactView artifact={m.artifact} />
                                        </li>
                                    ))}
                                    {busy && (
                                        <li className="flex items-center gap-2 text-sm text-muted-foreground">
                                            <Loader2 size={14} className="animate-spin" />
                                            The agents are working...
                                        </li>
                                    )}
                                </ul>
                            )}
                        </div>

                        <div className="border-t border-border pt-3 mt-3">
                            {staged.length > 0 && (
                                <div className="flex flex-wrap gap-2 mb-2" data-testid="chat-staged">
                                    {staged.map((f, i) => (
                                        <span key={i} className="uswds-tag">
                                            <FileText size={10} /> {f.filename}
                                            <button
                                                onClick={() =>
                                                    setStaged((prev) =>
                                                        prev.filter((_, j) => j !== i),
                                                    )
                                                }
                                                className="ml-1 hover:text-destructive"
                                                data-testid={`chat-staged-remove-${i}`}
                                            >
                                                <X size={10} />
                                            </button>
                                        </span>
                                    ))}
                                </div>
                            )}
                            <div
                                className={`flex gap-2 items-end border ${
                                    dragOver ? "border-primary" : "border-input"
                                } bg-white px-2 py-2 transition-colors`}
                                style={{ borderRadius: 2 }}
                            >
                                <button
                                    onClick={() => fileRef.current?.click()}
                                    className="p-2 text-muted-foreground hover:text-foreground"
                                    title="Attach files"
                                    data-testid="chat-attach-btn"
                                >
                                    <Paperclip size={16} />
                                </button>
                                <input
                                    ref={fileRef}
                                    type="file"
                                    multiple
                                    accept=".md,.mdx,.markdown,.txt,text/plain,text/markdown"
                                    hidden
                                    onChange={(e) => e.target.files && readFiles(e.target.files)}
                                    data-testid="chat-file-input"
                                />
                                <textarea
                                    value={input}
                                    onChange={(e) => setInput(e.target.value)}
                                    onKeyDown={onKey}
                                    rows={1}
                                    placeholder="Tell the agents what to do."
                                    className="flex-1 resize-none outline-none text-sm bg-transparent py-2"
                                    style={{ minHeight: 32, maxHeight: 200 }}
                                    data-testid="chat-input"
                                />
                                <button
                                    onClick={send}
                                    disabled={busy}
                                    className="inline-flex items-center gap-2 bg-uswds-blue text-white px-4 py-2 hover:bg-uswds-blue-dark disabled:opacity-60"
                                    style={{ borderRadius: 2 }}
                                    data-testid="chat-send-btn"
                                >
                                    {busy ? (
                                        <Loader2 size={16} className="animate-spin" />
                                    ) : (
                                        <Send size={16} />
                                    )}
                                </button>
                            </div>
                            <div className="text-xs text-muted-foreground mt-2">
                                Press Enter to send. Shift + Enter for a new line.
                            </div>
                        </div>
                    </div>
                </section>
            </div>
        </div>
    );
}
