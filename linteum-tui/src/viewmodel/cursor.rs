//! Per-session resume cursor and durable-frame deduplication.

use std::num::NonZeroUsize;

use lru::LruCache;

use crate::protocol::Frame;

use super::transcript::GapMarker;

/// The identity of a durable frame. `frame.id` is deliberately absent: replay
/// regenerates it.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct DedupeKey {
    pub session_id: String,
    pub seq: u64,
    pub frame: String,
    pub ordinal: u32,
    pub identity: Option<String>,
}

/// Tracks `last_seq` and the bounded set of durable frames already rendered.
#[derive(Debug, Clone)]
pub struct SessionCursor {
    last_seq: u64,
    seen: LruCache<DedupeKey, ()>,
}

impl Default for SessionCursor {
    fn default() -> Self {
        Self::new()
    }
}

impl SessionCursor {
    pub fn new() -> Self {
        Self {
            last_seq: 0,
            seen: LruCache::new(NonZeroUsize::new(10_000).expect("nonzero capacity")),
        }
    }

    /// Observe a frame. Returns `false` when it is a duplicate or ephemeral.
    /// Only non-ephemeral sequenced frames advance `last_seq`.
    pub fn observe(&mut self, frame: &Frame) -> bool {
        if frame.ephemeral {
            return false;
        }
        let Some(seq) = frame.seq else {
            return true;
        };
        let identity = frame
            .payload
            .get("call_id")
            .and_then(serde_json::Value::as_str)
            .map(str::to_owned)
            .or_else(|| {
                frame
                    .payload
                    .get("client_message_id")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_owned)
            });
        let key = DedupeKey {
            session_id: frame.session_id.clone().unwrap_or_default(),
            seq,
            frame: frame.frame.clone(),
            ordinal: frame.ordinal,
            identity,
        };
        if self.seen.contains(&key) {
            return false;
        }
        self.seen.put(key, ());
        if seq > self.last_seq {
            self.last_seq = seq;
        }
        true
    }

    pub fn after_seq(&self) -> u64 {
        self.last_seq
    }

    pub fn reset(&mut self) {
        self.last_seq = 0;
        self.seen.clear();
    }

    /// After a history gap, resume from the gap's `to_seq`.
    pub fn resubscribe_after_gap(&mut self, gap: &GapMarker) {
        if gap.to_seq > self.last_seq {
            self.last_seq = gap.to_seq;
        }
    }
}
