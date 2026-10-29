#![allow(clippy::field_reassign_with_default)]

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use linteum_tui::ui::widgets::slash_menu::{render_slash_menu, slash_menu_lines};
use linteum_tui::ui::{App, Theme};
use linteum_tui::viewmodel::{
    Effect, Input, Item, Overlay, SessionView, TuiState, UiAction, UserItem, reduce, slash,
};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

fn ids(query: &str) -> Vec<&'static str> {
    slash::filtered(query)
        .iter()
        .map(|command| command.id)
        .collect()
}

#[test]
fn slash_menu_filters_by_id_and_label() {
    let all = ids("");
    for id in [
        "files", "sidebar", "merge", "split", "collapse", "expand", "preview", "info", "refresh",
        "exit", "quit",
    ] {
        assert!(all.contains(&id), "missing /{id}");
    }
    assert_eq!(ids("q"), vec!["quit"]);
    assert_eq!(ids("EXIT"), vec!["exit"]);
    assert_eq!(ids("session"), vec!["mode", "exit"]);
    assert!(ids("zzz").is_empty());
    assert_eq!(slash::selected("/", 0).unwrap().id, "files");
    assert_eq!(slash::selected("/q", 99).unwrap().id, "quit");
}

#[test]
fn slash_quit_emits_quit_and_clears_the_composer() {
    let mut state = TuiState::default();
    state.composer.text = "/quit".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(matches!(&effects[0], Effect::Quit { .. }));
    assert!(state.composer.text.is_empty());
}

#[test]
fn slash_exit_leaves_the_session_and_returns_to_the_picker() {
    let mut state = TuiState::default();
    state.session = Some(SessionView {
        id: "s1".into(),
        ..Default::default()
    });
    state.transcript.items.push(Item::User(UserItem {
        text: "hi".into(),
        ..Default::default()
    }));
    state.composer.text = "/exit".into();

    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));

    assert!(state.session.is_none());
    assert!(state.transcript.items.is_empty());
    assert_eq!(state.overlay, Some(Overlay::Picker));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command)
            if command.name == "session.unsubscribe" && command.args["session_id"] == "s1"
    )));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command) if command.name == "session.list"
    )));
}

#[test]
fn slash_exit_without_a_session_toasts() {
    let mut state = TuiState::default();
    state.composer.text = "/exit".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(matches!(
        &effects[0],
        Effect::ShowToast(toast) if toast.text.contains("no active session")
    ));
}

#[test]
fn slash_runs_the_highlighted_command() {
    let mut state = TuiState::default();
    state.composer.text = "/".into();
    state.slash.selected = slash::filtered("")
        .iter()
        .position(|command| command.id == "quit")
        .unwrap();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(matches!(&effects[0], Effect::Quit { .. }));
}

#[test]
fn unknown_slash_command_toasts_and_clears() {
    let mut state = TuiState::default();
    state.composer.text = "/nope".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(matches!(
        &effects[0],
        Effect::ShowToast(toast) if toast.text.contains("/nope")
    ));
    assert!(state.composer.text.is_empty());
}

#[test]
fn typing_slash_resets_a_stale_selection() {
    let mut state = TuiState::default();
    state.slash.selected = 1;
    reduce(&mut state, Input::Text("/".into()));
    assert_eq!(state.slash.selected, 0);
}

#[test]
fn typing_slash_then_enter_runs_the_command() {
    let mut app = App::new(TuiState::default(), Theme::dark());

    // The first `/` is plain composer input, which opens the menu next draw.
    assert!(app.handle_slash_key(key(KeyCode::Char('/'))).is_none());
    let _ = reduce(&mut app.state, Input::Text("/".into()));

    let handled = app.handle_slash_key(key(KeyCode::Char('q'))).unwrap();
    assert!(handled.is_empty());
    assert_eq!(app.state.composer.text, "/q");

    let effects = app.handle_slash_key(key(KeyCode::Enter)).unwrap();
    assert!(matches!(&effects[0], Effect::Quit { .. }));
    assert!(app.state.composer.text.is_empty());
}

#[test]
fn tab_completes_the_highlighted_command() {
    let mut app = App::new(TuiState::default(), Theme::dark());
    app.state.composer.text = "/q".into();
    app.state.composer.cursor = 2;

    let effects = app.handle_slash_key(key(KeyCode::Tab)).unwrap();
    assert!(effects.is_empty());
    assert_eq!(app.state.composer.text, "/quit");
    assert_eq!(app.state.composer.cursor, 5);

    let effects = app.handle_slash_key(key(KeyCode::Enter)).unwrap();
    assert!(matches!(&effects[0], Effect::Quit { .. }));
}

#[test]
fn tab_completes_the_arrow_selected_command() {
    let mut app = App::new(TuiState::default(), Theme::dark());
    app.state.composer.text = "/".into();

    app.handle_slash_key(key(KeyCode::Down)).unwrap();
    let effects = app.handle_slash_key(key(KeyCode::Tab)).unwrap();
    assert!(effects.is_empty());
    assert_eq!(app.state.composer.text, "/sidebar");
}

#[test]
fn tab_with_no_match_is_consumed_without_completing() {
    let mut app = App::new(TuiState::default(), Theme::dark());
    app.state.composer.text = "/zzz".into();

    let effects = app.handle_slash_key(key(KeyCode::Tab)).unwrap();
    assert!(effects.is_empty());
    assert_eq!(app.state.composer.text, "/zzz");
}

#[test]
fn arrows_move_the_menu_selection_and_esc_cancels() {
    let mut app = App::new(TuiState::default(), Theme::dark());
    app.state.composer.text = "/".into();

    assert!(app.handle_slash_key(key(KeyCode::Down)).is_some());
    assert_eq!(app.state.slash.selected, 1);
    assert!(app.handle_slash_key(key(KeyCode::Up)).is_some());
    assert_eq!(app.state.slash.selected, 0);

    let effects = app.handle_slash_key(key(KeyCode::Esc)).unwrap();
    assert!(effects.is_empty());
    assert!(app.state.composer.text.is_empty());
}

#[test]
fn slash_menu_renders_above_the_composer_without_page_bleed() {
    let mut state = TuiState::default();
    state.composer.text = "/".into();
    let area = Rect::new(0, 0, 80, 24);
    let theme = Theme::dark();
    let mut buf = Buffer::empty(area);
    for cell in &mut buf.content {
        cell.set_symbol("X");
    }

    render_slash_menu(area, &mut buf, &state, &theme);

    let text: String = buf.content.iter().map(|cell| cell.symbol()).collect();
    assert!(text.contains("Commands"));
    assert!(text.contains("/exit"));
    assert!(text.contains("Exit session"));
    assert!(text.contains("/quit"));

    // The popup is anchored above the composer and cleared before drawing.
    let composer = linteum_tui::ui::layout(area, &state).composer;
    let height = slash_menu_lines(&state, &theme).len() as u16 + 2;
    let popup = Rect::new(2, composer.y - height, 72, height);
    for y in popup.top() + 1..popup.bottom() - 1 {
        for x in popup.left() + 1..popup.right() - 1 {
            assert_ne!(
                buf[(x, y)].symbol(),
                "X",
                "page glyph leaked into the menu at ({x}, {y})"
            );
        }
    }
    // The first command starts highlighted.
    assert_eq!(
        buf[(popup.left() + 1, popup.top() + 1)].bg,
        theme.selection.bg.expect("selection background")
    );
}

#[test]
fn slash_menu_hides_non_matching_commands() {
    let mut state = TuiState::default();
    state.composer.text = "/q".into();
    let area = Rect::new(0, 0, 80, 24);
    let mut buf = Buffer::empty(area);
    render_slash_menu(area, &mut buf, &state, &Theme::dark());
    let text: String = buf.content.iter().map(|cell| cell.symbol()).collect();
    assert!(text.contains("/quit"));
    assert!(!text.contains("/exit"));
}
