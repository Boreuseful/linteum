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
fn result_deserializes_with_name() {
    let line = r#"{"v":1,"kind":"result","id":"r-1","reply_to":"c-1","ok":true,"result":{"plugins":[]},"name":"plugin.list"}"#;
    match decode_envelope(line).unwrap() {
        Envelope::Result(result) => {
            assert_eq!(result.reply_to, "c-1");
            assert_eq!(result.name.as_deref(), Some("plugin.list"));
        }
        other => panic!("expected result, got {other:?}"),
    }
}

#[test]
fn result_without_name_deserializes_none() {
    let line = r#"{"v":1,"kind":"result","id":"r-1","reply_to":"c-1","ok":true,"result":{}}"#;
    match decode_envelope(line).unwrap() {
        Envelope::Result(result) => assert!(result.name.is_none()),
        other => panic!("expected result, got {other:?}"),
    }
}

#[test]
fn hello_result_with_instance_id_decodes() {
    let line = r#"{"v":1,"kind":"result","id":"r-1","reply_to":"c-1","ok":true,"result":{"protocol":1,"features":["view","replay"],"limits":{},"instance_id":"gw-1"}}"#;
    match decode_envelope(line).unwrap() {
        Envelope::Result(result) => {
            assert!(result.ok);
            assert_eq!(result.result["protocol"], 1);
            assert_eq!(result.result["instance_id"], "gw-1");
        }
        other => panic!("expected result, got {other:?}"),
    }
}

#[test]
fn hello_result_without_instance_id_decodes() {
    // Pre-v0.4 gateways omit `instance_id`; the wire shape must still decode.
    let line = r#"{"v":1,"kind":"result","id":"r-1","reply_to":"c-1","ok":true,"result":{"protocol":1,"features":["view","replay"],"limits":{}}}"#;
    match decode_envelope(line).unwrap() {
        Envelope::Result(result) => {
            assert!(result.ok);
            assert_eq!(result.result["protocol"], 1);
            assert!(result.result.get("instance_id").is_none());
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

#[test]
fn plugin_manage_commands_carry_only_an_id() {
    for (name, id) in [
        ("plugin.enable", "acme.search"),
        ("plugin.disable", "acme.search"),
    ] {
        let encoded = encode_command(&command(
            "c-03",
            name,
            serde_json::json!({"id": id}).as_object().unwrap().clone(),
            None,
        ))
        .unwrap();
        let value: serde_json::Value = serde_json::from_str(&encoded).unwrap();
        assert_eq!(value["kind"], "cmd");
        assert_eq!(value["name"], name);
        assert_eq!(value["args"], serde_json::json!({"id": id}));
        assert!(value.get("session_id").is_none());
    }
}

#[test]
fn plugin_list_command_has_no_session_or_args() {
    let encoded = encode_command(&command(
        "c-02",
        "plugin.list",
        serde_json::Map::new(),
        None,
    ))
    .unwrap();
    let value: serde_json::Value = serde_json::from_str(&encoded).unwrap();
    assert_eq!(value["kind"], "cmd");
    assert_eq!(value["name"], "plugin.list");
    assert_eq!(value["args"], serde_json::json!({}));
    assert!(value.get("session_id").is_none());
}
