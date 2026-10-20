use linteum_tui::protocol::{Envelope, command, decode_envelope, encode_command};

#[test]
fn command_round_trip() {
    let encoded = encode_command(&command(
        "c-01",
        "user.message",
        serde_json::json!({"text": "hi", "client_message_id": "m-01"})
            .as_object()
            .unwrap()
            .clone(),
        Some("s-01".into()),
    ))
    .unwrap();
    let value: serde_json::Value = serde_json::from_str(&encoded).unwrap();
    assert_eq!(value["v"], 1);
    assert_eq!(value["kind"], "cmd");
    assert_eq!(value["name"], "user.message");
    assert_eq!(value["session_id"], "s-01");
}

#[test]
fn result_deserializes() {
    let line = r#"{"v":1,"kind":"result","id":"r-1","reply_to":"c-1","ok":true,"result":{"accepted":true}}"#;
    match decode_envelope(line).unwrap() {
        Envelope::Result(result) => {
            assert_eq!(result.reply_to, "c-1");
            assert!(result.ok);
            assert_eq!(result.result["accepted"], true);
        }
        other => panic!("expected result, got {other:?}"),
    }
}

#[test]
fn frame_deserializes_optional_fields() {
    let line = r#"{"v":1,"kind":"frame","id":"f-1","frame":"assistant.delta","payload":{"text":"hi"},"session_id":"s-1","seq":7,"ephemeral":true,"replay":false,"ordinal":0}"#;
    match decode_envelope(line).unwrap() {
        Envelope::Frame(frame) => {
            assert_eq!(frame.frame, "assistant.delta");
            assert_eq!(frame.seq, Some(7));
            assert!(frame.ephemeral);
            assert!(!frame.replay);
        }
        other => panic!("expected frame, got {other:?}"),
    }
}

#[test]
fn unknown_kind_is_retained() {
    let line = r#"{"v":1,"kind":"mystery","payload":{"x":1}}"#;
    match decode_envelope(line).unwrap() {
        Envelope::Unknown { kind, body } => {
            assert_eq!(kind, "mystery");
            assert_eq!(body["payload"]["x"], 1);
        }
        other => panic!("expected unknown, got {other:?}"),
    }
}

#[test]
fn unknown_fields_are_ignored() {
    let line =
        r#"{"v":1,"kind":"frame","id":"f-1","frame":"user.message","payload":{},"new_field":42}"#;
    assert!(matches!(decode_envelope(line).unwrap(), Envelope::Frame(_)));
}

#[test]
fn invalid_json_is_an_error() {
    assert!(decode_envelope("{").is_err());
}

#[test]
fn error_frame_deserializes() {
    let line = r#"{"v":1,"kind":"error","error_info":{"code":"E_INTERNAL","source":"gateway","location":"x","message":"boom","exc_type":null,"detail":"","retryable":false,"enters_history":false,"context":{}}}"#;
    match decode_envelope(line).unwrap() {
        Envelope::Error(error) => assert_eq!(error.error_info.code, "E_INTERNAL"),
        other => panic!("expected error, got {other:?}"),
    }
}
