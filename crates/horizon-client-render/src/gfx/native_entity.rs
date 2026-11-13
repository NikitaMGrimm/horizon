//! The entity stage of Horizon's own shader packs (docs/api/shaders.md): the pack's `entity`
//! function gives the lit colour of each point of an entity's or a block entity's surface,
//! before the alpha test, the glint and fog. They still draw with Horizon's geometry and draw
//! lists; only their pipelines change. Their shader is the pack's stage
//! (`CheckedStage::source`), then entity.wgsl and static_entity.wgsl with every name prefixed
//! (`wgsl_prefix.rs`), then entries that hand the pack what `entity_color` combines: the
//! texture, the vertex colour and vanilla's shade apart, the flash, the light levels and the
//! lightmap's colour.
//!
//! The entity stream holds more than the stage draws (items, particles, beacon beams), in one
//! draw: the game says what each run of it is (`Renderer::take_entity_kinds`), the batch
//! writes that into each quad's layer (`EntityBatch::kinds`), and a quad without a kind goes
//! through `entity_color` as without the stage.

use horizon_shader_api::{check::CheckedStage, stages::GROUP};

use super::native_post::{NativeFrame, present_constants};

/// What a run of the entity geometry is to the stage, as `EntityInput.kind` numbers it. Items
/// and particles have kinds too, and reach the stage once they are drawn through it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum EntityKind {
    /// Mobs, players, what they hold and wear, vehicles, projectiles.
    Entity = 0,
    /// Chests, banners, bells, blocks a piston moves or the server just changed.
    BlockEntity = 1,
}

/// The bits of a quad's `Vertex::layer`, above `OIT_ENTITY`, that hold its kind: 0 for one
/// Horizon draws itself, else [`EntityKind`] + 1. Only in the batch's copy of the vertices,
/// and only while a pack with the stage runs.
pub(super) const KIND_SHIFT: u32 = 25;

/// A kind as a quad's layer carries it.
pub(super) fn kind_bits(kind: Option<EntityKind>) -> u32 {
    kind.map_or(0, |k| (k as u32 + 1) << KIND_SHIFT)
}

const PREFIX: &str = "horizon_vanilla_";

/// The entries around the pack's `entity`: entity.wgsl's vertex with the colour and the shade
/// kept apart and the light levels in the lightmap colour's place, then the stage between the
/// steps of `entity_color`.
const ENTRIES: &str = "
struct HorizonEntityOut {
    @builtin(position) clip: vec4<f32>,
    @location(0) uv: vec2<f32>,
    // The vertex colour without vanilla's shade, which is x on the front and y on the back.
    @location(1) tint: vec4<f32>,
    @location(2) shade: vec2<f32>,
    // Block and sky light, 16 per level, the dynamic lights' in it.
    @location(3) light: vec2<f32>,
    @location(4) position: vec3<f32>,
    @location(5) fog_distance: vec2<f32>,
    @location(6) overlay_color: vec4<f32>,
    @location(7) @interpolate(flat) overlay: u32,
    @location(8) @interpolate(flat) wrap: vec2<f32>,
    @location(9) glint_uv: vec2<f32>,
    // Bit 0: back faces are dropped (entity.wgsl's `cull`). Bit 1: the vertex has full light.
    // Bit 2: a translucent render type (`OIT_ENTITY`). From bit 3: the quad's kind, 0 for one
    // Horizon draws itself, else `EntityInput.kind` + 1.
    @location(10) @interpolate(flat) flags: u32,
}
fn horizon_entity_vertex(position: vec3<f32>, uv: vec2<f32>, layer: u32, color: vec4<f32>,
    light: vec4<u32>, normal: vec4<f32>, glint_uv: vec2<f32>, kind: u32) -> HorizonEntityOut {
    let v = horizon_vanilla_entity_vertex(position, uv, layer & 0xffffu, color, light, normal);
    var out: HorizonEntityOut;
    out.clip = v.clip;
    out.uv = uv;
    out.tint = color;
    // `entity_vertex` multiplies this into the colour.
    var shade = vec2(1.0);
    if abs(normal.w) >= 0.5 {
        let nether = horizon_vanilla_globals.time.y != 0.0;
        shade = vec2(horizon_vanilla_entity_light(normal.xyz, nether));
        if normal.w < 0.0 {
            shade.y = horizon_vanilla_entity_light(-normal.xyz, nether);
        }
    }
    out.shade = shade;
    out.light = vec2(max(f32(light.y), horizon_vanilla_dynamic_block_light(position)), f32(light.x));
    out.position = position;
    out.fog_distance = v.fog_distance;
    out.overlay_color = v.overlay_color;
    out.overlay = v.overlay;
    out.wrap = v.wrap;
    out.glint_uv = glint_uv;
    // Full light in the vertex itself (`mobs::render::FULL_BRIGHT`), whatever a dynamic light
    // adds: how every full-bright layer is drawn.
    let full = u32(light.x == 240u && light.y == 240u);
    out.flags = v.cull | (full << 1u) | (((layer >> 24u) & 1u) << 2u) | (kind << 3u);
    return out;
}
@vertex fn horizon_entity_vs(@location(0) position: vec3<f32>, @location(1) uv: vec2<f32>,
    @location(2) layer: u32, @location(3) color: vec4<f32>, @location(4) light: vec4<u32>,
    @location(5) normal: vec4<f32>, @location(6) glint_uv: vec2<f32>) -> HorizonEntityOut {
    return horizon_entity_vertex(position, uv, layer, color, light, normal, glint_uv,
        (layer >> HORIZON_ENTITY_KIND_SHIFT) & 7u);
}
// Retained block entities, placed as static_entity.wgsl's `vs_static_entity` places them.
@vertex fn horizon_entity_vs_static(@location(0) position: vec3<f32>, @location(1) uv: vec2<f32>,
    @location(2) layer: u32, @location(3) color: vec4<f32>, @location(4) light: vec4<u32>,
    @location(5) normal: vec4<f32>, @location(6) glint_uv: vec2<f32>,
    @location(7) section_origin: vec3<i32>) -> HorizonEntityOut {
    let block_delta = section_origin - horizon_vanilla_static_entity.camera_block.xyz;
    let camera_relative = vec3<f32>(block_delta) + position
        - horizon_vanilla_static_entity.camera_fraction.xyz;
    return horizon_entity_vertex(camera_relative, uv, layer, color, light, normal, glint_uv,
        HORIZON_ENTITY_BLOCK_ENTITY);
}
// The lightmap's colour at `light`, as `entity_vertex` reads it.
fn horizon_entity_lightmap(light: vec2<f32>) -> vec4<f32> {
    return textureSampleLevel(horizon_vanilla_lightmap, horizon_vanilla_lightmap_sampler,
        clamp(light / 256.0 + 0.5 / 16.0, vec2(0.5 / 16.0), vec2(15.5 / 16.0)), 0.0);
}
// A quad Horizon draws itself, as `entity_vertex` hands it to `entity_color`.
fn horizon_entity_own(in: HorizonEntityOut, unlit: bool) -> horizon_vanilla_Out {
    var out: horizon_vanilla_Out;
    out.uv = in.uv;
    out.color = vec4(in.tint.rgb * in.shade.x, in.tint.a);
    out.back_color = vec4(in.tint.rgb * in.shade.y, in.tint.a);
    out.fog_distance = in.fog_distance;
    out.overlay_color = in.overlay_color;
    out.overlay = in.overlay;
    out.wrap = in.wrap;
    let clip = horizon_vanilla_globals.view_proj * vec4(in.position, 1.0);
    out.proj = vec4(clip.xy * 0.5 + clip.w * 0.5, clip.zw);
    var light = vec4(1.0);
    if !unlit {
        light = horizon_entity_lightmap(in.light);
        if horizon_vanilla_globals.colored_count.x > 0u {
            let colored = horizon_vanilla_native_colored_light(in.position);
            light = vec4(min(light.rgb + colored, vec3(1.0)), light.a);
        }
    }
    out.light_color = light;
    out.glint_uv = in.glint_uv;
    out.cull = in.flags & 1u;
    return out;
}
@fragment fn horizon_entity_fs(in: HorizonEntityOut, @builtin(front_facing) front: bool)
    -> @location(0) vec4<f32> {
    // entity.wgsl's `unlit`: beacon beams, End portals and gateways aren't a texture times a
    // tint under a light, so they stay Horizon's.
    let o = in.overlay;
    let unlit = o == 2u || o == 4u || o == 6u || o == 9u || o == 10u || o == 11u || o == 12u;
    let kind = in.flags >> 3u;
    if kind == 0u || unlit {
        // As `fs_entity`.
        let own = horizon_entity_own(in, unlit);
        return horizon_vanilla_entity_written_color(own, horizon_vanilla_entity_color(own, front));
    }
    let albedo = textureSample(horizon_vanilla_atlas, horizon_vanilla_atlas_sampler, in.uv);
    var tint = in.tint;
    if o == 38u {
        // The dissolve mask's discard is Horizon's (`entity_color`).
        let mask = textureSampleLevel(horizon_vanilla_atlas, horizon_vanilla_atlas_sampler,
            in.glint_uv, 0.0);
        if tint.a < mask.a { discard; }
        tint.a = 1.0;
    }
    if (in.flags & 1u) != 0u && !front { discard; }
    let shade = select(in.shade.y, in.shade.x, front);
    let lightmap = horizon_entity_lightmap(in.light).rgb;
    let colored = horizon_vanilla_native_colored_light(in.position);
    let out = entity(EntityInput(horizon_decode_rgba(albedo), horizon_decode_rgba(tint),
        // Vanilla's texel says how much of the surface is kept, the stage's how much goes.
        vec4(horizon_decode(in.overlay_color.rgb), 1.0 - in.overlay_color.a),
        horizon_decode(lightmap), horizon_decode(vec3(shade)).x, in.position,
        f32((in.flags >> 1u) & 1u), horizon_entity_normal(in.position), kind - 1u,
        horizon_decode(colored), (in.flags & 4u) != 0u, min(in.light / 240.0, vec2(1.0)),
        in.uv, in.clip.xy / frame.resolution)).color;
    if out.a < 0.1 { discard; }
    // Vanilla's order from here: the glint, then fog, both in vanilla's gamma.
    var color = vec4(horizon_encode(out.rgb), out.a);
    if o == 7u || o == 8u || o == 33u {
        color = horizon_vanilla_glint_over(color, textureSample(horizon_vanilla_glint_tex,
            horizon_vanilla_glint_sampler, in.glint_uv));
    } else if o == 36u {
        color = horizon_vanilla_glint_over(color, textureSample(horizon_vanilla_glint_armor_tex,
            horizon_vanilla_glint_sampler, horizon_vanilla_armor_glint_uv(in.glint_uv)));
    }
    let fog = horizon_vanilla_globals.fog;
    let f = max(horizon_vanilla_linear_fog(in.fog_distance.x, fog.x, fog.y),
        horizon_vanilla_linear_fog(in.fog_distance.y, fog.z, fog.w));
    let fog_color = horizon_vanilla_globals.fog_color;
    return horizon_vanilla_frame_stored(
        vec4(mix(color.rgb, fog_color.rgb, f * fog_color.a), color.a));
}
";

/// The entity shader for the pack's entity stage `source`.
pub(super) fn shader_source(source: &str) -> String {
    format!(
        "{source}\n// Horizon's entities (entity.wgsl, static_entity.wgsl)\n{}\
         const HORIZON_ENTITY_KIND_SHIFT: u32 = {KIND_SHIFT}u;\n\
         const HORIZON_ENTITY_BLOCK_ENTITY: u32 = {}u;\n{ENTRIES}",
        super::wgsl_prefix::prefixed(&super::static_entity_shader(), PREFIX),
        EntityKind::BlockEntity as u32 + 1,
    )
}

/// The pack's entity pipelines: the entity stream's ordinary quads, and the retained block
/// entities.
pub struct NativeEntity {
    entities: wgpu::RenderPipeline,
    retained: wgpu::RenderPipeline,
}

impl NativeEntity {
    /// The pipelines of the pack's entity `stage` with the option values `constants`, into a
    /// frame of `format`, against the entities' group 0 and the retained block entities' group 1.
    pub fn new(
        device: &wgpu::Device,
        format: wgpu::TextureFormat,
        [entities, retained]: [&wgpu::BindGroupLayout; 2],
        frame: &NativeFrame,
        stage: &CheckedStage,
        constants: &[(String, f64)],
    ) -> Result<Self, String> {
        let text = shader_source(&stage.source);
        let module = naga::front::wgsl::parse_str(&text)
            .map_err(|e| format!("{}: {}", stage.file, e.emit_to_string(&text)))?;
        let mut constants = present_constants(&module, constants);
        let [(_, write_light), (_, linear)] =
            super::linear_blend::frame_constants(format, frame.linear);
        constants.push(("horizon_vanilla_HDR_WRITE_LIGHT", write_light));
        constants.push(("horizon_vanilla_FRAME_LINEAR", linear));
        let scope = device.push_error_scope(wgpu::ErrorFilter::Validation);
        let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some(&stage.label),
            source: wgpu::ShaderSource::Naga(std::borrow::Cow::Owned(module)),
        });
        // As the renderer's entity vertices (gfx.rs): `Vertex`, and a retained section's origin.
        let attributes = wgpu::vertex_attr_array![0=>Float32x3,1=>Float32x2,2=>Uint32,
            3=>Unorm8x4,4=>Uint8x4,5=>Snorm8x4,6=>Float32x2];
        let origin_attributes = wgpu::vertex_attr_array![7=>Sint32x3];
        let buffers = [
            Some(wgpu::VertexBufferLayout {
                array_stride: std::mem::size_of::<super::Vertex>() as u64,
                step_mode: wgpu::VertexStepMode::Vertex,
                attributes: &attributes,
            }),
            Some(wgpu::VertexBufferLayout {
                array_stride: std::mem::size_of::<[i32; 3]>() as u64,
                step_mode: wgpu::VertexStepMode::Instance,
                attributes: &origin_attributes,
            }),
        ];
        // Each as the renderer's own pipeline for the same quads (gfx.rs's `entity_pipeline`
        // and `static_entity_pipeline`): its depth bias and its blend.
        let pipeline = |vertex: &str, retained: Option<&wgpu::BindGroupLayout>| {
            let mut groups: [Option<&wgpu::BindGroupLayout>; GROUP as usize + 1] =
                Default::default();
            groups[0] = Some(entities);
            groups[1] = retained;
            groups[GROUP as usize] = Some(&frame.layout);
            let layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
                label: Some("Native shader pack entities"),
                bind_group_layouts: &groups,
                ..Default::default()
            });
            let options = wgpu::PipelineCompilationOptions {
                constants: &constants,
                ..Default::default()
            };
            device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
                label: Some(&stage.label),
                layout: Some(&layout),
                vertex: wgpu::VertexState {
                    module: &module,
                    entry_point: Some(vertex),
                    compilation_options: options.clone(),
                    buffers: &buffers[..1 + usize::from(retained.is_some())],
                },
                primitive: Default::default(),
                depth_stencil: Some(wgpu::DepthStencilState {
                    format: wgpu::TextureFormat::Depth32Float,
                    depth_write_enabled: Some(true),
                    depth_compare: Some(wgpu::CompareFunction::GreaterEqual),
                    stencil: Default::default(),
                    bias: wgpu::DepthBiasState {
                        constant: if retained.is_some() { 0 } else { 128 },
                        ..Default::default()
                    },
                }),
                multisample: Default::default(),
                fragment: Some(wgpu::FragmentState {
                    module: &module,
                    entry_point: Some("horizon_entity_fs"),
                    compilation_options: options,
                    targets: &[Some(wgpu::ColorTargetState {
                        format,
                        blend: retained
                            .is_none()
                            .then_some(wgpu::BlendState::ALPHA_BLENDING),
                        write_mask: wgpu::ColorWrites::ALL,
                    })],
                }),
                multiview_mask: None,
                cache: None,
            })
        };
        let built = Self {
            entities: pipeline("horizon_entity_vs", None),
            retained: pipeline("horizon_entity_vs_static", Some(retained)),
        };
        if let Some(e) = pollster::block_on(scope.pop()) {
            return Err(format!("{}: {e}", stage.file));
        }
        Ok(built)
    }
}

/// Sets the pipeline of the entity stream's ordinary quads, or of the `retained` block
/// entities: the `stage`'s with the pack's frame at its group, else Horizon's `own`.
pub(super) fn set_pipeline(
    pass: &mut wgpu::RenderPass<'_>,
    stage: Option<(&NativeEntity, &wgpu::BindGroup)>,
    own: &wgpu::RenderPipeline,
    retained: bool,
) {
    let Some((stage, frame)) = stage else {
        return pass.set_pipeline(own);
    };
    pass.set_pipeline(if retained {
        &stage.retained
    } else {
        &stage.entities
    });
    pass.set_bind_group(GROUP, frame, &[]);
}

impl super::Renderer {
    /// Whether a pack's `[entity]` stage draws this renderer's entities: the game then says
    /// what each run of them is (`take_entity_kinds`).
    pub fn draws_entity_stage(&self) -> bool {
        self.native.as_ref().is_some_and(|n| n.entity.is_some())
    }

    /// Takes what the next frame's entities (`Frame::entities`) are to the stage: marks at the
    /// first vertex of each run, in order, `None` for what Horizon lights itself, as it does
    /// what comes before the first mark. `kinds` is left empty, for the frame after.
    pub fn take_entity_kinds(&mut self, kinds: &mut Vec<(u32, Option<EntityKind>)>) {
        std::mem::swap(&mut self.entity_batch.kinds, kinds);
        kinds.clear();
    }
}

#[cfg(test)]
#[path = "native_entity_tests.rs"]
pub(super) mod tests;
