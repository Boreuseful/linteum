//! Screen layout and status bar.

use ratatui::layout::{Constraint, Layout, Rect};

use crate::viewmodel::{ConnectionState, TuiState};

pub const MIN_WIDTH: u16 = 80;
pub const MIN_HEIGHT: u16 = 24;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LayoutRects {
    pub header: Rect,
    pub transcript: Rect,
    pub sidebar: Rect,
    pub queue_strip: Rect,
    pub composer: Rect,
    pub status: Rect,
}

pub fn layout(area: Rect, state: &TuiState) -> LayoutRects {
    if area.width < MIN_WIDTH || area.height < MIN_HEIGHT {
        let zero = Rect::new(area.x, area.y, area.width, 0);
        return LayoutRects {
            header: area,
            transcript: zero,
            sidebar: zero,
            queue_strip: zero,
            composer: zero,
            status: zero,
        };
    }

    let composer_height = composer_height(state);
    let queue_height = if state.queue.entries.is_empty() { 0 } else { 1 };
    let chunks = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(queue_height),
        Constraint::Length(composer_height),
        Constraint::Length(1),
    ])
    .split(area);

    let body = chunks[1];
    let (transcript, sidebar) = if state.sidebar.visible {
        let width = (body.width / 3)
            .clamp(24, 40)
            .min(body.width.saturating_sub(MIN_WIDTH / 2));
        if width > 0 {
            let columns =
                Layout::horizontal([Constraint::Min(1), Constraint::Length(width)]).split(body);
            (columns[0], columns[1])
        } else {
            (body, Rect::new(body.x + body.width, body.y, 0, body.height))
        }
    } else {
        (body, Rect::new(body.x + body.width, body.y, 0, body.height))
    };

    LayoutRects {
        header: chunks[0],
        transcript,
        sidebar,
        queue_strip: chunks[2],
        composer: chunks[3],
        status: chunks[4],
    }
}

fn composer_height(state: &TuiState) -> u16 {
    let lines = state.composer.text.split('\n').count().max(1);
    lines.clamp(1, 6) as u16
}

/// The transcript area after reserving a row for a banner, if one is shown.
pub fn transcript_viewport(transcript: Rect, state: &TuiState) -> Rect {
    if banner_text(state).is_some() && transcript.height > 0 {
        Rect::new(
            transcript.x,
            transcript.y + 1,
            transcript.width,
            transcript.height - 1,
        )
    } else {
        transcript
    }
}

/// One-line notices that temporarily replace the first transcript row.
pub fn banner_text(state: &TuiState) -> Option<String> {
    if state.connection == ConnectionState::ReadOnly {
        return Some("⊘ read-only — writes are disabled".to_owned());
    }
    if state.session.as_ref().map(|session| session.phase.as_str()) == Some("paused") {
        return Some("Paused: budget exceeded — Ctrl+G to resume".to_owned());
    }
    if state.degraded_replay {
        return Some(
            "Replayed content unavailable: the gateway did not hydrate externalized payloads"
                .to_owned(),
        );
    }
    if !state.features.is_empty()
        && (!state.features.iter().any(|feature| feature == "approval")
            || !state.features.iter().any(|feature| feature == "artifacts"))
    {
        return Some(
            "Gateway lacks approval or artifact support; related actions are disabled".to_owned(),
        );
    }
    None
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SegmentKind {
    Connection,
    Phase,
    Round,
    Model,
    Tokens,
    Usd,
    Cache,
    Approvals,
    Subagents,
    Queue,
}

impl SegmentKind {
    fn text(self, state: &TuiState) -> Option<String> {
        match self {
            Self::Connection => Some(connection_label(state)),
            Self::Phase => state
                .session
                .as_ref()
                .map(|session| format!("phase {}", session.phase)),
            Self::Round => state
                .session
                .as_ref()
                .map(|session| format!("round {}", session.round)),
            Self::Model => state
                .session
                .as_ref()
                .and_then(|session| session.bound_model.clone()),
            Self::Tokens => {
                let total = state.usage.input_tokens + state.usage.output_tokens;
                (total > 0).then(|| {
                    format!(
                        "tokens {}/{}",
                        state.usage.input_tokens, state.usage.output_tokens
                    )
                })
            }
            Self::Usd => (state.usage.usd > 0.0).then(|| format!("${:.4}", state.usage.usd)),
            Self::Cache => {
                let total = state.usage.cache_read + state.usage.cache_write;
                (total > 0).then(|| {
                    format!(
                        "cache {}/{}",
                        state.usage.cache_read, state.usage.cache_write
                    )
                })
            }
            Self::Approvals => (!state.approvals.pending.is_empty())
                .then(|| format!("⚠ {}", state.approvals.pending.len())),
            Self::Subagents => (!state.subagents.is_empty()).then(|| {
                format!(
                    "agents {}/{}",
                    state.subagents.running_count(),
                    state.subagents.entries.len()
                )
            }),
            Self::Queue => (!state.queue.entries.is_empty())
                .then(|| format!("queued {}", state.queue.entries.len())),
        }
    }
}

fn connection_label(state: &TuiState) -> String {
    match &state.connection {
        ConnectionState::Live => "● live".to_owned(),
        ConnectionState::Replaying => "◌ replaying".to_owned(),
        ConnectionState::Reconnecting { retry_in } => {
            format!("↻ retry {}s", retry_in.as_secs().max(1))
        }
        ConnectionState::Offline => "✕ offline".to_owned(),
        ConnectionState::ReadOnly => "⊘ read-only".to_owned(),
        ConnectionState::Starting => "… starting".to_owned(),
        ConnectionState::Connecting => "… connecting".to_owned(),
        ConnectionState::Fatal => "✕ fatal".to_owned(),
    }
}

pub fn status_bar(state: &TuiState, width: u16) -> String {
    let kinds = [
        SegmentKind::Connection,
        SegmentKind::Phase,
        SegmentKind::Round,
        SegmentKind::Model,
        SegmentKind::Tokens,
        SegmentKind::Usd,
        SegmentKind::Cache,
        SegmentKind::Approvals,
        SegmentKind::Subagents,
        SegmentKind::Queue,
    ];
    let mut segments: Vec<(SegmentKind, String)> = kinds
        .iter()
        .filter_map(|kind| kind.text(state).map(|text| (*kind, text)))
        .collect();

    let mut total = total_width(&segments);
    let drop_order = [
        SegmentKind::Cache,
        SegmentKind::Usd,
        SegmentKind::Tokens,
        SegmentKind::Model,
        SegmentKind::Round,
        SegmentKind::Subagents,
    ];
    for kind in drop_order {
        if total <= width as usize {
            break;
        }
        if let Some(index) = segments
            .iter()
            .position(|(candidate, _)| *candidate == kind)
        {
            total = total.saturating_sub(segments[index].1.chars().count() + 3);
            segments.remove(index);
        }
    }

    let joined = segments
        .iter()
        .map(|(_, text)| text.as_str())
        .collect::<Vec<_>>()
        .join(" · ");
    if joined.chars().count() > width as usize {
        joined.chars().take(width as usize).collect()
    } else {
        joined
    }
}

fn total_width(segments: &[(SegmentKind, String)]) -> usize {
    segments
        .iter()
        .map(|(_, text)| text.chars().count())
        .sum::<usize>()
        + segments.len().saturating_sub(1) * 3
}
