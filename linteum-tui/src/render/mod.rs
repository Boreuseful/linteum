//! Text rendering helpers shared by the UI widgets.

pub mod highlight;
pub mod markdown;

pub use highlight::highlight_code;
pub use markdown::{render_markdown, wrap_lines};
