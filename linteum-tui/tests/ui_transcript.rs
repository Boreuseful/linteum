use linteum_tui::ui::Theme;
use linteum_tui::ui::layout;
use linteum_tui::ui::widgets::composer::ComposerWidget;
use linteum_tui::ui::widgets::transcript::{transcript_lines, visible_offset};
use linteum_tui::viewmodel::{
    AssistantItem, Input, Item, ScrollState, TuiState, UiAction, UserItem, reduce,
};
use ratatui::layout::Rect;
use ratatui::text::Line;

fn text(line: &Line<'_>) -> String {
    line.spans
        .iter()
        .map(|span| span.content.as_ref())
        .collect()
}

#[test]
fn composer_grows_to_six_lines_then_scrolls() {
    assert_eq!(ComposerWidget::desired_height(""), 1);
    assert_eq!(ComposerWidget::desired_height("a"), 1);
    assert_eq!(ComposerWidget::desired_height("a\nb"), 2);
    assert_eq!(ComposerWidget::desired_height(&"a\n".repeat(10)), 6);
}

#[test]
fn transcript_preserves_scroll_when_user_scrolled_up() {
    let mut state = TuiState::default();
    for index in 0..10 {
        state.transcript.items.push(Item::User(UserItem {
            text: format!("m{index}"),
            ..Default::default()
        }));
    }
    state.transcript.scroll.offset = 1;
    state.transcript.scroll.stick_to_bottom = false;
    let lines = transcript_lines(&state, 80, &Theme::dark());
    assert_eq!(visible_offset(&state, lines.len(), 3), 1);
    state.transcript.scroll.stick_to_bottom = true;
    assert_eq!(
        visible_offset(&state, lines.len(), 3),
        lines.len().saturating_sub(3)
    );
}

#[test]
fn page_up_from_live_bottom_scrolls_one_page_not_to_the_top() {
    let mut scroll = ScrollState::default();
    // 100 rendered lines in a 10-line viewport: the live bottom starts at 90.
    scroll.apply(UiAction::ScrollPageUp, 100, 10);
    assert!(!scroll.stick_to_bottom);
    assert_eq!(scroll.visible_offset(100, 10), 80);
}

#[test]
fn scroll_up_from_live_bottom_moves_one_line() {
    let mut scroll = ScrollState::default();
    scroll.apply(UiAction::ScrollUp, 100, 10);
    assert_eq!(scroll.visible_offset(100, 10), 89);
}

#[test]
fn page_down_reattaches_to_live_output() {
    let mut scroll = ScrollState::default();
    scroll.apply(UiAction::ScrollPageUp, 100, 10);
    scroll.apply(UiAction::ScrollPageDown, 100, 10);
    assert!(scroll.stick_to_bottom);
    assert_eq!(scroll.visible_offset(100, 10), 90);
}

#[test]
fn scroll_position_is_preserved_when_new_output_arrives() {
    let mut scroll = ScrollState::default();
    scroll.apply(UiAction::ScrollPageUp, 100, 10);
    assert_eq!(scroll.visible_offset(120, 10), 80);
}

#[test]
fn short_transcript_stays_pinned_to_live_output() {
    let mut scroll = ScrollState::default();
    scroll.apply(UiAction::ScrollPageUp, 6, 10);
    assert!(scroll.stick_to_bottom);
    assert_eq!(scroll.visible_offset(6, 10), 0);
}

#[test]
fn scroll_action_carries_render_metrics_through_the_reducer() {
    let mut state = TuiState::default();
    reduce(
        &mut state,
        Input::Scroll {
            action: UiAction::ScrollPageUp,
            total_lines: 100,
            viewport: 10,
        },
    );
    assert_eq!(state.transcript.scroll.visible_offset(100, 10), 80);
}

#[test]
fn long_cjk_output_fits_the_transcript_pane() {
    let mut state = TuiState::default();
    state.sidebar.visible = true;
    let long = "这是一段很长的中文输出，用来验证侧边栏打开时每一行都会按显示宽度换行，\
                不会越过对话区域被侧边栏裁掉。";
    state.transcript.items.push(Item::Assistant(AssistantItem {
        text: long.into(),
        ..Default::default()
    }));

    let rects = layout(Rect::new(0, 0, 80, 24), &state);
    let lines = transcript_lines(&state, rects.transcript.width, &Theme::dark());
    for line in &lines {
        assert!(
            line.width() <= rects.transcript.width as usize,
            "line is {} columns wide but the pane is {}",
            line.width(),
            rects.transcript.width
        );
    }
    let joined = lines.iter().map(text).collect::<String>();
    assert!(joined.contains(long.trim()));
}

#[test]
fn reasoning_renders_before_the_answer_text() {
    let mut state = TuiState::default();
    state.prefs.show_reasoning = true;
    state.transcript.items.push(Item::Assistant(AssistantItem {
        reasoning: "thinking first".into(),
        text: "answer second".into(),
        ..Default::default()
    }));

    let lines = transcript_lines(&state, 80, &Theme::dark());
    let rendered: Vec<String> = lines.iter().map(text).collect();
    let reasoning = rendered
        .iter()
        .position(|line| line.contains("thinking first"))
        .expect("reasoning line");
    let answer = rendered
        .iter()
        .position(|line| line.contains("answer second"))
        .expect("answer line");
    assert!(
        reasoning < answer,
        "reasoning must render above the answer text: {rendered:?}"
    );
}

#[test]
fn streaming_text_is_plain() {
    let mut state = TuiState::default();
    state.transcript.items.push(Item::Assistant(AssistantItem {
        text: "**not markdown**".into(),
        ..Default::default()
    }));
    let lines = transcript_lines(&state, 80, &Theme::dark());
    assert!(
        lines
            .iter()
            .any(|line| text(line).contains("**not markdown**"))
    );
}
