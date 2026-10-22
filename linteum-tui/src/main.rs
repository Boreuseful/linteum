use clap::Parser;
use linteum_tui::run::AppError;

#[tokio::main]
async fn main() {
    let cli = linteum_tui::cli::Cli::parse();
    if let Err(error) = linteum_tui::run::run_app(cli).await {
        let code = match &error {
            AppError::NotATerminal => 2,
            AppError::Config(linteum_tui::config::ConfigError::AttachRejectsGatewayFlags) => 2,
            _ => 1,
        };
        eprintln!("linteum-tui: {error}");
        std::process::exit(code);
    }
}
