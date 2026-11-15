import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { useLive } from "../hooks/useLive";
import { useAuth } from "./AuthContext";

// Shared dashboard state: connected repos, the PR board and its filters, and system health.
const ReviewContext = createContext(null);
export const useReview = () => useContext(ReviewContext);

export function ReviewProvider({ children }) {
  const { user } = useAuth();
  const [repos, setRepos] = useState([]);
  const [meta, setMeta] = useState({ public_url: false, poller: 0 });
  const [health, setHealth] = useState(null);
  const [board, setBoard] = useState({ columns: {}, counts: {}, total: 0 });
  const [view, setView] = useState("all");
  const [repoId, setRepoId] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [activeIncidents, setActiveIncidents] = useState(0);
  const requestId = useRef(0);

  const loadRepos = useCallback(async () => {
    const data = await api.repos();
    setRepos(data.repos);
    setMeta({ public_url: data.public_url, poller: data.poller });
    return data.repos;
  }, []);

  const loadBoard = useCallback(async () => {
    const id = ++requestId.current;
    setLoading(true);
    try {
      const data = await api.board({ view, repo_id: repoId, q: query });
      if (id === requestId.current) {
        setBoard(data);
        setError("");
      }
    } catch (e) {
      if (id === requestId.current) setError(e.message);
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [view, repoId, query]);

  const loadIncidentCount = useCallback(() => api.incidents().then((d) => setActiveIncidents(d.active)).catch(() => {}), []);

  useEffect(() => {
    if (!user) return;
    loadIncidentCount();
    loadRepos().catch((e) => setError(e.message));
    api.health().then(setHealth).catch(() => {});
  }, [user, loadRepos, loadIncidentCount]);

  // real-time: the server pushes a hint whenever a PR, review, job or CI incident changes
  useLive(user ? ["board", "incidents"] : [], () => {
    loadBoard();
    loadIncidentCount();
  });

  useEffect(() => {
    if (!user) return;
    const t = setTimeout(loadBoard, query ? 250 : 0); // debounce search typing
    return () => clearTimeout(t);
  }, [user, loadBoard, query]);

  const value = useMemo(() => ({
    repos, meta, health, board, view, setView, repoId, setRepoId, query, setQuery, loading, error, loadRepos, loadBoard, activeIncidents,
  }), [repos, meta, health, board, view, repoId, query, loading, error, loadRepos, loadBoard, activeIncidents]);

  return <ReviewContext.Provider value={value}>{children}</ReviewContext.Provider>;
}
