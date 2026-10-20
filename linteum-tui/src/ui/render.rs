//! Top-level frame renderer.

use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::widgets::{Paragraph, Widget};

use crate::viewmodel::{Focus, Overlay, TuiState};

use super::layout;
use super::overlays::{render_activity, render_approval_modal, render_artifact, render_help};
use super::palette;
use super::screens::{render_fatal, render_new_session, render_picker};
use super::strings;
use super::theme::Theme;
use super::widgets::{
    render_composer, render_lines, render_sidebar, render_slash_menu, render_transcript,
};

pub fn render(frame: &mut Frame, state: &TuiState, theme: &Theme) {
    let area = frame.area();
    if area.width < layout::MIN_WIDTH || area.height < layout::MIN_HEIGHT {
        Paragraph::new(strings::TOO_SMALL).render(area, frame.buffer_mut());
        return;
    }

    let rects = layout::layout(area, state);
    let title = state
        .session
        .as_ref()
        .and_then(|session| session.title.clone())
        .or_else(|| state.session.as_ref().map(|session| session.id.clone()))
        .unwrap_or_else(|| "no session".to_owned());
    let phase = state
        .session
        .as_ref()
        .map(|session| session.phase.as_str())
        .unwrap_or("idle");
    let model = state
        .session
        .as_ref()
        .and_then(|session| session.bound_model.as_deref())
        .unwrap_or("-");
    Paragraph::new(format!(
        "{} · {} · {} · {}",
        strings::APP_NAME,
        title,
        phase,
        model
    ))
    .style(theme.text)
    .render(rects.header, frame.buffer_mut());

    if let Some(banner) = layout::banner_text(state) {
        Paragraph::new(banner).style(theme.warning).render(
            Rect::new(
                rects.transcript.x,
                rects.transcript.y,
                rects.transcript.width,
                1,
            ),
            frame.buffer_mut(),
        );
    }
    let transcript_area = layout::transcript_viewport(rects.transcript, state);
    render_transcript(transcript_area, frame.buffer_mut(), state, theme);

    if rects.sidebar.width > 0 {
        render_sidebar(rects.sidebar, frame.buffer_mut(), state, theme);
    }

    if rects.queue_strip.height > 0 {
        Paragraph::new(queue_text(state))
            .style(theme.muted)
            .render(rects.queue_strip, frame.buffer_mut());
    }

    let mut cursor = render_composer(rects.composer, frame.buffer_mut(), state, theme);
    Paragraph::new(layout::status_bar(state, rects.status.width))
        .style(theme.muted)
        .render(rects.status, frame.buffer_mut());

    if state.overlay.is_none()
        && state.focus == Focus::Composer
        && state.composer.text.starts_with('/')
    {
        render_slash_menu(area, frame.buffer_mut(), state, theme);
    }

    if let Some(overlay) = state.overlay {
        match overlay {
            Overlay::Approvals => {
                render_approval_modal(area, frame.buffer_mut(), state, theme);
                cursor = None;
            }
            Overlay::Activity => {
                render_activity(area, frame.buffer_mut(), state, theme);
                cursor = None;
            }
            Overlay::Help => {
                render_help(area, frame.buffer_mut(), theme);
                cursor = None;
            }
            Overlay::Artifact => {
                render_artifact(area, frame.buffer_mut(), state, theme);
                cursor = None;
            }
            Overlay::Picker => {
                render_picker(area, frame.buffer_mut(), state, theme);
                cursor = None;
            }
            Overlay::NewSession => {
                cursor = render_new_session(area, frame.buffer_mut(), state, theme);
            }
            Overlay::Palette => {
                let lines = palette::palette_lines(state, theme);
                render_lines(area, frame.buffer_mut(), &lines);
                cursor = palette::palette_cursor(area, state);
            }
            Overlay::Fatal => {
                render_fatal(area, frame.buffer_mut(), state, theme);
                cursor = None;
            }
        }
    }

    if state.overlay.is_none() && state.focus == Focus::Sidebar {
        cursor = None;
    }

    if let Some(position) = cursor {
        frame.set_cursor_position(position);
    }
}

fn queue_text(state: &TuiState) -> String {
    let mut text = state
        .queue
        .entries
        .iter()
        .take(3)
        .map(|entry| format!("{:?}: {}", entry.state, entry.text.replace('\n', " ")))
        .collect::<Vec<_>>()
        .join(" · ");
    if state.queue.entries.len() > 3 {
        text.push_str(&format!(" · +{} more", state.queue.entries.len() - 3));
    }
    text
}
