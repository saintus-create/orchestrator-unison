import axios from "axios";

const BACKEND_URL = "http://localhost:8000";
export const API = `${BACKEND_URL}/api`;

export const api = axios.create({
    baseURL: API,
    timeout: 300000,
});

// A persistent session id for github auth context.
const SESSION_KEY = "orchestrator.session_id";
export function getSessionId() {
    let id = localStorage.getItem(SESSION_KEY);
    if (!id) {
        id = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
        localStorage.setItem(SESSION_KEY, id);
    }
    return id;
}
