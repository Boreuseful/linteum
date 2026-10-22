use linteum_tui::ui::Theme;
use linteum_tui::ui::widgets::tool_card::tool_card_lines;
use linteum_tui::viewmodel::ToolCard;
use ratatui::text::Line;

fn text(line: &Line<'_>) -> String {
    line.spans
        .iter()
        .map(|span| span.content.as_ref())
        .collect()
}

#[test]
fn tool_card_collapsed_by_default() {
    let card = ToolCard {
        name: "bash".into(),
        call_id: "c1".into(),
        args: Some("{\"x\":1}".into()),
        ..Default::default()
    };
    let lines = tool_card_lines(&card, &Theme::dark());
    assert!(lines.iter().any(|line| text(line).contains("bash")));
    assert!(!lines.iter().any(|line| text(line).contains("{\"x\":1}")));
}

#[test]
fn tool_card_expands_to_json() {
    let card = ToolCard {
        name: "bash".into(),
        call_id: "c1".into(),
        args: Some("{\"x\":1}".into()),
        output: Some("ok".into()),
        expanded: true,
        ..Default::default()
    };
    let lines = tool_card_lines(&card, &Theme::dark());
    assert!(lines.iter().any(|line| text(line).contains("{\"x\":1}")));
    assert!(lines.iter().any(|line| text(line).contains("ok")));
}
