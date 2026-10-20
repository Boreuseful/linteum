//! Artifact viewer.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::TuiState;

pub fn artifact_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let Some(artifact) = &state.artifact else {
        return Vec::new();
    };
    let mut lines = vec![Line::from(Span::styled(
        format!("{} · {} bytes", artifact.reference, artifact.size),
        theme.muted,
    ))];
    if artifact.binary {
        lines.push(Line::from(Span::styled(
            format!(
                "binary artifact ({} bytes) — not displayable in the TUI",
                artifact.size
            ),
            theme.warning,
        )));
    } else {
        lines.extend(
            artifact
                .text
                .lines()
                .map(|line| Line::from(Span::styled(line.to_owned(), theme.text))),
        );
    }
    if artifact.eof {
        lines.push(Line::from(Span::styled("(end of artifact)", theme.muted)));
    }
    lines
}

pub fn render_artifact(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    render_lines(area, buf, &artifact_lines(state, theme));
}
