//! Markdown to ratatui lines.

use pulldown_cmark::{CodeBlockKind, Event, Options, Parser, Tag, TagEnd};
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthChar;

use crate::ui::Theme;

use super::highlight::highlight_code;

pub fn render_markdown(text: &str, width: u16, theme: &Theme) -> Vec<Line<'static>> {
    let mut options = Options::empty();
    options.insert(Options::ENABLE_TABLES);
    options.insert(Options::ENABLE_STRIKETHROUGH);
    let parser = Parser::new_ext(text, options);

    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut current: Vec<Span<'static>> = Vec::new();
    let mut in_code = false;
    let mut code = String::new();
    let mut code_lang = String::new();
    let mut list_depth = 0usize;

    for event in parser {
        match event {
            Event::Start(Tag::Heading { .. }) => flush(&mut current, &mut lines),
            Event::End(TagEnd::Heading(_)) => {
                flush(&mut current, &mut lines);
                lines.push(Line::from(""));
            }
            Event::Start(Tag::Paragraph) | Event::End(TagEnd::Paragraph) => {
                flush(&mut current, &mut lines);
            }
            Event::Start(Tag::Item) => current.push(Span::styled(
                format!("{}- ", "  ".repeat(list_depth.saturating_sub(1))),
                theme.text,
            )),
            Event::End(TagEnd::Item) => flush(&mut current, &mut lines),
            Event::Start(Tag::List(_)) => list_depth += 1,
            Event::End(TagEnd::List(_)) => list_depth = list_depth.saturating_sub(1),
            Event::Start(Tag::CodeBlock(kind)) => {
                flush(&mut current, &mut lines);
                in_code = true;
                code.clear();
                code_lang = match kind {
                    CodeBlockKind::Fenced(lang) => lang.to_string(),
                    CodeBlockKind::Indented => String::new(),
                };
            }
            Event::End(TagEnd::CodeBlock) => {
                in_code = false;
                lines.extend(highlight_code(&code_lang, &code, theme));
                lines.push(Line::from(""));
            }
            Event::Text(text) => {
                if in_code {
                    code.push_str(&text);
                } else {
                    current.push(Span::styled(text.to_string(), theme.text));
                }
            }
            Event::Code(text) => current.push(Span::styled(text.to_string(), theme.code)),
            Event::SoftBreak | Event::HardBreak => flush(&mut current, &mut lines),
            Event::Start(Tag::Table(_)) => flush(&mut current, &mut lines),
            Event::End(TagEnd::Table) => lines.push(Line::from("")),
            Event::End(TagEnd::TableHead) => flush(&mut current, &mut lines),
            Event::Start(Tag::TableRow) => {
                flush(&mut current, &mut lines);
                current.clear();
            }
            Event::End(TagEnd::TableRow) => flush(&mut current, &mut lines),
            Event::Start(Tag::TableCell) => current.push(Span::styled("| ", theme.muted)),
            Event::End(TagEnd::TableCell) => current.push(Span::styled(" ", theme.muted)),
            Event::Rule => {
                flush(&mut current, &mut lines);
                lines.push(Line::from("---"));
            }
            _ => {}
        }
    }

    flush(&mut current, &mut lines);
    wrap_lines(lines, width)
}

fn flush(current: &mut Vec<Span<'static>>, lines: &mut Vec<Line<'static>>) {
    if !current.is_empty() {
        lines.push(Line::from(std::mem::take(current)));
    }
}

/// Wrap lines at `width` terminal columns. Wide (CJK) glyphs occupy two
/// columns, so counting scalars would let a line overflow its pane and be
/// clipped at the sidebar border. Span styles survive the wrap.
pub fn wrap_lines(lines: Vec<Line<'static>>, width: u16) -> Vec<Line<'static>> {
    let width = width.max(1) as usize;
    let mut output = Vec::new();
    for line in lines {
        output.extend(wrap_line(line, width));
    }
    output
}

fn wrap_line(line: Line<'static>, width: usize) -> Vec<Line<'static>> {
    if line.width() <= width {
        return vec![line];
    }

    let style = line.style;
    let alignment = line.alignment;
    let mut rows: Vec<Line<'static>> = Vec::new();
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut used = 0usize;

    for span in line.spans {
        let span_style = span.style;
        let mut chunk = String::new();
        for ch in span.content.chars() {
            let ch_width = UnicodeWidthChar::width(ch).unwrap_or(0);
            if used + ch_width > width && used > 0 {
                if !chunk.is_empty() {
                    spans.push(Span::styled(std::mem::take(&mut chunk), span_style));
                }
                rows.push(Line {
                    spans: std::mem::take(&mut spans),
                    style,
                    alignment,
                });
                used = 0;
            }
            chunk.push(ch);
            used += ch_width;
        }
        if !chunk.is_empty() {
            spans.push(Span::styled(chunk, span_style));
        }
    }

    if !spans.is_empty() || rows.is_empty() {
        rows.push(Line {
            spans,
            style,
            alignment,
        });
    }
    rows
}
