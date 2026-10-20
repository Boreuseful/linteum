use linteum_tui::render::highlight::highlight_code;
use linteum_tui::render::markdown::{render_markdown, wrap_lines};
use linteum_tui::ui::Theme;
use ratatui::text::Line;

fn text(line: &Line<'_>) -> String {
    line.spans
        .iter()
        .map(|span| span.content.as_ref())
        .collect()
}

#[test]
fn final_assistant_renders_markdown() {
    let lines = render_markdown(
        "# Title\n\n- a\n- b\n\n```rust\nfn main() {}\n```",
        80,
        &Theme::dark(),
    );
    assert!(lines.iter().any(|line| text(line).contains("Title")));
    assert!(lines.iter().any(|line| text(line).contains("fn main")));
}

#[test]
fn markdown_tables_render() {
    let lines = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |", 80, &Theme::dark());
    assert!(lines.iter().any(|line| text(line).contains('a')));
    assert!(lines.iter().any(|line| text(line).contains('1')));
}

#[test]
fn unknown_language_renders_monospace() {
    let lines = highlight_code("unknown", "let x = 1;", &Theme::dark());
    assert!(lines.iter().any(|line| text(line).contains("let x = 1;")));
}

#[test]
fn prose_wraps() {
    let lines = wrap_lines(vec![Line::from("abcdefghij")], 5);
    assert!(lines.len() >= 2);
}

#[test]
fn wide_glyphs_wrap_by_display_width() {
    let lines = wrap_lines(vec![Line::from("中文中文中文")], 4);
    assert_eq!(lines.len(), 3);
    for line in &lines {
        assert!(line.width() <= 4, "line is {} columns", line.width());
    }
    assert_eq!(lines.iter().map(text).collect::<String>(), "中文中文中文");
}

#[test]
fn wrapping_preserves_span_styles() {
    let style = ratatui::style::Style::default()
        .fg(ratatui::style::Color::Red)
        .add_modifier(ratatui::style::Modifier::BOLD);
    let lines = wrap_lines(
        vec![Line::from(vec![
            ratatui::text::Span::raw("ab"),
            ratatui::text::Span::styled("中文", style),
        ])],
        3,
    );
    assert!(lines.iter().any(|line| {
        line.spans
            .iter()
            .any(|span| span.style == style && span.content.contains('中'))
    }));
}
