//! Transcript, composer, and inline cards.

pub mod compaction_card;
pub mod composer;
pub mod error_card;
pub mod sidebar;
pub mod slash_menu;
pub mod tool_card;
pub mod transcript;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::Line;
use ratatui::widgets::Widget;

pub use compaction_card::{compaction_card_lines, render_compaction_card};
pub use composer::{ComposerWidget, render_composer};
pub use error_card::{error_card_lines, render_error_card};
pub use sidebar::render_sidebar;
pub use slash_menu::{render_slash_menu, slash_menu_lines};
pub use tool_card::{render_tool_card, tool_card_lines};
pub use transcript::{render_transcript, transcript_lines, visible_offset};

pub fn render_lines(area: Rect, buf: &mut Buffer, lines: &[Line<'static>]) {
    for (row, line) in lines.iter().take(area.height as usize).enumerate() {
        let row_area = Rect::new(area.x, area.y + row as u16, area.width, 1);
        // `Line::render` only writes the spans; clear the row first so page
        // glyphs cannot remain after the end of each line.
        for x in row_area.left()..row_area.right() {
            buf[(x, row_area.y)].reset();
        }
        line.clone().render(row_area, buf);
    }
}
