//! The guard against the classic cross-language failure: a field added on one side of the seam and a silent
//! mismatch on the other. This test reads the C header and asserts the Rust mirror still agrees with it.
//! It parses text rather than including the header, so it needs no C toolchain and runs on any machine.

use std::path::PathBuf;
use strata_abi::{Status, ABI_VERSION};

fn header() -> String {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    // crates/strata-abi -> crates -> rust-host -> repo root
    p.pop(); p.pop(); p.pop();
    p.push("include"); p.push("strata"); p.push("c"); p.push("abi.h");
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("cannot read {}: {e}", p.display()))
}

fn define_value(src: &str, name: &str) -> u32 {
    for line in src.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix(&format!("#define {name}")) {
            if let Some(v) = rest.trim().split_whitespace().next() {
                if let Ok(n) = v.parse::<u32>() {
                    return n;
                }
            }
        }
    }
    panic!("#define {name} not found in the header — the guard must not be able to pass silently");
}

fn enum_discriminants(src: &str, enum_name: &str) -> Vec<(String, u32)> {
    let mut out = Vec::new();
    let mut inside = false;
    for line in src.lines() {
        let l = line.trim();
        if l.starts_with("typedef enum") && l.contains(enum_name) {
            inside = true;
            continue;
        }
        if inside {
            if l.starts_with('}') {
                break;
            }
            // a member line looks like:  STRATA_OK = 0,   (possibly followed by a /* comment */)
            let body = l.split("/*").next().unwrap_or("").trim().trim_end_matches(',').trim();
            if let Some((name, val)) = body.split_once('=') {
                if let Ok(n) = val.trim().parse::<u32>() {
                    out.push((name.trim().to_string(), n));
                }
            }
        }
    }
    out
}

#[test]
fn version_constant_matches_the_header() {
    assert_eq!(define_value(&header(), "STRATA_ABI_VERSION"), ABI_VERSION);
}

#[test]
fn status_codes_match_the_header() {
    let src = header();
    let d = enum_discriminants(&src, "strata_status");
    assert!(!d.is_empty(), "parsed no status codes out of the header");

    let mut seen: Vec<(String, Status)> = vec![
        ("STRATA_OK".to_string(), Status::Ok),
        ("STRATA_ERR_WOULD_BLOCK".to_string(), Status::WouldBlock),
        ("STRATA_ERR_BUFFER_TOO_SHORT".to_string(), Status::BufferTooShort),
        ("STRATA_ERR_BAD_SLOT".to_string(), Status::BadSlot),
        ("STRATA_ERR_DEVICE_UNAVAILABLE".to_string(), Status::DeviceUnavailable),
        ("STRATA_ERR_ABI_VERSION".to_string(), Status::AbiVersion),
    ];
    seen.sort();
    let mut from_header: Vec<(String, Status)> = d
        .iter()
        .map(|(n, v)| (n.clone(), Status::from_c(*v as i32).unwrap_or_else(|| panic!("{n} = {v} has no Rust mirror"))))
        .collect();
    from_header.sort();
    assert_eq!(seen, from_header);
}

#[test]
fn expert_ref_is_a_plain_c_data_layout() {
    // The struct the host hands down: pointer, length, slot, tier. If a Rust type ever claims to represent it,
    // the layout has to agree byte for byte. 8 + 8 + 4 + 4 = 24, no padding, alignment 8.
    let src = header();
    assert!(src.contains("const unsigned char *bytes;"), "bytes field moved");
    assert!(src.contains("unsigned long long len;"), "len field moved");
    assert!(src.contains("int slot;"), "slot field moved");
    assert!(src.contains("int source_tier;"), "source_tier field moved");
}
