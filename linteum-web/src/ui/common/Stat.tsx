export function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value" title={value}>
        {value}
      </span>
    </div>
  );
}
