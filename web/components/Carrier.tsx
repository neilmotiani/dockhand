// The extraction prompt tells the model to answer "Unknown" rather than guess when it can't
// tell who delivered (app/extract/prompts.py). "Not visible" says that more plainly, and the
// tooltip says why.

export const NOT_VISIBLE_HELP =
  "No uniform, vehicle, logo or shipping label was readable in the frames the model saw. " +
  "It is told not to guess, so the carrier is left blank.";

export function carrierVisible(carrier: string | null | undefined): boolean {
  return !!carrier && carrier !== "Unknown";
}

export function Carrier({ carrier }: { carrier: string }) {
  if (carrierVisible(carrier)) return <strong>{carrier}</strong>;
  return (
    <span className="tip" tabIndex={0} aria-label={`Carrier not visible. ${NOT_VISIBLE_HELP}`}>
      <span className="not-visible">Not visible</span>
      <span className="tip-icon" aria-hidden>
        ⓘ
      </span>
      <span className="tip-body" role="tooltip">
        {NOT_VISIBLE_HELP}
      </span>
    </span>
  );
}
