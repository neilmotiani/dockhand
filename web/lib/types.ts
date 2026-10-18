// Response shapes of the read API (app/ledger/api.py). Timestamps are ISO 8601 strings.

export type Status = "matched" | "short" | "over" | "missing" | "unexpected" | "pending";
export type DiscrepancyStatus = "short" | "over" | "missing" | "unexpected";

export interface Media {
  snapshot: string | null;
  /** Rank of the snapshot among `frames` (the best-scoring frame), when known. */
  snapshot_frame: number | null;
  frames: string[];
  clip: string | null;
}

export interface Delivery {
  id: string;
  event_id: string;
  device_id: string | null;
  detected_at: string;
  carrier: string;
  package_count: number;
  label_text: string[];
  confidence: number | null;
  source: string;
  /** Simulated demo history (scripts/seed_demo_history.py), not a Ring capture. */
  simulated: boolean;
  model_id: string | null;
  prompt_version: string | null;
  frames_sampled: number | null;
  frames_sent: number | null;
  event_to_ledger_ms: number | null;
  media: Media;
}

export interface Expected {
  id: string;
  supplier: string;
  carrier: string | null;
  expected_count: number;
  window_start: string;
  window_end: string;
  reference: string | null;
  /** A simulated order (scripts/seed_demo_history.py). */
  simulated: boolean;
}

export interface LedgerDelivery extends Delivery {
  status: Status;
  reason: string | null;
  expected: Expected | null;
}

export interface LedgerExpected extends Expected {
  status: Status;
  reason: string | null;
  delivery: Delivery | null;
}

export interface Discrepancy {
  id: string;
  simulated: boolean;
  status: DiscrepancyStatus;
  reason: string;
  decided_at: string;
  delivery: Delivery | null;
  expected: Expected | null;
}

export interface PipelineEvent {
  id: string;
  origin: "webhook" | "poll" | "manual";
  device_id: string | null;
  event_type: string;
  sub_type: string | null;
  occurred_at: string | null;
  received_at: string;
  status: "pending" | "processing" | "done" | "ignored" | "skipped" | "failed";
  attempts: number;
  finished_at: string | null;
  outcome: string | null;
  last_error: string | null;
  delivery_id: string | null;
}

export interface RatioOrNull {
  value: number | null;
  k: number;
  n: number;
}

export interface SupplierRow {
  supplier: string;
  orders: number;
  pending: number;
  due: number;
  delivered: number;
  on_time: RatioOrNull;
  late: number;
  short_rate: RatioOrNull;
  over: number;
  missed: number;
  boxes_short: number;
  problem_rate: RatioOrNull;
  simulated_orders: number;
  last_order: string | null;
  worst: boolean;
}

export interface Suppliers {
  from: string;
  to: string;
  days: number;
  suppliers: SupplierRow[];
  worst: string | null;
  orders: number;
  simulated_orders: number;
  includes_simulated: boolean;
}

export interface Day {
  date: string;
  today: string;
  timezone: string;
  generated_at: string;
}

export interface Ratio {
  value: number;
  k: number;
  n: number;
}

export interface Stage {
  n: number;
  p50_ms: number;
  p95_ms: number;
  max_ms: number;
}

export interface Metrics {
  extraction: {
    file: string;
    generated_at: string;
    git_commit: string;
    prompt_version: string;
    rows: {
      source: string;
      source_label: string;
      model: string;
      model_label: string;
      n: number;
      count_exact: Ratio | null;
      carrier_accuracy: Ratio | null;
      false_alarm: Ratio | null;
      latency_ms_p50: number;
      usd_per_call: number;
    }[];
    caveats: string[];
  } | null;
  reconciliation: {
    file: string;
    generated_at: string;
    git_commit: string;
    data: string;
    n_days: number;
    n_expected: number;
    n_deliveries: number;
    precision: Ratio | null;
    recall: Ratio | null;
    caveats: string[];
  } | null;
  latency: {
    file: string;
    label: string;
    generated_at: string;
    git_commit: string;
    n_deliveries: number;
    percentile_method: string | null;
    event_to_ledger: Stage | null;
    stages: Record<string, Stage | null>;
  }[];
}
