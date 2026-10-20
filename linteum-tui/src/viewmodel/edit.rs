//! Byte-cursor helpers shared by editable text fields.

/// Clamp `index` to `text` and to a char boundary.
pub fn clamp(text: &str, index: usize) -> usize {
    let mut index = index.min(text.len());
    while index > 0 && !text.is_char_boundary(index) {
        index -= 1;
    }
    index
}

pub fn left(text: &str, cursor: usize) -> usize {
    let cursor = clamp(text, cursor);
    text[..cursor]
        .char_indices()
        .next_back()
        .map(|(index, _)| index)
        .unwrap_or(0)
}

pub fn right(text: &str, cursor: usize) -> usize {
    let cursor = clamp(text, cursor);
    text[cursor..]
        .char_indices()
        .nth(1)
        .map(|(offset, _)| cursor + offset)
        .unwrap_or(text.len())
}

pub fn line_start(text: &str, cursor: usize) -> usize {
    let cursor = clamp(text, cursor);
    text[..cursor]
        .rfind('\n')
        .map(|index| index + 1)
        .unwrap_or(0)
}

pub fn line_end(text: &str, cursor: usize) -> usize {
    let cursor = clamp(text, cursor);
    text[cursor..]
        .find('\n')
        .map(|offset| cursor + offset)
        .unwrap_or(text.len())
}

/// Move one line up, keeping the character column where possible.
pub fn up(text: &str, cursor: usize) -> usize {
    let cursor = clamp(text, cursor);
    let start = line_start(text, cursor);
    if start == 0 {
        return cursor;
    }
    let previous_end = start - 1; // the '\n' that ends the previous line
    let previous_start = line_start(text, previous_end);
    let column = text[start..cursor].chars().count();
    previous_start + char_offset(&text[previous_start..previous_end], column)
}

/// Move one line down, keeping the character column where possible.
pub fn down(text: &str, cursor: usize) -> usize {
    let cursor = clamp(text, cursor);
    let end = line_end(text, cursor);
    if end >= text.len() {
        return cursor;
    }
    let next_start = end + 1;
    let next_end = line_end(text, next_start);
    let start = line_start(text, cursor);
    let column = text[start..cursor].chars().count();
    next_start + char_offset(&text[next_start..next_end], column)
}

pub fn insert(text: &mut String, cursor: &mut usize, value: &str) {
    let at = clamp(text, *cursor);
    text.insert_str(at, value);
    *cursor = at + value.len();
}

pub fn insert_char(text: &mut String, cursor: &mut usize, ch: char) {
    let at = clamp(text, *cursor);
    text.insert(at, ch);
    *cursor = at + ch.len_utf8();
}

pub fn backspace(text: &mut String, cursor: &mut usize) {
    let at = clamp(text, *cursor);
    if at == 0 {
        return;
    }
    let previous = left(text, at);
    text.replace_range(previous..at, "");
    *cursor = previous;
}

pub fn delete(text: &mut String, cursor: usize) {
    let at = clamp(text, cursor);
    let next = right(text, at);
    if next > at {
        text.replace_range(at..next, "");
    }
}

/// Byte offset of the character column `column` within `text`.
fn char_offset(text: &str, column: usize) -> usize {
    text.char_indices()
        .nth(column)
        .map(|(index, _)| index)
        .unwrap_or(text.len())
}
