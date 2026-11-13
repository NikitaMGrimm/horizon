use super::{EntityKind, kind_bits, shader_source};
use crate::mesher::Vertex;

/// Vanilla's own as a pack writes it (docs/api/shaders-reference-0.2.md): the flash over the
/// surface in vanilla's gamma, then the light.
pub const VANILLA: &str = "
fn entity(input: EntityInput) -> Surface {
    let base = input.albedo.rgb * input.tint.rgb * input.shade;
    let flashed = pow(mix(pow(base, vec3(1.0 / 2.2)), pow(input.overlay.rgb, vec3(1.0 / 2.2)),
        input.overlay.a), vec3(2.2));
    let light = min(input.lightmap + input.colored_light, vec3(1.0));
    return Surface(vec4(flashed * light, input.albedo.a * input.tint.a));
}
";

/// A pack's entity shader whose stage is `entity`.
pub fn shader(entity: &str) -> String {
    use horizon_shader_api::{MANIFEST, Pack, check};
    let manifest = "schema = 1\nid = \"t\"\nname = \"T\"\nversion = \"1\"\napi = \"0.2\"\n\
        [entity]\nfile = \"entity.wgsl\"\n";
    let files = [(MANIFEST, manifest), ("entity.wgsl", entity)]
        .map(|(n, t)| (n.to_owned(), t.as_bytes().to_vec()))
        .into();
    let report = check(&Pack::from_files(files).unwrap()).unwrap();
    shader_source(&report.stages[0].source)
}

/// The shader the renderer builds around a pack's stage is valid WGSL with both of its
/// pipelines' entries, whatever names the pack's own code takes: Horizon's are prefixed.
#[test]
fn the_packs_entity_shader_validates() {
    let own_names = "const globals: f32 = 1.0;\nfn entity_color(c: f32) -> f32 { return c; }\n";
    for entity in [VANILLA.to_owned(), own_names.to_owned() + VANILLA] {
        let source = shader(&entity);
        let module = naga::front::wgsl::parse_str(&source)
            .unwrap_or_else(|e| panic!("{}", e.emit_to_string(&source)));
        naga::valid::Validator::new(
            naga::valid::ValidationFlags::all(),
            naga::valid::Capabilities::all(),
        )
        .validate(&module)
        .unwrap_or_else(|e| panic!("{}", e.emit_to_string(&source)));
        let entries: Vec<_> = module.entry_points.iter().map(|e| &*e.name).collect();
        for entry in [
            "horizon_entity_vs",
            "horizon_entity_vs_static",
            "horizon_entity_fs",
        ] {
            assert!(entries.contains(&entry), "{entry} in {entries:?}");
        }
    }
}

/// The kind is written above every bit the entity shaders read of a layer (the overlay, the
/// HDR tier, `OIT_ENTITY`), so a quad drawn by Horizon's own pipelines reads as before.
#[test]
fn a_kind_keeps_clear_of_the_layers_other_bits() {
    assert_eq!(kind_bits(None), 0);
    let kinds = [EntityKind::Entity, EntityKind::BlockEntity];
    for kind in kinds {
        let bits = kind_bits(Some(kind));
        assert_ne!(bits, 0);
        assert_eq!(bits & (super::super::entity_batch::OIT_ENTITY * 2 - 1), 0);
        assert_eq!((bits >> super::KIND_SHIFT) & 7, kind as u32 + 1);
    }
}

fn quad(layer: u32) -> [Vertex; 4] {
    [Vertex {
        layer,
        ..bytemuck::Zeroable::zeroed()
    }; 4]
}

/// The kinds follow the game's runs through the sorted batch: each ordinary quad takes the
/// kind of the last mark at or before its first vertex, and the other materials' quads none.
/// The frame after has none unless the game marks it again.
#[test]
fn quads_take_the_kind_of_their_run() {
    // Quads 0, 1, 3 and 4 are ordinary; quad 2 is another material's (overlay 11), sorted
    // out of them.
    let vertices: Vec<Vertex> = [0, 5, 11, 0, 7].into_iter().flat_map(quad).collect();
    let mut batch = super::super::entity_batch::EntityBatch::default();
    batch.kinds = vec![
        (4, Some(EntityKind::Entity)),
        (12, Some(EntityKind::BlockEntity)),
        (16, None),
    ];
    let kinds = |batch: &mut super::super::entity_batch::EntityBatch| -> Vec<u32> {
        batch.update(&vertices, &[], &[], &[], false);
        let quads = batch.combined.chunks(4);
        quads.map(|q| q[3].layer >> super::KIND_SHIFT).collect()
    };
    // The ordinary quads lead the batch: 0, 1, 3 and 4, then quad 2.
    assert_eq!(kinds(&mut batch), [0, 1, 2, 0, 0]);
    assert_eq!(batch.combined[4].layer & 0xffff, 5);
    assert_eq!(kinds(&mut batch), [0; 5]);
}
