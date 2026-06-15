import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useInterval } from "@/hooks/useInterval";
import { cn } from "@/lib/utils";

const LABELS: Record<string, string> = { all: "全部", "1H": "1H", "15m": "15m", "30m": "30m" };

export function IntervalSelector() {
  const [interval, setIv] = useInterval();
  const [available, setAvailable] = useState<string[]>(["1H"]);

  useEffect(() => {
    api.intervals().then(setAvailable).catch(() => setAvailable(["1H"]));
  }, []);

  const options = available.length > 1 ? ["all", ...available] : available;

  return (
    <div className="flex items-center gap-1 ml-2">
      <span className="text-xs text-muted-foreground mr-1">時間級別</span>
      {options.map((iv) => (
        <button
          key={iv}
          onClick={() => setIv(iv)}
          className={cn(
            "rounded-md px-2 py-1 text-xs font-medium transition-colors",
            interval === iv
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-muted-foreground hover:bg-muted/80",
          )}
        >
          {LABELS[iv] ?? iv}
        </button>
      ))}
    </div>
  );
}
