//! Step 6 of #16011 item 4: the compute and `[[before_world]]` passes of a `[ray_tracing]` pack
//! trace the scene, as do the post passes of one that keeps resources. Each pack runs through
//! the renderer's own calls (`NativePack::new`, `install`, `write`, `before_world`, the chain)
//! against a scene of one opaque quad 4 blocks ahead of the camera, on a device with ray
//! queries (skipped without one: on DirectX 12 that needs `WGPU_DX12_COMPILER=dxc`).
use super::super::super::native_pack::NativePack;
use super::super::trace::Traced;
use super::install;
use horizon_shader_api::{MANIFEST, Pack, check};

const HEAD: &str = "schema = 1\nid = \"p\"\nname = \"P\"\nversion = \"1\"\napi = \"0.2\"\n";
const IDENTITY: &str = "fn tonemap(input: TonemapInput) -> vec3<f32> { return input.color; }\n";

/// A device with ray queries, as the game opens one for a `[ray_tracing]` pack.
fn ray_device() -> Option<(wgpu::Instance, wgpu::Device, wgpu::Queue)> {
    let instance = crate::gfx::test_gpu::instance();
    let adapter = pollster::block_on(instance.request_adapter(&Default::default())).ok()?;
    let features = adapter.features() & wgpu::Features::EXPERIMENTAL_RAY_QUERY;
    let backend = adapter.get_info().backend;
    if features.is_empty() {
        eprintln!("no ray queries on {backend:?}: nothing traced");
        return None;
    }
    eprintln!("tracing on {backend:?}");
    let desc = wgpu::DeviceDescriptor {
        required_features: features,
        required_limits: adapter.limits(),
        experimental_features: horizon_hdr::experimental_features(),
        ..Default::default()
    };
    let (device, queue) = pollster::block_on(adapter.request_device(&desc)).ok()?;
    Some((instance, device, queue))
}

/// A scene: one quad of solid terrain, 16 blocks square at `z` in front of the camera, its
/// layer marked as over opaque texels (bit 15) so a ray stops on it without reading a sprite,
/// with its own `generation`.
struct Scene {
    generation: u64,
    tlas: wgpu::Tlas,
    store: wgpu::Buffer,
    _blas: wgpu::Blas,
    sprites: Vec<wgpu::TextureView>,
}

impl Scene {
    fn new(device: &wgpu::Device, queue: &wgpu::Queue, z: f32, generation: u64) -> Self {
        use crate::gfx::surface_setup::ray_query::{blas_input, quad_indices};
        let corners = [
            [-8.0, -8.0, z],
            [8.0, -8.0, z],
            [8.0, 8.0, z],
            [-8.0, 8.0, z],
        ];
        // `TerrainVertex`: the position, the uv, the layer and the light, the colour.
        let words: Vec<u32> = (corners.iter())
            .flat_map(|p| {
                [
                    p[0].to_bits(),
                    p[1].to_bits(),
                    p[2].to_bits(),
                    0,
                    0x8000,
                    !0,
                ]
            })
            .collect();
        let store = wgpu::util::DeviceExt::create_buffer_init(
            device,
            &wgpu::util::BufferInitDescriptor {
                label: Some("Test scene store"),
                contents: bytemuck::cast_slice(&words),
                usage: wgpu::BufferUsages::STORAGE | blas_input(device),
            },
        );
        let indices = quad_indices(device, 1);
        let size = wgpu::BlasTriangleGeometrySizeDescriptor {
            vertex_format: wgpu::VertexFormat::Float32x3,
            vertex_count: 4,
            index_format: Some(wgpu::IndexFormat::Uint32),
            index_count: Some(6),
            flags: wgpu::AccelerationStructureGeometryFlags::empty(),
        };
        let blas = device.create_blas(
            &wgpu::CreateBlasDescriptor {
                label: None,
                flags: wgpu::AccelerationStructureFlags::PREFER_FAST_TRACE,
                update_mode: wgpu::AccelerationStructureUpdateMode::Build,
            },
            wgpu::BlasGeometrySizeDescriptors::Triangles {
                descriptors: vec![size.clone()],
            },
        );
        let mut tlas = device.create_tlas(&wgpu::CreateTlasDescriptor {
            label: None,
            max_instances: 1,
            flags: wgpu::AccelerationStructureFlags::PREFER_FAST_TRACE,
            update_mode: wgpu::AccelerationStructureUpdateMode::Build,
        });
        let identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0];
        tlas[0] = Some(wgpu::TlasInstance::new(&blas, identity, 0, 1));
        let mut encoder = device.create_command_encoder(&Default::default());
        let entry = wgpu::BlasBuildEntry {
            blas: &blas,
            geometry: wgpu::BlasGeometries::TriangleGeometries(vec![wgpu::BlasTriangleGeometry {
                size: &size,
                vertex_buffer: &store,
                first_vertex: 0,
                vertex_stride: 24,
                index_buffer: Some(&indices),
                first_index: Some(0),
                transform_buffer: None,
                transform_buffer_offset: None,
            }]),
        };
        encoder.build_acceleration_structures(std::iter::once(&entry), std::iter::once(&tlas));
        queue.submit([encoder.finish()]);
        let sprite = || {
            device
                .create_texture(&wgpu::TextureDescriptor {
                    label: None,
                    size: wgpu::Extent3d {
                        width: 1,
                        height: 1,
                        depth_or_array_layers: 1,
                    },
                    mip_level_count: 1,
                    sample_count: 1,
                    dimension: wgpu::TextureDimension::D2,
                    format: wgpu::TextureFormat::Rgba8Unorm,
                    usage: wgpu::TextureUsages::TEXTURE_BINDING,
                    view_formats: &[],
                })
                .create_view(&wgpu::TextureViewDescriptor {
                    dimension: Some(wgpu::TextureViewDimension::D2Array),
                    ..Default::default()
                })
        };
        Self {
            generation,
            tlas,
            store,
            _blas: blas,
            sprites: (0..8).map(|_| sprite()).collect(),
        }
    }

    /// The scene as a frame traces it, the camera at the scene's origin.
    fn traced(&self) -> Traced<'_> {
        Traced {
            tlas: &self.tlas,
            solid_instances: 1,
            store: &self.store,
            generation: self.generation,
            camera: [0.0; 3],
            distance: 64.0,
        }
    }
}

/// A pack of `manifest` (with the identity tone curve) and `files`, built by `NativePack::new`
/// against `scene`'s sprites and installed.
fn pack(
    device: &wgpu::Device,
    queue: &wgpu::Queue,
    scene: &Scene,
    manifest: &str,
    files: &[(&str, &str)],
) -> NativePack {
    let manifest = format!("{HEAD}{manifest}[tonemap]\nfile = \"t.wgsl\"\n");
    let mut all: std::collections::BTreeMap<String, Vec<u8>> = (files.iter())
        .chain(&[("t.wgsl", IDENTITY)])
        .map(|(n, t)| ((*n).to_owned(), t.as_bytes().to_vec()))
        .collect();
    all.insert(MANIFEST.into(), manifest.into_bytes());
    let report = check(&Pack::from_files(all).unwrap()).unwrap();
    let (globals, empty) = crate::gfx::native_pack::tests::terrain_layouts(device);
    let terrain = crate::gfx::native_material::TerrainShape {
        sprite_views: &scene.sprites,
        ..crate::gfx::native_pack::tests::terrain_shape(&globals, &empty)
    };
    let (mut pack, _) =
        NativePack::new(device, queue, &terrain, report, &[], Default::default()).unwrap();
    install(device, Some(&mut pack.post), None);
    pack
}

/// Frame `n` of `pack` as the renderer draws it, with `scene` or without: its inputs, its
/// `[[before_world]]` passes, then its chain over a 4×4 world onto the screen; the screen's
/// first pixel.
fn frame(
    (device, queue): (&wgpu::Device, &wgpu::Queue),
    pack: &mut NativePack,
    n: u32,
    scene: Option<&Traced<'_>>,
) -> [u8; 4] {
    let target = |format, usage| {
        device.create_texture(&wgpu::TextureDescriptor {
            label: None,
            size: wgpu::Extent3d {
                width: 4,
                height: 4,
                depth_or_array_layers: 1,
            },
            mip_level_count: 1,
            sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format,
            usage,
            view_formats: &[],
        })
    };
    use wgpu::TextureUsages as T;
    let usage = T::RENDER_ATTACHMENT | T::TEXTURE_BINDING;
    let color = target(wgpu::TextureFormat::Rgba8Unorm, usage).create_view(&Default::default());
    let depth = target(wgpu::TextureFormat::Depth32Float, usage).create_view(&Default::default());
    let screen = target(
        wgpu::TextureFormat::Rgba8Unorm,
        T::RENDER_ATTACHMENT | T::COPY_SRC,
    );
    let hud = crate::hud::HudBatch::new([4, 4]);
    let mut inputs = crate::gfx::pack_uniforms::initial_history_tests::frame(&hud);
    inputs.pack.frame_timing = Some(crate::gfx::pack_uniforms::FrameTiming::nth(n, 50.0));
    pack.write(
        queue,
        &inputs,
        [4, 4],
        crate::gfx::output::DisplayValues::SDR,
        scene,
    );
    let mut encoder = device.create_command_encoder(&Default::default());
    pack.before_world(device, &mut encoder, scene);
    let world = (&color, &depth, &pack.frame.surface_view);
    let view = screen.create_view(&Default::default());
    pack.post.run(
        device,
        &mut encoder,
        world,
        &view,
        [4, 4],
        false,
        None,
        scene,
    );
    let buffer = crate::gfx::copy_frame(device, &mut encoder, &screen, [4, 4]);
    queue.submit([encoder.finish()]);
    (buffer.slice(..)).map_async(wgpu::MapMode::Read, |r| r.unwrap());
    device.poll(wgpu::PollType::wait_indefinitely()).unwrap();
    let pixel = buffer.slice(..).get_mapped_range().unwrap()[..4].try_into();
    pixel.unwrap()
}

/// `want` as the screen shows it (`horizon_encode`), within 1 of `got`.
fn shows(got: [u8; 4], want: [f32; 3]) -> bool {
    let shown = want.map(|v| (v.powf(1.0 / 2.2) * 255.0).round() as u8);
    got.iter().zip(shown).all(|(g, w)| g.abs_diff(w) <= 1)
}

/// The kept buffer `hit` of a pack that traces into it, and the last pass that shows it: red
/// the hit distance / 8 (0.5 for the quad, 0.25 on a miss), green `frame.has_ray_tracing`.
const HIT: &str = "[ray_tracing]\n[[resource]]\nname = \"hit\"\nbuffer = 16\nelement = \"f32\"\nkeep = \"view\"\n";
const SHOW: &str = "fn post(input: PostInput) -> vec4<f32> {\n    \
                    let d = hit[0];\n    \
                    return vec4<f32>(select(0.25, d / 8.0, d >= 0.0), f32(frame.has_ray_tracing), 0.0, 1.0);\n}\n";
/// `SHOW`'s colour for a hit at distance `d`, or a miss: red d / 8 or 0.25, green
/// `has_ray_tracing`.
fn hit_shown(d: Option<f32>) -> [f32; 3] {
    match d {
        Some(d) => [d / 8.0, 1.0, 0.0],
        None => [0.25, 0.0, 0.0],
    }
}

/// A compute file that traces straight ahead and keeps the hit's distance.
const TRACES: &str = "fn compute(input: ComputeInput) {\n    \
                      hit[0] = trace(vec3<f32>(0.0), vec3<f32>(0.0, 0.0, 1.0), 64.0).distance;\n}\n";

/// A pack's frames with the scene, twice, then with another (a TLAS regrown: the quad at
/// z = 6, a new generation), then without one (the stub: every ray misses), twice, with no
/// validation error. `shown(d)`: the colour for a hit at distance `d`, none for a miss.
fn traces(manifest: &str, files: &[(&str, &str)], shown: impl Fn(Option<f32>) -> [f32; 3]) {
    let Some((_, device, queue)) = ray_device() else {
        return;
    };
    let (first, second) = (
        Scene::new(&device, &queue, 4.0, 1),
        Scene::new(&device, &queue, 6.0, 2),
    );
    let scope = device.push_error_scope(wgpu::ErrorFilter::Validation);
    let mut pack = pack(&device, &queue, &first, manifest, files);
    let (near, far) = (first.traced(), second.traced());
    for (n, (scene, want)) in [
        (Some(&near), shown(Some(4.0))),
        (Some(&near), shown(Some(4.0))),
        (Some(&far), shown(Some(6.0))),
        (None, shown(None)),
        (None, shown(None)),
    ]
    .into_iter()
    .enumerate()
    {
        let got = frame((&device, &queue), &mut pack, n as u32 + 1, scene);
        assert!(shows(got, want), "frame {n}: {got:?}, {want:?} expected");
    }
    let error = pollster::block_on(scope.pop());
    assert!(error.is_none(), "{error:?}");
}

/// A compute `[[post]]` pass traces the scene and writes the hit's distance into a kept buffer
/// a later pass shows; without the scene its stub writes the miss. Run with
/// `WGPU_BACKEND=vulkan` and `WGPU_BACKEND=dx12 WGPU_DX12_COMPILER=dxc`.
#[test]
fn a_compute_pass_traces_the_scene_into_a_kept_buffer() {
    let manifest = format!(
        "{HIT}[[post]]\nfile = \"c.wgsl\"\ncompute = true\ndispatch = [1, 1, 1]\n\
         workgroup = [1, 1, 1]\nwrites = [\"hit\"]\n[[post]]\nfile = \"l.wgsl\"\nreads = [\"hit\"]\n"
    );
    let files = [("c.wgsl", TRACES), ("l.wgsl", SHOW)];
    traces(&manifest, &files, hit_shown);
}

/// The same from a `[[before_world]]` pass, which traces this frame's scene before the world
/// draws. Run with `WGPU_BACKEND=vulkan` and `WGPU_BACKEND=dx12 WGPU_DX12_COMPILER=dxc`.
#[test]
fn a_before_world_pass_traces_the_scene_into_a_kept_buffer() {
    let manifest = format!(
        "{HIT}[[before_world]]\nfile = \"w.wgsl\"\ndispatch = [1, 1, 1]\nworkgroup = [1, 1, 1]\n\
         writes = [\"hit\"]\n[[post]]\nfile = \"l.wgsl\"\nreads = [\"hit\"]\n"
    );
    let files = [("w.wgsl", TRACES), ("l.wgsl", SHOW)];
    traces(&manifest, &files, hit_shown);
}

/// A post pass of a pack that keeps resources reads its kept texture's `_previous` and traces
/// in one shader: red counts the frames by a quarter, green the hit's distance / 8 (0 on a
/// miss). Run with `WGPU_BACKEND=vulkan` and `WGPU_BACKEND=dx12 WGPU_DX12_COMPILER=dxc`.
#[test]
fn a_kept_post_pass_reads_its_previous_and_traces() {
    let manifest = "[ray_tracing]\n[[resource]]\nname = \"img\"\nformat = \"rgba16float\"\nscale = 1.0\n\
                    keep = \"view\"\n[[post]]\nfile = \"a.wgsl\"\noutput = \"img\"\n\
                    reads = [\"img_previous\"]\n[[post]]\nfile = \"l.wgsl\"\nreads = [\"img\"]\n";
    let a = "fn post(input: PostInput) -> vec4<f32> {\n    \
             let last = textureLoad(img_previous, vec2<i32>(input.pixel), 0).r;\n    \
             let d = trace(vec3<f32>(0.0), vec3<f32>(0.0, 0.0, 1.0), 64.0).distance;\n    \
             return vec4<f32>(last + 0.25, max(d, 0.0) / 8.0, 0.0, 1.0);\n}\n";
    let l = "fn post(input: PostInput) -> vec4<f32> {\n    \
             return vec4<f32>(textureLoad(img, vec2<i32>(input.pixel), 0).rgb, 1.0);\n}\n";
    let Some((_, device, queue)) = ray_device() else {
        return;
    };
    let scene = Scene::new(&device, &queue, 4.0, 1);
    let scope = device.push_error_scope(wgpu::ErrorFilter::Validation);
    let mut pack = pack(
        &device,
        &queue,
        &scene,
        manifest,
        &[("a.wgsl", a), ("l.wgsl", l)],
    );
    let traced = scene.traced();
    let frames = [
        (Some(&traced), [0.25, 0.5, 0.0]),
        (Some(&traced), [0.5, 0.5, 0.0]),
        (None, [0.75, 0.0, 0.0]),
    ];
    for (n, (scene, want)) in frames.into_iter().enumerate() {
        let got = frame((&device, &queue), &mut pack, n as u32 + 1, scene);
        assert!(shows(got, want), "frame {n}: {got:?}, {want:?} expected");
    }
    let error = pollster::block_on(scope.pop());
    assert!(error.is_none(), "{error:?}");
}

/// What a pack makes, counted with `Instance::generate_report`: a `[ray_tracing]` pack
/// without kept resources makes what it made before them (the chain's layout and one with the
/// scene, its passes' pipelines twice; a group a pass and the tonemap's on the first frame with
/// the scene, none on the next), and one that keeps resources and traces from a compute pass
/// a layout a pass, with the scene for the passes that trace, and its groups a parity once.
#[test]
fn a_traced_pack_makes_its_layouts_and_groups_once() {
    let Some((instance, device, queue)) = ray_device() else {
        return;
    };
    let scene = Scene::new(&device, &queue, 4.0, 1);
    let traced = scene.traced();
    let count = || {
        let hub = instance.generate_report()?.hub;
        let counts = [
            hub.bind_group_layouts,
            hub.bind_groups,
            hub.render_pipelines,
            hub.compute_pipelines,
        ];
        Some(counts.map(|c| c.num_allocated))
    };
    let made = |manifest: &str, files: &[(&str, &str)]| {
        let before = count()?;
        let mut pack = pack(&device, &queue, &scene, manifest, files);
        let built = count()?;
        frame((&device, &queue), &mut pack, 1, Some(&traced));
        let first = count()?;
        frame((&device, &queue), &mut pack, 2, Some(&traced));
        let second = count()?;
        let delta = |a: [usize; 4], b: [usize; 4]| [0, 1, 2, 3].map(|i| b[i] - a[i]);
        Some([
            delta(before, built),
            delta(built, first),
            delta(first, second),
        ])
    };
    let post = "fn post(input: PostInput) -> vec4<f32> {\n    \
                return vec4<f32>(trace(vec3<f32>(0.0), vec3<f32>(0.0, 0.0, 1.0), 8.0).distance);\n}\n";
    let plain = "[ray_tracing]\n[[post]]\nfile = \"a.wgsl\"\n[[post]]\nfile = \"b.wgsl\"\n";
    // Without a report (a backend outside wgpu-core) there is nothing to count.
    let Some(plain) = made(plain, &[("a.wgsl", post), ("b.wgsl", post)]) else {
        return;
    };
    eprintln!("traced chain: {plain:?}");
    let manifest = format!(
        "{HIT}[[post]]\nfile = \"c.wgsl\"\ncompute = true\ndispatch = [1, 1, 1]\n\
         workgroup = [1, 1, 1]\nwrites = [\"hit\"]\n[[post]]\nfile = \"l.wgsl\"\nreads = [\"hit\"]\n"
    );
    let kept = made(&manifest, &[("c.wgsl", TRACES), ("l.wgsl", SHOW)]).unwrap();
    // The frame's layouts and the chain's, then the one with the scene; the passes' pipelines
    // twice, the decode's and the tonemap's. A group a pass and the tonemap's on the first frame.
    assert_eq!(plain, [[3, 1, 6, 0], [0, 3, 0, 0], [0, 0, 0, 0]]);
    // The frame's and the chain's, a kept layout a pass (the compute one, the last), each again
    // with the scene; the compute pass's pipeline twice. Its groups once for each parity.
    assert_eq!(kept, [[6, 1, 4, 2], [0, 6, 0, 0], [0, 0, 0, 0]]);
}
