import { useSearchParams } from "react-router-dom";

const VALID = ["all", "1H", "15m", "30m"];

export function useInterval(): [string, (iv: string) => void] {
  const [params, setParams] = useSearchParams();
  const raw = params.get("interval");
  const interval = raw && VALID.includes(raw) ? raw : "1H";
  const setIv = (iv: string) => {
    const next = new URLSearchParams(params);
    next.set("interval", iv);
    setParams(next);
  };
  return [interval, setIv];
}
