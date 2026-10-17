import { ApiError } from "@/lib/api";

export function ApiProblem({ error }: { error: unknown }) {
  const message = error instanceof ApiError ? error.message : String(error);
  return (
    <div className="banner" role="alert">
      <strong>Can’t load the ledger.</strong> {message}
      <div className="small" style={{ marginTop: 4 }}>
        Start the API on port 8010 and check <code>DOCKHAND_API_URL</code> and{" "}
        <code>DOCKHAND_ADMIN_TOKEN</code> in <code>web/.env.local</code>.
      </div>
    </div>
  );
}
