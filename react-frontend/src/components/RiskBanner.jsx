import { ShieldAlert, ShieldCheck, ShieldX } from 'lucide-react';

export default function RiskBanner({ riskLevel, riskWarning, onConfirm, onCancel }) {
  const config = {
    safe: { cls: 'risk-safe', Icon: ShieldCheck, label: 'Safe' },
    moderate: { cls: 'risk-moderate', Icon: ShieldAlert, label: 'Moderate Risk' },
    risky: { cls: 'risk-risky', Icon: ShieldX, label: 'High Risk' },
  };

  const { cls, Icon, label } = config[riskLevel] || config.moderate;

  return (
    <div className={`risk-banner ${cls}`}>
      <div className="risk-banner-header">
        <Icon style={{ width: 16, height: 16 }} />
        <span className="badge badge-warning">{label}</span>
      </div>
      {riskWarning && <div className="risk-banner-text">{riskWarning}</div>}
      {onConfirm && (
        <div className="risk-banner-actions">
          <button className="btn btn-primary btn-sm" onClick={onConfirm}>
            <ShieldCheck style={{ width: 12, height: 12 }} />
            Execute Anyway
          </button>
          {onCancel && (
            <button className="btn btn-secondary btn-sm" onClick={onCancel}>
              Cancel
            </button>
          )}
        </div>
      )}
    </div>
  );
}
