//! Outbound user-message queue.
//!
//! A message is rendered only when its durable `user.message` frame arrives.
//! The receipt is used for acceptance/queueing/errors, never as proof of
//! delivery.

use std::collections::HashSet;

use serde_json::Value;

use crate::protocol::{CommandResult, Frame};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueueEntryState {
    Sending,
    Queued,
    Delivered,
    Failed,
}

#[derive(Debug, Clone)]
pub struct QueuedMessage {
    pub client_message_id: String,
    pub text: String,
    pub state: QueueEntryState,
    pub receipt: Option<CommandResult>,
}

#[derive(Debug, Clone, Default)]
pub struct QueueState {
    pub entries: Vec<QueuedMessage>,
    pub seen_client_ids: HashSet<String>,
}

impl QueueState {
    pub fn enqueue(&mut self, text: String, client_message_id: String) -> QueuedMessage {
        let entry = QueuedMessage {
            client_message_id,
            text,
            state: QueueEntryState::Sending,
            receipt: None,
        };
        self.entries.push(entry.clone());
        entry
    }

    /// Apply a `user.message` command result. Returns the text to restore to
    /// the composer when the send was not accepted.
    pub fn apply_result(&mut self, result: &CommandResult) -> Option<String> {
        let id = result
            .result
            .get("client_message_id")
            .and_then(Value::as_str);
        let index = match id {
            Some(id) => self
                .entries
                .iter()
                .position(|entry| entry.client_message_id == id),
            None => self.entries.iter().position(|entry| {
                matches!(
                    &entry.state,
                    QueueEntryState::Sending | QueueEntryState::Queued
                )
            }),
        };
        let index = index?;

        if result
            .result
            .get("duplicate")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            return None;
        }

        let accepted = result
            .result
            .get("accepted")
            .and_then(Value::as_bool)
            .unwrap_or(true);
        let queued = result
            .result
            .get("queued")
            .and_then(Value::as_bool)
            .unwrap_or(false);

        if !result.ok || !accepted {
            let entry = self.entries.remove(index);
            return Some(entry.text);
        }

        if queued {
            self.entries[index].state = QueueEntryState::Queued;
        }
        self.entries[index].receipt = Some(result.clone());
        None
    }

    /// Reconcile a durable `user.message` frame with the strip. Returns true
    /// when the `client_message_id` is new (the frame should be rendered);
    /// false for a duplicate frame.
    pub fn apply_user_message(&mut self, frame: &Frame) -> bool {
        let Some(id) = frame
            .payload
            .get("client_message_id")
            .and_then(Value::as_str)
        else {
            return true;
        };
        if !self.seen_client_ids.insert(id.to_owned()) {
            return false;
        }
        if let Some(index) = self
            .entries
            .iter()
            .position(|entry| entry.client_message_id == id)
        {
            self.entries.remove(index);
        }
        true
    }

    /// `notice{kind:"input_failed"}` carries no id; fail the oldest entry.
    pub fn apply_input_failed(&mut self) -> Option<String> {
        if self.entries.is_empty() {
            return None;
        }
        let entry = self.entries.remove(0);
        Some(entry.text)
    }

    /// Entries that still need a re-send after reconnect.
    pub fn resend_after_reconnect(&self) -> Vec<QueuedMessage> {
        self.entries
            .iter()
            .filter(|entry| {
                matches!(
                    &entry.state,
                    QueueEntryState::Sending | QueueEntryState::Queued
                )
            })
            .cloned()
            .collect()
    }
}

pub fn new_client_message_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}
