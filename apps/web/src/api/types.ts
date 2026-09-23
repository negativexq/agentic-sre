// Wire types mirroring apps/control_plane/console/dto.py. The console API is the
// only contract the UI depends on; it never sees internal persistence rows.

export type Resolution = "RESOLVED" | "AMBIGUOUS" | "INSUFFICIENT_EVIDENCE";
export type Confidence = "VERIFIED" | "LIKELY" | "UNVERIFIED";
export type ConnectorStatus = "connected" | "degraded" | "unavailable" | "not_configured";

export interface CausalHopView {
  source: string;
  relation: string;
  target: string;
  direction: string;
}

export interface FindingView {
  kind: string;
  entity: string;
  at: string | null;
  summary: string;
  temporal_role: string;
  onset_delta_seconds: number | null;
  evidence_ids: string[];
}

export interface HypothesisView {
  hypothesis_id: string;
  actor: string;
  epistemic_state: string;
  score: number;
  causal_explanation: string;
  reasons: string[];
}

export interface CandidateView {
  entity: string;
  score: number;
  strongest_signal: string;
}

export interface RemediationView {
  action: string;
  command: string;
  risk: string;
  requires_approval: boolean;
}

export interface StepView {
  actor: string;
  action: string;
  detail: string;
}

export interface InvestigationActionAuditView {
  turn_index: number;
  gap_id: string | null;
  gap_dimension: string | null;
  missing_fact: string | null;
  intent_id: string | null;
  intent_kind: string | null;
  action: string;
  capability: string | null;
  target: string | null;
  action_rationale: string;
  authorization_result: string;
  authorization_reason: string;
  backend_execution_status: string;
  observation_id: string | null;
  observation_outcome: string | null;
  returned_evidence_refs: string[];
  new_evidence_refs: string[];
  already_known_refs: string[];
  normalized_finding_ids: string[];
  affected_hypothesis_ids: string[];
  resolution_before: string;
  resolution_after: string | null;
  decision_state_changed: boolean | null;
  progress_classification: string;
}

export interface InvestigationAuditView {
  diagnosis_run_id: string;
  artifact_version: string;
  initial_resolution: string;
  final_resolution: string;
  stop_reason: string;
  turns: number;
  model_calls: number;
  tool_calls: number;
  action_audits: InvestigationActionAuditView[];
}

export interface DiagnosisView {
  incident_id: string;
  resolution: Resolution;
  confidence: Confidence;
  leading_root_actor: string | null;
  root_cause: string | null;
  is_resolved: boolean;
  summary: string;
  resolution_rationale: string | null;
  unresolved_dimensions: string[];
  causal_path: CausalHopView[];
  causal_explanation: string;
  initiating_findings: FindingView[];
  supporting_findings: FindingView[];
  contradictory_findings: FindingView[];
  evidence: FindingView[];
  competing_hypotheses: HypothesisView[];
  alternatives: CandidateView[];
  remediation: RemediationView[];
  steps: StepView[];
  investigation_audit: InvestigationAuditView | null;
  services: string[];
  alert_names: string[];
  onset: string | null;
  background_alerts_ignored: number;
  model_calls: number;
  mode: string;
}

export interface LifecyclePhaseView {
  name: string;
  at: string;
  detail: string;
  offset_seconds: number;
}

export interface TimelineEventView {
  event_type: string;
  timestamp: string;
}

export interface TimelineView {
  run_id: string | null;
  phases: LifecyclePhaseView[];
  note: string | null;
  alert_fired: string | null;
  incident_opened: string | null;
  diagnosis_ready: string | null;
  alert_to_diagnosis_seconds: number | null;
  reads: number;
  evidence_count: number;
  model_calls: number;
  events: TimelineEventView[];
}

export interface EvidenceView {
  evidence_id: string;
  source_type: string;
  source_system: string;
  collected_at: string;
  starts_at: string;
  ends_at: string;
  raw_result_reference: string;
  observation: Record<string, unknown>;
}

export interface IncidentListItem {
  incident_id: string;
  title: string;
  status: string;
  severity: string;
  source: string;
  service: string | null;
  leading_root_actor: string | null;
  confidence: Confidence | null;
  resolution: Resolution | null;
  has_diagnosis: boolean;
  created_at: string;
  updated_at: string;
  age_seconds: number;
}

export interface IncidentPage {
  items: IncidentListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface IncidentDetail {
  incident: IncidentListItem;
  diagnosis: DiagnosisView | null;
  timeline: TimelineView;
  evidence_count: number;
}

export interface ChangeView {
  change_id: string;
  timestamp: string;
  resource_type: string;
  resource_name: string;
  change_type: string;
  scope: string;
  revision: string | null;
  source: string | null;
  onset_delta_seconds: number | null;
  matches_leading_actor: boolean;
}

export interface ChangeFilters {
  scope?: string;
  change_type?: string;
  q?: string;
  limit?: number;
}

export interface ReportSummary {
  report_id: string;
  incident_id: string;
  title: string;
  severity: string;
  confidence: Confidence;
  resolution: Resolution;
  root_actor: string | null;
  leading_root_actor: string | null;
  diagnosis_run_id: string | null;
  report_version: string;
  generated_at: string;
}

export interface DeliveryView {
  delivery_id: string;
  report_id: string;
  recipients: string[];
  subject: string;
  status: string;
  error: string | null;
  created_at: string;
}

export interface ShareRequest {
  recipients: string[];
  include_pdf: boolean;
  idempotency_key?: string;
}

export interface SystemConnector {
  name: string;
  status: ConnectorStatus;
  detail: string | null;
}

export interface SystemStatus {
  connectors: SystemConnector[];
}

export interface SettingsView {
  watched_namespaces: string[];
  evidence_namespaces: string[];
  auto_diagnose: boolean;
  watch_interval_seconds: number;
  cluster_access: string;
  llm_enabled: boolean;
  llm_model: string | null;
  llm_max_calls: number;
  api_token_configured: boolean;
  email_configured: boolean;
  email_sender: string | null;
  prometheus_configured: boolean;
  loki_configured: boolean;
  tempo_configured: boolean;
  report_version: string;
}

export interface DashboardCounters {
  active_incidents: number;
  critical_incidents: number;
  diagnosing: number;
  resolved_diagnoses: number;
  median_diagnosis_seconds: number | null;
}

export interface DashboardSummary {
  counters: DashboardCounters;
  active_incidents: IncidentListItem[];
  recent_diagnoses: IncidentListItem[];
  system: SystemStatus;
  generated_at: string;
}

export interface IncidentFilters {
  status?: string;
  severity?: string;
  service?: string;
  confidence?: string;
  resolution?: string;
  active?: boolean;
  q?: string;
  limit?: number;
  offset?: number;
}
