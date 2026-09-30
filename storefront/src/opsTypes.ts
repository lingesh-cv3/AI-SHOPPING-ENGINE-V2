/**
 * The Operations data contracts - types only, no data.
 *
 * These describe what the incident and work-queue backends will return once they
 * exist. Nothing in the running app imports them yet, and nothing here is a mock:
 * there is no sample Incident anywhere in the codebase, because a believable
 * fixture on a screen is indistinguishable from a real one in a demo.
 *
 * The rules the types enforce are the ones the plan sets down:
 *
 *  - An impact figure carries its BASIS. A modelled figure must say how it was
 *    modelled, and an unknown one must say why; the compiler rejects either
 *    without its explanation, so a bare "$12,400 at risk" cannot be built.
 *  - A hypothesis is a separate object from the incident's facts, with its own
 *    evidence and source. An AI- or rule-proposed cause is never a field on the
 *    incident itself.
 *  - A verification is an auditable row (check, expected, observed, result, who,
 *    when), and an incident is only "resolved" by a passing one.
 */

export type ImpactBasis = "MEASURED" | "MODELED" | "UNKNOWN";

/** A pointer at an authoritative row, so a claim can be traced to its source. */
export interface Ref {
  type: string;
  id: string;
  label?: string;
}

interface ImpactFigureBase {
  metric: string;
  unit?: string;
  sourceRefs: Ref[];
  window?: { from: string; to: string };
}

export type ImpactFigure =
  | (ImpactFigureBase & {
      basis: "MEASURED";
      value: number;
    })
  | (ImpactFigureBase & {
      basis: "MODELED";
      value: number;
      /** Required: a modelled value with no stated basis must not compile. */
      model: {
        id: string;
        version: string;
        inputs: Record<string, unknown>;
        assumptions: string[];
      };
    })
  | (ImpactFigureBase & {
      basis: "UNKNOWN";
      value?: undefined;
      /** Required: "we do not know" must say why. */
      reason: string;
    });

export type VerificationResult = "PASS" | "FAIL" | "INCONCLUSIVE" | "UNAVAILABLE";

export interface Verification {
  id: string;
  check: string;
  expected: unknown;
  observed: unknown;
  result: VerificationResult;
  checkedAt: string;
  actor: { type: "SYSTEM" | "OPERATOR"; id: string };
  /** Where the observation came from, e.g. "adapter:get_capabilities". */
  source: string;
  evidenceRefs: Ref[];
}

export interface Hypothesis {
  id: string;
  claim: string;
  evidenceRefs: Ref[];
  confidence: number;
  /** A rule or a model proposed it; neither is an established fact. */
  source: "RULE" | "MODEL";
  status: "PROPOSED" | "SUPPORTED" | "REJECTED";
}

export type IncidentState =
  | "OPEN"
  | "ACKNOWLEDGED"
  | "INVESTIGATING"
  | "MITIGATING"
  | "VERIFYING"
  | "RESOLVED"
  | "CLOSED"
  | "BLOCKED_EXTERNAL"
  | "VERIFICATION_FAILED";

export interface AuditEvent {
  id: string;
  at: string;
  actor: { type: "OPERATOR" | "SYSTEM"; id: string };
  action: string;
  target: Ref;
  fromState?: string;
  toState?: string;
  result: string;
}

export interface Task {
  id: string;
  kind: string;
  ownerParty: "CV3" | "MERCHANT";
  assigneeId?: string;
  state: string;
  blockedReason?: string;
  dueAt?: string;
}

export interface PriorityFactor {
  name: string;
  value: number | string | null;
  weight: number;
  contribution: number;
}

/** Stored with the item so a historical priority can always be reproduced. */
export interface Priority {
  /** An immutable policy version, e.g. "prio-2026.1". */
  policyId: string;
  score: number;
  factors: PriorityFactor[];
}

/** A merchant reference that may be withheld from an operator not entitled to it. */
export type ConnectionRef =
  | { visible: true; connectionId: string; merchantName: string }
  | { visible: false };

export interface Incident {
  id: string;
  kind: string;
  classification?: string;
  state: IncidentState;
  severity: { level: string; ruleId: string };
  connections: ConnectionRef[];
  detectedAt: string;
  acknowledgedAt?: string;
  resolvedAt?: string;
  slaDueAt?: string;
  impact: ImpactFigure[];
  timeline: AuditEvent[];
  hypotheses: Hypothesis[];
  verifications: Verification[];
  tasks: Task[];
  priority?: Priority;
  reopenCount: number;
}

/** One row of the cross-merchant work queue. */
export interface WorkItem {
  id: string;
  itemType: string;
  incidentId?: string;
  taskId?: string;
  title: string;
  connections: ConnectionRef[];
  state: string;
  ownerParty: "CV3" | "MERCHANT";
  detectedAt: string;
  slaDueAt?: string;
  impact: ImpactFigure[];
  priority: Priority;
  /** Said whenever a cap or page applies - never a silent truncation. */
  coverage: { complete: boolean; truncatedAt?: number; asOf: string };
}
