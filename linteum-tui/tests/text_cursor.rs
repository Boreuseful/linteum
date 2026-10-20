#![allow(clippy::field_reassign_with_default)]

use linteum_tui::ui::Theme;
use linteum_tui::ui::palette::palette_cursor;
use linteum_tui::ui::screens::new_session::render_new_session;
use linteum_tui::ui::widgets::composer::render_composer;
use linteum_tui::viewmodel::{Input, NewSessionForm, Overlay, TuiState, UiAction, reduce};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::{Position, Rect};

fn composer_position(state: &TuiState, width: u16, height: u16) -> Option<Position> {
    let area = Rect::new(0, 0, width, height);
    let mut buf = Buffer::empty(area);
    render_composer(area, &mut buf, state, &Theme::dark())
}

#[test]
fn composer_inserts_and_deletes_at_the_cursor() {
    let mut state = TuiState::default();
    reduce(&mut state, Input::Text("abc".into()));
    reduce(&mut state, Input::Action(UiAction::CursorLeft));
    reduce(&mut state, Input::Action(UiAction::CursorLeft));
    reduce(&mut state, Input::Text("X".into()));
    assert_eq!(state.composer.text, "aXbc");
    assert_eq!(state.composer.cursor, 2);

    reduce(&mut state, Input::Action(UiAction::DeleteBackward));
    assert_eq!(state.composer.text, "abc");
    reduce(&mut state, Input::Action(UiAction::DeleteForward));
    assert_eq!(state.composer.text, "ac");
    assert_eq!(state.composer.cursor, 1);

    reduce(&mut state, Input::Action(UiAction::CursorHome));
    assert_eq!(state.composer.cursor, 0);
    reduce(&mut state, Input::Action(UiAction::CursorEnd));
    assert_eq!(state.composer.cursor, 2);
}

#[test]
fn composer_cursor_moves_between_lines() {
    let mut state = TuiState::default();
    reduce(&mut state, Input::Text("ab\ncde".into()));
    assert_eq!(state.composer.cursor, 6);

    reduce(&mut state, Input::Action(UiAction::CursorUp));
    assert_eq!(state.composer.cursor, 2);
    reduce(&mut state, Input::Action(UiAction::CursorDown));
    assert_eq!(state.composer.cursor, 5);

    reduce(&mut state, Input::Action(UiAction::CursorHome));
    assert_eq!(state.composer.cursor, 3);
    reduce(&mut state, Input::Action(UiAction::CursorEnd));
    assert_eq!(state.composer.cursor, 6);
}

#[test]
fn cursor_steps_over_multibyte_characters() {
    let mut state = TuiState::default();
    reduce(&mut state, Input::Text("你a".into()));
    assert_eq!(state.composer.cursor, "你a".len());

    reduce(&mut state, Input::Action(UiAction::CursorLeft));
    assert_eq!(state.composer.cursor, "你".len());
    reduce(&mut state, Input::Action(UiAction::CursorLeft));
    assert_eq!(state.composer.cursor, 0);

    reduce(&mut state, Input::Action(UiAction::DeleteForward));
    assert_eq!(state.composer.text, "a");
}

#[test]
fn composer_cursor_is_rendered_at_the_insertion_point() {
    let mut state = TuiState::default();
    state.composer.text = "hello".into();
    state.composer.cursor = 2;
    assert_eq!(composer_position(&state, 80, 3), Some(Position::new(4, 0)));

    state.composer.text = "ab\ncd".into();
    state.composer.cursor = 3;
    assert_eq!(composer_position(&state, 80, 3), Some(Position::new(2, 1)));
    state.composer.cursor = 5;
    assert_eq!(composer_position(&state, 80, 3), Some(Position::new(4, 1)));
}

#[test]
fn multiline_composer_prefixes_only_the_first_line() {
    let mut state = TuiState::default();
    state.composer.text = "hello\nworld".into();
    state.composer.cursor = 8;
    let area = Rect::new(0, 0, 80, 3);
    let mut buf = Buffer::empty(area);
    render_composer(area, &mut buf, &state, &Theme::dark());
    let row = |y: u16| -> String { (0..80).map(|x| buf[(x, y)].symbol()).collect() };
    assert!(row(0).starts_with("> hello"));
    assert!(row(1).starts_with("  world"));
}

#[test]
fn empty_composer_puts_the_cursor_after_the_prompt() {
    let state = TuiState::default();
    assert_eq!(composer_position(&state, 80, 1), Some(Position::new(2, 0)));
}

#[test]
fn long_composer_lines_scroll_to_keep_the_cursor_visible() {
    let mut state = TuiState::default();
    state.composer.text = "a".repeat(100);
    state.composer.cursor = 100;
    let position = composer_position(&state, 20, 1).expect("cursor");
    assert_eq!(position, Position::new(19, 0));
}

#[test]
fn multiline_composer_keeps_the_cursor_line_visible() {
    let mut state = TuiState::default();
    state.composer.text = (0..8)
        .map(|index| format!("line{index}"))
        .collect::<Vec<_>>()
        .join("\n");
    state.composer.cursor = state.composer.text.len();
    let position = composer_position(&state, 80, 6).expect("cursor");
    assert_eq!(position.y, 5);
}

#[test]
fn new_session_edits_the_focused_field_at_the_cursor() {
    let mut form = NewSessionForm::default();
    form.workspace = "ab".into();
    form.workspace_cursor = 2;
    form.insert_char('c');
    assert_eq!(form.workspace, "abc");

    form.move_left();
    form.insert_char('X');
    assert_eq!(form.workspace, "abXc");
    form.backspace();
    assert_eq!(form.workspace, "abc");

    form.focus = 1;
    form.insert_char('t');
    assert_eq!(form.title, "t");
    assert_eq!(form.workspace, "abc");
}

#[test]
fn new_session_fields_render_a_cursor() {
    let mut state = TuiState::default();
    state.overlay = Some(Overlay::NewSession);
    state.new_session.workspace = "/tmp".into();
    state.new_session.workspace_cursor = 4;
    let area = Rect::new(0, 0, 80, 24);
    let mut buf = Buffer::empty(area);
    let position = render_new_session(area, &mut buf, &state, &Theme::dark()).expect("cursor");
    assert_eq!(position, Position::new(16, 1));

    state.new_session.focus = 1;
    state.new_session.title = "demo".into();
    state.new_session.title_cursor = 2;
    let position = render_new_session(area, &mut buf, &state, &Theme::dark()).expect("cursor");
    assert_eq!(position, Position::new(14, 3));
}

#[test]
fn palette_filter_edits_and_renders_at_the_cursor() {
    let mut state = TuiState::default();
    state.palette.filter = "qu".into();
    state.palette.cursor = 2;
    state.palette.insert_char('i');
    assert_eq!(state.palette.filter, "qui");

    state.palette.move_left();
    state.palette.insert_char('X');
    assert_eq!(state.palette.filter, "quXi");
    state.palette.backspace();
    assert_eq!(state.palette.filter, "qui");

    state.palette.move_end();
    assert_eq!(
        palette_cursor(Rect::new(0, 0, 80, 24), &state),
        Some(Position::new(5, 0))
    );
}

#[test]
fn the_terminal_cursor_follows_the_composer() {
    let mut state = TuiState::default();
    state.composer.text = "hi".into();
    state.composer.cursor = 1;

    let backend = TestBackend::new(80, 24);
    let mut terminal = Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| linteum_tui::ui::render(frame, &state, &Theme::dark()))
        .unwrap();
    assert_eq!(
        terminal.get_cursor_position().unwrap(),
        Position::new(3, 22)
    );
}
