import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { App } from "./App.js";
import type { State } from "./types.js";
import "./style.css";

// The session capability arrives only in the fragment, never a request URL.
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has("session")) {
  sessionStorage.setItem("ii-pro-session", fragment.get("session")!);
  history.replaceState(null, "", location.pathname);
}
function LiveApp() {
  const [data, setData] = useState<State | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  async function refresh(signal?: AbortSignal) {
    try {
      const deadline = AbortSignal.timeout(10000);
      const response = await fetch("/api/state", {
        headers: {
          Authorization: `Bearer ${sessionStorage.getItem("ii-pro-session") || ""}`,
        },
        signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
        cache: "no-store",
        credentials: "omit",
      });
      if (!response.ok)
        throw new Error(
          response.status === 401
            ? "Сессия недоступна. Откройте панель через Start-Studio.cmd."
            : "Нет связи с локальным API. Показанные данные могли устареть.",
        );
      const value = (await response.json()) as State;
      if (value.api_version !== 1 || value.read_only !== true)
        throw new Error("Несовместимая версия локального API.");
      setData(value);
      setError(null);
    } catch (e) {
      const x = e as Error;
      if (x.name !== "AbortError")
        setError(
          ["TypeError", "TimeoutError"].includes(x.name)
            ? "Нет связи с локальным API. Данные могли устареть."
            : x.message,
        );
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      await refresh(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, []);
  return <App data={data} error={error} loading={loading} />;
}
createRoot(document.getElementById("root")!).render(<LiveApp />);
