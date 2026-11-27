import { useState } from "react";

const filename = "II_PRO_CONTINUE.zip";
type Picker = (options: { suggestedName: string }) => Promise<{
  name: string;
  createWritable(): Promise<{ write(blob: Blob): Promise<void>; close(): Promise<void> }>;
}>;

export function Continuity() {
  const [result, setResult] = useState<{ time: string; stage: string; head: string; destination: string }>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    setError("");
    setResult(undefined);
    try {
      const picker = (window as Window & { showSaveFilePicker?: Picker }).showSaveFilePicker;
      const target = picker ? await picker.call(window, { suggestedName: filename }) : undefined;
      const response = await fetch("/api/continuity", {
        credentials: "omit",
        cache: "no-store",
        headers: { Authorization: "Bearer " + (sessionStorage.getItem("ii-pro-session") || "") },
        signal: AbortSignal.timeout(15000),
      });
      const time = response.headers.get("X-II-Pro-Created");
      const stage = response.headers.get("X-II-Pro-Stage");
      const head = response.headers.get("X-II-Pro-Head");
      if (!response.ok || response.headers.get("X-II-Pro-Integrity") !== "verified" || !time || !stage || !head) {
        throw new Error("Пакет недоступен или не прошёл проверку.");
      }
      const blob = await response.blob();
      let destination: string;
      if (target) {
        const stream = await target.createWritable();
        await stream.write(blob);
        await stream.close();
        destination = `${target.name} · выбранная папка`;
      } else {
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        destination = `${filename} · смотрите загрузки браузера`;
      }
      setResult({ time, stage, head, destination });
    } catch (failure) {
      if (!(failure instanceof DOMException && failure.name === "AbortError")) {
        setError("Пакет не удалось создать или сохранить.");
      }
    } finally {
      setBusy(false);
    }
  }

  return <section className="panel" aria-label="Перенос контекста">
    <h2>Перенос контекста</h2>
    <button type="button" disabled={busy} onClick={() => void save()}>Создать пакет для нового чата</button>
    {error && <p role="alert">{error}</p>}
    {result && <dl aria-label="Сведения о пакете">
      <dt>Сформирован</dt><dd>{new Date(result.time).toLocaleString("ru-RU")}</dd>
      <dt>Этап</dt><dd>{result.stage}</dd>
      <dt>HEAD</dt><dd>{result.head}</dd>
      <dt>Место сохранения</dt><dd>{result.destination}</dd>
      <dt>Целостность архива</dt><dd>Проверена при формировании</dd>
    </dl>}
  </section>;
}
