//! The stages' WGSL against the Rust definitions: the preludes compile, the frame's WGSL layout
//! is the uploaded struct's, the WGSL of a pack without `[ray_tracing]` is what it was before the
//! key, and docs/api/shaders-reference-0.2.md is up to date. Rewrite the reference with
//! `HORIZON_WRITE_DOCS=1 cargo test -p horizon-shader-api --test it reference::`.
use horizon_shader_api::{
    Frame, Pack, StageKind, compose, package::sha256_hex, reference, stages::Tracing,
};
use naga::valid::Capabilities;
use std::path::Path;

fn root() -> &'static Path {
    Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn validate(text: &str) -> naga::Module {
    validate_for(text, Capabilities::default())
}

fn validate_for(text: &str, capabilities: Capabilities) -> naga::Module {
    let module = naga::front::wgsl::parse_str(text)
        .unwrap_or_else(|e| panic!("{}\n{text}", e.emit_to_string(text)));
    naga::valid::Validator::new(naga::valid::ValidationFlags::all(), capabilities)
        .validate(&module)
        .unwrap_or_else(|e| panic!("{}\n{text}", e.emit_to_string(text)));
    module
}

/// Vanilla's own entity colour (`entity_color` in the renderer's entity.wgsl), from the stage's
/// inputs: the reference prints the same function.
const VANILLA_ENTITY: &str = "\
fn entity(input: EntityInput) -> Surface {
    let base = input.albedo.rgb * input.tint.rgb * input.shade;
    // Vanilla lays the flash over the surface in its gamma (the 2.2 power), before the light.
    let gamma = vec3<f32>(1.0 / 2.2);
    let flashed = pow(mix(pow(base, gamma), pow(input.overlay.rgb, gamma), input.overlay.a), vec3<f32>(2.2));
    return Surface(vec4<f32>(flashed * input.lightmap, input.albedo.a * input.tint.a));
}
";

#[test]
fn the_reference_prints_vanilla_s_entity_function_as_it_is_checked() {
    let doc = reference::markdown();
    assert!(doc.contains(&format!("```wgsl\n{VANILLA_ENTITY}```")));
}

/// Vanilla's look, written as each stage.
fn vanilla(kind: StageKind) -> &'static str {
    match kind {
        StageKind::Sky => "fn sky(input: SkyInput) -> vec4<f32> { return input.color; }\n",
        StageKind::Material => {
            "fn material(input: MaterialInput) -> Surface {\n    \
             return Surface(input.albedo * input.tint * vec4<f32>(input.lightmap, 1.0));\n}\n"
        }
        StageKind::Vertex => {
            "fn vertex(input: VertexInput) -> vec3<f32> { return input.position; }\n"
        }
        StageKind::Weather => {
            "fn weather(input: WeatherInput) -> vec4<f32> { return input.color; }\n"
        }
        StageKind::Post => "fn post(input: PostInput) -> vec4<f32> { return input.color; }\n",
        StageKind::Tonemap => {
            "fn tonemap(input: TonemapInput) -> vec3<f32> { return input.color; }\n"
        }
        StageKind::Entity => VANILLA_ENTITY,
        StageKind::Compute | StageKind::BeforeWorld => {
            "fn compute(input: ComputeInput) {}
"
        }
    }
}

#[test]
fn every_stage_compiles_with_vanilla_s_function() {
    for kind in StageKind::ALL {
        let text = format!("{}{}{}", kind.prelude(), vanilla(kind), kind.entries());
        let module = validate(&text);
        assert!(
            module
                .entry_points
                .iter()
                .all(|e| e.name.starts_with("horizon_")),
            "{kind:?}"
        );
    }
}

/// A pack with `[ray_tracing]`: every stage without the scene, on any device, and with it, on
/// one with ray queries.
#[test]
fn every_stage_compiles_under_ray_tracing() {
    let traces = "fn post(input: PostInput) -> vec4<f32> {\n    \
                  let hit = trace(view_position(input.uv, input.depth), frame.sun_direction, frame.ray_distance);\n    \
                  let maps = hit_normal_map(hit) * hit_specular_map(hit) * f32(block_pbr_maps()) + hit.tangent;\n    \
                  let water = trace_layers(hit.position, frame.sun_direction, 8.0, RAY_SOLID | RAY_TRANSLUCENT);\n    \
                  let wet = f32(trace_any_layers(hit.position, hit.normal, 8.0, RAY_TRANSLUCENT)) * water.albedo.a;\n    \
                  let own = maps * hit.emission * f32(hit.fluid) * f32(hit.translucent) + wet;\n    \
                  return select(input.color, hit.albedo * hit.tint + own, hit.distance >= 0.0 && frame.has_ray_tracing == 1u);\n}\n";
    let shaded = "fn material(input: MaterialInput) -> Surface {\n    \
                  let lit = f32(!trace_any(input.position, frame.sun_direction, frame.ray_distance))\n        \
                      * hit_specular_map(trace(input.position, input.normal, 8.0)).r;\n    \
                  return Surface(input.albedo * input.tint * vec4<f32>(input.lightmap * lit, 1.0));\n}\n";
    for kind in StageKind::ALL {
        let own = match kind {
            StageKind::Post => traces,
            StageKind::Material => shaded,
            _ => vanilla(kind),
        };
        let stub = kind.prelude_traced(Tracing::Stub);
        validate(&format!("{stub}{own}{}", kind.entries()));
        let scene = kind.prelude_traced(Tracing::Scene);
        let text = format!("{scene}{own}{}", kind.entries());
        validate_for(&text, Capabilities::default() | Capabilities::RAY_QUERY);
        // Only the post and material stages' modules need the device's ray queries.
        let rays = matches!(kind, StageKind::Post | StageKind::Material);
        assert_eq!(scene.contains("ray_query"), rays, "{kind:?}");
        assert!(!stub.contains("ray_query") && !stub.contains("acceleration_structure"));
    }
}

/// The SHA-256 of the material stage's prelude for a `[ray_tracing]` pack whose material
/// calls a trace function, without the scene and with it.
const PINNED_TRACED_MATERIAL: [(Tracing, &str); 2] = [
    (
        Tracing::Stub,
        "1a08d67ea2ddc3bd367805934d75334aeeb98d1bd256fd3c5ef3aaede19b8b13",
    ),
    (
        Tracing::Scene,
        "3f1056e257319c8771181a5aa084eb71587185444dc6cdb29ab7f131bc0b40eb",
    ),
];

#[test]
fn the_traced_material_prelude_is_unchanged() {
    for (tracing, prelude) in PINNED_TRACED_MATERIAL {
        let have = StageKind::Material.prelude_traced(tracing);
        assert_eq!(sha256_hex(have.as_bytes()), prelude, "{tracing:?}");
    }
}

/// The SHA-256 of the prelude and entries of each stage for a pack without `[ray_tracing]`, as
/// they were before the key: a pack that doesn't ask for the scene is built from the same WGSL.
const PINNED_0_2: [(StageKind, &str, &str); 6] = [
    (
        StageKind::Sky,
        "5b23bdddd46ad693f668f185eb866732264675e807a6267e61c57e98b6c50789",
        "54bac4d416d804d8a96e1e31a6d62460f77573634c1e7bec16670641355d678a",
    ),
    (
        StageKind::Vertex,
        "2decfb5c350e7a6743a8026ceed70f068bc8ed0e53cffdac63c43ba733fef75a",
        "f30235a35a2ec15f502efcea364a4470227325fcef36328b5cd04528a916a8ba",
    ),
    (
        StageKind::Material,
        "ac7abe3e705bf333e4d751510387f2ff1b34855423c946cc677681e74d5aee1e",
        "4620cff4dc1cd533621daf65c9d0821fa8dfb383ada1d490b88787ef74504b1e",
    ),
    (
        StageKind::Weather,
        "5131c8e773b9f8cdb752cd2f265d49406f36ee3b38a1b97cd530e116189aa79f",
        "c2e4dabacd487692038c3fbf5708aed72a12d951c15184ceaf61ee1bb61b4982",
    ),
    (
        StageKind::Post,
        "59c7d81702af90bce227d6b1f4663ae33dce1ab0b18907d23f81c139606c309b",
        "f1ffc3c9e0692e1d1f49de4a34ae90b93f7f2eda2eaa42ce936f630139cc4648",
    ),
    (
        StageKind::Tonemap,
        "3c25c70bf75d8472fc8fa9bc5ab840c36ebc0bc0f9b59a3f7292a0242bc471f2",
        "2dcf7ee0874daeb033058ed55323b74c0da5de4a170cece2e6192500714c0b6a",
    ),
];

#[test]
fn the_wgsl_of_a_pack_without_ray_tracing_is_unchanged() {
    for (kind, prelude, entries) in PINNED_0_2 {
        let have = (
            sha256_hex(kind.prelude().as_bytes()),
            sha256_hex(kind.entries().as_bytes()),
        );
        assert_eq!(have, (prelude.to_owned(), entries.to_owned()), "{kind:?}");
    }
    // And a whole stage of a pack in the tree, as the game composes it.
    let dir = root().join("crates/horizon-shader-api/packs/golden_hour");
    let pack = Pack::read(&dir).unwrap();
    let glow = compose(&pack, StageKind::Post, "post/glow.wgsl");
    assert_eq!(
        // As the repository holds the files: a Windows checkout may give them CRLF.
        sha256_hex(glow.text.replace('\r', "").as_bytes()),
        "9aa5cac00d85a99143cf83a393b8af02acf0bbfe443c7158fbc0c95696a18553"
    );
}

/// The `Frame` struct the sky prelude declares (for a pack with `[ray_tracing]` or
/// without; with `kept`, as one with `[[resource]]` has it), as (name, offset) and its span.
fn frame_layout(ray_tracing: bool, kept: bool) -> (Vec<(String, usize)>, usize) {
    let tracing = if ray_tracing {
        Tracing::Stub
    } else {
        Tracing::None
    };
    // The prelude is the pack's (`tests/names.rs` has a kept one's): the struct alone here.
    let text = match kept {
        true => Frame::declared(ray_tracing, true).wgsl(),
        false => {
            let prelude = StageKind::Sky.prelude_traced(tracing);
            format!("{prelude}{}", vanilla(StageKind::Sky))
        }
    };
    let module = validate(&text);
    let (_, ty) = module
        .types
        .iter()
        .find(|(_, t)| t.name.as_deref() == Some("Frame"))
        .expect("Frame in the prelude");
    let naga::TypeInner::Struct { members, span } = &ty.inner else {
        panic!("Frame is a struct")
    };
    let members = members
        .iter()
        .map(|m| (m.name.clone().unwrap(), m.offset as usize))
        .collect();
    (members, *span as usize)
}

#[test]
fn the_frame_s_wgsl_layout_is_the_uploaded_struct_s() {
    let rust: Vec<_> = Frame::WGSL
        .fields
        .iter()
        .zip(Frame::OFFSETS)
        .map(|(f, &o)| (f.name.to_owned(), o))
        .collect();
    let size = std::mem::size_of::<Frame>();
    let (wgsl, span) = frame_layout(true, true);
    assert_eq!(span, size);
    assert_eq!(wgsl, rust);
    // Without `[ray_tracing]` the scene's two fields are a gap: the others stay where they are.
    let (wgsl, span) = frame_layout(false, true);
    assert_eq!(span, size);
    let mut untraced = rust.clone();
    untraced.drain(Frame::RAY_FIELDS);
    assert_eq!(wgsl, untraced);
    // Without `[[resource]]` the struct ends before the epochs, a prefix of the same buffer.
    let unkept = Frame::KEPT_FIELDS.len();
    let (wgsl, span) = frame_layout(true, false);
    assert_eq!(
        (span, wgsl),
        (size - 16, rust[..rust.len() - unkept].to_vec())
    );
    let (wgsl, span) = frame_layout(false, false);
    let untraced = &untraced[..untraced.len() - unkept];
    assert_eq!((span, wgsl), (size - 16, untraced.to_vec()));
}

#[test]
fn the_sample_pack_s_stages_compile() {
    let dir = root().join("crates/horizon-shader-api/tests/samples/schema-1");
    let pack = Pack::read(&dir).unwrap();
    let m = &pack.manifest;
    let stages = [
        (StageKind::Sky, m.sky.iter().collect::<Vec<_>>()),
        (StageKind::Material, m.material.iter().collect()),
        (StageKind::Post, m.post.iter().collect()),
    ];
    for (kind, list) in stages {
        for stage in list {
            let composed = compose(&pack, kind, &stage.file);
            validate(&composed.text);
            // Errors point into the pack's own file.
            let part = composed.parts.iter().find(|(f, _)| *f == stage.file);
            let part = part.unwrap().1.clone();
            let needle = format!("fn {}(", kind.function());
            let at = part.start + composed.text[part].find(&needle).unwrap();
            let source = pack.source(&stage.file).unwrap();
            let line = source.lines().position(|l| l.starts_with(&needle)).unwrap() + 1;
            assert_eq!(composed.locate(at), (stage.file.as_str(), line, 1));
        }
    }
}

#[test]
fn the_reference_is_up_to_date() {
    let doc = reference::PATH;
    let path = root().join(doc);
    let want = reference::markdown();
    if std::env::var_os("HORIZON_WRITE_DOCS").is_some() {
        std::fs::write(&path, &want).unwrap();
    }
    let have = std::fs::read_to_string(&path)
        .unwrap_or_default()
        .replace("\r\n", "\n");
    assert!(
        have == want,
        "{doc} is out of date: HORIZON_WRITE_DOCS=1 cargo test -p horizon-shader-api --test it reference::"
    );
}
