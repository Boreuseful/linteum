//! Approval list, modal state, and decisions.

use std::collections::HashSet;

use serde_json::{Map, Value};

use super::state::{ConnectionState, TuiState};
use super::transcript::ApprovalCard;
use super::{CommandSpec, Effect};

#[derive(Debug, Clone, Default)]
pub struct ApprovalState {
    pub pending: Vec<ApprovalCard>,
    pub open: Option<String>,
    pub decided: HashSet<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApprovalDecision {
    Grant,
    Deny { reason: Option<String> },
}

impl TuiState {
    /// Mark a pending approval and return the `approval.decide` command.
    /// The modal stays open until `approval.resolved` arrives, so a failure
    /// can be surfaced without losing the card.
    pub fn decide(&mut self, call_id: &str, decision: ApprovalDecision) -> Vec<Effect> {
        if self.approvals.decided.contains(call_id) {
            return Vec::new();
        }
        if !self
            .approvals
            .pending
            .iter()
            .any(|card| card.call_id == call_id)
        {
            return Vec::new();
        }
        if matches!(
            self.connection,
            ConnectionState::Offline | ConnectionState::ReadOnly
        ) {
            return Vec::new();
        }

        let mut args = Map::new();
        args.insert("call_id".into(), Value::String(call_id.to_owned()));
        match decision {
            ApprovalDecision::Grant => {
                args.insert("decision".into(), Value::String("grant".into()));
            }
            ApprovalDecision::Deny { reason } => {
                args.insert("decision".into(), Value::String("deny".into()));
                if let Some(reason) = reason {
                    args.insert("reason".into(), Value::String(reason));
                }
            }
        }

        vec![Effect::SendCommand(CommandSpec {
            name: "approval.decide".to_owned(),
            args,
            session_id: self.session.as_ref().map(|session| session.id.clone()),
        })]
    }
}
