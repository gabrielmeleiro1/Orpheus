import { memo, useEffect, useState } from "react";

function nowTimestamp(): string {
  return new Date().toLocaleTimeString("en-US", { hour12: false });
}

export const LiveClock = memo(function LiveClock() {
  const [clock, setClock] = useState<string>(() => nowTimestamp());

  useEffect(() => {
    const timer = window.setInterval(() => {
      setClock(nowTimestamp());
    }, 1000);

    return () => {
      window.clearInterval(timer);
    };
  }, []);

  return (
    <span id="clock" aria-label="Current time">
      {clock}
    </span>
  );
});
