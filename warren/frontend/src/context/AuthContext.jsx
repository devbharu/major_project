import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, setUnauthorizedHandler, tokenStore } from "../api/client";

const AuthContext = createContext(null);
export const useAuth = () => useContext(AuthContext);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [config, setConfig] = useState({ github_oauth: false, demo: false });
  const [loading, setLoading] = useState(true);

  const logout = useCallback(() => {
    tokenStore.clear();
    setUser(null);
  }, []);

  const loadUser = useCallback(async () => {
    if (!tokenStore.get()) return setUser(null);
    try {
      setUser(await api.me());
    } catch {
      logout();
    }
  }, [logout]);

  useEffect(() => {
    setUnauthorizedHandler(logout);
    Promise.all([api.authConfig().then(setConfig).catch(() => {}), loadUser()]).finally(() => setLoading(false));
  }, [loadUser, logout]);

  const signInWithToken = useCallback(async (token) => {
    tokenStore.set(token);
    await loadUser();
  }, [loadUser]);

  const value = useMemo(() => ({ user, config, loading, logout, signInWithToken }), [user, config, loading, logout, signInWithToken]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
