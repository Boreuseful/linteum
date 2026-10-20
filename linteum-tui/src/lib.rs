//! Standalone Rust terminal client for `linteum_agent`.
//!
//! The crate is split into a pure view-model and thin I/O/UI layers. See
//! `docs/en/design.md` for the module boundaries.

pub mod cli;
pub mod config;
pub mod connection;
pub mod logging;
pub mod protocol;
pub mod render;
pub mod run;
pub mod ui;
pub mod viewmodel;
pub mod workspace;
