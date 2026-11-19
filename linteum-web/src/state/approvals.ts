import type { ApprovalInfo } from "../protocol/frames";

export function upsertApproval(
  approvals: ApprovalInfo[],
  approval: ApprovalInfo,
): ApprovalInfo[] {
  if (!approval.callId) return approvals;
  const index = approvals.findIndex((item) => item.callId === approval.callId);
  if (index < 0) return [...approvals, approval];
  const copy = approvals.slice();
  copy[index] = { ...approvals[index], ...approval };
  return copy;
}

export function removeApproval(approvals: ApprovalInfo[], callId: string): ApprovalInfo[] {
  if (!callId) return approvals;
  return approvals.filter((item) => item.callId !== callId);
}
