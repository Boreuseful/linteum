use linteum_tui::ui::Theme;
use linteum_tui::ui::overlays::activity::activity_lines;
use linteum_tui::ui::overlays::approval::{
    approval_modal_lines, approval_popup_area, render_approval_modal,
};
use linteum_tui::ui::palette::{execute, palette_actions};
use linteum_tui::viewmodel::{ApprovalCard, Effect, Item, ToolCard, TuiState};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Modifier;
use ratatui::text::Line;

fn text(line: &Line<'_>) -> String {
    line.spans
        .iter()
        .map(|span| span.content.as_ref())
        .collect()
}

fn joined(lines: &[Line<'static>]) -> String {
    lines.iter().map(text).collect::<Vec<_>>().join("\n")
}

fn pending_state() -> TuiState {
    let mut state = TuiState::default();
    state.approvals.pending.push(ApprovalCard {
        call_id: "c1".into(),
        name: "write_file".into(),
        side_effect: Some("write".into()),
        args_preview: "{\"path\":\"x\"}".into(),
        requested_at: Some(1760680000.0),
        ..Default::default()
    });
    state.approvals.open = Some("c1".into());
    state
}

#[test]
fn approval_modal_has_no_default_focus() {
    let state = pending_state();
    let rendered = joined(&approval_modal_lines(&state, &Theme::dark()));
    assert!(rendered.contains("Grant (y)"));
    assert!(rendered.contains("Deny (n)"));
    assert!(!rendered.contains("[x]"));
    assert!(!rendered.contains("> Grant"));
}

#[test]
fn approval_modal_shows_args_preview_and_requested_at() {
    let state = pending_state();
    let rendered = joined(&approval_modal_lines(&state, &Theme::dark()));
    assert!(rendered.contains("{\"path\":\"x\"}"));
    assert!(rendered.contains("requested_at"));
}

#[test]
fn approval_popup_clears_the_page_behind_it() {
    let state = pending_state();
    let theme = Theme::dark();
    let area = Rect::new(0, 0, 80, 24);
    let popup = approval_popup_area(area, &state, &theme).expect("approval popup");

    let mut buf = Buffer::empty(area);
    for cell in &mut buf.content {
        cell.set_symbol("X");
    }
    render_approval_modal(area, &mut buf, &state, &theme);

    // Every cell inside the frame was cleared before the modal was drawn, so
    // page glyphs cannot interleave with the approval text.
    for y in popup.top() + 1..popup.bottom() - 1 {
        for x in popup.left() + 1..popup.right() - 1 {
            assert_ne!(
                buf[(x, y)].symbol(),
                "X",
                "page glyph leaked into the popup at ({x}, {y})"
            );
        }
    }

    // The page outside the popup remains as a dimmed backdrop.
    let backdrop = buf[(0, 0)].clone();
    assert_eq!(backdrop.symbol(), "X");
    assert!(backdrop.modifier.contains(Modifier::DIM));

    let rendered: String = buf.content.iter().map(|cell| cell.symbol()).collect();
    assert!(rendered.contains("Approval required (1 of 1)"));
    assert!(rendered.contains("Grant (y)"));
}

#[test]
fn approval_text_is_centered_and_decisions_are_colored() {
    let state = pending_state();
    let theme = Theme::dark();
    let area = Rect::new(0, 0, 80, 24);
    let popup = approval_popup_area(area, &state, &theme).expect("approval popup");
    let mut buf = Buffer::empty(area);
    render_approval_modal(area, &mut buf, &state, &theme);

    // The title is centered on the top border.
    let title = "Approval required (1 of 1)";
    let title_row: String = (popup.left()..popup.right())
        .map(|x| buf[(x, popup.top())].symbol())
        .collect();
    let title_byte = title_row.find(title).expect("title");
    let title_start = title_row[..title_byte].chars().count() as u16;
    let title_end = title_start + title.chars().count() as u16;
    assert!(
        title_start.abs_diff(popup.width - title_end) <= 1,
        "title is not centered"
    );

    // Grant is green, Deny is red, and the decision row is centered.
    let mut seen = false;
    for y in popup.top() + 1..popup.bottom() - 1 {
        let row: String = (popup.left()..popup.right())
            .map(|x| buf[(x, y)].symbol())
            .collect();
        let (Some(grant), Some(deny)) = (row.find("[ Grant (y) ]"), row.find("[ Deny (n) ]"))
        else {
            continue;
        };
        let grant_x = popup.left() + row[..grant].chars().count() as u16;
        let deny_x = popup.left() + row[..deny].chars().count() as u16;
        assert_eq!(buf[(grant_x, y)].fg, theme.success.fg.expect("green"));
        assert_eq!(buf[(deny_x, y)].fg, theme.danger.fg.expect("red"));

        let inner_left = popup.left() + 1;
        let inner_right = popup.right() - 1;
        let first = (inner_left..inner_right)
            .find(|&x| buf[(x, y)].symbol() != " ")
            .expect("decision text");
        let last = (inner_left..inner_right)
            .rev()
            .find(|&x| buf[(x, y)].symbol() != " ")
            .expect("decision text");
        let leading = first - inner_left;
        let trailing = (inner_right - 1) - last;
        assert!(
            leading.abs_diff(trailing) <= 1,
            "decision row is not centered: {leading} vs {trailing}"
        );
        seen = true;
        break;
    }
    assert!(seen, "decision row not rendered");
}

#[test]
fn activity_routes_notice_kinds() {
    let mut state = TuiState::default();
    state.activity.push("budget", "paused");
    let rendered = joined(&activity_lines(&state, &Theme::dark()));
    assert!(rendered.contains("budget"));
    assert!(rendered.contains("paused"));
}

#[test]
fn palette_contains_every_action() {
    let actions = palette_actions();
    assert!(actions.iter().any(|action| action.id == "quit"));
    assert!(actions.iter().any(|action| action.id == "toggle_reasoning"));
}

#[test]
fn palette_execute_toggles_reasoning() {
    let mut state = TuiState::default();
    let action = palette_actions()
        .into_iter()
        .find(|action| action.id == "toggle_reasoning")
        .unwrap();
    let effects = execute(action, &mut state);
    assert!(state.prefs.show_reasoning);
    assert!(effects.is_empty());
}

#[test]
fn palette_execute_opens_last_artifact() {
    let mut state = TuiState::default();
    state.transcript.items.push(Item::Tool(ToolCard {
        output_ref: Some("art:abc".into()),
        ..Default::default()
    }));
    let action = palette_actions()
        .into_iter()
        .find(|action| action.id == "open_artifact")
        .unwrap();
    let effects = execute(action, &mut state);
    assert_eq!(state.artifact.as_ref().unwrap().reference, "art:abc");
    assert!(matches!(&effects[0], Effect::SendCommand(command)
        if command.name == "artifact.read" && command.args["ref"] == "art:abc"));
}

#[test]
fn palette_execute_toggles_last_tool() {
    let mut state = TuiState::default();
    state.transcript.items.push(Item::Tool(ToolCard {
        call_id: "c1".into(),
        ..Default::default()
    }));
    let action = palette_actions()
        .into_iter()
        .find(|action| action.id == "toggle_last_tool")
        .unwrap();
    execute(action, &mut state);
    let card = state
        .transcript
        .items
        .iter()
        .find_map(|item| match item {
            Item::Tool(card) => Some(card),
            _ => None,
        })
        .unwrap();
    assert!(card.expanded);
}
