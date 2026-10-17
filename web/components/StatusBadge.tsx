import type { Status } from "@/lib/types";

// Icon + label + colour, so status never depends on colour alone.
const STATUS: Record<Status, { icon: string; label: string }> = {
  matched: { icon: "✓", label: "Matched" },
  short: { icon: "▼", label: "Short" },
  over: { icon: "▲", label: "Over" },
  missing: { icon: "✕", label: "Missing" },
  unexpected: { icon: "?", label: "Unexpected" },
  pending: { icon: "◷", label: "Pending" },
};

export function StatusBadge({
  status,
  large = false,
  label,
}: {
  status: Status;
  large?: boolean;
  label?: string;
}) {
  const s = STATUS[status];
  return (
    <span className={`badge s-${status}${large ? " badge-lg" : ""}`}>
      <span className="badge-icon" aria-hidden>
        {s.icon}
      </span>
      {label ?? s.label}
    </span>
  );
}
