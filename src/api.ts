export type Connection = { base: string; token: string };
declare global {
  interface Window {
    desktop?: {
      connection: () => Promise<Connection>;
      takePhoto: (options: { label: string; aspectRatio: number }) => Promise<{
        image: string; width: number; height: number; method: string; captured_at: string;
      }>;
      cancelPhoto: () => Promise<void>;
      setTestPhoto: (image: string) => Promise<void>;
      diagnostic: (event: string, detail?: string | number) => Promise<void>;
      focus: (value: boolean) => Promise<void>;
      onFocus: (fn: (value: boolean) => void) => () => void;
      onSuspend: (fn: (reason: string) => void) => () => void;
      setFinalizing: (value: boolean) => Promise<void>;
      saveKeys: (keys: Record<string, string>) => Promise<void>;
      board: (
        html: string,
        bounds: { x: number; y: number; width: number; height: number },
      ) => Promise<void>;
      closeBoard: () => Promise<void>;
      resizeBoard: (bounds: {
        x: number;
        y: number;
        width: number;
        height: number;
      }) => Promise<void>;
    };
  }
}
export async function request<T = any>(route: string, body?: unknown, method?: string): Promise<T> {
  const connection = await window.desktop?.connection();
  if (!connection) throw Error('請從桌面程式開啟；瀏覽器目前只提供介面預覽。');
  const form = body instanceof FormData;
  const res = await fetch(connection.base + route, {
    method: method ?? (body ? 'POST' : 'GET'),
    headers: {
      Authorization: 'Bearer ' + connection.token,
      ...(!form ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? (form ? body : JSON.stringify(body)) : undefined,
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({ detail: '連線失敗' }));
    throw Error(e.detail ?? '操作失敗');
  }
  return res.json();
}
